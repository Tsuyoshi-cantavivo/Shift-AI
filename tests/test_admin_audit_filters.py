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
