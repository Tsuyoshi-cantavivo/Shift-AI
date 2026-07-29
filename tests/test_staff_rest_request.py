"""スタッフの休希望（availability='rest'）が店舗に届くまでのテスト。

【背景】
  カレンダーで「休み」を選ぶとセルに「休み」と出て提出も成功するのに、店舗側に
  1件も届いていなかった。原因は2箇所:

    1. public/app.js の提出処理が `if (w.type === 'rest') return;` で捨てていた。
    2. src/app.py の POST /api/staff/requests に rest 分岐が無く、届いた場合でも
       終日（00:00〜23:59）ではなく店舗終業時刻までの希望として保存し、さらに
       重複チェックに掛けていた。

  正解は同一リポジトリ内にある: `_wish_times()`（設計書 §3 の表）と、管理者側の
  POST /api/shop/my-requests。スタッフ側だけがその扱いを受け取っていなかった。

  さらに GET /api/staff/requests が availability を返さないため、提出済み一覧で
  休希望が「00:00-23:59」＝24時間働きたい希望のように見えていた。
"""
import re
from pathlib import Path

import db as dbmod
from helpers import (
    auth, extract_js_function, insert_shop, insert_staff, make_session, run_js,
)

DAY = "2026-08-03"
OTHER = "2026-08-04"

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")


def _setup(client):
    """スタッフ1名と募集期間を持つ店舗を用意し、staff トークンを返す。"""
    shop_id = insert_shop()
    staff_id = insert_staff(shop_id, "pt001", "アルバイト")
    dbmod.execute(
        "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
        "VALUES (?,?,?,?,1)",
        (shop_id, "2026-08-01", "2026-08-31", "2099-12-31"))
    return shop_id, staff_id, make_session("staff", staff_id, shop_id)


def _shifts_of(staff_id):
    return dbmod.query_all(
        "SELECT start_datetime, end_datetime, availability, reason FROM shifts "
        "WHERE staff_id=? AND status='requested' ORDER BY start_datetime",
        (staff_id,))


# ============================================================
# サーバー: 休希望の保存
# ============================================================
class TestRestIsStoredAsFullDay:
    def test_rest_spans_the_whole_day(self, client):
        """設計書 §3 の表どおり 00:00:00〜23:59:59 で保存されること。"""
        _shop, staff_id, tok = _setup(client)
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{DAY}T00:00:00", "availability": "rest"}],
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["submitted"] == 1

        rows = _shifts_of(staff_id)
        assert len(rows) == 1, rows
        assert rows[0]["availability"] == "rest"
        assert rows[0]["start_datetime"] == f"{DAY}T00:00:00"
        assert rows[0]["end_datetime"] == f"{DAY}T23:59:59", \
            f"終日になっていない（店舗終業時刻で切られている）: {rows[0]['end_datetime']}"

    def test_rest_is_exempt_from_the_overlap_check(self, client):
        """終日 00:00〜23:59 は既存希望と必ず重なる。rest は重複判定から外すこと。

        外さないと、先に時間指定を出している日には休希望を出せなくなる
        （管理者側 /api/shop/my-requests は既にこの扱い）。
        """
        _shop, staff_id, tok = _setup(client)
        first = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{DAY}T17:00:00", "end_datetime": f"{DAY}T22:00:00"}],
        }, headers=auth(tok))
        assert first.get_json()["submitted"] == 1

        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{DAY}T00:00:00", "availability": "rest"}],
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["submitted"] == 1, f"休希望がスキップされた: {body}"
        assert body["skipped_overlap"] == 0, body

        avails = [x["availability"] for x in _shifts_of(staff_id)]
        assert "rest" in avails, avails

    def test_flexible_types_still_end_at_shop_closing_time(self, client):
        """rest 分岐を足しても any/morning/evening の挙動は変えないこと。"""
        _shop, staff_id, tok = _setup(client)
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{DAY}T09:00:00", "availability": "any"}],
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        rows = _shifts_of(staff_id)
        assert rows[0]["availability"] == "any"
        assert rows[0]["start_datetime"] == f"{DAY}T09:00:00"
        # 終日ではない（rest 分岐に吸い込まれていない）
        assert not rows[0]["end_datetime"].endswith("T23:59:59"), rows[0]

    def test_unknown_availability_is_rejected(self, client):
        """語彙外の値は保存しないこと（_WISH_AVAILABILITY_VALUES と揃える）。

        通すと shifts.availability に任意の文字列が入り、希望表・カレンダーの
        .wmark が知らないクラス名で描画される。
        """
        _shop, staff_id, tok = _setup(client)
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{DAY}T09:00:00", "availability": "<img src=x>"}],
        }, headers=auth(tok))
        assert r.status_code == 400, f"語彙外の availability が通った: {r.get_json()}"
        assert _shifts_of(staff_id) == [], "拒否したのに保存されている"


# ============================================================
# サーバー: 提出済み一覧が availability を返す
# ============================================================
class TestListReturnsAvailability:
    def test_get_includes_availability(self, client):
        """availability が無いと、一覧で休希望を時間帯希望と区別できない。"""
        _shop, _staff_id, tok = _setup(client)
        client.post("/api/staff/requests", json={
            "shifts": [
                {"start_datetime": f"{DAY}T00:00:00", "availability": "rest"},
                {"start_datetime": f"{OTHER}T17:00:00", "end_datetime": f"{OTHER}T22:00:00"},
            ],
        }, headers=auth(tok))
        r = client.get("/api/staff/requests", headers=auth(tok))
        assert r.status_code == 200
        reqs = r.get_json()["requests"]
        assert len(reqs) == 2, reqs
        for x in reqs:
            assert "availability" in x, f"availability が返っていない: {x}"
        by_day = {x["start_datetime"][:10]: x for x in reqs}
        assert by_day[DAY]["availability"] == "rest"
        assert by_day[OTHER]["availability"] in (None, "", "time"), by_day[OTHER]


# ============================================================
# フロント: 提出ペイロードと一覧表示
# ============================================================
_DEPS = ["hm", "esc", "_wtiShortTime", "badge"]


def _frag(*names):
    return [extract_js_function(APP_JS, n) for n in (*_DEPS, *names)]


class TestWishSubmitPayload:
    def _payload(self, state_json):
        return run_js(_frag("wishSubmitPayload"),
                      f"JSON.stringify(wishSubmitPayload({state_json}))")

    def test_rest_is_included_as_a_full_day_request(self):
        """今回の不具合の本体。休みが提出対象から落ちていた。"""
        import json
        out = json.loads(self._payload("{'2026-08-03': {type:'rest'}}"))
        assert len(out) == 1, f"休みが送られていない: {out}"
        assert out[0] == {
            "start_datetime": "2026-08-03T00:00:00",
            "end_datetime": "2026-08-03T23:59:59",
            "availability": "rest",
        }, out[0]

    def test_time_request_sends_the_entered_range(self):
        import json
        out = json.loads(self._payload(
            "{'2026-08-03': {type:'time',start:'2026-08-03T17:00',end:'2026-08-03T22:00'}}"))
        assert out[0] == {
            "start_datetime": "2026-08-03T17:00:00",
            "end_datetime": "2026-08-03T22:00:00",
        }, out[0]

    def test_flexible_request_sends_availability(self):
        import json
        out = json.loads(self._payload("{'2026-08-03': {type:'morning'}}"))
        assert out[0] == {"start_datetime": "2026-08-03T09:00:00", "availability": "morning"}, out[0]

    def test_empty_state_yields_nothing(self):
        assert self._payload("{}") == "[]"


class TestReqRangeText:
    def _text(self, r_json):
        return run_js(_frag("reqRangeText"), f"reqRangeText({r_json})")

    def test_rest_shows_as_a_day_off_not_a_24_hour_range(self):
        """「00:00-23:59」は24時間働きたい希望に見える。"""
        out = self._text("{availability:'rest',start_datetime:'2026-08-03T00:00:00',end_datetime:'2026-08-03T23:59:59'}")
        assert out == "休み", f"休希望が時間帯として表示されている: {out!r}"

    def test_flexible_types_show_their_labels(self):
        assert self._text("{availability:'any',start_datetime:'2026-08-03T09:00:00',end_datetime:'2026-08-03T22:00:00'}") == "いつでも"
        assert self._text("{availability:'morning',start_datetime:'2026-08-03T09:00:00',end_datetime:'2026-08-03T22:00:00'}") == "早番"
        assert self._text("{availability:'evening',start_datetime:'2026-08-03T09:00:00',end_datetime:'2026-08-03T22:00:00'}") == "遅番"

    def test_time_request_shows_the_actual_range(self):
        out = self._text("{availability:'time',start_datetime:'2026-08-03T17:00:00',end_datetime:'2026-08-03T22:00:00'}")
        assert out == "17:00-22:00", out

    def test_legacy_row_without_availability_shows_the_range(self):
        """availability 列が追加される前の行も壊れないこと。"""
        out = self._text("{start_datetime:'2026-08-03T17:00:00',end_datetime:'2026-08-03T22:00:00'}")
        assert out == "17:00-22:00", out


# ============================================================
# フロント: 設定した日を未設定に戻せる
# ============================================================
class TestClearADay:
    def test_clear_button_appears_only_when_something_is_set(self):
        """従来は全ボタンが値を代入するだけで、取り消す手段が無かった。"""
        set_html = run_js(_frag("wishPickerBodyHtml"), "wishPickerBodyHtml({type:'rest'})")
        assert "data-clear" in set_html, f"取り消しボタンが無い:\n{set_html}"
        unset_html = run_js(_frag("wishPickerBodyHtml"), "wishPickerBodyHtml(undefined)")
        assert "data-clear" not in unset_html, f"未設定なのに取り消しボタンがある:\n{unset_html}"

    def test_picker_wires_the_clear_button_to_delete_the_day(self):
        body = extract_js_function(APP_JS, "openWishPicker")
        assert "data-clear" in body, "取り消しボタンが配線されていない"
        assert "delete wishState[day]" in body, "取り消しても wishState から消えていない"


# ============================================================
# 配線
# ============================================================
class TestCallSitesUseTheHelpers:
    def test_submit_handler_uses_the_payload_builder(self):
        # 提出ハンドラは無名の addEventListener なのでソース全体から該当行を見る
        assert "wishSubmitPayload(wishState)" in APP_JS, \
            "提出処理が自前でペイロードを組み立てている"
        # 「なぜ直したか」を説明するコメントに同じ文字列が出るので、コメントを除いて探す
        code = re.sub(r"/\*.*?\*/", "", APP_JS, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        assert "=== 'rest') return" not in code, "休みを捨てる行が残っている"

    def test_my_reqs_uses_req_range_text(self):
        assert "reqRangeText(r)" in APP_JS, "提出済み一覧が availability を見ていない"
