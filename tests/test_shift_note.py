"""tests/test_shift_note.py - シフト店長メモ PATCH のテスト。"""
import db as dbmod
from helpers import insert_shop, insert_staff, make_session, auth

DAY = "2026-08-03"


def _confirmed_shift(shop_id, staff_id):
    return dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{DAY}T09:00:00", f"{DAY}T18:00:00", "confirmed", "手動"))["last_row_id"]


def test_set_and_clear_shift_note(client):
    shop_id = insert_shop()
    st = insert_staff(shop_id, "E1", "社員", "employee")
    sid = _confirmed_shift(shop_id, st)
    tok = make_session("shop", shop_id, shop_id)
    r = client.patch(f"/api/shop/shifts/{sid}/note",
                     json={"note": "新人研修のため増員"}, headers=auth(tok))
    assert r.status_code == 200
    assert dbmod.query_one("SELECT note FROM shifts WHERE id=?", (sid,))["note"] == "新人研修のため増員"
    # 空文字でクリア → NULL
    r2 = client.patch(f"/api/shop/shifts/{sid}/note", json={"note": ""}, headers=auth(tok))
    assert r2.status_code == 200
    assert dbmod.query_one("SELECT note FROM shifts WHERE id=?", (sid,))["note"] in (None, "")


def test_note_on_missing_shift_404(client):
    shop_id = insert_shop()
    tok = make_session("shop", shop_id, shop_id)
    r = client.patch("/api/shop/shifts/99999/note", json={"note": "x"}, headers=auth(tok))
    assert r.status_code == 404


def test_note_scoped_to_own_shop(client):
    shop1 = insert_shop(code="S1")
    shop2 = insert_shop(code="S2")
    st = insert_staff(shop1, "E1", "社員", "employee")
    sid = _confirmed_shift(shop1, st)
    tok2 = make_session("shop", shop2, shop2)
    # 別店舗のトークンでは対象外（404）
    r = client.patch(f"/api/shop/shifts/{sid}/note", json={"note": "x"}, headers=auth(tok2))
    assert r.status_code == 404
