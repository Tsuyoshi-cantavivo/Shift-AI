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
