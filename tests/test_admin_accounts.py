"""システム管理者アカウントの管理API。

背景: system_admins への UPDATE がコード全体でゼロで、/api/init が作る
初期パスワードを変更する正規の手段が存在しなかった。S4 で初期パスワードを
ランダム化したため、変更手段が無いままだと発行された値を一生使い続けることになる。

※ 管理者アカウントの一覧/追加/削除（S3）は Phase 2 で実装する。
"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _token(client, admin_id="admin", pw="Admin123"):
    r = client.post("/api/login", json={"user_code": admin_id, "password": pw})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


class TestAdminPasswordChange:
    def test_change_password(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "Admin123", "new_password": "NewPass456"})
        assert r.status_code == 200
        # 新パスワードでログインできる
        assert client.post("/api/login", json={"user_code": "admin",
                                               "password": "NewPass456"}).status_code == 200
        # 旧パスワードでは不可
        assert client.post("/api/login", json={"user_code": "admin",
                                               "password": "Admin123"}).status_code == 400

    def test_wrong_current_password_is_rejected(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "wrong", "new_password": "NewPass456"})
        assert r.status_code == 400
        assert client.post("/api/login", json={"user_code": "admin",
                                               "password": "Admin123"}).status_code == 200

    def test_weak_password_is_rejected(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "Admin123", "new_password": "short"})
        assert r.status_code == 400

    def test_other_sessions_are_revoked(self, client):
        """変更後、自分の現在のセッションは生き、他のセッションは失効すること。"""
        insert_admin("admin", "Admin123")
        old = _token(client)
        cur = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(cur),
                       json={"current_password": "Admin123", "new_password": "NewPass456"})
        assert r.status_code == 200
        assert client.get("/api/me", headers=_hdr(cur)).status_code == 200
        assert client.get("/api/me", headers=_hdr(old)).status_code == 401

    def test_requires_admin_role(self, client):
        sid = insert_shop("SHOP1", "pw12345678")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "pw12345678", "new_password": "NewPass456"})
        assert r.status_code == 403

    def test_requires_auth(self, client):
        r = client.put("/api/admin/password",
                       json={"current_password": "x", "new_password": "NewPass456"})
        assert r.status_code == 401

    def test_change_is_audited(self, client):
        admin_id = insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "Admin123", "new_password": "NewPass456"})
        assert r.status_code == 200
        row = dbmod.query_one("SELECT * FROM audit_logs WHERE action='admin.password_change' "
                              "ORDER BY id DESC LIMIT 1")
        assert row is not None, "パスワード変更が監査ログに残っていない"
        assert row["actor_role"] == "admin"
        assert row["target_type"] == "system_admin"
        assert row["target_id"] == admin_id
        # 入力されたパスワードが記録されていないこと
        joined = f"{row['actor_name']} {row['detail']}"
        assert "NewPass456" not in joined and "Admin123" not in joined
