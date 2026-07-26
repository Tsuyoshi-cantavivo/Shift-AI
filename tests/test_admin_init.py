"""POST /api/init のガードに関するテスト。

背景: 認証不要のまま公開されており、system_admins が空なら誰でも
初期管理者を作れる（DBリセット直後の乗っ取り窓）状態だった。
"""
import os

import db as dbmod
from helpers import insert_admin


def test_init_is_disabled_by_default(client, monkeypatch):
    """ALLOW_INIT が未設定なら 403 で拒否され、管理者が作られないこと。"""
    monkeypatch.delenv("ALLOW_INIT", raising=False)
    r = client.post("/api/init")
    assert r.status_code == 403
    assert dbmod.query_one("SELECT id FROM system_admins LIMIT 1") is None


def test_init_creates_admin_when_allowed(client, monkeypatch):
    """ALLOW_INIT=1 なら管理者を作り、生成パスワードを返すこと。"""
    monkeypatch.setenv("ALLOW_INIT", "1")
    r = client.post("/api/init")
    assert r.status_code == 200
    data = r.get_json()
    pw = data["logins"]["admin"]["password"]
    assert pw and pw != "admin123", "固定パスワードが返っている"
    assert len(pw) >= 12

    # 返ってきたパスワードで実際にログインできること
    r = client.post("/api/login", json={"user_code": "admin", "password": pw})
    assert r.status_code == 200


def test_init_is_noop_when_admin_exists(client, monkeypatch):
    """管理者が既にいる場合は ALLOW_INIT=1 でも作らないこと。"""
    monkeypatch.setenv("ALLOW_INIT", "1")
    insert_admin("admin", "Admin123")
    r = client.post("/api/init")
    assert r.status_code == 200
    assert r.get_json()["logins"] == {}
    rows = dbmod.query_all("SELECT id FROM system_admins")
    assert len(rows) == 1
