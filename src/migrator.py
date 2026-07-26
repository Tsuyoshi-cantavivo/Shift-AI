"""migrator.py - migrations/*.sql の適用状態をステートメント単位で追跡し、未適用分だけ適用する。

背景:
  0003 の適用が本番D1で「前半（staffs 再構築）失敗・後半（shop_holidays 作成）成功」
  という部分適用になった事故がある（migrations/0004_fix_student_role_check.sql:3-27）。
  ファイル単位の記録ではこの状態を「適用済み」とも「未適用」とも表現できないため、
  ステートメント単位で記録する。

方針:
  - 起動時の自動適用はしない。本番D1で意図せずDDLが走るのを避けるため、
    管理画面のボタン、または `python src/migrator.py apply` で明示的に適用する。
  - 今後のマイグレーションは冪等な形で書く（CREATE ... IF NOT EXISTS）。
    ALTER TABLE ADD COLUMN だけは冪等にできないため、ここで列の有無を見てスキップする。
"""
import os
import re

from db import query_all, query_one, execute

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS_DIR = os.path.join(BASE_DIR, "migrations")

# 0004 以前は再実行が危険（0004 は staffs テーブルの再構築を含む）。
# schema_migrations が空のときに一度だけ「適用済み」として記録してから運用を始める。
LEGACY_FILES = (
    "0002_add_manager_role.sql",
    "0003_add_student_role_and_holidays.sql",
    "0004_fix_student_role_check.sql",
)

_ADD_COLUMN_RE = re.compile(r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)",
                            re.IGNORECASE)


def _ensure_table():
    execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT NOT NULL, stmt_index INTEGER NOT NULL, "
            "applied_at TEXT DEFAULT (datetime('now')), "
            "PRIMARY KEY (filename, stmt_index))")


def _strip_comments(sql):
    """-- 行コメントと /* */ ブロックコメントを除去する。"""
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    out = []
    for line in sql.split("\n"):
        idx = line.find("--")
        out.append(line[:idx] if idx >= 0 else line)
    return "\n".join(out)


def split_statements(sql):
    """SQL をステートメントに分割する。

    素朴な `split(";")` だと文字列リテラル内のセミコロンで誤分割するため、
    シングルクォートの内外を追跡する。'' はエスケープされたクォートとして扱う。
    """
    sql = _strip_comments(sql)
    out, buf, in_str, i = [], [], False, 0
    while i < len(sql):
        ch = sql[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        out.append(stmt)
    return out


def _migration_files():
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    return sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))


def _read_statements(filename):
    with open(os.path.join(MIGRATIONS_DIR, filename), "r", encoding="utf-8") as f:
        return split_statements(f.read())


def _mark_applied(filename, stmt_index):
    execute("INSERT OR IGNORE INTO schema_migrations (filename, stmt_index) VALUES (?,?)",
            (filename, stmt_index))


def _backfill_legacy():
    """schema_migrations が空のときだけ、レガシーのマイグレーションを適用済みとして記録する。"""
    if query_one("SELECT 1 AS x FROM schema_migrations LIMIT 1"):
        return
    for filename in LEGACY_FILES:
        path = os.path.join(MIGRATIONS_DIR, filename)
        if not os.path.exists(path):
            continue
        for i in range(len(_read_statements(filename))):
            _mark_applied(filename, i)


def status():
    """全マイグレーションのステートメントを、適用済みフラグつきで返す。"""
    _ensure_table()
    _backfill_legacy()
    applied = {(r["filename"], r["stmt_index"])
               for r in query_all("SELECT filename, stmt_index FROM schema_migrations")}
    out = []
    for filename in _migration_files():
        for i, sql in enumerate(_read_statements(filename)):
            out.append({"filename": filename, "stmt_index": i,
                        "applied": (filename, i) in applied, "sql": sql})
    return out


def _column_exists(table, column):
    try:
        rows = query_all(f"PRAGMA table_info({table})")
    except Exception:
        return False
    return any(r.get("name") == column for r in rows)


def _should_skip(sql):
    """冪等にできない DDL のうち、既に適用済みと判断できるものを検出する。

    ALTER TABLE ADD COLUMN は IF NOT EXISTS を書けないため、列の有無で判定する。
    """
    m = _ADD_COLUMN_RE.match(sql)
    if not m:
        return False
    return _column_exists(m.group(1), m.group(2))


def apply_pending():
    """未適用のステートメントを順に適用する。

    失敗したらそこで中断する（後続が前提を失っている可能性があるため）。
    成功した分は schema_migrations に記録済みなので、再実行すれば失敗箇所から再開する。
    """
    result = {"applied": [], "skipped": [], "failed": None}
    for item in status():
        if item["applied"]:
            continue
        ref = {"filename": item["filename"], "stmt_index": item["stmt_index"],
               "sql": item["sql"][:200]}
        if _should_skip(item["sql"]):
            _mark_applied(item["filename"], item["stmt_index"])
            result["skipped"].append(ref)
            continue
        try:
            execute(item["sql"])
        except Exception as e:
            print(f"[migrator] FAILED {item['filename']}#{item['stmt_index']}: {e}", flush=True)
            result["failed"] = dict(ref, error=str(e))
            break
        _mark_applied(item["filename"], item["stmt_index"])
        result["applied"].append(ref)
    return result


if __name__ == "__main__":
    import json
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "apply":
        print(json.dumps(apply_pending(), ensure_ascii=False, indent=2))
    elif cmd == "status":
        for it in status():
            mark = "x" if it["applied"] else " "
            print(f"[{mark}] {it['filename']}#{it['stmt_index']}: {it['sql'][:70]}")
    else:
        print("usage: python src/migrator.py [status|apply]")
        sys.exit(1)
