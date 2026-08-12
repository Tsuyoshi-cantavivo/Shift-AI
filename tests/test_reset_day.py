"""1日分のシフトをやり直す（日別リセット）のテスト。

背景: AI生成は期間単位でしかやり直せない。1日だけ組み直したいときに期間ごと
作り直すと、他の日で手を入れた調整まで巻き戻る。店長の要望
「確定のまま残すが、やり直したい時もあるので1日ごとにリセットボタンがほしい」
に対応する。

このテストが守る不変:
  1. その日に始まるシフトを種類を問わず全部消す（手動確定も含む）
  2. 他の日は1件も触らない
  3. 他店舗のデータは触らない（テナント分離）
  4. 消す前に内訳を出せる（何が消えるか分からないまま押させない）
  5. スタッフの希望は wish_history に残る（shifts を消しても失われない）
"""
import db as dbmod
from helpers import insert_shop, insert_staff, insert_wish, make_session, auth

DAY, NEXT = "2026-08-03", "2026-08-04"


def _setup():
    shop_id = insert_shop()
    staff_id = insert_staff(shop_id, "P1", "パート", "part_time", 1100)
    return shop_id, staff_id


def _ins(shop_id, staff_id, day, st, en, status, reason):
    return dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{day}T{st}:00", f"{day}T{en}:00", status, reason),
    )["last_row_id"]


def _seed_day(shop_id, staff_id, day):
    """1日分に4種類そろえる。"""
    return {
        "manual": _ins(shop_id, staff_id, day, "20:00", "22:00", "confirmed", "手動追加"),
        "draft": _ins(shop_id, staff_id, day, "09:00", "13:00", "requested", "AIドラフト: 希望シフト"),
        "pending": _ins(shop_id, staff_id, day, "04:00", "06:00", "requested", "最低勤務時間未満のため調整待ち"),
        "wish": _ins(shop_id, staff_id, day, "14:00", "18:00", "requested", "スタッフ希望提出"),
    }


# ---------- 内訳の事前表示 ----------

def test_day_summary_breaks_down_by_kind(client):
    shop_id, staff_id = _setup()
    _seed_day(shop_id, staff_id, DAY)
    tok = make_session("shop", shop_id, shop_id)
    r = client.get(f"/api/shop/shifts/day-summary?date={DAY}", headers=auth(tok))
    assert r.status_code == 200
    d = r.get_json()
    assert d["total"] == 4
    assert d["manual"] == 1 and d["drafts"] == 1 and d["pending"] == 1 and d["other"] == 1


def test_day_summary_rejects_bad_date(client):
    shop_id, _ = _setup()
    tok = make_session("shop", shop_id, shop_id)
    assert client.get("/api/shop/shifts/day-summary?date=2026-13-99", headers=auth(tok)).status_code == 400
    assert client.get("/api/shop/shifts/day-summary", headers=auth(tok)).status_code == 400


# ---------- リセット本体 ----------

def test_reset_deletes_every_kind_including_manual(client):
    """手で入れた確定も消す。残すと「やり直し」にならない。"""
    shop_id, staff_id = _setup()
    ids = _seed_day(shop_id, staff_id, DAY)
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/reset-day", json={"date": DAY}, headers=auth(tok))
    assert r.status_code == 200
    d = r.get_json()
    assert d["deleted"] == 4
    assert d["manual"] == 1 and d["drafts"] == 1 and d["pending"] == 1 and d["other"] == 1
    for key, sid in ids.items():
        assert dbmod.query_one("SELECT id FROM shifts WHERE id=?", (sid,)) is None, f"{key} が残っている"


def test_reset_does_not_touch_other_days(client):
    shop_id, staff_id = _setup()
    _seed_day(shop_id, staff_id, DAY)
    keep = _seed_day(shop_id, staff_id, NEXT)
    tok = make_session("shop", shop_id, shop_id)
    client.post("/api/shop/shifts/reset-day", json={"date": DAY}, headers=auth(tok))
    for key, sid in keep.items():
        assert dbmod.query_one("SELECT id FROM shifts WHERE id=?", (sid,)) is not None, f"翌日の{key}が消えた"


def test_reset_does_not_touch_other_shops(client):
    shop_a, staff_a = _setup()
    shop_b = insert_shop(code="OTHER")
    staff_b = insert_staff(shop_b, "P9", "他店", "part_time", 1100)
    _seed_day(shop_a, staff_a, DAY)
    other = _seed_day(shop_b, staff_b, DAY)
    tok = make_session("shop", shop_a, shop_a)
    client.post("/api/shop/shifts/reset-day", json={"date": DAY}, headers=auth(tok))
    for key, sid in other.items():
        assert dbmod.query_one("SELECT id FROM shifts WHERE id=?", (sid,)) is not None, f"他店舗の{key}が消えた"


def test_reset_keeps_wish_history(client):
    """shifts を消してもスタッフの希望そのものは残る（再生成の入力にも使える）。"""
    shop_id, staff_id = _setup()
    insert_wish(shop_id, staff_id, DAY, "14:00", "18:00")
    _ins(shop_id, staff_id, DAY, "14:00", "18:00", "requested", "スタッフ希望提出")
    tok = make_session("shop", shop_id, shop_id)
    client.post("/api/shop/shifts/reset-day", json={"date": DAY}, headers=auth(tok))
    wishes = dbmod.query_all("SELECT id FROM wish_history WHERE shop_id=? AND start_datetime LIKE ?",
                             (shop_id, f"{DAY}%"))
    assert len(wishes) == 1, "希望履歴まで消えている"


def test_reset_on_empty_day_is_harmless(client):
    shop_id, _ = _setup()
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/reset-day", json={"date": DAY}, headers=auth(tok))
    assert r.status_code == 200
    assert r.get_json()["deleted"] == 0


def test_reset_rejects_bad_date(client):
    shop_id, _ = _setup()
    tok = make_session("shop", shop_id, shop_id)
    assert client.post("/api/shop/shifts/reset-day", json={"date": "2026-02-30"}, headers=auth(tok)).status_code == 400
    assert client.post("/api/shop/shifts/reset-day", json={}, headers=auth(tok)).status_code == 400


def test_reset_is_audited(client):
    shop_id, staff_id = _setup()
    _seed_day(shop_id, staff_id, DAY)
    tok = make_session("shop", shop_id, shop_id)
    client.post("/api/shop/shifts/reset-day", json={"date": DAY}, headers=auth(tok))
    logs = dbmod.query_all("SELECT action, detail FROM audit_logs WHERE action=?", ("shift.reset_day",))
    assert len(logs) == 1
    assert DAY in logs[0]["detail"] and "deleted=4" in logs[0]["detail"]


def test_staff_cannot_reset_day(client):
    """スタッフが他人のシフトごと消せてはいけない。"""
    shop_id, staff_id = _setup()
    _seed_day(shop_id, staff_id, DAY)
    tok = make_session("staff", staff_id, shop_id)
    r = client.post("/api/shop/shifts/reset-day", json={"date": DAY}, headers=auth(tok))
    assert r.status_code in (401, 403)
    assert dbmod.query_all("SELECT id FROM shifts WHERE shop_id=?", (shop_id,))
