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
        """代理中でもスタッフ用APIには化けないこと。

        brief は /api/staff/myshift を挙げていたが実在しないため、
        require_auth(["staff"]) で守られた実在の GET（/api/staff/dashboard）を使う。
        """
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        r = client.get("/api/staff/dashboard", headers=_hdr(t))
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
