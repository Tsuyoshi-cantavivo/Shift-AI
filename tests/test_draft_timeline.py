"""AIドラフトのタイムライン直接調整 API の回帰テスト。"""

import db as dbmod
from helpers import auth, insert_shop, insert_staff, make_session


MON = "2026-08-03"


def make_ai_draft():
    shop_id = insert_shop(code="DRAG1")
    staff_id = insert_staff(shop_id, "P1", "ドラフト担当")
    shift_id = dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, "
        "status, reason) VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{MON}T09:00:00", f"{MON}T17:00:00",
         "requested", "AIドラフト: 希望シフト"),
    )["last_row_id"]
    return shop_id, staff_id, shift_id, make_session("shop", shop_id, shop_id)


def get_shift(client, token, shift_id):
    response = client.get(f"/api/shop/shifts?start={MON}&end={MON}", headers=auth(token))
    assert response.status_code == 200
    return next(item for item in response.get_json()["shifts"] if item["id"] == shift_id)


def draft_update_payload(current, start="09:15", end="17:15"):
    return {
        "start_datetime": f"{MON}T{start}:00",
        "end_datetime": f"{MON}T{end}:00",
        "updated_at": current.get("updated_at") or current["created_at"],
    }


def test_draft_time_patch_updates_only_ai_draft_at_15_minute_boundary(client):
    _, _, shift_id, token = make_ai_draft()
    current = get_shift(client, token, shift_id)

    response = client.patch(
        f"/api/shop/shifts/{shift_id}/draft-time",
        json=draft_update_payload(current),
        headers=auth(token),
    )

    assert response.status_code == 200, response.get_json()
    saved = response.get_json()["shift"]
    assert saved["start_datetime"] == f"{MON}T09:15:00"
    assert saved["end_datetime"] == f"{MON}T17:15:00"
    assert saved["updated_at"]
    row = dbmod.query_one(
        "SELECT status, reason, start_datetime, end_datetime FROM shifts WHERE id=?", (shift_id,)
    )
    assert row == {
        "status": "requested",
        "reason": "AIドラフト: 希望シフト",
        "start_datetime": f"{MON}T09:15:00",
        "end_datetime": f"{MON}T17:15:00",
    }


def test_draft_time_patch_rejects_non_quarter_hour_and_preserves_draft(client):
    _, _, shift_id, token = make_ai_draft()
    current = get_shift(client, token, shift_id)

    response = client.patch(
        f"/api/shop/shifts/{shift_id}/draft-time",
        json=draft_update_payload(current, start="09:07", end="17:00"),
        headers=auth(token),
    )

    assert response.status_code == 400
    row = dbmod.query_one("SELECT start_datetime, end_datetime FROM shifts WHERE id=?", (shift_id,))
    assert row == {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"}


def test_draft_time_patch_rejects_stale_update(client):
    _, _, shift_id, token = make_ai_draft()
    current = get_shift(client, token, shift_id)
    first = client.patch(
        f"/api/shop/shifts/{shift_id}/draft-time",
        json=draft_update_payload(current),
        headers=auth(token),
    )
    assert first.status_code == 200

    stale = client.patch(
        f"/api/shop/shifts/{shift_id}/draft-time",
        json=draft_update_payload(current, start="09:30", end="17:30"),
        headers=auth(token),
    )

    assert stale.status_code == 409
    row = dbmod.query_one("SELECT start_datetime, end_datetime FROM shifts WHERE id=?", (shift_id,))
    assert row == {"start_datetime": f"{MON}T09:15:00", "end_datetime": f"{MON}T17:15:00"}


def test_draft_time_patch_rejects_confirmed_shift(client):
    shop_id = insert_shop(code="LOCK1")
    staff_id = insert_staff(shop_id, "P1", "確定担当")
    shift_id = dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{MON}T09:00:00", f"{MON}T17:00:00", "confirmed", "手動追加"),
    )["last_row_id"]
    token = make_session("shop", shop_id, shop_id)
    current = get_shift(client, token, shift_id)

    response = client.patch(
        f"/api/shop/shifts/{shift_id}/draft-time",
        json=draft_update_payload(current),
        headers=auth(token),
    )

    assert response.status_code == 409
    row = dbmod.query_one("SELECT start_datetime, end_datetime FROM shifts WHERE id=?", (shift_id,))
    assert row == {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"}
