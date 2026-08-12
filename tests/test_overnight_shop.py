"""tests/test_overnight_shop.py - 日をまたぐ営業（04:00〜翌02:00）の店で報告された3件。

  1. 17日に「24-L」（24時からラスト）と入れると 18日のシフトになってしまう
  2. 22:00〜02:00 がシフト表に正しく出ない（終了が同日で保存され長さが負になる）
  3. 固定シフト「月曜 00:00〜02:00」が火曜0時にならない

いずれも「カレンダー日＝営業日」と決め打っていたことが原因。営業日は
その日の営業開始から翌日側の営業終了までの一続きで、8/17 の営業日は
8/18 02:00 まで続く。
"""
import json

import db as dbmod
import shift_engine
from helpers import insert_shop, insert_staff, insert_pattern, insert_fixed, make_session, auth

# 04:00〜翌02:00 の営業。cutoff は 120分。
OVERNIGHT_SETTINGS = {
    "min_daily_hours": 1,
    "max_daily_hours": 9,
    "shift_hours": {
        "bulk_mode": True,
        "bulk": {"start_time": "04:00", "end_time": "02:00", "is_closed": False},
        "days": {},
    },
}

MON = "2026-08-03"  # 月曜
TUE = "2026-08-04"


def _overnight_shop():
    """営業 04:00〜翌02:00、深夜帯までパターンがある店を作る。"""
    shop_id = insert_shop(settings=OVERNIGHT_SETTINGS)
    insert_pattern(shop_id, "早朝", "04:00", "07:00", 1)
    insert_pattern(shop_id, "昼", "07:00", "19:00", 1)
    insert_pattern(shop_id, "夜", "19:00", "02:00", 1)
    return shop_id


class TestFixedShiftOnBusinessDay:
    """課題3: 固定シフト「月曜 00:00〜02:00」は火曜0時に置かれること。"""

    def test_midnight_fixed_shift_lands_on_next_calendar_day(self):
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "深夜太郎", role="part_time")
        insert_fixed(staff_id, 1, "00:00", "02:00")  # 1=月曜

        res = shift_engine.auto_generate(shop_id, OVERNIGHT_SETTINGS, MON, MON)
        mine = [s for s in res["confirmed"]
                if s["staff_id"] == staff_id and s["reason"].startswith("固定シフト")]
        assert len(mine) == 1, res["confirmed"]
        # 月曜の営業日の深夜帯 = 火曜 00:00〜02:00
        assert mine[0]["start"] == f"{TUE}T00:00:00"
        assert mine[0]["end"] == f"{TUE}T02:00:00"

    def test_daytime_fixed_shift_is_unchanged(self):
        """日中の固定シフトは従来どおり当日に置く（営業日変換の巻き添えにしない）。"""
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P2", "昼太郎", role="part_time")
        insert_fixed(staff_id, 1, "09:00", "17:00")

        res = shift_engine.auto_generate(shop_id, OVERNIGHT_SETTINGS, MON, MON)
        mine = [s for s in res["confirmed"]
                if s["staff_id"] == staff_id and s["reason"].startswith("固定シフト")]
        assert len(mine) == 1
        assert mine[0]["start"] == f"{MON}T09:00:00"
        assert mine[0]["end"] == f"{MON}T17:00:00"

    def test_overnight_fixed_shift_ends_next_day(self):
        """22:00〜02:00 の固定シフトは当日22時〜翌2時（従来の日またぎ扱いを維持）。"""
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P3", "夜太郎", role="part_time")
        insert_fixed(staff_id, 1, "22:00", "02:00")

        res = shift_engine.auto_generate(shop_id, OVERNIGHT_SETTINGS, MON, MON)
        mine = [s for s in res["confirmed"]
                if s["staff_id"] == staff_id and s["reason"].startswith("固定シフト")]
        assert len(mine) == 1
        assert mine[0]["start"] == f"{MON}T22:00:00"
        assert mine[0]["end"] == f"{TUE}T02:00:00"

    def test_daytime_shop_keeps_calendar_day(self):
        """日中営業（09:00-22:00）の店では 00:00〜02:00 の固定はそのまま当日。

        営業日変換は日をまたぐ店だけの話。ここが動くと既存店の配置が全部ずれる。
        """
        settings = {"min_daily_hours": 1, "shift_hours": {
            "bulk_mode": True,
            "bulk": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
            "days": {}}}
        shop_id = insert_shop(settings=settings)
        insert_pattern(shop_id, "終日", "00:00", "23:00", 1)
        staff_id = insert_staff(shop_id, "P1", "太郎", role="part_time")
        insert_fixed(staff_id, 1, "00:00", "02:00")

        res = shift_engine.auto_generate(shop_id, settings, MON, MON)
        mine = [s for s in res["confirmed"]
                if s["staff_id"] == staff_id and s["reason"].startswith("固定シフト")]
        assert len(mine) == 1
        assert mine[0]["start"] == f"{MON}T00:00:00"
        assert mine[0]["end"] == f"{MON}T02:00:00"


class TestWishImportMidnight:
    """課題1: 17日の「24-L」を17日の希望として取り込むこと。"""

    def _bulk(self, client, token, wishes):
        return client.post("/api/shop/wishes/bulk",
                           data=json.dumps({"wishes": wishes}),
                           content_type="application/json", headers=auth(token))

    def test_extended_hour_wish_belongs_to_the_written_day(self, client):
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "深夜太郎")
        token = make_session("shop", shop_id, shop_id)

        r = self._bulk(client, token, [{
            "staff_id": staff_id, "date": "2026-08-17", "availability": "time",
            "start": "24:00", "end": "26:00", "raw": "8/17 24-L",
        }])
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["created"] == 1

        row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM wish_history WHERE staff_id=?",
            (staff_id,))
        # 実時刻は 8/18 の 0〜2時。だが「8/17 の営業日」の希望である
        assert row["start_datetime"] == "2026-08-18T00:00:00"
        assert row["end_datetime"] == "2026-08-18T02:00:00"

        got = client.get("/api/shop/wishes?start=2026-08-01&end=2026-08-31",
                         headers=auth(token)).get_json()
        assert got["wishes"][0]["business_date"] == "2026-08-17"

    def test_zero_hour_wish_is_read_as_the_same_business_day(self, client):
        """LLM が 24:00 を 00:00 と書いても、書かれた日の深夜帯として扱う。"""
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "深夜太郎")
        token = make_session("shop", shop_id, shop_id)

        r = self._bulk(client, token, [{
            "staff_id": staff_id, "date": "2026-08-17", "availability": "time",
            "start": "00:00", "end": "02:00", "raw": "8/17 24-L",
        }])
        assert r.status_code == 200, r.get_json()
        row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM wish_history WHERE staff_id=?",
            (staff_id,))
        assert row["start_datetime"] == "2026-08-18T00:00:00"
        assert row["end_datetime"] == "2026-08-18T02:00:00"

    def test_overnight_wish_ends_next_day(self, client):
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "夜太郎")
        token = make_session("shop", shop_id, shop_id)

        self._bulk(client, token, [{
            "staff_id": staff_id, "date": "2026-08-17", "availability": "time",
            "start": "22:00", "end": "02:00", "raw": "8/17 22-2",
        }])
        row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM wish_history WHERE staff_id=?",
            (staff_id,))
        assert row["start_datetime"] == "2026-08-17T22:00:00"
        assert row["end_datetime"] == "2026-08-18T02:00:00"

    def test_flexible_wish_end_is_not_before_start(self, client):
        """「いつでも」希望の終了は営業終了(02:00)。同日で組むと負の長さになる。"""
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "太郎")
        token = make_session("shop", shop_id, shop_id)

        self._bulk(client, token, [{
            "staff_id": staff_id, "date": "2026-08-17", "availability": "any",
            "start": None, "end": None, "raw": "8/17 いつでも",
        }])
        row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM wish_history WHERE staff_id=?",
            (staff_id,))
        assert row["start_datetime"] == "2026-08-17T09:00:00"
        assert row["end_datetime"] == "2026-08-18T02:00:00"


class TestShiftSpanValidation:
    """課題2: 終了が開始より前のシフトを保存させない。"""

    def test_post_rejects_backwards_span(self, client):
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "夜太郎")
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/shifts", data=json.dumps({
            "staff_id": staff_id,
            "start_datetime": "2026-08-17T22:00:00",
            "end_datetime": "2026-08-17T02:00:00",  # 日付を翌日にし忘れた
        }), content_type="application/json", headers=auth(token))
        assert r.status_code == 400
        assert "終了は開始より後" in (r.get_json().get("error") or "")
        assert dbmod.query_all("SELECT id FROM shifts WHERE shop_id=?", (shop_id,)) == []

    def test_post_accepts_overnight_span(self, client):
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "夜太郎")
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/shifts", data=json.dumps({
            "staff_id": staff_id,
            "start_datetime": "2026-08-17T22:00:00",
            "end_datetime": "2026-08-18T02:00:00",
        }), content_type="application/json", headers=auth(token))
        assert r.status_code == 200, r.get_json()
        row = dbmod.query_one("SELECT status, start_datetime, end_datetime FROM shifts "
                              "WHERE shop_id=?", (shop_id,))
        assert row["status"] == "confirmed"
        assert row["end_datetime"] == "2026-08-18T02:00:00"

    def test_put_rejects_backwards_span(self, client):
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "夜太郎")
        token = make_session("shop", shop_id, shop_id)
        sid = dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,?)",
            (shop_id, staff_id, "2026-08-17T22:00:00", "2026-08-18T02:00:00", "requested"),
        )["last_row_id"]

        r = client.put(f"/api/shop/shifts/{sid}", data=json.dumps({
            "staff_id": staff_id,
            "start_datetime": "2026-08-17T22:00:00",
            "end_datetime": "2026-08-17T02:00:00",
            "status": "confirmed",
        }), content_type="application/json", headers=auth(token))
        assert r.status_code == 400
        row = dbmod.query_one("SELECT end_datetime FROM shifts WHERE id=?", (sid,))
        assert row["end_datetime"] == "2026-08-18T02:00:00"  # 元のまま


class TestShiftListBusinessDate:
    """シフト一覧が営業日を返し、月末の深夜帯を落とさないこと。"""

    def test_business_date_of_late_night_shift(self, client):
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "夜太郎")
        token = make_session("shop", shop_id, shop_id)
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,?)",
            (shop_id, staff_id, "2026-09-01T00:00:00", "2026-09-01T02:00:00", "confirmed"))

        got = client.get("/api/shop/shifts?start=2026-08-01&end=2026-08-31",
                         headers=auth(token)).get_json()
        assert got["day_cutoff_min"] == 120
        assert len(got["shifts"]) == 1  # 期間を1日広げて拾えている
        assert got["shifts"][0]["business_date"] == "2026-08-31"

    def test_next_period_late_night_is_excluded(self, client):
        """広げた1日ぶんでも、営業日が期間外なら返さない。"""
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "昼太郎")
        token = make_session("shop", shop_id, shop_id)
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,?)",
            (shop_id, staff_id, "2026-09-01T09:00:00", "2026-09-01T17:00:00", "confirmed"))

        got = client.get("/api/shop/shifts?start=2026-08-01&end=2026-08-31",
                         headers=auth(token)).get_json()
        assert got["shifts"] == []

    def test_first_day_excludes_previous_business_day(self, client):
        """期間の初日 00:00〜02:00 は前日の営業日。期間に混ぜて返さない。"""
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "夜太郎")
        token = make_session("shop", shop_id, shop_id)
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,?)",
            (shop_id, staff_id, "2026-08-01T01:00:00", "2026-08-01T02:00:00", "confirmed"))

        got = client.get("/api/shop/shifts?start=2026-08-01&end=2026-08-31",
                         headers=auth(token)).get_json()
        assert got["shifts"] == []

    def test_daytime_shop_business_date_is_calendar_date(self, client):
        shop_id = insert_shop(settings={"shift_hours": {
            "bulk_mode": True,
            "bulk": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
            "days": {}}})
        insert_pattern(shop_id, "昼", "09:00", "22:00", 1)
        staff_id = insert_staff(shop_id, "P1", "太郎")
        token = make_session("shop", shop_id, shop_id)
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,?)",
            (shop_id, staff_id, "2026-08-17T09:00:00", "2026-08-17T17:00:00", "confirmed"))

        got = client.get("/api/shop/shifts?start=2026-08-01&end=2026-08-31",
                         headers=auth(token)).get_json()
        assert got["day_cutoff_min"] == 0
        assert got["shifts"][0]["business_date"] == "2026-08-17"


class TestFinalizeAndResetOnBusinessDay:
    """確定・やり直しが、シフト表と同じ範囲（営業日）を対象にすること。"""

    def _draft(self, shop_id, staff_id, start, end):
        return dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, staff_id, start, end, "requested", "AIドラフト: 夜"),
        )["last_row_id"]

    def test_finalize_covers_last_business_day_midnight(self, client):
        """期間最終日の深夜帯（翌カレンダー日 0時台）も確定すること。

        ここが漏れると、シフト表には 8/31 の枠として出ているのに
        「確定」を押しても requested のまま残る（＝確定しない）。
        """
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "夜太郎")
        token = make_session("shop", shop_id, shop_id)
        sid = self._draft(shop_id, staff_id, "2026-09-01T00:00:00", "2026-09-01T02:00:00")

        r = client.post("/api/shop/shifts/finalize",
                        data=json.dumps({"start_date": "2026-08-01", "end_date": "2026-08-31"}),
                        content_type="application/json", headers=auth(token))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["finalized"] == 1
        assert dbmod.query_one("SELECT status FROM shifts WHERE id=?", (sid,))["status"] == "confirmed"

    def test_finalize_leaves_next_business_day(self, client):
        """翌営業日（9/1 の日中）は期間外なので確定しない。"""
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "昼太郎")
        token = make_session("shop", shop_id, shop_id)
        sid = self._draft(shop_id, staff_id, "2026-09-01T09:00:00", "2026-09-01T17:00:00")

        r = client.post("/api/shop/shifts/finalize",
                        data=json.dumps({"start_date": "2026-08-01", "end_date": "2026-08-31"}),
                        content_type="application/json", headers=auth(token))
        assert r.get_json()["finalized"] == 0
        assert dbmod.query_one("SELECT status FROM shifts WHERE id=?", (sid,))["status"] == "requested"

    def test_reset_day_matches_the_day_view(self, client):
        """「この日をやり直す」は、その営業日に見えている枠だけを消すこと。"""
        shop_id = _overnight_shop()
        s1 = insert_staff(shop_id, "P1", "夜太郎")
        s2 = insert_staff(shop_id, "P2", "翌日太郎")
        token = make_session("shop", shop_id, shop_id)
        late = self._draft(shop_id, s1, "2026-08-18T00:00:00", "2026-08-18T02:00:00")  # 8/17の営業日
        next_day = self._draft(shop_id, s2, "2026-08-18T09:00:00", "2026-08-18T17:00:00")  # 8/18の営業日

        summary = client.get("/api/shop/shifts/day-summary?date=2026-08-17",
                             headers=auth(token)).get_json()
        assert summary["total"] == 1

        r = client.post("/api/shop/shifts/reset-day",
                        data=json.dumps({"date": "2026-08-17"}),
                        content_type="application/json", headers=auth(token))
        assert r.get_json()["deleted"] == 1
        assert dbmod.query_one("SELECT id FROM shifts WHERE id=?", (late,)) is None
        assert dbmod.query_one("SELECT id FROM shifts WHERE id=?", (next_day,)) is not None


class TestShortageCountsLateNight:
    """深夜帯を埋めたシフトが「不足」に数えられ続けないこと。"""

    def test_midnight_shift_covers_the_business_day(self, client):
        shop_id = _overnight_shop()
        staff_id = insert_staff(shop_id, "P1", "深夜太郎")
        token = make_session("shop", shop_id, shop_id)
        # 8/17 の営業日の深夜帯（実時刻は 8/18 0〜2時）をちょうど埋める
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,?)",
            (shop_id, staff_id, "2026-08-18T00:00:00", "2026-08-18T02:00:00", "confirmed"))

        got = client.get("/api/shop/shortage?start=2026-08-17&end=2026-08-17",
                         headers=auth(token)).get_json()
        # 24:00〜26:00（拡張分 1440〜1560）に重なる不足区間が残っていないこと。
        # 区間はマージされるので start_min だけを見ると 19:00 始まりの区間に
        # 埋もれて検出できない（重なりで判定する）。
        late = [g for g in got["shortage_unique"]
                if g["date"] == "2026-08-17" and g["start_min"] < 1560 and g["end_min"] > 1440]
        assert late == [], got["shortage_unique"]
