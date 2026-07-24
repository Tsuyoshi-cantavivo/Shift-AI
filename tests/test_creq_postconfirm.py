"""tests/test_creq_postconfirm.py - 確定後の変更申請フロー（承認/却下）テスト。

- 却下でスタッフに通知が飛ぶ
- change 承認で確定シフトの時間が更新される
- 承認で必要人数超過が生じた場合、対象日の確定シフトへ over_cap_flag が付く
"""
import db as dbmod
from helpers import insert_shop, insert_staff, insert_pattern, make_session, auth

DAY = "2026-08-03"


def _confirmed(shop_id, staff_id, st, en, reason="確定"):
    return dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{DAY}T{st}:00", f"{DAY}T{en}:00", "confirmed", reason))["last_row_id"]


def _creq(shop_id, staff_id, rtype, shift_id=None, ds=None, de=None):
    return dbmod.execute(
        "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, desired_start, desired_end, status) "
        "VALUES (?,?,?,?,?,?, 'pending')",
        (shop_id, staff_id, shift_id, rtype, ds, de))["last_row_id"]


def test_reject_notifies_staff(client):
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "社員", "employee")
    sid = _confirmed(shop_id, st, "09:00", "18:00")
    crid = _creq(shop_id, st, "change", shift_id=sid,
                 ds=f"{DAY}T10:00:00", de=f"{DAY}T18:00:00")
    before = dbmod.query_one(
        "SELECT count(*) c FROM notifications WHERE staff_id=?", (st,))["c"]
    tok = make_session("shop", shop_id, shop_id)
    r = client.put(f"/api/shop/change-requests/{crid}",
                   json={"action": "reject"}, headers=auth(tok))
    assert r.status_code == 200
    after = dbmod.query_one(
        "SELECT count(*) c FROM notifications WHERE staff_id=?", (st,))["c"]
    assert after == before + 1
    assert dbmod.query_one("SELECT status FROM change_requests WHERE id=?", (crid,))["status"] == "rejected"


def test_approve_change_updates_shift(client):
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "社員", "employee")
    sid = _confirmed(shop_id, st, "09:00", "18:00")
    crid = _creq(shop_id, st, "change", shift_id=sid,
                 ds=f"{DAY}T10:00:00", de=f"{DAY}T17:00:00")
    tok = make_session("shop", shop_id, shop_id)
    r = client.put(f"/api/shop/change-requests/{crid}",
                   json={"action": "approve"}, headers=auth(tok))
    assert r.status_code == 200
    row = dbmod.query_one("SELECT start_datetime, end_datetime FROM shifts WHERE id=?", (sid,))
    assert row["start_datetime"].endswith("T10:00:00")
    assert row["end_datetime"].endswith("T17:00:00")


def test_approve_add_flags_over_cap(client):
    shop_id = insert_shop()
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # 必要1名
    a = insert_staff(shop_id, "E1", "社員1", "employee")
    b = insert_staff(shop_id, "E2", "社員2", "employee")
    _confirmed(shop_id, a, "09:00", "18:00")  # 既に1名確定
    # b の追加申請（同枠 → 承認で超過）
    crid = _creq(shop_id, b, "add", shift_id=None,
                 ds=f"{DAY}T09:00:00", de=f"{DAY}T18:00:00")
    tok = make_session("shop", shop_id, shop_id)
    r = client.put(f"/api/shop/change-requests/{crid}",
                   json={"action": "approve"}, headers=auth(tok))
    assert r.status_code == 200
    # 追加された b のシフトに over_cap_flag が付く
    flagged = dbmod.query_all(
        "SELECT over_cap_flag FROM shifts WHERE shop_id=? AND staff_id=?", (shop_id, b))
    assert flagged and (flagged[0]["over_cap_flag"] or 0) == 1
