"""確定（finalize）が「調整待ち」を確定してしまう不具合の回帰テスト。

実データで見つかった症状:
  ミニストップイオンレイクタウン店の 8/1〜8/15 に、
  status='confirmed' / reason='最低勤務時間未満のため調整待ち' の行が13件あった。
  04:00-06:00 の2時間で、店舗の1日最低勤務時間（3時間）を下回る。

原因:
  エンジンは置けなかった希望を pending として status='requested' +
  「〜のため調整待ち」の理由で書き出す（src/shift_engine.py の
  pending.append 2箇所）。これは「人が調整しないと使えない」印であって
  シフトではない。ところが POST /api/shop/shifts/finalize は期間内の
  requested を無条件に全部 confirmed へ変換していたため、
  エンジン自身が「置けない」と判断した枠が確定シフトとして公開され、
  スタッフに確定通知まで飛んでいた。

このテストが守る不変:
  1. 「調整待ち」の行は確定されない（requested のまま残る）
  2. AIドラフトとスタッフ希望は従来どおり確定される
  3. エンジンが作る pending の理由は、finalize 側の判定条件と必ず一致する
     （文言だけ変えて判定が外れる、という壊れ方を防ぐ）
"""
import db as dbmod
import shift_engine
from utils import ENGINE_PENDING_REASON_SUFFIX, is_engine_pending_reason
from helpers import insert_shop, insert_staff, insert_pattern, insert_wish, make_session, auth

MON, TUE = "2026-08-03", "2026-08-04"
SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6, "default_hourly_wage": 1100}


def _setup(shop_settings=None):
    shop_id = insert_shop(settings=shop_settings or SETTINGS)
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)
    staff_id = insert_staff(shop_id, "E1", "社員", "employee", 2000)
    return shop_id, staff_id


def _insert(shop_id, staff_id, day, st, en, status, reason):
    return dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{day}T{st}:00", f"{day}T{en}:00", status, reason),
    )["last_row_id"]


# ---------- 1. 調整待ちは確定されない ----------

def test_finalize_does_not_confirm_engine_pending(client):
    """実データで見つかった行そのものを再現する。"""
    shop_id, staff_id = _setup()
    pending_id = _insert(shop_id, staff_id, MON, "04:00", "06:00",
                         "requested", "最低勤務時間未満のため調整待ち")
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/finalize",
                    json={"start_date": MON, "end_date": MON}, headers=auth(tok))
    assert r.status_code == 200

    row = dbmod.query_one("SELECT status, reason FROM shifts WHERE id=?", (pending_id,))
    assert row["status"] == "requested", (
        "エンジンが『置けない』と判断した枠が確定シフトとして公開されている")
    assert row["reason"] == "最低勤務時間未満のため調整待ち"


def test_finalize_does_not_confirm_flex_pending(client):
    """もう一方の pending 理由（配置可能な不足枠がなかった）も同じ扱い。"""
    shop_id, staff_id = _setup()
    pending_id = _insert(shop_id, staff_id, MON, "09:00", "18:00",
                         "requested", "配置可能な不足枠がなかったため調整待ち")
    tok = make_session("shop", shop_id, shop_id)
    client.post("/api/shop/shifts/finalize",
                json={"start_date": MON, "end_date": MON}, headers=auth(tok))
    assert dbmod.query_one("SELECT status FROM shifts WHERE id=?", (pending_id,))["status"] == "requested"


def test_finalize_does_not_notify_for_pending_only(client):
    """調整待ちしか無い期間を確定しても、スタッフに確定通知を飛ばさない。"""
    shop_id, staff_id = _setup()
    _insert(shop_id, staff_id, MON, "04:00", "06:00", "requested", "最低勤務時間未満のため調整待ち")
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/finalize",
                    json={"start_date": MON, "end_date": MON}, headers=auth(tok))
    assert r.get_json()["finalized"] == 0
    notifs = dbmod.query_all(
        "SELECT title FROM notifications WHERE shop_id=? AND staff_id=?", (shop_id, staff_id))
    assert all("確定" not in (n["title"] or "") for n in notifs)


# ---------- 2. 従来どおり確定されるもの ----------

def test_finalize_still_confirms_drafts_and_wishes(client):
    """AIドラフトとスタッフ希望は今までどおり確定される（退行防止）。"""
    shop_id, staff_id = _setup()
    draft_id = _insert(shop_id, staff_id, MON, "09:00", "18:00",
                       "requested", "AIドラフト: 希望シフト")
    wish_id = _insert(shop_id, staff_id, TUE, "09:00", "18:00",
                      "requested", "スタッフ希望提出")
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/finalize",
                    json={"start_date": MON, "end_date": TUE}, headers=auth(tok))
    assert r.get_json()["finalized"] == 2
    assert dbmod.query_one("SELECT status, reason FROM shifts WHERE id=?", (draft_id,))["status"] == "confirmed"
    # AIドラフトのプレフィックスは確定時に外れる（既存仕様）
    assert dbmod.query_one("SELECT reason FROM shifts WHERE id=?", (draft_id,))["reason"] == "希望シフト"
    assert dbmod.query_one("SELECT status FROM shifts WHERE id=?", (wish_id,))["status"] == "confirmed"


def test_finalize_mixed_confirms_only_the_ready_ones(client):
    """同じ期間にドラフトと調整待ちが混在しても、確定するのはドラフトだけ。"""
    shop_id, staff_id = _setup()
    draft_id = _insert(shop_id, staff_id, MON, "09:00", "18:00",
                       "requested", "AIドラフト: 希望シフト")
    pending_id = _insert(shop_id, staff_id, TUE, "04:00", "06:00",
                         "requested", "最低勤務時間未満のため調整待ち")
    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/finalize",
                    json={"start_date": MON, "end_date": TUE}, headers=auth(tok))
    assert r.get_json()["finalized"] == 1
    assert dbmod.query_one("SELECT status FROM shifts WHERE id=?", (draft_id,))["status"] == "confirmed"
    assert dbmod.query_one("SELECT status FROM shifts WHERE id=?", (pending_id,))["status"] == "requested"


# ---------- 3. エンジンと finalize の判定を結びつける ----------

def test_engine_pending_reasons_match_finalize_filter():
    """エンジンが作る pending の理由は、必ず finalize 側の判定に引っかかること。

    判定は理由の文言に依存しているので、エンジン側の文言だけ変わると
    「調整待ちが確定される」不具合が黙って戻る。両者をこのテストで結ぶ。
    """
    # 同じ日に2本の時間指定希望を出す。1本目は置けるが2本目は同日内重複で
    # 置けず pending へ回る（reason は「同日内重複のため調整待ち」）。
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "通", "09:00", "18:00", 1)
    staff_id = insert_staff(shop_id, "P1", "パート", "part_time", 1100)
    insert_wish(shop_id, staff_id, MON, "09:00", "13:00")
    insert_wish(shop_id, staff_id, MON, "14:00", "18:00")

    result = shift_engine.auto_generate(shop_id, SETTINGS, MON, MON)
    pending = result["pending"]
    assert pending, "この条件では pending が出るはず（テストの前提が壊れている）"
    for p in pending:
        assert p["status"] == "requested"
        assert is_engine_pending_reason(p["reason"]), (
            f"エンジンの pending 理由 {p['reason']!r} が finalize の判定"
            f"（末尾が {ENGINE_PENDING_REASON_SUFFIX!r}）から外れている")


def test_is_engine_pending_reason_boundaries():
    assert is_engine_pending_reason("最低勤務時間未満のため調整待ち")
    assert is_engine_pending_reason("配置可能な不足枠がなかったため調整待ち")
    assert not is_engine_pending_reason("AIドラフト: 希望シフト")
    assert not is_engine_pending_reason("スタッフ希望提出")
    assert not is_engine_pending_reason("手動追加")
    # 理由なし（NULL）は調整待ち扱いにしない＝従来どおり確定される
    assert not is_engine_pending_reason(None)
    assert not is_engine_pending_reason("")
