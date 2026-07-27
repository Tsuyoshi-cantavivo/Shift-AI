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
