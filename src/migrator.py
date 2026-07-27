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

# 識別子: 素の裸の単語（bareword）、または "..." / `...` / [...] で囲まれたクォート識別子。
# COLUMN キーワードは省略可能（`ALTER TABLE t ADD col TEXT` はSQLiteで有効な構文）。
_IDENT_RE = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)'
_ADD_COLUMN_RE = re.compile(
    rf"^\s*ALTER\s+TABLE\s+({_IDENT_RE})\s+ADD\s+(?:COLUMN\s+)?({_IDENT_RE})",
    re.IGNORECASE)


def _strip_ident_quotes(name):
    """識別子を囲む `"..."` / `` `...` `` / `[...]` を剥がす。"""
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ('"', '`'):
        return name[1:-1]
    if len(name) >= 2 and name[0] == "[" and name[-1] == "]":
        return name[1:-1]
    return name


def _ensure_table():
    execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT NOT NULL, stmt_index INTEGER NOT NULL, "
            "applied_at TEXT DEFAULT (datetime('now')), "
            "PRIMARY KEY (filename, stmt_index))")


def split_statements(sql):
    """SQL をステートメントに分割する。

    素朴な `split(";")` だと文字列リテラル内のセミコロンで誤分割するため、
    シングルクォートの内外を追跡する。'' はエスケープされたクォートとして扱う。

    コメント除去（-- 行コメント / /* */ ブロックコメント）も同じ走査の中で行う。
    以前は別パスで正規表現による除去を先に行っていたが、それだと文字列リテラルの
    内外を区別できず、`'a--b'` のようなリテラル内の `--` まで消してクォート閉じ忘れの
    壊れた文になったり、`'a/*b*/c'` が例外も出さず静かに `'ac'` になったりする
    （レビュー指摘）。文字列の外にいるときだけコメントとして読み飛ばすことで、
    1パスで正しく処理する。
    """
    n = len(sql)
    out, buf, in_str, i = [], [], False, 0
    while i < n:
        ch = sql[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            # 行コメント: 改行の直前まで読み飛ばす（改行自体は通常どおり処理させる）
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            # ブロックコメント: 閉じタグの直後まで読み飛ばす
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
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


# Cloudflare D1 は「1クエリあたりのバインドパラメータ100個」が上限。
# backfill は1行あたり2バインド（filename, stmt_index）なので50行まで1文に入る。
# 現状のレガシーは39行で1文に収まるが、将来ファイルが増えても壊れないよう分割する。
D1_MAX_BINDS = 100
_BACKFILL_BINDS_PER_ROW = 2


def _mark_applied_bulk(rows):
    """(filename, stmt_index) のリストを、まとめ INSERT で記録する。

    【なぜ1文にするのか】D1 は 1文 = 1 HTTP 往復でトランザクションが張れない。
    39本の個別 INSERT だと 39 往復のあいだどこで落ちても部分適用になり、しかも
    ロールバックできない。1文にすれば「全部書けたか、1行も書けなかったか」の
    どちらかに収束する。
    """
    per_query = D1_MAX_BINDS // _BACKFILL_BINDS_PER_ROW
    for i in range(0, len(rows), per_query):
        chunk = rows[i:i + per_query]
        placeholders = ",".join(["(?,?)"] * len(chunk))
        binds = []
        for filename, stmt_index in chunk:
            binds += [filename, stmt_index]
        execute("INSERT OR IGNORE INTO schema_migrations (filename, stmt_index) VALUES "
                + placeholders, tuple(binds))


def _legacy_rows():
    """レガシーファイルの全ステートメントを (filename, stmt_index) で列挙する。"""
    out = []
    for filename in LEGACY_FILES:
        path = os.path.join(MIGRATIONS_DIR, filename)
        if not os.path.exists(path):
            continue
        for i in range(len(_read_statements(filename))):
            out.append((filename, i))
    return out


def _backfill_legacy():
    """レガシーのマイグレーションを「適用済み」として記録する。

    【ガードの意味】記録が1つも無い＝このDBは migrator の管理外で運用されてきた、
    ということ。0004 以前は手作業で適用済みの前提なので、再実行が危険な
    レガシーを「適用済み」として記録してから運用を始める。

    【なぜ「空かどうか」ではなく「レガシー以外の記録があるか」で判定するのか】
    旧実装は「schema_migrations が完全に空のときだけ」だった。だが記録は
    39本の個別 INSERT で、D1 ではトランザクションが張れない。途中で落ちて1行でも
    書けると、その1行がガードを永久に閉じ、残りのレガシー文が恒久的に「未適用」の
    ままになる。その状態で「未適用を適用」を押すと、0004 のバックアップ表作成
    （CREATE TABLE _mig_* AS SELECT *）だけが「適用済み」としてスキップされ、
    DELETE FROM shifts / wish_history / fixed_shifts / change_requests だけが
    走ってデータが消える（レビュアーの実測で全消失を再現）。
    「レガシー以外の記録が無い＝まだ管理下に入っていない」を条件にすることで、
    途中失敗した backfill は次回の呼び出しで必ず完了する（自己修復）。
    レガシー以外の記録が1つでもあれば、以降は二度と backfill しない。
    """
    legacy = _legacy_rows()
    if not legacy:
        return
    existing = {(r["filename"], r["stmt_index"])
                for r in query_all("SELECT filename, stmt_index FROM schema_migrations")}
    if existing - set(legacy):
        # レガシー以外の記録がある＝既に migrator の管理下。何もしない。
        return
    missing = [row for row in legacy if row not in existing]
    if not missing:
        return
    _mark_applied_bulk(missing)


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
    table = _strip_ident_quotes(m.group(1))
    column = _strip_ident_quotes(m.group(2))
    return _column_exists(table, column)


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
