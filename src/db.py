"""db.py - データベースアクセスヘルパ（Flask版）。

2つのモードをサポート:
  - DB_MODE=local (デフォルト): ローカル SQLite ファイル
  - DB_MODE=d1: Cloudflare D1 REST API経由

D1モードの場合、以下の環境変数が必要:
  - CF_ACCOUNT_ID: Cloudflare アカウントID
  - CF_D1_DATABASE_ID: D1データベースID
  - CF_D1_API_TOKEN: D1アクセス権限のあるAPIトークン
"""
import os
import json
import sqlite3
from dotenv import load_dotenv

load_dotenv()

DB_MODE = os.getenv("DB_MODE", "local")
DB_PATH = os.getenv("DB_PATH", "shift.db")

# D1 REST API 設定
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")
CF_D1_DATABASE_ID = os.getenv("CF_D1_DATABASE_ID", "")
CF_D1_API_TOKEN = os.getenv("CF_D1_API_TOKEN", "")
D1_API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/d1/database/{CF_D1_DATABASE_ID}/query"


# ============================================================
# ローカル SQLite モード（従来通り）
# ============================================================
_shared_conn = None
# 共有接続がインメモリかどうかのフラグ（接続作成時に固定）。
# ※ DB_PATH の現在値に依存すると、テストが DB_PATH を一時変更した
#    際に共有インメモリ接続が誤って close され、後続テストが
#    "Cannot operate on a closed database" で壊れるため。
_shared_is_in_memory = False


def _get_local_conn():
    """ローカルSQLite接続を返す。

    :memory: モードではプロセスで1つの接続を使い回す（データ永続化のため）。
    ファイルモードでは呼び出しごとに新しい接続を生成（DB_PATH の変更に追従）。
    """
    global _shared_conn, _shared_is_in_memory
    if DB_PATH == ":memory:":
        _shared_is_in_memory = True
        if _shared_conn is None:
            _shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
            _shared_conn.row_factory = sqlite3.Row
            _shared_conn.execute("PRAGMA foreign_keys = ON")
        return _shared_conn
    _shared_is_in_memory = False
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _local_maybe_close(conn):
    # フラグベース判定: インメモリ共有接続は絶対に閉じない（データ消失防止）。
    # ファイル接続は呼び出しごとに閉じる（リソース解放）。
    if _shared_is_in_memory:
        return
    conn.close()


# ============================================================
# 公開API（テスト・初期化用）
# ============================================================
def get_conn():
    """ローカルSQLite接続を返す（テスト用）。D1モードでは意味なし。"""
    return _get_local_conn()


def _maybe_close(conn):
    """接続を閉じる（インメモリモードでは閉じない）。"""
    _local_maybe_close(conn)


# ============================================================
# D1 REST API モード
# ============================================================
def _d1_execute_sql(sql, params=None):
    """D1 REST API でSQLを実行し、結果を返す。"""
    import requests as req
    headers = {
        "Authorization": f"Bearer {CF_D1_API_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {"sql": sql}
    if params:
        body["params"] = list(params)
    resp = req.post(D1_API_URL, headers=headers, json=body, timeout=30)
    data = resp.json()
    if not data.get("success"):
        errs = data.get("errors", [])
        raise RuntimeError(f"D1 API error: {errs}")
    result = data["result"][0]
    if not result.get("success"):
        raise RuntimeError(f"D1 query failed: {result}")
    return result


# ============================================================
# 統合API（モード自動切替）
# ============================================================
def query_all(sql, params=()):
    """SELECT結果を dict のリストで返す。"""
    if DB_MODE == "d1":
        result = _d1_execute_sql(sql, params)
        return result.get("results", [])
    conn = _get_local_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        _local_maybe_close(conn)


def query_one(sql, params=()):
    """先頭1行をdictで返す（無ければNone）。"""
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """INSERT/UPDATE/DELETEを実行し meta({last_row_id}) を返す。"""
    if DB_MODE == "d1":
        result = _d1_execute_sql(sql, params)
        meta = result.get("meta", {})
        return {"last_row_id": meta.get("last_row_id", 0)}
    conn = _get_local_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return {"last_row_id": cur.lastrowid}
    finally:
        _local_maybe_close(conn)


def insert_row(table, row):
    """dictからINSERTを構築して実行。None値はSQL NULLを使用。"""
    cols = list(row.keys())
    placeholders = []
    binds = []
    for v in row.values():
        if v is None:
            placeholders.append("NULL")
        else:
            placeholders.append("?")
            binds.append(v)
    sql = "INSERT INTO {} ({}) VALUES ({})".format(table, ",".join(cols), ",".join(placeholders))
    return execute(sql, tuple(binds))


def init_schema(schema_path="schema.sql"):
    """schema.sqlを実行してテーブルを整備。

    D1モードでは個別ステートメントに分割して実行。
    ※ 本番D1では wrangler d1 execute で事前初期化を推奨。
    """
    if DB_MODE == "d1":
        with open(schema_path, "r", encoding="utf-8") as f:
            script = f.read()
        # セミコロンで分割（簡易版: コメント行と空行を除外）
        statements = []
        for line in script.split("\n"):
            stripped = line.strip()
            if stripped.startswith("--") or not stripped:
                continue
            statements.append(line)
        full_sql = "\n".join(statements)
        # D1 は1回のAPI呼び出しで複数ステートメントを実行可能
        try:
            _d1_execute_sql(full_sql)
        except Exception:
            # 複数ステートメントで失敗した場合は個別に実行
            for stmt in full_sql.split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    try:
                        _d1_execute_sql(stmt)
                    except Exception:
                        pass  # 既存テーブル等はスキップ
        return
    conn = _get_local_conn()
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        _local_maybe_close(conn)
