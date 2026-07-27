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
