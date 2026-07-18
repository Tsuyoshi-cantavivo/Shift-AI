"""db.py - SQLite3 アクセスヘルパ（Flask版）。

sqlite3.Row で dict ライクに取得し、関数は dict / list[dict] を返す。

DB_PATH に ":memory:" を指定すると、プロセス内で共有する単一の
インメモリ接続を用いてテスト実行が可能（接続を閉じてもデータが残る）。
"""
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.getenv("DB_PATH", "shift.db")

# テスト用の共有インメモリ接続（DB_PATH == ":memory:" のとき使用）
_shared_conn = None


def get_conn():
    """SQLite 接続を返す。

    :memory: モードではプロセスで1つの接続を使い回す（データを永続化するため）。
    ファイルモードでは呼び出しごとに新しい接続を生成する。
    """
    global _shared_conn
    if DB_PATH == ":memory:":
        if _shared_conn is None:
            _shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
            _shared_conn.row_factory = sqlite3.Row
            _shared_conn.execute("PRAGMA foreign_keys = ON")
        return _shared_conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _maybe_close(conn):
    """インメモリモードでは閉じない（データ消失を防ぐ）。ファイルモードでは閉じる。"""
    if DB_PATH != ":memory:":
        conn.close()


def query_all(sql, params=()):
    """SELECT 結果を dict のリストで返す。"""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        _maybe_close(conn)


def query_one(sql, params=()):
    """先頭1行を dict で返す（無ければ None）。"""
    conn = get_conn()
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    finally:
        _maybe_close(conn)


def execute(sql, params=()):
    """INSERT/UPDATE/DELETE を実行し meta({last_row_id}) を返す。"""
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return {"last_row_id": cur.lastrowid}
    finally:
        _maybe_close(conn)


def insert_row(table, row):
    """dict から INSERT を構築して実行。None の列は SQL の NULL リテラルを使用。"""
    cols = list(row.keys())
    placeholders, binds = [], []
    for v in row.values():
        if v is None:
            placeholders.append("NULL")
        else:
            placeholders.append("?")
            binds.append(v)
    sql = "INSERT INTO {} ({}) VALUES ({})".format(table, ",".join(cols), ",".join(placeholders))
    return execute(sql, tuple(binds))


def init_schema(schema_path="schema.sql"):
    """schema.sql を実行してテーブルを整備（既存テーブルは維持）。"""
    conn = get_conn()
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        _maybe_close(conn)
