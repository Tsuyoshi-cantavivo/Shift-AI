"""tests/test_over_cap_finalize.py - 必要人数超過確定の可視化テスト。

- shifts に over_cap_flag / note 列があること
- 確定(finalize)時に必要人数を超える枠に重なる確定シフトへ over_cap_flag=1 が付くこと
- 超過が無い場合はフラグ 0・over_cap=0
- CSVエクスポートに 超過 / メモ 列が出ること
"""
import db as dbmod
from helpers import insert_shop, insert_staff, insert_pattern, make_session, auth

DAY = "2026-08-03"  # 月曜
SETTINGS = {"default_hourly_wage": 1100}


def _draft(shop_id, staff_id, st, en, reason="AIドラフト: テスト"):
    dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{DAY}T{st}:00", f"{DAY}T{en}:00", "requested", reason))


def test_shifts_has_over_cap_columns(client):
    cols = {r["name"] for r in dbmod.query_all("PRAGMA table_info(shifts)")}
    assert "over_cap_flag" in cols
    assert "note" in cols


def test_finalize_flags_over_cap_shifts(client):
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # 必要1名
    a = insert_staff(shop_id, "E1", "社員1", "employee", 2000)
    b = insert_staff(shop_id, "E2", "社員2", "employee", 2000)
    _draft(shop_id, a, "09:00", "18:00")
    _draft(shop_id, b, "09:00", "18:00")  # 同一枠に2人目 → 超過
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/finalize",
                    json={"start_date": DAY, "end_date": DAY}, headers=auth(tok))
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["finalized"] == 2
    assert data["over_cap"] == 2
    flags = [row["over_cap_flag"] for row in dbmod.query_all(
        "SELECT over_cap_flag FROM shifts WHERE shop_id=? AND status='confirmed'", (shop_id,))]
    assert flags == [1, 1]
    # reason はスタッフ側にも返るため超過テキストは書き込まない（over_cap_flag のみ）
    reasons = [row["reason"] for row in dbmod.query_all(
        "SELECT reason FROM shifts WHERE shop_id=? AND status='confirmed'", (shop_id,))]
    assert all("必要人数超過" not in (rs or "") for rs in reasons)


def test_finalize_no_over_cap(client):
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # 必要1名
    a = insert_staff(shop_id, "E1", "社員1", "employee", 2000)
    _draft(shop_id, a, "09:00", "18:00")  # 1名だけ → 超過なし
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/finalize",
                    json={"start_date": DAY, "end_date": DAY}, headers=auth(tok))
    assert r.status_code == 200
    assert r.get_json()["over_cap"] == 0
    row = dbmod.query_one(
        "SELECT over_cap_flag FROM shifts WHERE shop_id=? AND status='confirmed'", (shop_id,))
    assert (row["over_cap_flag"] or 0) == 0


def test_staff_api_does_not_expose_flag_or_note(client):
    """スタッフ向け API は over_cap_flag / note を返さない（店長のみ）。"""
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)
    a = insert_staff(shop_id, "E1", "社員1", "employee", 2000)
    b = insert_staff(shop_id, "E2", "社員2", "employee", 2000)
    _draft(shop_id, a, "09:00", "18:00")
    _draft(shop_id, b, "09:00", "18:00")
    shop_tok = make_session("shop", shop_id, shop_id)
    client.post("/api/shop/shifts/finalize",
                json={"start_date": DAY, "end_date": DAY}, headers=auth(shop_tok))
    sid = dbmod.query_one("SELECT id FROM shifts WHERE shop_id=? LIMIT 1", (shop_id,))["id"]
    client.patch(f"/api/shop/shifts/{sid}/note", json={"note": "内部メモ"}, headers=auth(shop_tok))
    staff_tok = make_session("staff", a, shop_id)
    r = client.get(f"/api/staff/shifts?start={DAY}&end={DAY}", headers=auth(staff_tok))
    assert r.status_code == 200
    shifts = r.get_json()["shifts"]
    assert shifts, "スタッフに確定シフトが見えるべき"
    for s in shifts:
        assert "over_cap_flag" not in s
        assert "note" not in s
        assert "必要人数超過" not in (s.get("reason") or "")


def test_export_includes_flag_and_note(client):
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)
    a = insert_staff(shop_id, "E1", "社員1", "employee", 2000)
    b = insert_staff(shop_id, "E2", "社員2", "employee", 2000)
    _draft(shop_id, a, "09:00", "18:00")
    _draft(shop_id, b, "09:00", "18:00")
    tok = make_session("shop", shop_id, shop_id)
    client.post("/api/shop/shifts/finalize",
                json={"start_date": DAY, "end_date": DAY}, headers=auth(tok))
    # メモも1件付ける
    sid = dbmod.query_one("SELECT id FROM shifts WHERE shop_id=? LIMIT 1", (shop_id,))["id"]
    client.patch(f"/api/shop/shifts/{sid}/note", json={"note": "研修増員"}, headers=auth(tok))
    r = client.get(f"/api/shop/shifts/export?start={DAY}&end={DAY}", headers=auth(tok))
    assert r.status_code == 200
    text = r.data.decode("utf-8")
    header = text.splitlines()[0]
    assert "超過" in header and "メモ" in header
    assert "研修増員" in text
