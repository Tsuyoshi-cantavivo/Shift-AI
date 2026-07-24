"""tests/test_audit_log.py - 監査ログの記録と閲覧エンドポイントのテスト。"""
import db as dbmod
from helpers import (
    insert_admin, insert_shop, insert_staff, insert_pattern, make_session, auth,
)

DAY = "2026-08-03"


def test_audit_logs_table_exists(client):
    names = {r["name"] for r in dbmod.query_all(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "audit_logs" in names


def test_role_change_is_audited(client):
    admin_id = insert_admin()
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "社員", "part_time")
    tok = make_session("admin", admin_id)
    r = client.put(f"/api/admin/shops/{shop_id}/staffs/{st}/role",
                   json={"role": "employee"}, headers=auth(tok))
    assert r.status_code == 200, r.get_json()
    row = dbmod.query_one(
        "SELECT * FROM audit_logs WHERE action='staff.role_change' AND target_id=?", (st,))
    assert row is not None
    assert row["actor_role"] == "admin"
    assert "part_time->employee" in (row["detail"] or "")


def test_finalize_is_audited(client):
    shop_id = insert_shop()
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)
    a = insert_staff(shop_id, "E1", "社員", "employee")
    dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, a, f"{DAY}T09:00:00", f"{DAY}T18:00:00", "requested", "AIドラフト: x"))
    tok = make_session("shop", shop_id, shop_id)
    client.post("/api/shop/shifts/finalize",
                json={"start_date": DAY, "end_date": DAY}, headers=auth(tok))
    row = dbmod.query_one("SELECT * FROM audit_logs WHERE action='shift.finalize'")
    assert row is not None
    assert row["actor_role"] == "shop"


def test_admin_can_list_and_filter_audit_logs(client):
    admin_id = insert_admin()
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "社員", "part_time")
    tok = make_session("admin", admin_id)
    # いくつか操作して監査ログを積む
    client.put(f"/api/admin/shops/{shop_id}/staffs/{st}/role",
               json={"role": "employee"}, headers=auth(tok))
    client.put(f"/api/admin/shops/{shop_id}",
               json={"shop_name": "更新後", "is_active": 1}, headers=auth(tok))
    r = client.get("/api/admin/audit-logs?limit=50", headers=auth(tok))
    assert r.status_code == 200
    logs = r.get_json()["logs"]
    assert isinstance(logs, list) and len(logs) >= 2
    # action フィルタ
    r2 = client.get("/api/admin/audit-logs?action=shop.update", headers=auth(tok))
    logs2 = r2.get_json()["logs"]
    assert len(logs2) >= 1
    assert all(x["action"] == "shop.update" for x in logs2)


def test_audit_logs_requires_admin(client):
    shop_id = insert_shop()
    tok = make_session("shop", shop_id, shop_id)
    r = client.get("/api/admin/audit-logs", headers=auth(tok))
    assert r.status_code == 403
