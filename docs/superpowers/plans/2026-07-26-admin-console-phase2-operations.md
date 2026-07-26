# システム管理者コンソール Phase 2（運営機能）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** システム管理者が全社の状況を把握し、店舗のライフサイクルを管理し、サポート時に顧客の画面を閲覧できるようにする。

**Architecture:** 管理者機能を `src/admin_api.py` と `public/admin.js` に切り出す（Blueprint は使わず `register_admin_routes(app, ...)` で登録）。代理閲覧は `sessions.acting_shop_id` を `require_auth` の一箇所で解釈することで実現し、GET 以外は 403 で弾く。スキーマ変更は `src/migrator.py` がステートメント単位で適用状態を追跡する。

**Tech Stack:** Python 3 / Flask / SQLite（本番は Cloudflare D1 REST API）/ pytest / Vanilla JS / Bootstrap 5 / Playwright

**設計書:** `docs/superpowers/specs/2026-07-26-admin-console-design.md` の §3, §4, §6
**前提:** Phase 1（`docs/superpowers/plans/2026-07-26-admin-console-phase1-security.md`）が完了していること

## Global Constraints

- コメントは日本語で書き、「なぜ」に焦点を当てる
- コミットメッセージは `fix:` / `feat:` / `refactor:` プレフィックス + 日本語サマリ
- DBアクセスは `query_all` / `query_one` / `execute` / `insert_row`（local/D1 自動切替）
- 他テナントのリソースは 403 ではなく **404**
- 入力エラーは `raise ValueError(...)` → 400、認可エラーは `abort(403)`
- **フロントエンドの規約**（`public/app.js` の既存パターンに厳密に従う）:
  - 画面は `SCREENS.<name> = async function (el) { ... }`
  - データ取得は `const tok = navToken(); const d = await api('/...'); if (!isAlive(tok)) return;`
  - `innerHTML` に差す値は必ず `esc()` を通す
  - コンポーネントは `pageHead()` / `card()` / `sectionTitle()` / `badge()` / `emptyState()` / `kpiCard()`
  - 表は `.table-wrap > table.data-table`、数値列は `class="t-num num"`
  - 操作は `openModal(title, bodyHtml, onSave, opts)`、結果は `toast(msg, 'success'|'error')`
  - イベントは再描画のたび `querySelectorAll('[data-x]').forEach((b) => b?.addEventListener(...))`
  - **新しい色を発明しない。** 既存トークン（`var(--ink)` `var(--info)` `var(--rule)` 等）のみ使う。`tests/test_design_tokens.py` が落ちる
- `execute()` は毎回 `commit()` する（`src/db.py:138`）。トランザクションは張れないので、破壊的操作はどこまで進んだかを返して再実行可能にする
- テストは `.venv/bin/python -m pytest tests/ -q` で全件通ること
- 構文チェック: `node --check public/app.js`、`node --check public/admin.js`、`.venv/bin/python -c "import ast; ast.parse(open('src/app.py').read())"`

---

### Task 1: マイグレーション適用エンジン（`src/migrator.py`）

`migrations/*.sql` をステートメント単位で適用し、`schema_migrations` に記録する。`0003` の事故（前半失敗・後半成功の部分適用）を検出できるようにするのが目的。

**Files:**
- Create: `src/migrator.py`
- Test: `tests/test_migrator.py`

**Interfaces:**
- Produces:
  - `split_statements(sql: str) -> list[str]`
  - `status() -> list[dict]` — 各要素 `{"filename": str, "stmt_index": int, "applied": bool, "sql": str}`
  - `apply_pending() -> dict` — `{"applied": [...], "skipped": [...], "failed": dict|None}`
  - 定数 `LEGACY_FILES: tuple[str, ...]`

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_migrator.py`:

```python
"""migrator.py のテスト。

背景: 0003 の適用が本番D1で「前半失敗・後半成功」の部分適用になった事故がある
(migrations/0004_fix_student_role_check.sql:3-27)。ファイル単位の記録では
この状態を表現できないため、ステートメント単位で記録する。
"""
import os

import pytest

import db as dbmod
import migrator


class TestSplitStatements:
    def test_splits_on_semicolon(self):
        sql = "CREATE TABLE a (id INTEGER); CREATE TABLE b (id INTEGER);"
        assert migrator.split_statements(sql) == [
            "CREATE TABLE a (id INTEGER)",
            "CREATE TABLE b (id INTEGER)",
        ]

    def test_drops_line_comments(self):
        sql = "-- これはコメント\nCREATE TABLE a (id INTEGER);\n-- 末尾コメント"
        assert migrator.split_statements(sql) == ["CREATE TABLE a (id INTEGER)"]

    def test_drops_block_comments(self):
        sql = "/* 複数行\n   コメント */\nCREATE TABLE a (id INTEGER);"
        assert migrator.split_statements(sql) == ["CREATE TABLE a (id INTEGER)"]

    def test_keeps_semicolon_inside_string_literal(self):
        """文字列リテラル内のセミコロンで分割しないこと。"""
        sql = "INSERT INTO t (v) VALUES ('a;b'); INSERT INTO t (v) VALUES ('c');"
        assert migrator.split_statements(sql) == [
            "INSERT INTO t (v) VALUES ('a;b')",
            "INSERT INTO t (v) VALUES ('c')",
        ]

    def test_handles_escaped_quote(self):
        """'' でエスケープされたシングルクォートを正しく扱うこと。"""
        sql = "INSERT INTO t (v) VALUES ('it''s;fine'); SELECT 1;"
        assert migrator.split_statements(sql) == [
            "INSERT INTO t (v) VALUES ('it''s;fine')",
            "SELECT 1",
        ]

    def test_ignores_empty_statements(self):
        assert migrator.split_statements(";;\n\n;") == []


class TestStatus:
    def test_legacy_files_are_backfilled_as_applied(self):
        """schema_migrations が空のとき、0004 以前は適用済みとして記録されること。

        0004 は staffs テーブルの再構築を含み、再実行が危険なため。
        """
        rows = migrator.status()
        legacy = [r for r in rows if r["filename"] in migrator.LEGACY_FILES]
        assert legacy, "レガシーのマイグレーションファイルが見つからない"
        assert all(r["applied"] for r in legacy), "レガシーが未適用と判定されている"

    def test_new_migration_is_pending(self):
        rows = migrator.status()
        new = [r for r in rows if r["filename"] == "0005_admin_console.sql"]
        assert new, "0005 が status に現れない"


class TestApplyPending:
    def test_applies_pending_and_records(self, tmp_path, monkeypatch):
        """未適用のステートメントを適用し、schema_migrations に記録すること。"""
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(migrator, "LEGACY_FILES", ())
        (tmp_path / "0009_test.sql").write_text(
            "CREATE TABLE IF NOT EXISTS mig_test (id INTEGER);\n"
            "CREATE INDEX IF NOT EXISTS idx_mig_test ON mig_test(id);\n",
            encoding="utf-8")

        result = migrator.apply_pending()
        assert result["failed"] is None
        assert len(result["applied"]) == 2

        # 2回目は何も適用されない（記録済みのため）
        result2 = migrator.apply_pending()
        assert result2["applied"] == []
        dbmod.execute("DROP TABLE IF EXISTS mig_test")

    def test_skips_add_column_when_column_exists(self, tmp_path, monkeypatch):
        """ALTER TABLE ADD COLUMN は冪等でないため、列が既にあればスキップすること。"""
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(migrator, "LEGACY_FILES", ())
        dbmod.execute("CREATE TABLE IF NOT EXISTS mig_col (id INTEGER, already TEXT)")
        (tmp_path / "0009_test.sql").write_text(
            "ALTER TABLE mig_col ADD COLUMN already TEXT;\n", encoding="utf-8")

        result = migrator.apply_pending()
        assert result["failed"] is None, f"スキップされず失敗している: {result['failed']}"
        assert len(result["skipped"]) == 1
        dbmod.execute("DROP TABLE IF EXISTS mig_col")

    def test_partial_failure_records_only_successes(self, tmp_path, monkeypatch):
        """途中で失敗したとき、成功した分だけが記録され、失敗箇所が返ること。"""
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(migrator, "LEGACY_FILES", ())
        (tmp_path / "0009_test.sql").write_text(
            "CREATE TABLE IF NOT EXISTS mig_ok (id INTEGER);\n"
            "THIS IS NOT VALID SQL;\n"
            "CREATE TABLE IF NOT EXISTS mig_never (id INTEGER);\n",
            encoding="utf-8")

        result = migrator.apply_pending()
        assert len(result["applied"]) == 1
        assert result["failed"] is not None
        assert result["failed"]["stmt_index"] == 1
        # 3文目は実行されていない
        assert dbmod.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mig_never'") is None

        # 再実行すると失敗箇所から再開する（1文目は記録済みなのでスキップ）
        result2 = migrator.apply_pending()
        assert result2["applied"] == []
        assert result2["failed"]["stmt_index"] == 1
        dbmod.execute("DROP TABLE IF EXISTS mig_ok")
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_migrator.py -v`
Expected: `ModuleNotFoundError: No module named 'migrator'` で全件 FAIL

- [ ] **Step 3: `src/migrator.py` を実装**

```python
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
```

- [ ] **Step 4: conftest に `schema_migrations` を追加**

`tests/conftest.py` の `_TABLES` に `"schema_migrations"` を追加する（`login_attempts` の隣）。テストごとに適用状態がリセットされる。

- [ ] **Step 5: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_migrator.py -v`
Expected: `TestSplitStatements` と `TestApplyPending` は PASS。`TestStatus` の2件は `0005_admin_console.sql` がまだ無いため FAIL する（Task 2 で作る）

- [ ] **Step 6: コミット**

```bash
git add src/migrator.py tests/test_migrator.py tests/conftest.py
git commit -m "feat(db): マイグレーション適用エンジンを追加

0003 の部分適用事故を踏まえ、ステートメント単位で適用状態を記録する。
起動時の自動適用はせず、明示的な apply でのみDDLを走らせる。
ALTER TABLE ADD COLUMN は冪等にできないため、列の有無を見てスキップする。"
```

---

### Task 2: スキーマ変更（`migrations/0005` と `schema.sql`）

**Files:**
- Create: `migrations/0005_admin_console.sql`
- Modify: `schema.sql`
- Test: `tests/test_migrator.py`（Task 1 の `TestStatus` が通るようになる）

**Interfaces:**
- Produces: `sessions.acting_shop_id`, `shops.is_archived`, `shops.archived_at`, `schema_migrations` テーブル

- [ ] **Step 1: マイグレーションファイルを作成**

新規ファイル `migrations/0005_admin_console.sql`:

```sql
-- 0005_admin_console.sql
-- システム管理者コンソール（設計書: docs/superpowers/specs/2026-07-26-admin-console-design.md）
--
-- 適用方法:
--   ローカル: python src/migrator.py apply
--   本番D1  : 管理者画面「システム」→ マイグレーション → 未適用を適用
--
-- 【規約】今後のマイグレーションは冪等な形で書くこと。
--   - CREATE TABLE / CREATE INDEX は IF NOT EXISTS を付ける
--   - ALTER TABLE ADD COLUMN は IF NOT EXISTS を書けないため、migrator が
--     PRAGMA table_info で列の有無を見てスキップする

-- 1. マイグレーション適用状態の追跡（migrator 自身も CREATE するが、
--    新規環境で schema.sql だけを流した場合のために定義を残す）
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT NOT NULL,
  stmt_index INTEGER NOT NULL,
  applied_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (filename, stmt_index)
);

-- 2. ログイン試行のレート制限（Phase 1 で ensure_db が作るが、記録として残す）
CREATE TABLE IF NOT EXISTS login_attempts (
  attempt_key  TEXT PRIMARY KEY,
  fail_count   INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  updated_at   TEXT
);

-- 3. 代理閲覧: admin セッションが一時的に見ている店舗
ALTER TABLE sessions ADD COLUMN acting_shop_id INTEGER;

-- 4. 店舗アーカイブ
ALTER TABLE shops ADD COLUMN is_archived INTEGER DEFAULT 0;
ALTER TABLE shops ADD COLUMN archived_at TEXT;
```

- [ ] **Step 2: `schema.sql` を更新**

新規環境は `init_schema()`（`src/db.py:165`）が `schema.sql` を `executescript` する。テストも毎回これを流すため、**`ALTER TABLE` ではなく `CREATE TABLE` の定義に列を含める**こと（`ALTER` を書くと2回目の実行で "duplicate column name" になる）。

`sessions` の定義（`schema.sql:114-121`）に `acting_shop_id` を追加:

```sql
CREATE TABLE IF NOT EXISTS sessions (
  token          TEXT PRIMARY KEY,
  role           TEXT NOT NULL CHECK(role IN ('admin','shop','staff')),
  user_id        INTEGER NOT NULL,
  shop_id        INTEGER,
  acting_shop_id INTEGER,   -- 代理閲覧中の店舗（admin セッションのみ）
  created_at     TEXT DEFAULT (datetime('now')),
  expires_at     TEXT
);
```

`shops` の定義（`schema.sql:15-23`）に `is_archived` / `archived_at` を追加する（既存の列定義はそのままに、末尾へ2行足す）:

```sql
  is_archived INTEGER DEFAULT 0,
  archived_at TEXT
```

`schema.sql` の末尾に `schema_migrations` の定義を追加する（`login_attempts` は Phase 1 で追加済み）:

```sql
-- -----------------------------------------------------------
-- 16. schema_migrations: マイグレーションの適用状態（ステートメント単位）
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT NOT NULL,
  stmt_index INTEGER NOT NULL,
  applied_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (filename, stmt_index)
);
```

- [ ] **Step 3: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_migrator.py -v`
Expected: 全件 PASS（`TestStatus::test_new_migration_is_pending` が通るようになる）

- [ ] **Step 4: 全テストを実行**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 5: ローカルDBに適用**

Run: `.venv/bin/python src/migrator.py status`
Expected: `0005_admin_console.sql` の各行が `[ ]`（未適用）で表示される

Run: `.venv/bin/python src/migrator.py apply`
Expected: `applied` に4〜6件、`failed` は `null`

Run: `sqlite3 shift.db "PRAGMA table_info(sessions);" | grep acting`
Expected: `acting_shop_id` が表示される

- [ ] **Step 6: コミット**

```bash
git add migrations/0005_admin_console.sql schema.sql
git commit -m "feat(db): 管理者コンソール用のスキーマ変更

sessions.acting_shop_id（代理閲覧）、shops.is_archived/archived_at（アーカイブ）、
schema_migrations（適用状態の追跡）を追加。schema.sql には ALTER ではなく
CREATE TABLE の定義として書く（init_schema が毎回 executescript するため）。"
```

---

### Task 3: 管理者APIを `src/admin_api.py` へ切り出す（純移設）

**機能変更を一切しない純粋な移設。** 機能追加と同じコミットに混ぜると差分レビューが不可能になるため、独立したタスクにする。

**Files:**
- Create: `src/admin_api.py`
- Modify: `src/app.py`（管理者ルート約440行を削除し、登録呼び出しを追加）

**Interfaces:**
- Produces: `register_admin_routes(app, *, require_auth, audit, summarize_shifts)` — `/api/admin/*` の全ルートを `app` に登録する

- [ ] **Step 1: 移設対象を洗い出す**

Run: `grep -n '@app\.\(get\|post\|put\|patch\|delete\)("/api/admin' src/app.py`

Expected: 19本のルートが表示される。加えて `/api/admin/notifications` 系2本（`src/app.py:1626, 1632`）も対象。

移設する関数（`src/app.py` の行番号は Phase 1 の変更で前後している可能性があるので、必ず grep で現在位置を確認すること）:
`admin_shops`, `admin_create_shop`, `admin_update_shop`, `admin_shop_stats`, `admin_shop_staffs`,
`admin_shop_staff_update_role`, `admin_shop_staff_update`, `admin_audit_logs`, `admin_debug_db_schema`,
`admin_db_migrate`, `admin_db_restore_staffs`, `admin_db_diagnostic`, `admin_shop_staff_reset_password`,
`admin_shop_staffs_post`, `admin_shop_migrate_legacy_manager`, `admin_shop_next_period`,
`admin_shop_summary`, `admin_notifs`, `admin_notifs_readall`

- [ ] **Step 2: `src/admin_api.py` の骨格を作る**

`src/app.py` 側にしか無いヘルパは `require_auth` / `audit` / `summarize_shifts` の3つ。それ以外（`calc_next_period`, `parse_settings`, `validate_password`, `jst_now` は `utils`、`query_all` 等は `db`、`hash_password` 等は `auth`）は直接 import できる。循環 import を避けるため、この3つだけをキーワード引数で受け取る。

```python
"""admin_api.py - システム管理者向け API (/api/admin/*)。

src/app.py が肥大化していたため切り出した。Blueprint は使わず、
register_admin_routes(app, ...) の中で既存と同じ @app.get/post デコレータで登録する
（url_prefix 等の新しい概念を持ち込まないため）。

app.py 側にしか無いヘルパ（require_auth / audit / summarize_shifts）は
循環 import を避けるためキーワード引数で受け取る。
"""
from flask import request, jsonify, abort

from db import query_all, query_one, execute, insert_row
from auth import hash_password, verify_password, strip_password
from utils import calc_next_period, jst_now, parse_settings, validate_password


def register_admin_routes(app, *, require_auth, audit, summarize_shifts):
    """/api/admin/* の全ルートを app に登録する。"""

    @app.get("/api/admin/shops")
    def admin_shops():
        require_auth(["admin"])
        ...
```

- [ ] **Step 3: 各関数を機械的に移設する**

`src/app.py` から対象関数を切り取り、`register_admin_routes` の中へインデント1段（4スペース）深くして貼る。**関数の中身は1文字も変えない。** デコレータもそのまま持っていく。

移設後、`src/app.py` から対象関数と、それに付随していたセクションコメント（`# 管理者` 等）を削除する。

- [ ] **Step 4: `src/app.py` に登録呼び出しを追加**

`src/app.py` の `ensure_db()` 定義の直前（全ルート定義とヘルパ定義が終わった位置）に追加する。`summarize_shifts` は `src/app.py:148` で定義されているのでこの時点で参照可能。

```python
# 管理者API（/api/admin/*）は src/admin_api.py に切り出してある。
# require_auth / audit / summarize_shifts は app.py 側にしか無いため引数で渡す。
import admin_api
admin_api.register_admin_routes(
    app, require_auth=require_auth, audit=audit, summarize_shifts=summarize_shifts)
```

- [ ] **Step 5: 構文チェックとテスト**

Run: `.venv/bin/python -c "import ast; ast.parse(open('src/app.py').read())" && .venv/bin/python -c "import ast; ast.parse(open('src/admin_api.py').read())"`
Expected: 出力なし

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: **全件 PASS。** 移設は機能変更を伴わないので、1件でも落ちたら移設ミス。`NameError` が出たら、その名前を `admin_api.py` の import か `register_admin_routes` の引数に足す。

- [ ] **Step 6: ルート数が変わっていないことを確認**

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'src')
import app
routes = sorted(str(r) for r in app.app.url_map.iter_rules() if '/api/admin' in str(r))
print(len(routes)); [print(r) for r in routes]
"
```
Expected: 21本（既存19 + notifications 2本）。移設前に同じコマンドを実行して控えておき、一致することを確認する。

- [ ] **Step 7: コミット**

```bash
git add src/app.py src/admin_api.py
git commit -m "refactor: 管理者APIを src/admin_api.py に切り出し

src/app.py が4000行超で保守が難しくなっていたため。Blueprint は使わず
register_admin_routes(app, ...) で登録する。機能変更は一切していない。"
```

---

### Task 4: 管理画面を `public/admin.js` へ切り出す（純移設）

**Files:**
- Create: `public/admin.js`
- Modify: `public/app.js`（管理画面コード約490行を削除）
- Modify: `public/index.html`（script タグ追加）
- Modify: `src/app.py`（キャッシュバスティングとキャッシュヘッダ）
- Modify: `tests/test_design_tokens.py`（走査対象に追加）

**Interfaces:**
- Consumes: `app.js` のグローバル（`SCREENS`, `api`, `esc`, `openModal`, `toast`, `card`, `pageHead`, `sectionTitle`, `badge`, `emptyState`, `kpiCard`, `navToken`, `isAlive`, `safeSetHTML`, `navigateTo`, `roleLabel`, `roleClass`）
- Produces: `SCREENS.adminHome` / `adminShops` / `adminShopDetail` / `adminAudit` と関連モーダル関数

- [ ] **Step 1: 移設対象を確認**

Run: `grep -n 'SCREENS\.admin\|function openAdmin\|function openDbMaintenance\|AUDIT_ACTION_LABELS\|function auditActionLabel' public/app.js`

移設する定義: `SCREENS.adminHome`, `openDbMaintenanceModal`, `SCREENS.adminShops`, `SCREENS.adminShopDetail`, `AUDIT_ACTION_LABELS`, `auditActionLabel`, `SCREENS.adminAudit`, `openAdminStaffEditModal`, `openAdminRoleModal`, `openAdminPwResetModal`, `openAdminAddStaffModal`, `openAdminMigrateModal`

**`NAV_DEFS`（`public/app.js:464-487`）は移設しない。** 全ロール共通の定義であり、`admin.js` より先に評価される必要があるため。

- [ ] **Step 2: `public/admin.js` を作る**

```js
/* admin.js - システム管理者向け画面（SCREENS.admin*）。

   app.js が5000行近くになっていたため切り出した。モジュール化していない
   （bundler を使わない）ので、app.js のグローバル関数（api / esc / openModal /
   toast / card / pageHead / navToken / isAlive 等）をそのまま利用する。
   index.html で app.js の後に読み込むこと。 */
```

以降に、Step 1 で洗い出した定義を `public/app.js` から**1文字も変えずに**移設する。`SCREENS` は `app.js` で `const SCREENS = {}` として定義済みなので、`admin.js` からは `SCREENS.adminHome = ...` と代入するだけでよい。

- [ ] **Step 3: `public/index.html` に script タグを追加**

`public/index.html:105` の `<script src="app.js"></script>` の**直後**に追加する。順序が逆だと `SCREENS` が未定義になる。

```html
  <script src="admin.js"></script>
```

- [ ] **Step 4: キャッシュバスティングに `admin.js` を追加**

`src/app.py` の `_index_html_with_asset_version()` を修正する。

```python
    try:
        js_mtime = int(os.path.getmtime(os.path.join(PUBLIC_DIR, "app.js")))
        admin_mtime = int(os.path.getmtime(os.path.join(PUBLIC_DIR, "admin.js")))
        css_mtime = int(os.path.getmtime(os.path.join(PUBLIC_DIR, "style.css")))
        html = html.replace('src="app.js"', f'src="app.js?v={js_mtime}"')
        html = html.replace('src="admin.js"', f'src="admin.js?v={admin_mtime}"')
        html = html.replace('href="style.css"', f'href="style.css?v={css_mtime}"')
    except Exception:
        pass
```

同じファイルの `static_files()` のキャッシュ判定も修正する。

```python
        if path in ("app.js", "admin.js", "style.css"):
```

`admin.js` を足し忘れると「新しい app.js と古い admin.js」の組み合わせが起きる。

- [ ] **Step 5: デザイントークンのテスト対象に追加**

`tests/test_design_tokens.py:16` の `JS_PATH` の直後に追加し、`_read_js()` が両方を連結して返すようにする。

```python
JS_PATH = Path(__file__).resolve().parents[1] / "public" / "app.js"
# 管理画面は admin.js に切り出してある。ここに入れないと廃止トークンの
# 再導入検査（TestOldTokensRemoved）が管理画面をすり抜ける。
ADMIN_JS_PATH = Path(__file__).resolve().parents[1] / "public" / "admin.js"
```

`_read_js()` の定義を探し、`JS_PATH.read_text(...)` を `JS_PATH.read_text(...) + "\n" + ADMIN_JS_PATH.read_text(...)` に変える。実装前に `_read_js` の現在の定義を読むこと。

`tests/test_design_tokens.py:165` と `:169` のエラーメッセージ内の `"public/app.js"` は `"public/app.js または admin.js"` に変える。

- [ ] **Step 6: 構文チェックとテスト**

Run: `node --check public/app.js && node --check public/admin.js`
Expected: 出力なし

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 7: ブラウザで動作確認**

```bash
PORT=5555 FLASK_DEBUG=1 .venv/bin/python src/app.py
```

管理者でログインし、ホーム / 店舗一覧 / 店舗詳細 / 監査ログの4画面が移設前と同じに表示されることを確認する。ブラウザのコンソールにエラーが無いこと（`SCREENS.adminXxx is not a function` が出たら読み込み順の問題）。

- [ ] **Step 8: コミット**

```bash
git add public/admin.js public/app.js public/index.html src/app.py tests/test_design_tokens.py
git commit -m "refactor(ui): 管理画面を public/admin.js に切り出し

public/app.js が5000行近くになっていたため。モジュール化していないので
app.js のグローバル関数をそのまま使う（index.html で app.js の後に読む）。
キャッシュバスティングとデザイントークン検査の対象にも admin.js を追加した。
機能変更は一切していない。"
```

---

### Task 5: 管理者アカウント管理 API

2人目の運営者を追加する手段が無い。

※ 自分のパスワード変更（S2 / `PUT /api/admin/password`）は **Phase 1 の Task 10 で実装済み**。
`tests/test_admin_accounts.py` は既に存在し、`TestAdminPasswordChange` が入っている。
このタスクでは同ファイルに `TestAdminAccounts` を追記する形になる。
Task 3 で `src/admin_api.py` へ移設する際、`admin_change_password` も一緒に移すこと。

**Files:**
- Modify: `src/admin_api.py`
- Test: `tests/test_admin_accounts.py`（既存に追記）

**Interfaces:**
- Produces:
  - `GET /api/admin/admins` → `{"admins": [{"id", "admin_id", "name", "created_at"}]}`
  - `POST /api/admin/admins` body `{admin_id, name, password}` → `{"ok": True, "id": int}`
  - `DELETE /api/admin/admins/<int:aid>` → `{"ok": True}`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_admin_accounts.py` に追記する（ファイル冒頭の import と `_token` / `_hdr` は
Phase 1 で定義済み）:

```python
class TestAdminAccounts:
    def test_list_admins(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.get("/api/admin/admins", headers=_hdr(t))
        assert r.status_code == 200
        admins = r.get_json()["admins"]
        assert len(admins) == 1
        assert admins[0]["admin_id"] == "admin"
        assert "password_hash" not in admins[0], "パスワードハッシュが漏れている"

    def test_create_admin(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.post("/api/admin/admins", headers=_hdr(t),
                        json={"admin_id": "ops2", "name": "運営2", "password": "OpsPass123"})
        assert r.status_code == 200
        # 作った管理者でログインできる
        assert client.post("/api/login", json={"user_code": "ops2",
                                               "password": "OpsPass123"}).status_code == 200

    def test_duplicate_admin_id_is_rejected(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.post("/api/admin/admins", headers=_hdr(t),
                        json={"admin_id": "admin", "name": "重複", "password": "OpsPass123"})
        assert r.status_code == 400

    def test_delete_admin(self, client):
        insert_admin("admin", "Admin123")
        other = insert_admin("ops2", "OpsPass123", name="運営2")
        t = _token(client)
        r = client.delete(f"/api/admin/admins/{other}", headers=_hdr(t))
        assert r.status_code == 200
        assert dbmod.query_one("SELECT id FROM system_admins WHERE id=?", (other,)) is None

    def test_cannot_delete_self(self, client):
        insert_admin("admin", "Admin123")
        insert_admin("ops2", "OpsPass123", name="運営2")
        t = _token(client)
        me = dbmod.query_one("SELECT id FROM system_admins WHERE admin_id='admin'")["id"]
        r = client.delete(f"/api/admin/admins/{me}", headers=_hdr(t))
        assert r.status_code == 400

    def test_cannot_delete_last_admin(self, client):
        insert_admin("admin", "Admin123")
        other = insert_admin("ops2", "OpsPass123", name="運営2")
        t = _token(client, "ops2", "OpsPass123")
        me = dbmod.query_one("SELECT id FROM system_admins WHERE admin_id='admin'")["id"]
        r = client.delete(f"/api/admin/admins/{me}", headers=_hdr(t))
        assert r.status_code == 200
        # 残り1人になったので、自分も消せない
        r = client.delete(f"/api/admin/admins/{other}", headers=_hdr(t))
        assert r.status_code == 400

    def test_deleting_admin_revokes_their_sessions(self, client):
        insert_admin("admin", "Admin123")
        other = insert_admin("ops2", "OpsPass123", name="運営2")
        other_token = _token(client, "ops2", "OpsPass123")
        t = _token(client)
        assert client.delete(f"/api/admin/admins/{other}", headers=_hdr(t)).status_code == 200
        assert client.get("/api/me", headers=_hdr(other_token)).status_code == 401
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_accounts.py -v`
Expected: `TestAdminAccounts` が全件 FAIL（404 が返る）。
`TestAdminPasswordChange` は Phase 1 実装済みなので PASS のままであること。

- [ ] **Step 3: 実装する**

`src/admin_api.py` の `register_admin_routes` の中に追加する。`gen_token` は不要。`hash_password` / `validate_password` はファイル冒頭で import 済み。

```python
    def _current_admin_id():
        """require_auth(["admin"]) 済みの前提で、自分の system_admins.id を返す。"""
        from flask import g
        return (getattr(g, "user", None) or {}).get("id")

    @app.get("/api/admin/admins")
    def admin_list_admins():
        require_auth(["admin"])
        rows = query_all("SELECT id, admin_id, name, created_at FROM system_admins ORDER BY id")
        return jsonify({"admins": rows})

    @app.post("/api/admin/admins")
    def admin_create_admin():
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        new_id = (body.get("admin_id") or "").strip()
        name = (body.get("name") or "").strip() or "システム管理者"
        pw = body.get("password") or ""
        if not new_id:
            raise ValueError("管理者IDを入力してください")
        if new_id == "admin" and query_one("SELECT id FROM system_admins WHERE admin_id='admin'"):
            raise ValueError("その管理者IDは既に使われています")
        if query_one("SELECT id FROM system_admins WHERE admin_id=?", (new_id,)):
            raise ValueError("その管理者IDは既に使われています")
        msg = validate_password(pw)
        if msg:
            raise ValueError(msg)
        meta = execute("INSERT INTO system_admins (admin_id, password_hash, name) VALUES (?,?,?)",
                       (new_id, hash_password(pw), name))
        audit("admin.create", target_type="system_admin", target_id=meta["last_row_id"],
              detail=f"admin_id={new_id}")
        return jsonify({"ok": True, "id": meta["last_row_id"]})

    @app.delete("/api/admin/admins/<int:aid>")
    def admin_delete_admin(aid):
        require_auth(["admin"])
        me = _current_admin_id()
        if aid == me:
            raise ValueError("自分自身は削除できません")
        target = query_one("SELECT id, admin_id FROM system_admins WHERE id=?", (aid,))
        if target is None:
            abort(404, description="管理者が見つかりません")
        total = query_one("SELECT COUNT(*) AS c FROM system_admins")["c"]
        if total <= 1:
            raise ValueError("最後の管理者は削除できません")
        execute("DELETE FROM sessions WHERE role='admin' AND user_id=?", (aid,))
        execute("DELETE FROM system_admins WHERE id=?", (aid,))
        audit("admin.delete", target_type="system_admin", target_id=aid,
              detail=f"admin_id={target['admin_id']}")
        return jsonify({"ok": True})
```

`from flask import g` はファイル冒頭の import に移してよい（`_current_admin_id` の中に置いているのは、既存 import を壊さないための保守的な書き方）。

- [ ] **Step 4: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_admin_accounts.py -v`
Expected: 全件 PASS

- [ ] **Step 5: 全テスト**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 6: コミット**

```bash
git add src/admin_api.py tests/test_admin_accounts.py
git commit -m "feat(admin): システム管理者アカウントの管理APIを追加

2人目以降の運営者を追加・削除できるようにした。
最後の1人と自分自身は削除できない。"
```

---

### Task 6: 代理閲覧 API（閲覧のみ）

管理者は `/api/shop/*` が全て403のため、サポート時に顧客のシフト実データを一切見られない。`sessions.acting_shop_id` を立て、`require_auth` の一箇所だけで店舗権限に化けさせる。**GET 以外は 403 で弾く。**

**Files:**
- Modify: `src/app.py`（`require_auth` に分岐追加、`/api/me` に `impersonating` 追加）
- Modify: `src/admin_api.py`（開始・解除エンドポイント）
- Test: `tests/test_impersonation.py`（新規）

**Interfaces:**
- Produces:
  - `POST /api/admin/impersonate/<int:shop_id>` → `{"ok": True, "shop": {"id", "shop_name", "shop_code"}}`
  - `DELETE /api/admin/impersonate` → `{"ok": True}`
  - `GET /api/me` のレスポンスに `impersonating: {"shop_id": int, "shop_name": str} | None`
  - `g.impersonating: bool` — 代理中の GET リクエストで True

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_impersonation.py`:

```python
"""代理閲覧（impersonation）のテスト。

管理者はサポート時に顧客の画面を見る必要があるが、書き込みは許さない。
運営者が顧客の確定シフトを壊す事故を構造的に防ぐため、GET のみ許可する。
"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    assert r.status_code == 200
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _shop_with_staff():
    sid = insert_shop("SHOP1", "pw12345678", name="レイクタウン店")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    insert_staff(sid, "p1", "アルバイト太郎")
    return sid


class TestImpersonateStart:
    def test_start_and_read_shop_data(self, client):
        """代理開始後、店舗APIのGETが通ること。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        # 代理前は403
        assert client.get("/api/shop/staffs", headers=_hdr(t)).status_code == 403

        r = client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        assert r.status_code == 200
        assert r.get_json()["shop"]["shop_name"] == "レイクタウン店"

        r = client.get("/api/shop/staffs", headers=_hdr(t))
        assert r.status_code == 200
        names = [s["name"] for s in r.get_json()["staffs"]]
        assert "アルバイト太郎" in names

    def test_unknown_shop_returns_404(self, client):
        t = _admin_token(client)
        assert client.post("/api/admin/impersonate/99999", headers=_hdr(t)).status_code == 404

    def test_shop_role_cannot_impersonate(self, client):
        sid = _shop_with_staff()
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        assert client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t)).status_code == 403


class TestImpersonateReadOnly:
    def test_write_is_forbidden(self, client):
        """代理中は POST/PUT/DELETE が 403 になること。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))

        r = client.post("/api/shop/staffs", headers=_hdr(t),
                        json={"staff_code": "new1", "name": "新人", "role": "part_time",
                              "password": "pw12345678"})
        assert r.status_code == 403, "代理中に書き込みができてしまう"

        staff = dbmod.query_one("SELECT id FROM staffs WHERE staff_code='p1'")
        r = client.delete(f"/api/shop/staffs/{staff['id']}", headers=_hdr(t))
        assert r.status_code == 403
        assert dbmod.query_one("SELECT id FROM staffs WHERE staff_code='p1'") is not None


class TestImpersonateScope:
    def test_admin_api_still_works_during_impersonation(self, client):
        """代理中でも /api/admin/* は管理者として動くこと（戻れなくならないため）。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        r = client.get("/api/admin/shops", headers=_hdr(t))
        assert r.status_code == 200

    def test_staff_api_is_not_impersonated(self, client):
        """代理中でもスタッフ用APIには化けないこと。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        r = client.get("/api/staff/myshift", headers=_hdr(t))
        assert r.status_code == 403

    def test_me_reports_impersonating(self, client):
        sid = _shop_with_staff()
        t = _admin_token(client)
        assert client.get("/api/me", headers=_hdr(t)).get_json().get("impersonating") is None
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        me = client.get("/api/me", headers=_hdr(t)).get_json()
        assert me["role"] == "admin", "代理中でも /api/me は管理者のまま返すこと"
        assert me["impersonating"]["shop_id"] == sid
        assert me["impersonating"]["shop_name"] == "レイクタウン店"


class TestImpersonateEnd:
    def test_stop_restores_admin(self, client):
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        assert client.get("/api/shop/staffs", headers=_hdr(t)).status_code == 200

        r = client.delete("/api/admin/impersonate", headers=_hdr(t))
        assert r.status_code == 200
        assert client.get("/api/shop/staffs", headers=_hdr(t)).status_code == 403
        assert client.get("/api/me", headers=_hdr(t)).get_json().get("impersonating") is None

    def test_deleted_shop_during_impersonation_returns_409(self, client):
        """代理中の店舗が消えた場合、別テナントに着地せず 409 になること。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        dbmod.execute("UPDATE sessions SET acting_shop_id=99999 WHERE role='admin'")
        r = client.get("/api/shop/staffs", headers=_hdr(t))
        assert r.status_code == 409


class TestImpersonateAudit:
    def test_start_and_end_are_audited(self, client):
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        client.delete("/api/admin/impersonate", headers=_hdr(t))
        actions = [r["action"] for r in dbmod.query_all(
            "SELECT action FROM audit_logs WHERE action LIKE 'admin.impersonate%'")]
        assert "admin.impersonate_start" in actions
        assert "admin.impersonate_end" in actions
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_impersonation.py -v`
Expected: 全件 FAIL

- [ ] **Step 3: `require_auth` に分岐を追加**

`src/app.py` の `require_auth`。**`if role not in allowed:` の手前**に挿入する。位置を間違えると管理者が 403 で弾かれて代理に入れない。

```python
    role = session["role"]

    # 代理閲覧: admin セッションに acting_shop_id が立っているとき、店舗用APIに限り
    # 店舗権限として振る舞う。"admin" not in allowed を条件にしているのは、
    #   - /api/admin/* は管理者のまま動かす（「管理者に戻る」を確実に押せるようにする）
    #   - /api/me も管理者のまま動かす（impersonating 情報を返すため）
    #   - require_auth(["staff"]) には化けない
    # ため。書き込みは許さない（運営者が顧客の確定シフトを壊す事故を構造的に防ぐ）。
    acting = session.get("acting_shop_id")
    if role == "admin" and acting and "shop" in allowed and "admin" not in allowed:
        if request.method != "GET":
            abort(403, description="代理閲覧中はデータを変更できません")
        shop = query_one("SELECT * FROM shops WHERE id=?", (acting,))
        if shop is None:
            abort(409, description="代理閲覧中の店舗が見つかりません")
        g.role = "shop"
        g.user = strip_password(shop)
        g.shop_id = acting
        g.impersonating = True
        return "shop", g.user, acting

    if role not in allowed:
        abort(403, description="権限がありません")
```

通常経路の末尾（`g.shop_id = session.get("shop_id")` の隣）に `g.impersonating = False` を追加する。`audit()` などが `getattr(g, "impersonating", False)` で判定できるようにするため。

- [ ] **Step 4: `/api/me` に `impersonating` を追加**

`src/app.py` の `me()` を修正する。`require_auth` は `session` を返さないため、トークンから直接引く。

```python
@app.get("/api/me")
def me():
    role, user, _ = require_auth(["admin", "shop", "staff"])
    result = {"role": role, "user": user}
    if role == "admin":
        # 代理閲覧中かどうかをフロントに伝える（警告バーとナビ切替に使う）
        token = request.headers.get("Authorization", "")[7:]
        row = query_one("SELECT acting_shop_id FROM sessions WHERE token=?", (token,))
        acting = (row or {}).get("acting_shop_id")
        shop = query_one("SELECT id, shop_name FROM shops WHERE id=?", (acting,)) if acting else None
        result["impersonating"] = ({"shop_id": shop["id"], "shop_name": shop["shop_name"]}
                                   if shop else None)
    if role == "shop":
        ...  # 既存のまま
    return jsonify(result)
```

- [ ] **Step 5: 開始・解除エンドポイントを実装**

`src/admin_api.py` の `register_admin_routes` の中に追加する。

```python
    @app.post("/api/admin/impersonate/<int:shop_id>")
    def admin_impersonate_start(shop_id):
        require_auth(["admin"])
        shop = query_one("SELECT id, shop_code, shop_name FROM shops WHERE id=?", (shop_id,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        token = request.headers.get("Authorization", "")[7:]
        execute("UPDATE sessions SET acting_shop_id=? WHERE token=?", (shop_id, token))
        audit("admin.impersonate_start", target_type="shop", target_id=shop_id, shop_id=shop_id,
              detail=f"{shop['shop_code']} の代理閲覧を開始（閲覧のみ）")
        return jsonify({"ok": True, "shop": shop})

    @app.delete("/api/admin/impersonate")
    def admin_impersonate_stop():
        require_auth(["admin"])
        token = request.headers.get("Authorization", "")[7:]
        row = query_one("SELECT acting_shop_id FROM sessions WHERE token=?", (token,))
        acting = (row or {}).get("acting_shop_id")
        execute("UPDATE sessions SET acting_shop_id=NULL WHERE token=?", (token,))
        if acting:
            audit("admin.impersonate_end", target_type="shop", target_id=acting, shop_id=acting,
                  detail="代理閲覧を終了")
        return jsonify({"ok": True})
```

- [ ] **Step 6: 監査ログのラベルを追加**

`public/admin.js` の `AUDIT_ACTION_LABELS` に追記する。

```js
  'admin.impersonate_start': '代理閲覧開始',
  'admin.impersonate_end': '代理閲覧終了',
  'admin.create': '管理者追加',
  'admin.delete': '管理者削除',
```

`admin.password_change` は Phase 1 で追加済みなので、Task 4 の移設でそのまま `admin.js` 側へ運ばれている。

- [ ] **Step 7: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_impersonation.py -v`
Expected: 全件 PASS

補足: `test_staff_api_is_not_impersonated` で使う `/api/staff/myshift` が実在するか確認すること。無ければ `grep -n 'api/staff/' src/app.py | head` で実在する GET エンドポイントに差し替える。

- [ ] **Step 8: 全テスト**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 9: コミット**

```bash
git add src/app.py src/admin_api.py public/admin.js tests/test_impersonation.py
git commit -m "feat(admin): 代理閲覧（閲覧のみ）を追加

管理者は /api/shop/* が全て403で、サポート時に顧客のシフト実データを
一切見られなかった。sessions.acting_shop_id を require_auth の一箇所で
解釈して店舗権限に化けさせる。GET 以外は403で弾き、運営者が顧客の
確定シフトを壊す事故を構造的に防ぐ。開始・終了は監査ログに記録する。"
```

---

### Task 7: 代理閲覧のフロントエンド

**Files:**
- Modify: `public/app.js`（警告バー、ナビ切替、`/api/me` の結果保持）
- Modify: `public/admin.js`（店舗詳細に「代理閲覧」ボタン）
- Modify: `public/style.css`（警告バーのスタイル）

**Interfaces:**
- Consumes: `GET /api/me` の `impersonating`、`POST /api/admin/impersonate/<id>`、`DELETE /api/admin/impersonate`
- Produces: グローバル `window._impersonating`、関数 `renderImpersonationBar()`、`stopImpersonation()`

- [ ] **Step 1: 警告バーのスタイルを追加**

`public/style.css` の末尾に追加する。**新しい色を発明せず既存トークンを使う**（`tests/test_design_tokens.py` が落ちる）。

```css
/* 代理閲覧バー — 誤操作防止のため常時可視にする。
   意味色ベタ地の抜き文字は #fff ではなく var(--paper) を使う規約に従う。 */
.impersonation-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 16px;
  background: var(--warning);
  color: var(--paper);
  font-size: 0.875rem;
}
.impersonation-bar .btn {
  flex-shrink: 0;
}
```

- [ ] **Step 2: 警告バーの描画を実装**

`public/app.js` の `applyTheme` 付近（グローバルヘルパの並び）に追加する。

```js
// 代理閲覧の状態。/api/me の impersonating をそのまま保持する。
window._impersonating = null;

function renderImpersonationBar() {
  const existing = document.getElementById('impersonationBar');
  if (existing) existing.remove();
  const info = window._impersonating;
  if (!info) return;
  const header = document.querySelector('.app-header');
  if (!header) return;
  const bar = document.createElement('div');
  bar.id = 'impersonationBar';
  bar.className = 'impersonation-bar';
  bar.innerHTML = `<span><i class="bi bi-eye"></i> ${esc(info.shop_name)} を代理閲覧中（閲覧のみ・変更はできません）</span>` +
    `<button class="btn btn-sm btn-light" id="stopImpersonateBtn">管理者に戻る</button>`;
  header.insertAdjacentElement('afterend', bar);
  document.getElementById('stopImpersonateBtn')?.addEventListener('click', stopImpersonation);
}

async function stopImpersonation() {
  try {
    await api('/admin/impersonate', { method: 'DELETE' });
    window._impersonating = null;
    renderImpersonationBar();
    renderNav();
    navigateTo('adminShops');
    toast('管理者に戻りました', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}
```

- [ ] **Step 3: `/api/me` の結果を反映する**

`public/app.js` で `/api/me` を呼んでいる箇所（起動時のセッション復元）を探す。

Run: `grep -n "api('/me')\|/me'" public/app.js`

その呼び出しの直後に追加する。

```js
    window._impersonating = me.impersonating || null;
    renderImpersonationBar();
```

- [ ] **Step 4: ナビを切り替える**

`public/app.js:508` 付近の `renderNav()` を探し、ナビ定義を選ぶ行を修正する。代理中は店舗のナビを出す（管理者に戻るのはバーのボタンから）。

```js
  // 代理閲覧中は店舗のナビを出す。管理者に戻るのは警告バーのボタンから。
  const navKey = window._impersonating ? 'shop' : (localStorage.getItem('shift_role') || 'staff');
  const defs = NAV_DEFS[navKey] || [];
```

実装前に `renderNav()` の現在の実装を読み、ロールを取得している変数名に合わせること。

- [ ] **Step 5: 店舗詳細に「代理閲覧」ボタンを追加**

`public/admin.js` の `SCREENS.adminShopDetail` の上部ボタン群（「スタッフ追加」の隣）に追加する。

```js
      `<button class="btn btn-sm btn-light" id="impersonateBtn"><i class="bi bi-eye"></i> この店舗を代理閲覧</button>`
```

ハンドラを同じ関数内に追加する。

```js
  document.getElementById('impersonateBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-eye"></i> 代理閲覧',
      `<p class="mb-2">この店舗の画面を<strong>閲覧のみ</strong>の権限で開きます。</p>
       <p class="small text-secondary mb-0">データの変更はできません。開始と終了は監査ログに記録されます。</p>`,
      async (w, close) => {
        try {
          const d = await api(`/admin/impersonate/${window._adminShopId}`, { method: 'POST' });
          window._impersonating = { shop_id: d.shop.id, shop_name: d.shop.shop_name };
          close();
          renderImpersonationBar();
          renderNav();
          navigateTo('dashboard');
          toast(`${d.shop.shop_name} を代理閲覧中です`, 'success');
        } catch (e) { toast(e.message, 'error'); }
      },
      { saveLabel: '代理閲覧を開始' }));
```

- [ ] **Step 6: 構文チェック**

Run: `node --check public/app.js && node --check public/admin.js`
Expected: 出力なし

- [ ] **Step 7: デザイントークンのテスト**

Run: `.venv/bin/python -m pytest tests/test_design_tokens.py -v`
Expected: 全件 PASS。落ちた場合は新しい色を使っている

- [ ] **Step 8: ブラウザで動作確認**

```bash
PORT=5555 FLASK_DEBUG=1 .venv/bin/python src/app.py
```

管理者でログイン → 店舗一覧 → 店舗詳細 → 「この店舗を代理閲覧」→ ダッシュボードが店舗のものになり、上部に黄色い警告バーが出ること。シフトの保存を試みてエラーになること。「管理者に戻る」で店舗一覧に戻り、バーが消えること。リロードしても代理状態が維持されること（`/api/me` から復元される）。

- [ ] **Step 9: コミット**

```bash
git add public/app.js public/admin.js public/style.css
git commit -m "feat(ui): 代理閲覧の警告バーとナビ切替を追加

代理中は店舗のナビを出し、ヘッダ直下に常時可視の警告バーを表示する。
リロード後も /api/me の impersonating から状態を復元する。"
```

---

### Task 8: システム画面（`SCREENS.adminSystem`）

管理者アカウント管理・マイグレーション適用・DB診断を1画面に集約する。一斉通知は Task 14 で同じ画面に追加する。

**Files:**
- Modify: `src/admin_api.py`（マイグレーション状態・適用のエンドポイント）
- Modify: `public/admin.js`（`SCREENS.adminSystem`）
- Modify: `public/app.js`（`NAV_DEFS.admin` に4項目目）
- Test: `tests/test_admin_console.py`（新規）

**Interfaces:**
- Produces:
  - `GET /api/admin/migrations` → `{"migrations": [{"filename", "stmt_index", "applied", "sql"}], "pending": int}`
  - `POST /api/admin/migrations/apply` → `{"applied": [...], "skipped": [...], "failed": dict|None}`

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_admin_console.py`:

```python
"""管理者コンソールのAPI（マイグレーション適用・ダッシュボード等）。"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    assert r.status_code == 200
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


class TestMigrationsApi:
    def test_status_lists_migrations(self, client):
        t = _admin_token(client)
        r = client.get("/api/admin/migrations", headers=_hdr(t))
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data["migrations"], list)
        assert data["migrations"], "マイグレーションが1件も返っていない"
        assert isinstance(data["pending"], int)

    def test_apply_returns_result_shape(self, client):
        t = _admin_token(client)
        r = client.post("/api/admin/migrations/apply", headers=_hdr(t))
        assert r.status_code == 200
        data = r.get_json()
        assert set(["applied", "skipped", "failed"]).issubset(data.keys())

    def test_apply_is_audited(self, client):
        t = _admin_token(client)
        client.post("/api/admin/migrations/apply", headers=_hdr(t))
        row = dbmod.query_one("SELECT action FROM audit_logs WHERE action='admin.migrate' "
                              "ORDER BY id DESC LIMIT 1")
        assert row is not None, "マイグレーション適用が監査ログに残っていない"

    def test_requires_admin_role(self, client):
        sid = insert_shop("SHOP1", "pw12345678")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        assert client.get("/api/admin/migrations", headers=_hdr(t)).status_code == 403
        assert client.post("/api/admin/migrations/apply", headers=_hdr(t)).status_code == 403
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_console.py -v`
Expected: 全件 FAIL（404）

- [ ] **Step 3: エンドポイントを実装**

`src/admin_api.py` の冒頭に `import migrator` を追加し、`register_admin_routes` の中に追加する。

```python
    @app.get("/api/admin/migrations")
    def admin_migrations_status():
        require_auth(["admin"])
        rows = migrator.status()
        return jsonify({"migrations": rows,
                        "pending": sum(1 for r in rows if not r["applied"])})

    @app.post("/api/admin/migrations/apply")
    def admin_migrations_apply():
        require_auth(["admin"])
        result = migrator.apply_pending()
        detail = (f"applied={len(result['applied'])} skipped={len(result['skipped'])}"
                  f" failed={'yes' if result['failed'] else 'no'}")
        audit("admin.migrate", target_type="schema", detail=detail)
        return jsonify(result)
```

- [ ] **Step 4: ナビに「システム」を追加**

`public/app.js:482-486` の `NAV_DEFS.admin` を4項目にする。

```js
  admin: [
    { key: 'adminHome', icon: 'bi-house-door', label: 'ホーム', mobile: true },
    { key: 'adminShops', icon: 'bi-shop', label: '店舗', mobile: true },
    { key: 'adminAudit', icon: 'bi-clipboard-data', label: '監査ログ', mobile: true },
    { key: 'adminSystem', icon: 'bi-gear', label: 'システム', mobile: true },
  ],
```

- [ ] **Step 5: `SCREENS.adminSystem` を実装**

`public/admin.js` に追加する。既存の `openDbMaintenanceModal` はこの画面に統合するので、統合後に旧関数と `SCREENS.adminHome` からのボタンを削除する。

```js
let adminSystemTab = 'admins';

SCREENS.adminSystem = async function (el) {
  el.innerHTML = pageHead('システム', 'bi-gear') +
    `<div class="tabs no-print">
       <button class="tab ${adminSystemTab === 'admins' ? 'active' : ''}" data-tab="admins">管理者アカウント</button>
       <button class="tab ${adminSystemTab === 'migrations' ? 'active' : ''}" data-tab="migrations">マイグレーション</button>
       <button class="tab ${adminSystemTab === 'diagnostic' ? 'active' : ''}" data-tab="diagnostic">DB診断</button>
     </div><div id="sysBody"></div>`;
  el.querySelectorAll('.tab').forEach((t) => t?.addEventListener('click', () => {
    adminSystemTab = t.dataset.tab;
    el.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === adminSystemTab));
    renderAdminSystemTab(document.getElementById('sysBody'));
  }));
  renderAdminSystemTab(document.getElementById('sysBody'));
};

function renderAdminSystemTab(body) {
  ({ admins: renderAdminAccountsTab, migrations: renderMigrationsTab,
     diagnostic: renderDiagnosticTab }[adminSystemTab])(body);
}

async function renderAdminAccountsTab(body) {
  const tok = navToken();
  body.innerHTML = card(sectionTitle('bi-person-badge', '管理者アカウント',
    '<button class="btn btn-primary btn-sm" id="addAdminBtn"><i class="bi bi-plus-lg"></i></button>') +
    '<div id="adminList"></div>') +
    card(sectionTitle('bi-key', '自分のパスワード') +
      '<button class="btn btn-light btn-sm" id="chgPwBtn"><i class="bi bi-key"></i> パスワードを変更</button>');
  const d = await api('/admin/admins');
  if (!isAlive(tok)) return;
  const list = document.getElementById('adminList');
  if (!list) return;
  list.innerHTML = d.admins.length ? d.admins.map((a) => `
    <div class="list-row">
      <div><strong>${esc(a.name || '')}</strong> <span class="text-secondary">${esc(a.admin_id)}</span>
        <div class="small text-secondary">作成 ${esc((a.created_at || '').replace('T', ' '))}</div></div>
      <button class="btn btn-sm btn-outline-danger" data-deladmin="${a.id}" data-name="${esc(a.admin_id)}"><i class="bi bi-trash"></i></button>
    </div>`).join('') : emptyState('bi-person-badge', '管理者がいません');
  list.querySelectorAll('[data-deladmin]').forEach((b) => b?.addEventListener('click', () =>
    openModal('<i class="bi bi-trash text-danger"></i> 管理者の削除',
      `<div class="text-center py-2">
         <div class="mb-2"><i class="bi bi-exclamation-triangle-fill text-danger" style="font-size:2.2rem"></i></div>
         <p class="mb-1"><strong>${esc(b.dataset.name)}</strong> を削除しますか？</p>
         <p class="small text-secondary mb-0">この管理者のセッションは即座に無効になります。</p>
       </div>`,
      async (w, close) => {
        try {
          await api(`/admin/admins/${b.dataset.deladmin}`, { method: 'DELETE' });
          close(); toast('削除しました', 'success'); renderAdminAccountsTab(body);
        } catch (e) { toast(e.message, 'error'); }
      }, { saveLabel: '削除する', btnClass: 'btn-danger' })));

  document.getElementById('addAdminBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-plus-lg"></i> 管理者を追加',
      `<label class="form-label" for="naId">管理者ID <span class="text-danger">*</span></label>
       <input id="naId" class="form-control mb-2" placeholder="例: ops2" autocomplete="username">
       <label class="form-label" for="naName">氏名</label>
       <input id="naName" class="form-control mb-2" placeholder="例: 運営 花子">
       <label class="form-label" for="naPw">パスワード <span class="text-danger">*</span></label>
       <input id="naPw" type="password" class="form-control" autocomplete="new-password">
       <div class="pw-rules mt-2">
         <span class="pw-rule" data-rule="len"><i class="bi bi-circle"></i>8文字以上</span>
         <span class="pw-rule" data-rule="alpha"><i class="bi bi-circle"></i>英字を含む</span>
         <span class="pw-rule" data-rule="digit"><i class="bi bi-circle"></i>数字を含む</span>
       </div>
       <div class="form-error" id="naErr"></div>`,
      async (w, close) => {
        const err = w.querySelector('#naErr');
        try {
          await api('/admin/admins', { method: 'POST', body: JSON.stringify({
            admin_id: w.querySelector('#naId').value.trim(),
            name: w.querySelector('#naName').value.trim(),
            password: w.querySelector('#naPw').value }) });
          close(); toast('追加しました', 'success'); renderAdminAccountsTab(body);
        } catch (e) { if (err) err.textContent = e.message; }
      }, { saveLabel: '追加する' }));

  document.getElementById('chgPwBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-key"></i> パスワード変更',
      `<label class="form-label" for="cpCur">現在のパスワード</label>
       <input id="cpCur" type="password" class="form-control mb-2" autocomplete="current-password">
       <label class="form-label" for="cpNew">新しいパスワード</label>
       <input id="cpNew" type="password" class="form-control" autocomplete="new-password">
       <div class="pw-rules mt-2">
         <span class="pw-rule" data-rule="len"><i class="bi bi-circle"></i>8文字以上</span>
         <span class="pw-rule" data-rule="alpha"><i class="bi bi-circle"></i>英字を含む</span>
         <span class="pw-rule" data-rule="digit"><i class="bi bi-circle"></i>数字を含む</span>
       </div>
       <p class="small text-secondary mt-2 mb-0">変更すると、他の端末のログインは無効になります。</p>
       <div class="form-error" id="cpErr"></div>`,
      async (w, close) => {
        const err = w.querySelector('#cpErr');
        try {
          await api('/admin/password', { method: 'PUT', body: JSON.stringify({
            current_password: w.querySelector('#cpCur').value,
            new_password: w.querySelector('#cpNew').value }) });
          close(); toast('パスワードを変更しました', 'success');
        } catch (e) { if (err) err.textContent = e.message; }
      }, { saveLabel: '変更する' }));
}

async function renderMigrationsTab(body) {
  const tok = navToken();
  body.innerHTML = card(sectionTitle('bi-database-gear', 'マイグレーション') +
    '<div id="migSummary" class="mb-2"></div><div id="migList"></div>' +
    '<button class="btn btn-primary btn-sm mt-3" id="migApplyBtn"><i class="bi bi-play-fill"></i> 未適用を適用</button>');
  const d = await api('/admin/migrations');
  if (!isAlive(tok)) return;
  const summary = document.getElementById('migSummary');
  if (!summary) return;
  summary.innerHTML = d.pending
    ? badge(`未適用 ${d.pending} 件`, 'warning')
    : badge('すべて適用済み', 'success');
  document.getElementById('migList').innerHTML =
    `<div class="table-wrap"><table class="data-table"><thead><tr><th>状態</th><th>ファイル</th><th class="t-num">#</th><th>SQL</th></tr></thead><tbody>` +
    d.migrations.map((m) => `<tr>
      <td>${badge(m.applied ? '適用済み' : '未適用', m.applied ? 'success' : 'warning')}</td>
      <td class="small">${esc(m.filename)}</td>
      <td class="t-num num">${m.stmt_index}</td>
      <td class="small">${esc((m.sql || '').slice(0, 80))}</td>
    </tr>`).join('') + '</tbody></table></div>';

  document.getElementById('migApplyBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-database-gear"></i> マイグレーションの適用',
      `<p class="mb-1">未適用のステートメントを順に実行します。</p>
       <p class="small text-secondary mb-0">失敗した時点で中断します。成功した分は記録されるため、再実行すれば失敗箇所から再開します。</p>`,
      async (w, close) => {
        try {
          const r = await api('/admin/migrations/apply', { method: 'POST' });
          close();
          if (r.failed) {
            toast(`${r.applied.length}件適用後、${r.failed.filename}#${r.failed.stmt_index} で失敗: ${r.failed.error}`, 'error');
          } else {
            toast(`${r.applied.length}件を適用しました`, 'success');
          }
          renderMigrationsTab(body);
        } catch (e) { toast(e.message, 'error'); }
      }, { saveLabel: '適用する' }));
}

async function renderDiagnosticTab(body) {
  const tok = navToken();
  body.innerHTML = card(sectionTitle('bi-clipboard-pulse', 'DB診断') + '<div id="diagBody">読み込み中…</div>');
  const d = await api('/admin/debug/db-schema');
  if (!isAlive(tok)) return;
  const target = document.getElementById('diagBody');
  if (!target) return;
  target.innerHTML =
    `<div class="mb-2">${badge(d.student_supported ? 'student ロール対応済み' : 'student ロール未対応', d.student_supported ? 'success' : 'warning')}
      ${badge(d.shop_holidays_exists ? 'shop_holidays あり' : 'shop_holidays なし', d.shop_holidays_exists ? 'success' : 'warning')}</div>
     <details><summary class="small text-secondary">技術詳細</summary><pre class="small" style="white-space:pre-wrap">${esc(JSON.stringify(d, null, 2))}</pre></details>`;
}
```

`/admin/debug/db-schema` のレスポンスキー（`student_supported` / `shop_holidays_exists`）は実装によって名前が違う可能性がある。実装前に `src/admin_api.py` の `admin_debug_db_schema` を読み、実際のキー名に合わせること。

- [ ] **Step 6: 旧DBメンテナンスUIを削除**

`public/admin.js` から `openDbMaintenanceModal` の定義と、`SCREENS.adminHome` にある「データベース状態確認・更新」ボタンおよびそのハンドラを削除する。機能は `adminSystem` に移った。

- [ ] **Step 7: テストと構文チェック**

Run: `.venv/bin/python -m pytest tests/test_admin_console.py -v && node --check public/admin.js && node --check public/app.js`
Expected: すべて成功

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 8: ブラウザで動作確認**

管理者でログイン →「システム」タブ → 3タブがすべて表示され、管理者の追加・削除・パスワード変更、マイグレーション一覧の表示と適用ができること。

- [ ] **Step 9: コミット**

```bash
git add src/admin_api.py public/admin.js public/app.js tests/test_admin_console.py
git commit -m "feat(admin): システム画面を追加（管理者アカウント・マイグレーション・DB診断）

ナビに4項目目「システム」を追加。管理者アカウントの一覧/追加/削除と
自分のパスワード変更、マイグレーションの適用状態表示と適用、DB診断を集約した。
旧「データベース状態確認・更新」モーダルはこの画面に統合して削除。"
```

---

### Task 9: 店舗のアーカイブ・復元・設定編集 API

**Files:**
- Modify: `src/admin_api.py`
- Modify: `src/app.py`（ログイン時の検索条件に `is_archived=0` を追加）
- Test: `tests/test_admin_shop_lifecycle.py`（新規）

**Interfaces:**
- Produces:
  - `POST /api/admin/shops/<int:sid>/archive` → `{"ok": True}`
  - `POST /api/admin/shops/<int:sid>/unarchive` → `{"ok": True}`
  - `PUT /api/admin/shops/<int:sid>/settings` body = settings の部分 dict → `{"ok": True, "settings": {...}}`
  - `GET /api/admin/shops` に `?include_archived=1` を追加（既定はアーカイブ済みを除外）

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_admin_shop_lifecycle.py`:

```python
"""店舗のアーカイブ・復元・設定編集。"""
import json

import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


class TestArchive:
    def test_archive_hides_from_default_list(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        insert_shop("SHOP2", name="店2")

        assert client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t)).status_code == 200

        codes = [s["shop_code"] for s in client.get("/api/admin/shops", headers=_hdr(t)).get_json()["shops"]]
        assert "SHOP1" not in codes
        assert "SHOP2" in codes

        codes = [s["shop_code"] for s in client.get("/api/admin/shops?include_archived=1",
                                                    headers=_hdr(t)).get_json()["shops"]]
        assert "SHOP1" in codes

    def test_archive_sets_flags_and_deactivates(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        row = dbmod.query_one("SELECT is_archived, archived_at, is_active FROM shops WHERE id=?", (sid,))
        assert row["is_archived"] == 1
        assert row["archived_at"]
        assert row["is_active"] == 0

    def test_archive_revokes_sessions(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", "pw12345678", name="店1")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        shop_token = r.get_json()["token"]
        assert client.get("/api/me", headers=_hdr(shop_token)).status_code == 200

        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert client.get("/api/me", headers=_hdr(shop_token)).status_code == 401

    def test_archived_shop_cannot_login(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", "pw12345678", name="店1")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        assert r.status_code == 400

    def test_unarchive_restores(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert client.post(f"/api/admin/shops/{sid}/unarchive", headers=_hdr(t)).status_code == 200
        row = dbmod.query_one("SELECT is_archived, archived_at, is_active FROM shops WHERE id=?", (sid,))
        assert row["is_archived"] == 0
        assert row["archived_at"] is None
        # 復元しても有効化はしない（明示的に有効化させる）
        assert row["is_active"] == 0

    def test_archive_is_audited(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert dbmod.query_one("SELECT id FROM audit_logs WHERE action='shop.archive'") is not None


class TestShopSettings:
    def test_update_settings_merges(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1", settings={"default_hourly_wage": 1100,
                                                          "max_daily_hours": 8})
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=_hdr(t),
                       json={"default_hourly_wage": 1200})
        assert r.status_code == 200
        s = json.loads(dbmod.query_one("SELECT settings FROM shops WHERE id=?", (sid,))["settings"])
        assert s["default_hourly_wage"] == 1200
        assert s["max_daily_hours"] == 8, "既存キーが消えている"

    def test_unknown_key_is_rejected(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=_hdr(t),
                       json={"evil_key": 1})
        assert r.status_code == 400

    def test_unknown_shop_returns_404(self, client):
        t = _admin_token(client)
        r = client.put("/api/admin/shops/99999/settings", headers=_hdr(t),
                       json={"default_hourly_wage": 1200})
        assert r.status_code == 404
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_shop_lifecycle.py -v`
Expected: 全件 FAIL

- [ ] **Step 3: `admin_shops` にアーカイブ除外を追加**

`src/admin_api.py` の `admin_shops` を修正する。

```python
    @app.get("/api/admin/shops")
    def admin_shops():
        require_auth(["admin"])
        # アーカイブ済みは既定で隠す。運営が普段見るのは稼働中の店舗だけのため。
        if request.args.get("include_archived") == "1":
            rows = query_all("SELECT * FROM shops ORDER BY is_archived, id")
        else:
            rows = query_all("SELECT * FROM shops WHERE COALESCE(is_archived,0)=0 ORDER BY id")
        return jsonify({"shops": [strip_password(r) for r in rows]})
```

`COALESCE` を使うのは、`ALTER TABLE ADD COLUMN` 以前からある行の `is_archived` が NULL になり得るため。既存実装が `strip_password` を通していない場合はその形に合わせる（移設前の挙動を変えないこと）。

- [ ] **Step 4: アーカイブ・復元・設定編集を実装**

`src/admin_api.py` の `register_admin_routes` の中に追加する。

```python
    # shops.settings で受け付けるキー（src/utils.py の parse_settings 利用箇所と対応）。
    # 未知のキーを弾くのは、タイプミスが黙って保存されてシフト生成に効かない事故を防ぐため。
    _SETTINGS_KEYS = {
        "business_hours", "default_hourly_wage", "max_consecutive_days", "max_daily_hours",
        "max_employee_daily_hours", "min_daily_hours", "night_premium_rate",
        "period_mode", "shift_hours", "transport_per_day",
    }

    @app.post("/api/admin/shops/<int:sid>/archive")
    def admin_shop_archive(sid):
        require_auth(["admin"])
        shop = query_one("SELECT id, shop_code FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        now = jst_now().strftime("%Y-%m-%d %H:%M:%S")
        execute("UPDATE shops SET is_archived=1, archived_at=?, is_active=0 WHERE id=?", (now, sid))
        # ログイン中のユーザーを追い出す（アーカイブ後もセッションが生きていると
        # 停止したはずの店舗が操作できてしまう）
        execute("DELETE FROM sessions WHERE shop_id=?", (sid,))
        audit("shop.archive", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={shop['shop_code']}")
        return jsonify({"ok": True})

    @app.post("/api/admin/shops/<int:sid>/unarchive")
    def admin_shop_unarchive(sid):
        require_auth(["admin"])
        shop = query_one("SELECT id, shop_code FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        # is_active は 0 のまま。復元と再稼働は別の判断なので明示的に有効化させる。
        execute("UPDATE shops SET is_archived=0, archived_at=NULL WHERE id=?", (sid,))
        audit("shop.unarchive", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={shop['shop_code']}")
        return jsonify({"ok": True})

    @app.put("/api/admin/shops/<int:sid>/settings")
    def admin_shop_update_settings(sid):
        require_auth(["admin"])
        shop = query_one("SELECT id, settings FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        body = request.get_json(silent=True) or {}
        unknown = set(body.keys()) - _SETTINGS_KEYS
        if unknown:
            raise ValueError(f"未知の設定キーです: {', '.join(sorted(unknown))}")
        merged = parse_settings(shop.get("settings"))
        merged.update(body)
        import json as _json
        execute("UPDATE shops SET settings=? WHERE id=?",
                (_json.dumps(merged, ensure_ascii=False), sid))
        audit("shop.update", target_type="shop", target_id=sid, shop_id=sid,
              detail="settings:" + ",".join(sorted(body.keys())))
        return jsonify({"ok": True, "settings": merged})
```

`import json as _json` はファイル冒頭の import に移してよい。

- [ ] **Step 5: ログイン時にアーカイブ済みを除外**

`src/app.py` の `login()` にある2つのクエリを修正する。

staff / manager の検索:

```python
    staff = query_one(
        "SELECT s.* FROM staffs s JOIN shops sh ON s.shop_id=sh.id "
        "WHERE sh.shop_code=? AND s.staff_code=? AND s.is_resigned=0 AND sh.is_active=1 "
        "AND COALESCE(sh.is_archived,0)=0",
        (shop_code, user_code))
```

旧店主ログイン:

```python
        shop = query_one("SELECT * FROM shops WHERE shop_code=? AND is_active=1 "
                         "AND COALESCE(is_archived,0)=0", (shop_code,))
```

- [ ] **Step 6: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_admin_shop_lifecycle.py -v`
Expected: 全件 PASS

- [ ] **Step 7: 全テスト**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 8: コミット**

```bash
git add src/admin_api.py src/app.py tests/test_admin_shop_lifecycle.py
git commit -m "feat(admin): 店舗のアーカイブ・復元・設定編集APIを追加

アーカイブは一覧から隠し、is_active=0 にしてセッションを全削除する。
ログイン時の検索条件からも除外する。settings は未知のキーを拒否して
タイプミスが黙って保存される事故を防ぐ。"
```

---

### Task 10: 店舗のエクスポートと完全削除 API

**Files:**
- Modify: `src/admin_api.py`
- Test: `tests/test_admin_shop_delete.py`（新規）

**Interfaces:**
- Produces:
  - `GET /api/admin/shops/<int:sid>/export` → JSON ファイル（`Content-Disposition: attachment`）
  - `DELETE /api/admin/shops/<int:sid>` body `{confirm_code: str}` → `{"ok": True, "deleted": [テーブル名...]}`

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_admin_shop_delete.py`:

```python
"""店舗のエクスポートと完全削除。"""
import json

import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff, insert_pattern


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _shop_with_data():
    sid = insert_shop("SHOP1", "pw12345678", name="レイクタウン店")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    insert_staff(sid, "p1", "太郎")
    insert_pattern(sid, "早番", "09:00", "17:00", 2)
    return sid


class TestExport:
    def test_export_contains_shop_data(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        r = client.get(f"/api/admin/shops/{sid}/export", headers=_hdr(t))
        assert r.status_code == 200
        assert "attachment" in r.headers.get("Content-Disposition", "")
        data = json.loads(r.get_data(as_text=True))
        assert data["shop"]["shop_code"] == "SHOP1"
        assert len(data["staffs"]) == 2
        assert len(data["shift_patterns"]) == 1

    def test_export_excludes_password_hash(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        r = client.get(f"/api/admin/shops/{sid}/export", headers=_hdr(t))
        raw = r.get_data(as_text=True)
        assert "password_hash" not in raw, "パスワードハッシュがエクスポートに含まれている"

    def test_export_is_audited(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        client.get(f"/api/admin/shops/{sid}/export", headers=_hdr(t))
        assert dbmod.query_one("SELECT id FROM audit_logs WHERE action='shop.export'") is not None

    def test_unknown_shop_returns_404(self, client):
        t = _admin_token(client)
        assert client.get("/api/admin/shops/99999/export", headers=_hdr(t)).status_code == 404


class TestDelete:
    def test_delete_requires_archived(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 400, "アーカイブ前に削除できてしまう"
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is not None

    def test_delete_requires_matching_confirm_code(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "WRONG"})
        assert r.status_code == 400
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is not None

    def test_delete_removes_all_dependent_rows(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is None
        assert dbmod.query_one("SELECT id FROM staffs WHERE shop_id=?", (sid,)) is None
        assert dbmod.query_one("SELECT id FROM shift_patterns WHERE shop_id=?", (sid,)) is None
        assert dbmod.query_one("SELECT token FROM sessions WHERE shop_id=?", (sid,)) is None

    def test_delete_keeps_audit_logs(self, client):
        """監査ログは運営の記録なので消さないこと。"""
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t), json={"confirm_code": "SHOP1"})
        rows = dbmod.query_all("SELECT id FROM audit_logs WHERE shop_id=?", (sid,))
        assert rows, "監査ログまで消えている"
        row = dbmod.query_one("SELECT detail FROM audit_logs WHERE action='shop.delete'")
        assert row is not None
        assert "SHOP1" in (row["detail"] or ""), "店舗コードが記録に残っていない"

    def test_delete_does_not_touch_other_shops(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        other = insert_shop("SHOP2", name="店2")
        insert_staff(other, "p9", "別店の人")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t), json={"confirm_code": "SHOP1"})
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (other,)) is not None
        assert dbmod.query_one("SELECT id FROM staffs WHERE shop_id=?", (other,)) is not None

    def test_delete_is_idempotent_on_retry(self, client):
        """再実行しても壊れないこと（execute が毎回commitしロールバックできないため）。"""
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                             json={"confirm_code": "SHOP1"}).status_code == 200
        # 店舗が消えたので2回目は404
        assert client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                             json={"confirm_code": "SHOP1"}).status_code == 404
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_shop_delete.py -v`
Expected: 全件 FAIL

- [ ] **Step 3: 実装する**

`src/admin_api.py` の `register_admin_routes` の中に追加する。`Response` を `from flask import ...` に追加すること。

```python
    # 店舗に紐づくテーブル。削除は FK の依存順（子→親）に実行する。
    # fixed_shifts は shop_id を持たず staff_id 経由でしか辿れないため別扱い。
    _SHOP_SCOPED_TABLES = [
        "sessions", "notifications", "change_requests", "wish_history", "shifts",
        "shift_pattern_weekday_required", "shift_patterns", "shift_request_periods",
        "shop_holidays",
    ]

    def _collect_shop_data(sid):
        """店舗に属する全行を dict で集める。password_hash は含めない。"""
        shop = query_one("SELECT * FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        data = {"shop": strip_password(shop), "exported_at": jst_now().strftime("%Y-%m-%d %H:%M:%S")}
        data["staffs"] = [strip_password(r) for r in
                          query_all("SELECT * FROM staffs WHERE shop_id=?", (sid,))]
        for table in _SHOP_SCOPED_TABLES:
            if table == "sessions":
                continue  # セッションは機微情報で復元価値も無い
            data[table] = query_all(f"SELECT * FROM {table} WHERE shop_id=?", (sid,))
        data["fixed_shifts"] = query_all(
            "SELECT fs.* FROM fixed_shifts fs JOIN staffs s ON fs.staff_id=s.id WHERE s.shop_id=?",
            (sid,))
        return data

    @app.get("/api/admin/shops/<int:sid>/export")
    def admin_shop_export(sid):
        require_auth(["admin"])
        data = _collect_shop_data(sid)
        code = data["shop"].get("shop_code") or str(sid)
        audit("shop.export", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={code}")
        import json as _json
        body = _json.dumps(data, ensure_ascii=False, indent=2)
        filename = f"shop-{code}-{jst_now().strftime('%Y%m%d')}.json"
        resp = Response(body, content_type="application/json; charset=utf-8")
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    @app.delete("/api/admin/shops/<int:sid>")
    def admin_shop_delete(sid):
        require_auth(["admin"])
        shop = query_one("SELECT id, shop_code, is_archived FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        if not shop.get("is_archived"):
            raise ValueError("先にアーカイブしてください（誤削除を防ぐため）")
        body = request.get_json(silent=True) or {}
        if (body.get("confirm_code") or "").strip() != shop["shop_code"]:
            raise ValueError("店舗コードが一致しません")

        # NOTE: execute() は毎回 commit するためトランザクションを張れない
        # (src/db.py:138)。途中で失敗しても、どこまで消したかを返して再実行できる
        # ようにする。既に消えたテーブルへの DELETE は0件で成功するため冪等。
        deleted = []
        # fixed_shifts は shop_id を持たないので staff_id 経由で先に消す
        execute("DELETE FROM fixed_shifts WHERE staff_id IN (SELECT id FROM staffs WHERE shop_id=?)",
                (sid,))
        deleted.append("fixed_shifts")
        for table in _SHOP_SCOPED_TABLES:
            execute(f"DELETE FROM {table} WHERE shop_id=?", (sid,))
            deleted.append(table)
        execute("DELETE FROM staffs WHERE shop_id=?", (sid,))
        deleted.append("staffs")
        execute("DELETE FROM shops WHERE id=?", (sid,))
        deleted.append("shops")

        # audit_logs は消さない。運営の記録であり、店舗が消えた事実こそ残す必要がある。
        audit("shop.delete", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={shop['shop_code']} を完全削除")
        return jsonify({"ok": True, "deleted": deleted})
```

- [ ] **Step 4: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_admin_shop_delete.py -v`
Expected: 全件 PASS

`test_delete_removes_all_dependent_rows` が FK 制約エラーで落ちる場合、`_SHOP_SCOPED_TABLES` の順序が依存関係に合っていない。`schema.sql` の FK 定義を確認して並べ替える。

- [ ] **Step 5: 全テスト**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 6: コミット**

```bash
git add src/admin_api.py tests/test_admin_shop_delete.py
git commit -m "feat(admin): 店舗のエクスポートと完全削除を追加

削除はアーカイブ済みかつ店舗コードの一致を条件にする。トランザクションが
張れないため、どこまで消したかを返して再実行できるようにした。
監査ログは運営の記録なので消さず、detail に店舗コードを残す。"
```

---

### Task 11: 店舗詳細のタブ化と危険な操作UI

**Files:**
- Modify: `public/admin.js`（`SCREENS.adminShopDetail` を4タブに再構成）

**Interfaces:**
- Consumes: Task 9/10 の API、Task 6/7 の代理閲覧

- [ ] **Step 1: タブの骨格を実装**

`public/admin.js` の `SCREENS.adminShopDetail` を書き換える。タブの実装は `SCREENS.settings`（`public/app.js:3679-3693`）の既存パターンに従う。

```js
let adminShopTab = 'overview';

SCREENS.adminShopDetail = async function (el) {
  const tok = navToken();
  const sid = window._adminShopId;
  const shops = await api('/admin/shops?include_archived=1');
  if (!isAlive(tok)) return;
  const shop = (shops.shops || []).find((s) => s.id === sid);
  if (!shop) { el.innerHTML = emptyState('bi-shop', '店舗が見つかりません'); return; }

  el.innerHTML = pageHead(shop.shop_name || '(名称未設定)', 'bi-shop', shop.shop_code) +
    `<div class="tabs no-print">
       <button class="tab ${adminShopTab === 'overview' ? 'active' : ''}" data-tab="overview">概要</button>
       <button class="tab ${adminShopTab === 'staffs' ? 'active' : ''}" data-tab="staffs">スタッフ</button>
       <button class="tab ${adminShopTab === 'settings' ? 'active' : ''}" data-tab="settings">設定</button>
       <button class="tab ${adminShopTab === 'danger' ? 'active' : ''}" data-tab="danger">危険な操作</button>
     </div><div id="shopTabBody"></div>`;
  el.querySelectorAll('.tab').forEach((t) => t?.addEventListener('click', () => {
    adminShopTab = t.dataset.tab;
    el.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === adminShopTab));
    renderShopTab(document.getElementById('shopTabBody'), shop);
  }));
  renderShopTab(document.getElementById('shopTabBody'), shop);
};

function renderShopTab(body, shop) {
  ({ overview: renderShopOverviewTab, staffs: renderShopStaffsTab,
     settings: renderShopSettingsTab, danger: renderShopDangerTab }[adminShopTab])(body, shop);
}
```

- [ ] **Step 2: 概要タブを実装**

既存の期間集計ロジック（旧 `SCREENS.adminShopDetail` の期間指定と集計テーブル）を `renderShopOverviewTab(body, shop)` にそのまま移し、先頭に代理閲覧ボタンを追加する（Task 7 Step 5 のハンドラをここに置く）。

```js
async function renderShopOverviewTab(body, shop) {
  body.innerHTML =
    card(`<div class="flex gap-2 flex-wrap">
      <button class="btn btn-sm btn-light" id="impersonateBtn"><i class="bi bi-eye"></i> この店舗を代理閲覧</button>
      ${shop.is_archived ? badge('アーカイブ済み', 'warning') : badge(shop.is_active ? '稼働中' : '停止中', shop.is_active ? 'success' : 'warning')}
    </div>`) +
    card(sectionTitle('bi-calendar-range', '期間集計') +
      `<div class="row mb-3">
         <div class="col-5"><label class="form-label" for="sumStart">開始</label><input type="date" id="sumStart" class="form-control"></div>
         <div class="col-5"><label class="form-label" for="sumEnd">終了</label><input type="date" id="sumEnd" class="form-control"></div>
         <div class="col-2 flex items-end"><button class="btn btn-primary w-full" id="sumLoadBtn">表示</button></div>
       </div><div id="sumBody"><div class="text-secondary small">「表示」ボタンを押してください</div></div>`);
  // 既存実装の「次回募集期間を初期値にする」処理と集計テーブル描画をここへ移す
  // （旧 SCREENS.adminShopDetail の /admin/shops/<id>/periods/next と
  //   /admin/shops/summary/<id> の呼び出しをそのまま使う）
}
```

移設元の正確なコードは `git show HEAD~N:public/app.js` ではなく、現在の `public/admin.js` にある旧 `SCREENS.adminShopDetail` から切り出すこと。

- [ ] **Step 3: スタッフタブを実装**

旧 `SCREENS.adminShopDetail` のスタッフ一覧部分（検索ボックス + 各行の編集/ロール変更/PWリセット、および「スタッフ追加」「旧仕様から manager 昇格」ボタン）を `renderShopStaffsTab(body, shop)` にそのまま移す。**ロジックは変えない。**

- [ ] **Step 4: 設定タブを実装**

```js
async function renderShopSettingsTab(body, shop) {
  const s = shop.settings ? (typeof shop.settings === 'string' ? JSON.parse(shop.settings || '{}') : shop.settings) : {};
  const num = (v) => (v === undefined || v === null ? '' : v);
  body.innerHTML =
    card(sectionTitle('bi-shop', '店舗情報') +
      `<label class="form-label" for="stName">店舗名</label>
       <input id="stName" class="form-control mb-2" value="${esc(shop.shop_name || '')}">
       <label class="form-label" for="stCode">店舗コード</label>
       <input id="stCode" class="form-control mb-2" value="${esc(shop.shop_code || '')}">
       <div class="form-error" id="stErr"></div>
       <button class="btn btn-primary btn-sm mt-2" id="stSaveBtn">保存</button>`) +
    card(sectionTitle('bi-sliders', 'シフト設定') +
      `<div class="row">
         <div class="col-6"><label class="form-label" for="cfWage">既定時給</label><input type="number" id="cfWage" class="form-control mb-2" value="${num(s.default_hourly_wage)}"></div>
         <div class="col-6"><label class="form-label" for="cfMaxDaily">1日の上限時間</label><input type="number" id="cfMaxDaily" class="form-control mb-2" value="${num(s.max_daily_hours)}"></div>
         <div class="col-6"><label class="form-label" for="cfMinDaily">1日の下限時間</label><input type="number" id="cfMinDaily" class="form-control mb-2" value="${num(s.min_daily_hours)}"></div>
         <div class="col-6"><label class="form-label" for="cfEmpDaily">社員の1日上限</label><input type="number" id="cfEmpDaily" class="form-control mb-2" value="${num(s.max_employee_daily_hours)}"></div>
         <div class="col-6"><label class="form-label" for="cfConsec">連勤上限（日）</label><input type="number" id="cfConsec" class="form-control mb-2" value="${num(s.max_consecutive_days)}"></div>
         <div class="col-6"><label class="form-label" for="cfTransport">1日あたり交通費</label><input type="number" id="cfTransport" class="form-control mb-2" value="${num(s.transport_per_day)}"></div>
       </div>
       <div class="form-error" id="cfErr"></div>
       <button class="btn btn-primary btn-sm mt-2" id="cfSaveBtn">保存</button>`);

  document.getElementById('stSaveBtn')?.addEventListener('click', async () => {
    const err = document.getElementById('stErr');
    if (err) err.textContent = '';
    try {
      await api(`/admin/shops/${shop.id}`, { method: 'PUT', body: JSON.stringify({
        shop_name: document.getElementById('stName').value.trim(),
        shop_code: document.getElementById('stCode').value.trim() }) });
      toast('保存しました', 'success');
      navigateTo('adminShopDetail');
    } catch (e) { if (err) err.textContent = e.message; }
  });

  document.getElementById('cfSaveBtn')?.addEventListener('click', async () => {
    const err = document.getElementById('cfErr');
    if (err) err.textContent = '';
    const pick = (id) => {
      const v = document.getElementById(id).value.trim();
      return v === '' ? undefined : Number(v);
    };
    const payload = {};
    const map = { cfWage: 'default_hourly_wage', cfMaxDaily: 'max_daily_hours',
                  cfMinDaily: 'min_daily_hours', cfEmpDaily: 'max_employee_daily_hours',
                  cfConsec: 'max_consecutive_days', cfTransport: 'transport_per_day' };
    Object.keys(map).forEach((id) => { const v = pick(id); if (v !== undefined) payload[map[id]] = v; });
    try {
      await api(`/admin/shops/${shop.id}/settings`, { method: 'PUT', body: JSON.stringify(payload) });
      toast('保存しました', 'success');
    } catch (e) { if (err) err.textContent = e.message; }
  });
}
```

- [ ] **Step 5: 危険な操作タブを実装**

エクスポートを1回押すまで削除ボタンを無効にする。

```js
function renderShopDangerTab(body, shop) {
  body.innerHTML = card(sectionTitle('bi-exclamation-triangle', '危険な操作') +
    `<div class="list-row">
       <div><strong>${shop.is_archived ? 'アーカイブを解除' : 'アーカイブ'}</strong>
         <div class="small text-secondary">${shop.is_archived
           ? '一覧に再表示します。稼働させるには別途「有効化」が必要です。'
           : '一覧から隠し、ログインを停止します。データは残ります。'}</div></div>
       <button class="btn btn-sm btn-light" id="archBtn">${shop.is_archived ? '解除' : 'アーカイブ'}</button>
     </div>
     <div class="list-row">
       <div><strong>データのエクスポート</strong>
         <div class="small text-secondary">この店舗の全データを JSON でダウンロードします。</div></div>
       <button class="btn btn-sm btn-light" id="expBtn"><i class="bi bi-download"></i> ダウンロード</button>
     </div>
     <div class="list-row">
       <div><strong>完全削除</strong>
         <div class="small text-secondary">${shop.is_archived
           ? 'アーカイブ済みです。エクスポートしてから削除できます。'
           : '先にアーカイブしてください。'}</div></div>
       <button class="btn btn-sm btn-outline-danger" id="delBtn" disabled><i class="bi bi-trash"></i> 完全削除</button>
     </div>`);

  document.getElementById('archBtn')?.addEventListener('click', async () => {
    const path = shop.is_archived ? 'unarchive' : 'archive';
    try {
      await api(`/admin/shops/${shop.id}/${path}`, { method: 'POST' });
      toast(shop.is_archived ? 'アーカイブを解除しました' : 'アーカイブしました', 'success');
      navigateTo('adminShopDetail');
    } catch (e) { toast(e.message, 'error'); }
  });

  document.getElementById('expBtn')?.addEventListener('click', async () => {
    try {
      // api() は JSON を返す前提なので、ファイル取得は fetch を直に使う
      const res = await fetch(`/api/admin/shops/${shop.id}/export`, {
        headers: { Authorization: 'Bearer ' + localStorage.getItem('shift_token') } });
      if (!res.ok) throw new Error('エクスポートに失敗しました');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `shop-${shop.shop_code}.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast('エクスポートしました', 'success');
      // エクスポート済みのときだけ削除を許可する（取り返しのつかない操作の前に
      // 必ずバックアップを取らせる）
      const del = document.getElementById('delBtn');
      if (del && shop.is_archived) del.disabled = false;
    } catch (e) { toast(e.message, 'error'); }
  });

  document.getElementById('delBtn')?.addEventListener('click', () =>
    openModal('<i class="bi bi-trash text-danger"></i> 店舗の完全削除',
      `<div class="text-center py-2">
         <div class="mb-2"><i class="bi bi-exclamation-triangle-fill text-danger" style="font-size:2.2rem"></i></div>
         <p class="mb-1"><strong>${esc(shop.shop_name || '')}</strong> と、その全スタッフ・シフト・希望を削除します。</p>
         <p class="small text-secondary">この操作は取り消せません。監査ログのみ記録として残ります。</p>
       </div>
       <label class="form-label" for="delCode">確認のため店舗コード <strong>${esc(shop.shop_code)}</strong> を入力してください</label>
       <input id="delCode" class="form-control" autocomplete="off">
       <div class="form-error" id="delErr"></div>`,
      async (w, close) => {
        const err = w.querySelector('#delErr');
        try {
          await api(`/admin/shops/${shop.id}`, { method: 'DELETE',
            body: JSON.stringify({ confirm_code: w.querySelector('#delCode').value.trim() }) });
          close(); toast('削除しました', 'success'); navigateTo('adminShops');
        } catch (e) { if (err) err.textContent = e.message; }
      }, { saveLabel: '完全に削除する', btnClass: 'btn-danger' }));
}
```

- [ ] **Step 6: 店舗一覧にアーカイブ表示トグルを追加**

`SCREENS.adminShops` に追加する。

```js
let adminShowArchived = false;
```

一覧のヘッダに切替ボタンを置き、`load()` の中で `api('/admin/shops' + (adminShowArchived ? '?include_archived=1' : ''))` を呼ぶ。行にはアーカイブ済みバッジを出す。

- [ ] **Step 7: 構文チェックとテスト**

Run: `node --check public/admin.js && .venv/bin/python -m pytest tests/ -q`
Expected: すべて成功

- [ ] **Step 8: ブラウザで動作確認**

店舗詳細の4タブが切り替わること。設定タブで店舗名を変更して保存できること。危険な操作タブでアーカイブ→エクスポート→完全削除の順にしか進めないこと（エクスポート前は削除ボタンが disabled）。

- [ ] **Step 9: コミット**

```bash
git add public/admin.js
git commit -m "feat(ui): 店舗詳細をタブ化し、設定編集と危険な操作を追加

概要/スタッフ/設定/危険な操作の4タブに再構成。設定タブで店舗名・店舗コード・
シフト設定を編集できるようにした（トグルで店舗名が消えた際の復旧手段でもある）。
完全削除はエクスポートを1回押すまでボタンを無効にする。"
```

---

### Task 12: 全社ダッシュボード

管理者ホームがボタン2つだけで、SaaS運営に必要な指標が何も見えない。

**Files:**
- Modify: `src/admin_api.py`
- Modify: `public/admin.js`（`SCREENS.adminHome`）
- Test: `tests/test_admin_dashboard.py`（新規）

**Interfaces:**
- Produces: `GET /api/admin/dashboard` →
  ```
  {"kpi": {"shops_total", "shops_active", "shops_inactive", "shops_archived",
           "staffs_total", "confirmed_this_month", "attention_count"},
   "attention": [{"kind", "shop_id", "shop_name", "detail"}],
   "recent_audit": [{"created_at", "actor_name", "action", "detail"}]}
  ```
  `kind` は `"deadline_passed"` / `"no_manager"` / `"stale_login"` / `"pending_migration"` のいずれか

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_admin_dashboard.py`:

```python
"""全社ダッシュボード。"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


class TestDashboardKpi:
    def test_counts_shops_by_state(self, client):
        t = _admin_token(client)
        insert_shop("A", name="稼働")
        b = insert_shop("B", name="停止")
        c = insert_shop("C", name="アーカイブ")
        dbmod.execute("UPDATE shops SET is_active=0 WHERE id=?", (b,))
        client.post(f"/api/admin/shops/{c}/archive", headers=_hdr(t))

        k = client.get("/api/admin/dashboard", headers=_hdr(t)).get_json()["kpi"]
        assert k["shops_total"] == 3
        assert k["shops_active"] == 1
        assert k["shops_inactive"] == 1
        assert k["shops_archived"] == 1

    def test_counts_staffs(self, client):
        t = _admin_token(client)
        sid = insert_shop("A", name="店")
        insert_staff(sid, "p1", "太郎")
        insert_staff(sid, "p2", "花子")
        k = client.get("/api/admin/dashboard", headers=_hdr(t)).get_json()["kpi"]
        assert k["staffs_total"] == 2


class TestDashboardAttention:
    def test_flags_shop_without_manager(self, client):
        t = _admin_token(client)
        sid = insert_shop("A", name="管理者不在店")
        insert_staff(sid, "p1", "太郎", role="part_time")
        d = client.get("/api/admin/dashboard", headers=_hdr(t)).get_json()
        kinds = [a["kind"] for a in d["attention"]]
        assert "no_manager" in kinds
        item = next(a for a in d["attention"] if a["kind"] == "no_manager")
        assert item["shop_id"] == sid
        assert item["shop_name"] == "管理者不在店"

    def test_does_not_flag_shop_with_manager(self, client):
        t = _admin_token(client)
        sid = insert_shop("A", name="正常店")
        insert_staff(sid, "mgr", "店長", role="manager")
        d = client.get("/api/admin/dashboard", headers=_hdr(t)).get_json()
        no_mgr = [a for a in d["attention"] if a["kind"] == "no_manager"]
        assert no_mgr == []

    def test_archived_shops_are_not_flagged(self, client):
        t = _admin_token(client)
        sid = insert_shop("A", name="アーカイブ店")
        insert_staff(sid, "p1", "太郎", role="part_time")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        d = client.get("/api/admin/dashboard", headers=_hdr(t)).get_json()
        assert [a for a in d["attention"] if a["shop_id"] == sid] == []

    def test_attention_count_matches_list(self, client):
        t = _admin_token(client)
        sid = insert_shop("A", name="店")
        insert_staff(sid, "p1", "太郎", role="part_time")
        d = client.get("/api/admin/dashboard", headers=_hdr(t)).get_json()
        assert d["kpi"]["attention_count"] == len(d["attention"])


class TestDashboardAudit:
    def test_returns_recent_audit(self, client):
        t = _admin_token(client)
        insert_shop("A", name="店")
        d = client.get("/api/admin/dashboard", headers=_hdr(t)).get_json()
        assert isinstance(d["recent_audit"], list)
        assert len(d["recent_audit"]) <= 10
        # ログイン記録が入っているはず（Phase 1 で auth.login を記録済み）
        assert any(r["action"] == "auth.login" for r in d["recent_audit"])


def test_requires_admin_role(client):
    sid = insert_shop("SHOP1", "pw12345678")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                        "password": "pw12345678"})
    t = r.get_json()["token"]
    assert client.get("/api/admin/dashboard", headers=_hdr(t)).status_code == 403
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_dashboard.py -v`
Expected: 全件 FAIL

- [ ] **Step 3: 実装する**

`src/admin_api.py` の `register_admin_routes` の中に追加する。

```python
    @app.get("/api/admin/dashboard")
    def admin_dashboard():
        require_auth(["admin"])
        now = jst_now()
        today = now.strftime("%Y-%m-%d")
        month_start = now.strftime("%Y-%m-01")

        shops = query_all("SELECT id, shop_name, shop_code, is_active, "
                          "COALESCE(is_archived,0) AS is_archived FROM shops")
        active = [s for s in shops if not s["is_archived"] and s["is_active"]]
        inactive = [s for s in shops if not s["is_archived"] and not s["is_active"]]
        archived = [s for s in shops if s["is_archived"]]
        live_ids = {s["id"] for s in shops if not s["is_archived"]}

        staffs_total = query_one(
            "SELECT COUNT(*) AS c FROM staffs WHERE is_resigned=0")["c"]
        confirmed = query_one(
            "SELECT COUNT(*) AS c FROM shifts WHERE status='confirmed' AND start_datetime>=?",
            (month_start + "T00:00:00",))["c"]

        attention = []

        # 1. 締切を過ぎているのにシフトが未確定の店舗
        rows = query_all(
            "SELECT p.shop_id, p.start_date, p.end_date, p.deadline "
            "FROM shift_request_periods p WHERE p.is_active=1 AND p.deadline < ?", (today,))
        by_id = {s["id"]: s for s in shops}
        for p in rows:
            if p["shop_id"] not in live_ids:
                continue
            has = query_one(
                "SELECT 1 AS x FROM shifts WHERE shop_id=? AND status='confirmed' "
                "AND start_datetime>=? AND start_datetime<=? LIMIT 1",
                (p["shop_id"], p["start_date"] + "T00:00:00", p["end_date"] + "T23:59:59"))
            if not has:
                shop = by_id.get(p["shop_id"], {})
                attention.append({"kind": "deadline_passed", "shop_id": p["shop_id"],
                                  "shop_name": shop.get("shop_name"),
                                  "detail": f"締切 {p['deadline']} を過ぎていますが未確定です"})

        # 2. manager が1人もいない店舗（＝誰もログインできない）
        for s in shops:
            if s["is_archived"]:
                continue
            has_mgr = query_one(
                "SELECT 1 AS x FROM staffs WHERE shop_id=? AND role='manager' AND is_resigned=0 LIMIT 1",
                (s["id"],))
            if not has_mgr:
                attention.append({"kind": "no_manager", "shop_id": s["id"],
                                  "shop_name": s["shop_name"],
                                  "detail": "店舗管理者がいないため誰もログインできません"})

        # 3. 30日以上どのユーザーもログインしていない店舗
        for s in shops:
            if s["is_archived"]:
                continue
            last = query_one(
                "SELECT MAX(created_at) AS last FROM audit_logs "
                "WHERE shop_id=? AND action='auth.login'", (s["id"],))
            last_at = (last or {}).get("last")
            if not last_at:
                continue  # ログイン記録が一度も無い場合は判定しない（導入直後の誤検知を避ける）
            try:
                days = (now - datetime.strptime(last_at, "%Y-%m-%d %H:%M:%S")).days
            except (ValueError, TypeError):
                continue
            if days >= 30:
                attention.append({"kind": "stale_login", "shop_id": s["id"],
                                  "shop_name": s["shop_name"],
                                  "detail": f"{days}日間ログインがありません"})

        # 4. 未適用のマイグレーション
        pending = [m for m in migrator.status() if not m["applied"]]
        if pending:
            attention.append({"kind": "pending_migration", "shop_id": None, "shop_name": None,
                              "detail": f"未適用のマイグレーションが {len(pending)} 件あります"})

        recent = query_all(
            "SELECT created_at, actor_role, actor_name, action, detail FROM audit_logs "
            "ORDER BY id DESC LIMIT 10")

        return jsonify({
            "kpi": {"shops_total": len(shops), "shops_active": len(active),
                    "shops_inactive": len(inactive), "shops_archived": len(archived),
                    "staffs_total": staffs_total, "confirmed_this_month": confirmed,
                    "attention_count": len(attention)},
            "attention": attention,
            "recent_audit": recent,
        })
```

`datetime` を `src/admin_api.py` の import に追加する（`from datetime import datetime`）。

- [ ] **Step 4: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_admin_dashboard.py -v`
Expected: 全件 PASS

- [ ] **Step 5: `SCREENS.adminHome` を実装**

`public/admin.js` の `SCREENS.adminHome` を書き換える。

```js
const ATTENTION_LABELS = {
  deadline_passed: '締切超過・未確定',
  no_manager: '店舗管理者が不在',
  stale_login: '長期未ログイン',
  pending_migration: '未適用のマイグレーション',
};

SCREENS.adminHome = async function (el) {
  const tok = navToken();
  el.innerHTML = pageHead('ダッシュボード', 'bi-speedometer2') +
    '<div id="dashKpi" class="row mb-3"></div><div id="dashAttention"></div><div id="dashAudit"></div>';
  const d = await api('/admin/dashboard');
  if (!isAlive(tok)) return;
  const kpiEl = document.getElementById('dashKpi');
  if (!kpiEl) return;
  const k = d.kpi;
  kpiEl.innerHTML =
    `<div class="col-6 col-lg-3">${kpiCard('bi-shop', '稼働店舗', k.shops_active,
       `停止${k.shops_inactive} / アーカイブ${k.shops_archived}`)}</div>
     <div class="col-6 col-lg-3">${kpiCard('bi-people', '在籍スタッフ', k.staffs_total, '全店合計')}</div>
     <div class="col-6 col-lg-3">${kpiCard('bi-calendar-check', '今月の確定シフト', k.confirmed_this_month, '件')}</div>
     <div class="col-6 col-lg-3">${kpiCard('bi-exclamation-triangle', '要対応', k.attention_count,
       k.attention_count ? '確認してください' : '問題ありません',
       k.attention_count ? 'warning' : 'success')}</div>`;

  document.getElementById('dashAttention').innerHTML =
    card(sectionTitle('bi-exclamation-triangle', '要対応') +
      (d.attention.length ? d.attention.map((a) => `
        <div class="list-row" ${a.shop_id ? `style="cursor:pointer" data-attshop="${a.shop_id}"` : ''}>
          <div>${badge(ATTENTION_LABELS[a.kind] || a.kind, 'warning')}
            <strong class="ms-2">${esc(a.shop_name || '—')}</strong>
            <div class="small text-secondary">${esc(a.detail)}</div></div>
          ${a.shop_id ? '<i class="bi bi-chevron-right text-secondary"></i>' : ''}
        </div>`).join('') : emptyState('bi-check-circle', '対応が必要な項目はありません')));
  document.querySelectorAll('[data-attshop]').forEach((b) => b?.addEventListener('click', () => {
    window._adminShopId = +b.dataset.attshop;
    navigateTo('adminShopDetail');
  }));

  document.getElementById('dashAudit').innerHTML =
    card(sectionTitle('bi-clock-history', '最近の操作',
      '<button class="btn btn-sm btn-light" id="toAuditBtn">監査ログへ</button>') +
      (d.recent_audit.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>日時</th><th>操作者</th><th>操作</th><th>詳細</th></tr></thead><tbody>` +
        d.recent_audit.map((l) => `<tr>
          <td class="small">${esc((l.created_at || '').replace('T', ' '))}</td>
          <td class="small">${esc(l.actor_name || l.actor_role || '—')}</td>
          <td>${badge(auditActionLabel(l.action), 'info')}</td>
          <td class="small">${esc(l.detail || '')}</td></tr>`).join('') +
        '</tbody></table></div>' : emptyState('bi-clock-history', '操作履歴がありません')));
  document.getElementById('toAuditBtn')?.addEventListener('click', () => navigateTo('adminAudit'));
};
```

`kpiCard` の引数順（`icon, label, value, sub, variant`）は `public/app.js:227` の定義に従っている。実装前に定義を読んで確認すること。

- [ ] **Step 6: 構文チェックとテスト**

Run: `node --check public/admin.js && .venv/bin/python -m pytest tests/ -q`
Expected: すべて成功

- [ ] **Step 7: ブラウザで動作確認**

管理者でログインし、ホームに KPI 4枚・要対応リスト・最近の操作が表示されること。要対応の行をクリックすると該当店舗の詳細に飛ぶこと。

- [ ] **Step 8: コミット**

```bash
git add src/admin_api.py public/admin.js tests/test_admin_dashboard.py
git commit -m "feat(admin): 全社ダッシュボードを追加

ボタン2つだけだった管理者ホームを、KPI・要対応リスト・最近の操作の画面にした。
要対応は「締切超過で未確定」「店舗管理者が不在」「30日以上未ログイン」
「未適用のマイグレーション」の4種。定期ジョブ基盤が無いため、
締切リマインドの代わりに管理者が能動的に気づける形にしている。"
```

---

### Task 13: 監査ログの強化（フィルタ・ページング・CSV）

**Files:**
- Modify: `src/admin_api.py`（`admin_audit_logs` の拡張、CSV エンドポイント追加）
- Modify: `public/admin.js`（`SCREENS.adminAudit`）
- Test: `tests/test_admin_audit_filters.py`（新規）

**Interfaces:**
- Produces:
  - `GET /api/admin/audit-logs` にクエリ `shop` / `action` / `start` / `end` / `actor` / `before_id` / `limit` を追加 → `{"logs": [...], "has_more": bool}`
  - `GET /api/admin/audit-logs.csv` — 同じフィルタで CSV を返す

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_admin_audit_filters.py`:

```python
"""監査ログのフィルタ・ページング・CSV出力。"""
import db as dbmod
from helpers import insert_admin, insert_shop


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _log(action, created_at, actor_name="運営", shop_id=None, detail=""):
    dbmod.execute(
        "INSERT INTO audit_logs (actor_role, actor_name, action, shop_id, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("admin", actor_name, action, shop_id, detail, created_at))


class TestFilters:
    def test_filter_by_date_range(self, client):
        t = _admin_token(client)
        _log("shop.create", "2026-07-01 10:00:00")
        _log("shop.create", "2026-07-15 10:00:00")
        _log("shop.create", "2026-07-31 10:00:00")
        r = client.get("/api/admin/audit-logs?start=2026-07-10&end=2026-07-20", headers=_hdr(t))
        assert r.status_code == 200
        logs = [l for l in r.get_json()["logs"] if l["action"] == "shop.create"]
        assert len(logs) == 1
        assert logs[0]["created_at"].startswith("2026-07-15")

    def test_filter_by_actor_partial_match(self, client):
        t = _admin_token(client)
        _log("shop.create", "2026-07-15 10:00:00", actor_name="山田太郎")
        _log("shop.create", "2026-07-15 11:00:00", actor_name="鈴木花子")
        r = client.get("/api/admin/audit-logs?actor=山田", headers=_hdr(t))
        logs = [l for l in r.get_json()["logs"] if l["action"] == "shop.create"]
        assert len(logs) == 1
        assert logs[0]["actor_name"] == "山田太郎"

    def test_filter_by_action(self, client):
        t = _admin_token(client)
        _log("shop.create", "2026-07-15 10:00:00")
        _log("shop.update", "2026-07-15 11:00:00")
        r = client.get("/api/admin/audit-logs?action=shop.update", headers=_hdr(t))
        assert all(l["action"] == "shop.update" for l in r.get_json()["logs"])

    def test_filter_by_shop(self, client):
        t = _admin_token(client)
        sid = insert_shop("A", name="店")
        _log("shop.update", "2026-07-15 10:00:00", shop_id=sid)
        _log("shop.update", "2026-07-15 11:00:00", shop_id=99999)
        r = client.get(f"/api/admin/audit-logs?shop={sid}", headers=_hdr(t))
        assert all(l["shop_id"] == sid for l in r.get_json()["logs"])


class TestPaging:
    def test_before_id_pages_backwards(self, client):
        t = _admin_token(client)
        for i in range(5):
            _log("shop.create", f"2026-07-1{i} 10:00:00", detail=f"n{i}")
        r = client.get("/api/admin/audit-logs?limit=2", headers=_hdr(t))
        first = r.get_json()
        assert len(first["logs"]) == 2
        assert first["has_more"] is True

        last_id = first["logs"][-1]["id"]
        r = client.get(f"/api/admin/audit-logs?limit=2&before_id={last_id}", headers=_hdr(t))
        second = r.get_json()["logs"]
        assert len(second) == 2
        assert all(l["id"] < last_id for l in second), "同じ行が再度返っている"

    def test_limit_is_capped_at_500(self, client):
        t = _admin_token(client)
        r = client.get("/api/admin/audit-logs?limit=99999", headers=_hdr(t))
        assert r.status_code == 200
        assert len(r.get_json()["logs"]) <= 500


class TestCsv:
    def test_csv_download(self, client):
        t = _admin_token(client)
        _log("shop.create", "2026-07-15 10:00:00", detail="テスト")
        r = client.get("/api/admin/audit-logs.csv", headers=_hdr(t))
        assert r.status_code == 200
        assert "attachment" in r.headers.get("Content-Disposition", "")
        body = r.get_data(as_text=True)
        assert "日時" in body
        assert "shop.create" in body

    def test_csv_escapes_formula_injection(self, client):
        """=cmd で始まるセルが数式として解釈されないようエスケープされること。"""
        t = _admin_token(client)
        _log("shop.create", "2026-07-15 10:00:00", actor_name="=cmd|'/c calc'!A1")
        r = client.get("/api/admin/audit-logs.csv", headers=_hdr(t))
        body = r.get_data(as_text=True)
        assert "'=cmd" in body, "Formula Injection 対策が効いていない"

    def test_csv_respects_filters(self, client):
        t = _admin_token(client)
        _log("shop.create", "2026-07-01 10:00:00", detail="範囲外")
        _log("shop.create", "2026-07-15 10:00:00", detail="範囲内")
        r = client.get("/api/admin/audit-logs.csv?start=2026-07-10&end=2026-07-20", headers=_hdr(t))
        body = r.get_data(as_text=True)
        assert "範囲内" in body
        assert "範囲外" not in body


def test_requires_admin_role(client):
    from helpers import insert_staff
    sid = insert_shop("SHOP1", "pw12345678")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                        "password": "pw12345678"})
    t = r.get_json()["token"]
    assert client.get("/api/admin/audit-logs", headers=_hdr(t)).status_code == 403
    assert client.get("/api/admin/audit-logs.csv", headers=_hdr(t)).status_code == 403
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_audit_filters.py -v`
Expected: ほとんどが FAIL

- [ ] **Step 3: 実装する**

`src/admin_api.py` の `admin_audit_logs` を以下に置き換え、CSV エンドポイントを追加する。`_csv_safe` は `src/app.py:45` にあるので、`deps` 経由ではなく `admin_api.py` 内に同等のものを置くと二重定義になる。**`register_admin_routes` の引数に `csv_safe` を追加して受け取る**こと（`src/app.py` の登録呼び出しにも `csv_safe=_csv_safe` を足す）。

```python
    def _audit_filters():
        """クエリ文字列から WHERE 句と bind を組み立てる。"""
        where, binds = [], []
        shop = request.args.get("shop")
        if shop:
            where.append("shop_id=?"); binds.append(int(shop))
        action = request.args.get("action")
        if action:
            where.append("action=?"); binds.append(action)
        start = request.args.get("start")
        if start:
            where.append("created_at>=?"); binds.append(start + " 00:00:00")
        end = request.args.get("end")
        if end:
            where.append("created_at<=?"); binds.append(end + " 23:59:59")
        actor = request.args.get("actor")
        if actor:
            where.append("actor_name LIKE ?"); binds.append(f"%{actor}%")
        return where, binds

    @app.get("/api/admin/audit-logs")
    def admin_audit_logs():
        require_auth(["admin"])
        where, binds = _audit_filters()
        before_id = request.args.get("before_id")
        if before_id:
            where.append("id<?"); binds.append(int(before_id))
        try:
            limit = min(int(request.args.get("limit", 100)), 500)
        except (TypeError, ValueError):
            limit = 100
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        # has_more の判定のため1件多く取る
        rows = query_all(f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT ?",
                         tuple(binds) + (limit + 1,))
        has_more = len(rows) > limit
        return jsonify({"logs": rows[:limit], "has_more": has_more})

    @app.get("/api/admin/audit-logs.csv")
    def admin_audit_logs_csv():
        require_auth(["admin"])
        where, binds = _audit_filters()
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        # CSV は画面のページングと無関係にフィルタ結果全件を出す（上限5000）
        rows = query_all(f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT 5000",
                         tuple(binds))
        header = ["日時", "操作者ロール", "操作者", "操作", "対象種別", "対象ID", "店舗ID", "詳細"]
        lines = [",".join(csv_safe(h) for h in header)]
        for r in rows:
            lines.append(",".join(csv_safe(v) for v in [
                r.get("created_at"), r.get("actor_role"), r.get("actor_name"), r.get("action"),
                r.get("target_type"), r.get("target_id"), r.get("shop_id"), r.get("detail")]))
        # Excel が UTF-8 と判定できるよう BOM を付ける
        body = "﻿" + "\r\n".join(lines)
        filename = f"audit-logs-{jst_now().strftime('%Y%m%d')}.csv"
        resp = Response(body, content_type="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
```

`register_admin_routes` のシグネチャを更新する。

```python
def register_admin_routes(app, *, require_auth, audit, summarize_shifts, csv_safe):
```

`src/app.py` の登録呼び出しも更新する。

```python
admin_api.register_admin_routes(
    app, require_auth=require_auth, audit=audit, summarize_shifts=summarize_shifts,
    csv_safe=_csv_safe)
```

- [ ] **Step 4: 画面を更新**

`public/admin.js` の `SCREENS.adminAudit` を書き換える。フィルタ4つ・「もっと見る」・CSV ダウンロードを追加する。

```js
SCREENS.adminAudit = async function (el) {
  const tok = navToken();
  const shops = await api('/admin/shops?include_archived=1');
  if (!isAlive(tok)) return;
  el.innerHTML = pageHead('監査ログ', 'bi-clipboard-data') +
    card(sectionTitle('bi-funnel', 'フィルタ') +
      `<div class="row">
         <div class="col-6 col-lg-3"><label class="form-label" for="auStart">開始日</label><input type="date" id="auStart" class="form-control mb-2"></div>
         <div class="col-6 col-lg-3"><label class="form-label" for="auEnd">終了日</label><input type="date" id="auEnd" class="form-control mb-2"></div>
         <div class="col-6 col-lg-3"><label class="form-label" for="auShop">店舗</label>
           <select id="auShop" class="form-select mb-2"><option value="">すべて</option>
             ${shops.shops.map((s) => `<option value="${s.id}">${esc(s.shop_name || s.shop_code)}</option>`).join('')}
           </select></div>
         <div class="col-6 col-lg-3"><label class="form-label" for="auActor">操作者</label><input id="auActor" class="form-control mb-2" placeholder="氏名の一部"></div>
         <div class="col-6 col-lg-3"><label class="form-label" for="auAction">操作</label>
           <select id="auAction" class="form-select mb-2"><option value="">すべて</option>
             ${Object.keys(AUDIT_ACTION_LABELS).map((a) => `<option value="${esc(a)}">${esc(AUDIT_ACTION_LABELS[a])}</option>`).join('')}
           </select></div>
         <div class="col-12 flex gap-2 items-end">
           <button class="btn btn-primary btn-sm" id="auLoad"><i class="bi bi-search"></i> 表示</button>
           <button class="btn btn-light btn-sm" id="auCsv"><i class="bi bi-download"></i> CSV</button>
         </div>
       </div>`) +
    card('<div id="auBody"><div class="text-secondary small">「表示」ボタンを押してください</div></div>' +
         '<div class="text-center mt-2"><button class="btn btn-light btn-sm d-none" id="auMore">もっと見る</button></div>');

  let rows = [];
  const qs = (beforeId) => {
    const p = new URLSearchParams();
    const v = (id) => document.getElementById(id).value.trim();
    if (v('auStart')) p.set('start', v('auStart'));
    if (v('auEnd')) p.set('end', v('auEnd'));
    if (v('auShop')) p.set('shop', v('auShop'));
    if (v('auActor')) p.set('actor', v('auActor'));
    if (v('auAction')) p.set('action', v('auAction'));
    p.set('limit', '100');
    if (beforeId) p.set('before_id', beforeId);
    return p;
  };

  const render = () => {
    const body = document.getElementById('auBody');
    if (!body) return;
    body.innerHTML = rows.length
      ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>日時</th><th>操作者</th><th>操作</th><th>対象</th><th>詳細</th></tr></thead><tbody>` +
        rows.map((l) => `<tr>
          <td class="small">${esc((l.created_at || '').replace('T', ' '))}</td>
          <td class="small">${esc(l.actor_name || l.actor_role || '—')}</td>
          <td>${badge(auditActionLabel(l.action), (l.action || '').indexOf('reject') >= 0 || (l.action || '').indexOf('fail') >= 0 || (l.action || '').indexOf('delete') >= 0 ? 'warning' : 'info')}</td>
          <td class="small">${esc(l.target_type || '')}${l.target_id ? ' #' + l.target_id : ''}</td>
          <td class="small">${esc(l.detail || '')}</td></tr>`).join('') + '</tbody></table></div>'
      : emptyState('bi-clipboard-data', '該当するログがありません');
  };

  const load = async (beforeId) => {
    const tok2 = navToken();
    const d = await api('/admin/audit-logs?' + qs(beforeId).toString());
    if (!isAlive(tok2)) return;
    rows = beforeId ? rows.concat(d.logs) : d.logs;
    render();
    const more = document.getElementById('auMore');
    if (more) more.classList.toggle('d-none', !d.has_more);
  };

  document.getElementById('auLoad')?.addEventListener('click', () => load());
  document.getElementById('auMore')?.addEventListener('click', () => {
    if (rows.length) load(rows[rows.length - 1].id);
  });
  document.getElementById('auCsv')?.addEventListener('click', async () => {
    try {
      const p = qs();
      p.delete('limit');
      const res = await fetch('/api/admin/audit-logs.csv?' + p.toString(), {
        headers: { Authorization: 'Bearer ' + localStorage.getItem('shift_token') } });
      if (!res.ok) throw new Error('ダウンロードに失敗しました');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'audit-logs.csv'; a.click();
      URL.revokeObjectURL(url);
    } catch (e) { toast(e.message, 'error'); }
  });
  load();
};
```

- [ ] **Step 5: テストと構文チェック**

Run: `.venv/bin/python -m pytest tests/test_admin_audit_filters.py -v && node --check public/admin.js`
Expected: すべて成功

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 6: コミット**

```bash
git add src/admin_api.py src/app.py public/admin.js tests/test_admin_audit_filters.py
git commit -m "feat(admin): 監査ログに期間・操作者フィルタ、ページング、CSV出力を追加

before_id カーソル方式の「もっと見る」で全件辿れるようにした。CSV は
既存の _csv_safe を使って Formula Injection を防ぎ、Excel 向けに BOM を付ける。"
```

---

### Task 14: 全店舗一斉通知

**Files:**
- Modify: `src/admin_api.py`
- Modify: `public/admin.js`（`adminSystem` に4タブ目）
- Test: `tests/test_admin_announcements.py`（新規）

**Interfaces:**
- Produces:
  - `POST /api/admin/announcements` body `{shop_ids: int[]|null, audience: 'managers'|'all', title, body}` → `{"ok": True, "shops": int, "recipients": int}`
  - `GET /api/admin/notifications` → `{"announcements": [{"created_at", "title", "shops", "recipients"}]}`

- [ ] **Step 1: 失敗するテストを書く**

新規ファイル `tests/test_admin_announcements.py`:

```python
"""全店舗一斉通知。"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _two_shops():
    a = insert_shop("A", name="店A")
    insert_staff(a, "mgrA", "店長A", role="manager")
    insert_staff(a, "p1", "太郎")
    b = insert_shop("B", name="店B")
    insert_staff(b, "mgrB", "店長B", role="manager")
    return a, b


class TestAnnounce:
    def test_to_managers_of_all_shops(self, client):
        t = _admin_token(client)
        a, b = _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "managers",
                              "title": "メンテナンス", "body": "8/1 に実施します"})
        assert r.status_code == 200
        assert r.get_json()["shops"] == 2
        rows = dbmod.query_all("SELECT shop_id, staff_id FROM notifications WHERE type='announcement'")
        assert len(rows) == 2
        assert all(x["staff_id"] is None for x in rows)

    def test_to_all_staff(self, client):
        t = _admin_token(client)
        a, b = _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "all",
                              "title": "お知らせ", "body": "本文"})
        assert r.status_code == 200
        rows = dbmod.query_all("SELECT shop_id, staff_id FROM notifications WHERE type='announcement'")
        # 店舗向け2件 + スタッフ3人分
        assert len(rows) == 5
        assert r.get_json()["recipients"] == 3

    def test_selected_shops_only(self, client):
        t = _admin_token(client)
        a, b = _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": [a], "audience": "managers",
                              "title": "個別", "body": "本文"})
        assert r.status_code == 200
        rows = dbmod.query_all("SELECT shop_id FROM notifications WHERE type='announcement'")
        assert {x["shop_id"] for x in rows} == {a}

    def test_archived_shops_are_excluded(self, client):
        t = _admin_token(client)
        a, b = _two_shops()
        client.post(f"/api/admin/shops/{b}/archive", headers=_hdr(t))
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "managers",
                              "title": "お知らせ", "body": "本文"})
        assert r.get_json()["shops"] == 1

    def test_title_is_required(self, client):
        t = _admin_token(client)
        _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "managers", "title": "", "body": "本文"})
        assert r.status_code == 400

    def test_same_created_at_within_batch(self, client):
        """配信履歴のグルーピングのため、同一バッチは created_at が揃うこと。"""
        t = _admin_token(client)
        _two_shops()
        client.post("/api/admin/announcements", headers=_hdr(t),
                    json={"shop_ids": None, "audience": "all", "title": "お知らせ", "body": "本文"})
        stamps = {r["created_at"] for r in
                  dbmod.query_all("SELECT created_at FROM notifications WHERE type='announcement'")}
        assert len(stamps) == 1, "バッチ内で created_at がばらついている"

    def test_is_audited(self, client):
        t = _admin_token(client)
        _two_shops()
        client.post("/api/admin/announcements", headers=_hdr(t),
                    json={"shop_ids": None, "audience": "managers", "title": "お知らせ", "body": "本文"})
        assert dbmod.query_one("SELECT id FROM audit_logs WHERE action='admin.announce'") is not None


class TestHistory:
    def test_history_groups_by_batch(self, client):
        t = _admin_token(client)
        _two_shops()
        client.post("/api/admin/announcements", headers=_hdr(t),
                    json={"shop_ids": None, "audience": "managers", "title": "1回目", "body": "本文"})
        r = client.get("/api/admin/notifications", headers=_hdr(t))
        assert r.status_code == 200
        items = r.get_json()["announcements"]
        assert len(items) == 1
        assert items[0]["title"] == "1回目"
        assert items[0]["shops"] == 2

    def test_history_requires_admin(self, client):
        sid = insert_shop("SHOP1", "pw12345678")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        assert client.get("/api/admin/notifications", headers=_hdr(t)).status_code == 403
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `.venv/bin/python -m pytest tests/test_admin_announcements.py -v`
Expected: 全件 FAIL

- [ ] **Step 3: 実装する**

既存の `admin_notifs` / `admin_notifs_readall` を以下に置き換える（Phase 1 の S3 で `require_auth` を足した空スタブ）。

```python
    _ANNOUNCE_MAX_ROWS = 5000

    @app.post("/api/admin/announcements")
    def admin_announce():
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        title = (body.get("title") or "").strip()
        text = (body.get("body") or "").strip()
        audience = body.get("audience") or "managers"
        if not title:
            raise ValueError("件名を入力してください")
        if audience not in ("managers", "all"):
            raise ValueError("配信対象が不正です")

        shop_ids = body.get("shop_ids")
        if shop_ids:
            shops = query_all(
                "SELECT id FROM shops WHERE COALESCE(is_archived,0)=0 AND is_active=1 "
                "AND id IN ({})".format(",".join("?" * len(shop_ids))), tuple(shop_ids))
        else:
            shops = query_all("SELECT id FROM shops WHERE COALESCE(is_archived,0)=0 AND is_active=1")
        if not shops:
            raise ValueError("配信先の店舗がありません")

        # created_at はバッチで1つの値に揃える。配信履歴を (created_at, title) で
        # グルーピングするため、datetime('now') 任せにすると行ごとにずれる。
        stamp = jst_now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [(s["id"], None) for s in shops]
        recipients = 0
        if audience == "all":
            ids = [s["id"] for s in shops]
            staffs = query_all(
                "SELECT id, shop_id FROM staffs WHERE is_resigned=0 AND shop_id IN ({})".format(
                    ",".join("?" * len(ids))), tuple(ids))
            rows += [(st["shop_id"], st["id"]) for st in staffs]
            recipients = len(staffs)

        if len(rows) > _ANNOUNCE_MAX_ROWS:
            raise ValueError(f"配信件数が多すぎます（{len(rows)}件）。店舗を分けて配信してください")

        # D1 は REST API の1往復＝1クエリのため、1件ずつ挿すと配信が実用速度にならない。
        # 1文のまとめ INSERT にする。
        placeholders = ",".join(["(?,?,?,?,?,0,?)"] * len(rows))
        binds = []
        for shop_id, staff_id in rows:
            binds += [shop_id, staff_id, "announcement", title, text, stamp]
        execute("INSERT INTO notifications (shop_id, staff_id, type, title, body, is_read, created_at) "
                "VALUES " + placeholders, tuple(binds))

        audit("admin.announce", target_type="announcement", detail=
              f"{title} / 店舗{len(shops)}件 / 対象{'全員' if audience == 'all' else '店舗管理者'}")
        return jsonify({"ok": True, "shops": len(shops), "recipients": recipients})

    @app.get("/api/admin/notifications")
    def admin_notifs():
        """一斉通知の配信履歴。

        notifications にバッチIDが無いため (created_at, title) で束ねる。
        この2列で一意になるのは、配信時に created_at をバッチで揃えているため。
        """
        require_auth(["admin"])
        rows = query_all(
            "SELECT created_at, title, COUNT(DISTINCT shop_id) AS shops, "
            "SUM(CASE WHEN staff_id IS NOT NULL THEN 1 ELSE 0 END) AS recipients "
            "FROM notifications WHERE type='announcement' "
            "GROUP BY created_at, title ORDER BY created_at DESC LIMIT 100")
        return jsonify({"announcements": rows})

    @app.put("/api/admin/notifications/read-all")
    def admin_notifs_readall():
        require_auth(["admin"])
        # 管理者は配信履歴を見るだけで、自分宛の未読という概念が無い。
        # フロントの共通ヘッダが呼ぶため、互換のために残す。
        return jsonify({"ok": True})
```

`INSERT ... VALUES` に `is_read` の `0` をリテラルで書いているのは、placeholder 数を行あたり6に揃えるため。

- [ ] **Step 4: テストを実行**

Run: `.venv/bin/python -m pytest tests/test_admin_announcements.py -v`
Expected: 全件 PASS

- [ ] **Step 5: システム画面に「お知らせ配信」タブを追加**

`public/admin.js` の `SCREENS.adminSystem` のタブに4つ目を足し、`renderAdminSystemTab` の分岐に `announce: renderAnnounceTab` を追加する。

```js
async function renderAnnounceTab(body) {
  const tok = navToken();
  const shops = await api('/admin/shops');
  if (!isAlive(tok)) return;
  body.innerHTML =
    card(sectionTitle('bi-megaphone', 'お知らせ配信') +
      `<label class="form-label" for="anTitle">件名 <span class="text-danger">*</span></label>
       <input id="anTitle" class="form-control mb-2" placeholder="例: メンテナンスのお知らせ">
       <label class="form-label" for="anBody">本文</label>
       <textarea id="anBody" class="form-control mb-2" rows="4"></textarea>
       <div class="row">
         <div class="col-6"><label class="form-label" for="anScope">配信先の店舗</label>
           <select id="anScope" class="form-select mb-2">
             <option value="all">すべての稼働店舗</option>
             <option value="select">店舗を選ぶ</option>
           </select></div>
         <div class="col-6"><label class="form-label" for="anAudience">受け取る人</label>
           <select id="anAudience" class="form-select mb-2">
             <option value="managers">店舗管理者のみ</option>
             <option value="all">全スタッフ</option>
           </select></div>
       </div>
       <div id="anShopPick" class="d-none mb-2">
         ${shops.shops.map((s) => `<label class="me-3"><input type="checkbox" class="an-shop" value="${s.id}"> ${esc(s.shop_name || s.shop_code)}</label>`).join('')}
       </div>
       <div class="form-error" id="anErr"></div>
       <button class="btn btn-primary btn-sm mt-2" id="anSend"><i class="bi bi-send"></i> 配信する</button>`) +
    card(sectionTitle('bi-clock-history', '配信履歴') + '<div id="anHistory"></div>');

  document.getElementById('anScope')?.addEventListener('change', (e) =>
    document.getElementById('anShopPick').classList.toggle('d-none', e.target.value !== 'select'));

  const loadHistory = async () => {
    const d = await api('/admin/notifications');
    const h = document.getElementById('anHistory');
    if (!h) return;
    h.innerHTML = d.announcements.length
      ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>日時</th><th>件名</th><th class="t-num">店舗</th><th class="t-num">個人宛</th></tr></thead><tbody>` +
        d.announcements.map((a) => `<tr>
          <td class="small">${esc((a.created_at || '').replace('T', ' '))}</td>
          <td>${esc(a.title || '')}</td>
          <td class="t-num num">${a.shops}</td>
          <td class="t-num num">${a.recipients || 0}</td></tr>`).join('') + '</tbody></table></div>'
      : emptyState('bi-megaphone', '配信履歴がありません');
  };

  document.getElementById('anSend')?.addEventListener('click', () =>
    openModal('<i class="bi bi-send"></i> 配信の確認',
      '<p class="mb-0">この内容で配信します。配信後は取り消せません。</p>',
      async (w, close) => {
        const err = document.getElementById('anErr');
        if (err) err.textContent = '';
        const scope = document.getElementById('anScope').value;
        const picked = Array.from(document.querySelectorAll('.an-shop:checked')).map((c) => +c.value);
        try {
          const r = await api('/admin/announcements', { method: 'POST', body: JSON.stringify({
            shop_ids: scope === 'select' ? picked : null,
            audience: document.getElementById('anAudience').value,
            title: document.getElementById('anTitle').value.trim(),
            body: document.getElementById('anBody').value.trim() }) });
          close();
          toast(`${r.shops}店舗に配信しました`, 'success');
          document.getElementById('anTitle').value = '';
          document.getElementById('anBody').value = '';
          loadHistory();
        } catch (e) { close(); if (err) err.textContent = e.message; }
      }, { saveLabel: '配信する' }));

  loadHistory();
}
```

`AUDIT_ACTION_LABELS` に `'admin.announce': 'お知らせ配信'` と、Task 9/10 の `'shop.archive': '店舗アーカイブ'`, `'shop.unarchive': 'アーカイブ解除'`, `'shop.delete': '店舗削除'`, `'shop.export': '店舗エクスポート'`, `'admin.migrate': 'スキーマ適用'` を追加する。

- [ ] **Step 6: テストと構文チェック**

Run: `.venv/bin/python -m pytest tests/ -q && node --check public/admin.js`
Expected: すべて成功

- [ ] **Step 7: コミット**

```bash
git add src/admin_api.py public/admin.js tests/test_admin_announcements.py
git commit -m "feat(admin): 全店舗一斉通知を追加

宛先は「全店舗/選択店舗」×「店舗管理者のみ/全スタッフ」。D1 は1往復1クエリ
なので1文のまとめ INSERT にする。配信履歴のグルーピングのため created_at は
バッチで1つの値に揃える。空スタブだった /api/admin/notifications を
配信履歴を返す実装に置き換えた。"
```

---

### Task 15: E2E テストと総仕上げ

**Files:**
- Create: `e2e/admin-console.spec.js`
- Test: 全体

- [ ] **Step 1: E2E ヘルパを確認**

Run: `grep -n "ensureAdmin\|loginAsAdmin" e2e/helpers.js`

既存の `ensureAdmin` / `loginAsAdmin` のシグネチャを確認する。Phase 1 で `/api/init` を `ALLOW_INIT=1` 必須にしたため、`ensureAdmin` の実装が変わっている可能性がある。

- [ ] **Step 2: E2E テストを書く**

新規ファイル `e2e/admin-console.spec.js`:

```js
const { test, expect } = require('@playwright/test');
const { ensureAdmin, ensureShop, loginAsAdmin, attachConsoleCollector } = require('./helpers');

test.describe('システム管理者コンソール', () => {
  test.beforeEach(async ({ page }) => {
    attachConsoleCollector(page);
    await ensureAdmin();
    await ensureShop();
  });

  test('ダッシュボードにKPIと要対応が表示される', async ({ page }) => {
    await loginAsAdmin(page);
    await expect(page.locator('#dashKpi')).toBeVisible();
    await expect(page.getByText('稼働店舗')).toBeVisible();
    await expect(page.getByText('要対応')).toBeVisible();
  });

  test('ナビにシステムタブがあり、3つ以上のタブが表示される', async ({ page }) => {
    await loginAsAdmin(page);
    await page.getByRole('button', { name: 'システム' }).first().click();
    await expect(page.getByRole('button', { name: '管理者アカウント' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'マイグレーション' })).toBeVisible();
  });

  test('店舗詳細から代理閲覧に入り、警告バーが出て戻れる', async ({ page }) => {
    await loginAsAdmin(page);
    await page.getByRole('button', { name: '店舗' }).first().click();
    await page.locator('[data-detail]').first().click();
    await page.getByRole('button', { name: /代理閲覧/ }).click();
    await page.getByRole('button', { name: '代理閲覧を開始' }).click();

    const bar = page.locator('#impersonationBar');
    await expect(bar).toBeVisible();
    await expect(bar).toContainText('閲覧のみ');

    await page.getByRole('button', { name: '管理者に戻る' }).click();
    await expect(page.locator('#impersonationBar')).toHaveCount(0);
  });

  test('店舗の有効/無効トグルで店舗名が消えない', async ({ page }) => {
    await loginAsAdmin(page);
    await page.getByRole('button', { name: '店舗' }).first().click();
    const nameBefore = await page.locator('[data-detail] strong').first().textContent();
    await page.locator('[data-toggle]').first().click();
    await page.waitForTimeout(500);
    const nameAfter = await page.locator('[data-detail] strong').first().textContent();
    expect(nameAfter).toBe(nameBefore);
    expect((nameAfter || '').trim()).not.toBe('');
  });

  test('監査ログのフィルタとCSVボタンが表示される', async ({ page }) => {
    await loginAsAdmin(page);
    await page.getByRole('button', { name: '監査ログ' }).first().click();
    await expect(page.locator('#auStart')).toBeVisible();
    await expect(page.locator('#auActor')).toBeVisible();
    await expect(page.getByRole('button', { name: /CSV/ })).toBeVisible();
  });
});
```

`ensureShop` のシグネチャが違う場合は `e2e/helpers.js` の定義に合わせる。

- [ ] **Step 3: E2E を実行**

Run: `npx playwright test e2e/admin-console.spec.js`
Expected: 全件 PASS。セレクタが合わない場合は `npx playwright test --debug` で確認して直す

- [ ] **Step 4: E2E 全体**

Run: `npx playwright test`
Expected: 全件 PASS。既存 E2E が管理画面の構造変更で落ちる場合は、そのテストを新しい構造に合わせる

- [ ] **Step 5: 全ユニットテスト**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全件 PASS

- [ ] **Step 6: シフトエンジンの不変量テスト**

Run: `.venv/bin/python tests/run_tests.py`
Expected: PASS

- [ ] **Step 7: 構文チェック**

Run: `node --check public/app.js && node --check public/admin.js && .venv/bin/python -c "import ast; ast.parse(open('src/app.py').read())" && .venv/bin/python -c "import ast; ast.parse(open('src/admin_api.py').read())" && .venv/bin/python -c "import ast; ast.parse(open('src/migrator.py').read())"`
Expected: すべて出力なし

- [ ] **Step 8: デザイントークン検査**

Run: `.venv/bin/python -m pytest tests/test_design_tokens.py -v`
Expected: 全件 PASS。落ちた場合は `public/admin.js` で新しい色を使っている

- [ ] **Step 9: 本番D1へのマイグレーション適用**

デプロイ後、管理者画面「システム」→「マイグレーション」で未適用件数を確認し、「未適用を適用」を実行する。`failed` が返った場合は、そのステートメントを手動で調べてから再実行する（成功分は記録済みなので失敗箇所から再開する）。

- [ ] **Step 10: 本番の初期パスワード変更**

Phase 1 で `/api/init` の初期パスワードはランダム化したが、**既存の本番管理者は `admin123` のままの可能性が高い**。「システム」→「管理者アカウント」→「パスワードを変更」で必ず変更する。

- [ ] **Step 11: 最終コミット**

```bash
git add e2e/admin-console.spec.js
git commit -m "test(e2e): 管理者コンソールの受け入れテストを追加

ダッシュボード表示、システムタブ、代理閲覧の開始と終了、店舗トグルで
店舗名が消えないこと、監査ログのフィルタ表示を確認する。"
```
