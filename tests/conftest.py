"""tests/conftest.py - pytest 共通設定。

- DB_PATH=:memory: を強制し、プロセス内で共有するインメモリSQLiteを使用（テスト高速化・非破壊）。
- LLM_API_KEY を空にし、AI関数は「未接続」状態で決定的に動作するようにする。
  （AI設定は .env のみ管理。テストでは未接続 = unavailable を検証）
- src/ をモジュールパスに追加し、app をインポートしてスキーマを初期化。
- 各テスト前に全テーブルをクリアする autouse フィクスチャを提供。
"""
import os
import sys

# ★ app/db のインポートより先に環境変数を固定
os.environ["DB_PATH"] = ":memory:"
os.environ["LLM_API_KEY"] = ""

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_BASE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest

import db as dbmod
import app as appmod  # noqa: E402  (インポート時に ensure_db -> init_schema が走る)

SCHEMA_PATH = os.path.join(_BASE, "schema.sql")

# 外部キー依存関係に従った子→親の削除順序
_TABLES = [
    "change_requests",
    "shifts",
    "wish_history",
    "fixed_shifts",
    "shift_pattern_weekday_required",
    "shift_request_periods",
    "shift_patterns",
    "shop_holidays",
    "sessions",
    "notifications",
    "staffs",
    "shops",
    "system_admins",
]


@pytest.fixture(autouse=True)
def db_reset():
    """各テストごとにスキーマを整備し、全テーブルを空にする。"""
    dbmod.init_schema(SCHEMA_PATH)
    conn = dbmod.get_conn()
    try:
        for t in _TABLES:
            conn.execute(f"DELETE FROM {t}")
        # AUTOINCREMENT の連番をリセット（表が存在しなければ無害）
        try:
            conn.execute("DELETE FROM sqlite_sequence")
        except Exception:
            pass
        conn.commit()
    finally:
        dbmod._maybe_close(conn)
    yield


@pytest.fixture()
def client():
    """Flask テストクライアント。"""
    return appmod.app.test_client()
