"""tests/test_over_cap_boundaries.py - 超過判定の境界・リセット・認可のテスト。

UT レビューで指摘された境界ケースを固定する:
- ちょうど必要人数（cnt == req）は超過ではない（strict > 判定）
- 曜日別必要人数オーバーライドが効く
- フラグの 1→0 リセット
- 認可（staff は note/finalize 不可、非 admin は編集不可）
"""
import db as dbmod
from app import _flag_over_cap_shifts
from helpers import insert_shop, insert_staff, insert_pattern, make_session, auth

DAY = "2026-08-03"  # 月曜 → weekday index (Monday.weekday()=0 +1)%7 = 1


def _confirm(shop_id, staff_id, st="09:00", en="18:00"):
    return dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{DAY}T{st}:00", f"{DAY}T{en}:00", "confirmed", "確定"))["last_row_id"]


def test_exactly_at_capacity_is_not_over(client):
    shop_id = insert_shop()
    insert_pattern(shop_id, "通", "09:00", "18:00", 2)  # 必要2名
    a = insert_staff(shop_id, "E1", "A", "employee")
    b = insert_staff(shop_id, "E2", "B", "employee")
    _confirm(shop_id, a)
    _confirm(shop_id, b)  # ちょうど2名 = 上限内
    flagged = _flag_over_cap_shifts(shop_id, DAY + "T00:00:00", DAY + "T23:59:59")
    assert flagged == 0
    rows = dbmod.query_all("SELECT over_cap_flag FROM shifts WHERE shop_id=?", (shop_id,))
    assert all((r["over_cap_flag"] or 0) == 0 for r in rows)


def test_weekday_override_lowers_requirement(client):
    shop_id = insert_shop()
    pid = insert_pattern(shop_id, "通", "09:00", "18:00", 2)  # 通常は必要2名
    # 月曜(weekday=1)は必要1名に上書き
    dbmod.execute(
        "INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) "
        "VALUES (?,?,?,?)", (pid, shop_id, 1, 1))
    a = insert_staff(shop_id, "E1", "A", "employee")
    b = insert_staff(shop_id, "E2", "B", "employee")
    _confirm(shop_id, a)
    _confirm(shop_id, b)  # 必要1名の日に2名 → 超過
    flagged = _flag_over_cap_shifts(shop_id, DAY + "T00:00:00", DAY + "T23:59:59")
    assert flagged == 2


def test_flag_resets_from_one_to_zero(client):
    shop_id = insert_shop()
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # 必要1名
    a = insert_staff(shop_id, "E1", "A", "employee")
    b = insert_staff(shop_id, "E2", "B", "employee")
    id_a = _confirm(shop_id, a)
    id_b = _confirm(shop_id, b)  # 2名 → 超過
    assert _flag_over_cap_shifts(shop_id, DAY + "T00:00:00", DAY + "T23:59:59") == 2
    # b を削除 → a は超過解消
    dbmod.execute("DELETE FROM shifts WHERE id=?", (id_b,))
    assert _flag_over_cap_shifts(shop_id, DAY + "T00:00:00", DAY + "T23:59:59") == 0
    assert (dbmod.query_one("SELECT over_cap_flag FROM shifts WHERE id=?", (id_a,))["over_cap_flag"] or 0) == 0


def test_staff_cannot_patch_note(client):
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "A", "employee")
    sid = _confirm(shop_id, st)
    tok = make_session("staff", st, shop_id)
    r = client.patch(f"/api/shop/shifts/{sid}/note", json={"note": "x"}, headers=auth(tok))
    assert r.status_code == 403


def test_staff_cannot_finalize(client):
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "A", "employee")
    tok = make_session("staff", st, shop_id)
    r = client.post("/api/shop/shifts/finalize",
                    json={"start_date": DAY, "end_date": DAY}, headers=auth(tok))
    assert r.status_code == 403


def test_non_admin_cannot_edit_staff(client):
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "A", "employee")
    tok = make_session("shop", shop_id, shop_id)  # shop ロールでは admin 専用 API 不可
    r = client.put(f"/api/admin/shops/{shop_id}/staffs/{st}",
                   json={"name": "x"}, headers=auth(tok))
    assert r.status_code == 403


def test_finalize_missing_dates_400(client):
    shop_id = insert_shop()
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/finalize", json={}, headers=auth(tok))
    assert r.status_code == 400
