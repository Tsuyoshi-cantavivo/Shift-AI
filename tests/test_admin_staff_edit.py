"""tests/test_admin_staff_edit.py - 管理者によるスタッフ汎用編集エンドポイント。"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff, make_session, auth


def test_admin_edit_staff_fields(client):
    admin_id = insert_admin()
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "元太郎", "part_time", wage=1000)
    tok = make_session("admin", admin_id)
    r = client.put(f"/api/admin/shops/{shop_id}/staffs/{st}",
                   json={"name": "改名太郎", "hourly_wage": 1200, "is_resigned": 1},
                   headers=auth(tok))
    assert r.status_code == 200, r.get_json()
    row = dbmod.query_one(
        "SELECT name, hourly_wage, is_resigned FROM staffs WHERE id=?", (st,))
    assert row["name"] == "改名太郎"
    assert row["hourly_wage"] == 1200
    assert row["is_resigned"] == 1
    # 監査される
    assert dbmod.query_one(
        "SELECT id FROM audit_logs WHERE action='staff.update' AND target_id=?", (st,))


def test_admin_edit_invalid_role_400(client):
    admin_id = insert_admin()
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "太郎", "part_time")
    tok = make_session("admin", admin_id)
    r = client.put(f"/api/admin/shops/{shop_id}/staffs/{st}",
                   json={"role": "boss"}, headers=auth(tok))
    assert r.status_code == 400


def test_admin_edit_student_caps_hours(client):
    admin_id = insert_admin()
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "学生", "part_time", maxh=160)
    tok = make_session("admin", admin_id)
    r = client.put(f"/api/admin/shops/{shop_id}/staffs/{st}",
                   json={"role": "student", "max_hours_per_month": 160}, headers=auth(tok))
    assert r.status_code == 200
    row = dbmod.query_one("SELECT role, max_hours_per_month FROM staffs WHERE id=?", (st,))
    assert row["role"] == "student"
    assert row["max_hours_per_month"] == 80


def test_admin_edit_missing_staff_404(client):
    admin_id = insert_admin()
    shop_id = insert_shop()
    tok = make_session("admin", admin_id)
    r = client.put(f"/api/admin/shops/{shop_id}/staffs/99999",
                   json={"name": "x"}, headers=auth(tok))
    assert r.status_code == 404
