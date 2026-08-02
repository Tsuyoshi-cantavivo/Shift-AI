"""tests/test_foreign_worker_role.py — 外国籍アルバイトロールと週28時間上限。

実行: ./.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v

設計書: docs/superpowers/specs/2026-08-02-foreign-worker-role-design.md

このロールに付く制約は週28hのみ。学生ロールの月80h上限・学生のみシフト禁止は
重ねない（月80hは週換算18.6hで、重ねると週28hの判定がほぼ発火しなくなるため）。
"""
import pytest

import shift_engine
from helpers import (
    insert_shop, insert_staff, insert_pattern, insert_wish, make_session, auth,
)

SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 31, "default_hourly_wage": 1100}

# 2026-08-01(土) 〜 08-14(金)。1日 09:00-18:00（休憩60分）＝実働8h。
# 週28h なら連続7日間に入れられるのは3本（24h）まで。4本目で32h になり超過する。
AUG = [f"2026-08-{d:02d}" for d in range(1, 15)]


class TestForeignWorkerRoleAccepted:
    def test_create_staff_with_foreign_worker_role(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "FW1", "name": "外国籍太郎",
            "password": "Fwk12345", "role": "foreign_worker",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)
        from db import query_one
        row = query_one("SELECT role FROM staffs WHERE id=?", (r.get_json()["id"],))
        assert row["role"] == "foreign_worker"

    def test_foreign_worker_is_not_capped_at_80_hours(self):
        """学生の月80h上限は重ねない（週28hだけが効く）。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "FW2", "外国籍花子", "foreign_worker", 1100, 0, 160)
        from db import query_one
        row = query_one("SELECT max_hours_per_month FROM staffs WHERE id=?", (sid,))
        assert row["max_hours_per_month"] == 160

    def test_unknown_role_is_still_rejected(self, client):
        """新しい値を足しても、でたらめなロールは拒否され続けること。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "XX1", "name": "不正", "password": "Xxx12345",
            "role": "not_a_role",
        }, headers=auth(tok))
        assert r.status_code == 400


class TestShiftsWeeklyCapAckColumn:
    def test_column_exists_with_default_zero(self):
        """承諾フラグの列があり、既定が0であること。"""
        from db import execute, query_one
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "FW3", "外国籍次郎", "foreign_worker")
        meta = execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,'confirmed')",
            (shop_id, sid, "2026-08-10T09:00:00", "2026-08-10T17:00:00"))
        row = query_one("SELECT weekly_cap_ack FROM shifts WHERE id=?", (meta["last_row_id"],))
        assert row["weekly_cap_ack"] == 0


def _shop_with_daily_pattern():
    """毎日 09:00-18:00 に1人必要な店（1日実働8h）。"""
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "終日", "09:00", "18:00", 1)
    return shop_id


def _spans_of(result, staff_id):
    return [(c["start"], c["end"], c.get("break") or 0)
            for c in result["confirmed"] if c["staff_id"] == staff_id]


class TestEngineWeeklyCap:
    def test_engine_does_not_place_over_28h_in_any_7_days(self):
        """外国籍アルバイトは、どの連続7日間も28hを超えないこと。"""
        from weekly_hours import minutes_by_day, exceeds_weekly_cap
        shop_id = _shop_with_daily_pattern()
        sid = insert_staff(shop_id, "FW10", "外国籍A", "foreign_worker", 1100, 0, 160)
        for d in AUG:
            insert_wish(shop_id, sid, d, "09:00", "18:00")
        r = shift_engine.auto_generate(shop_id, SETTINGS, AUG[0], AUG[-1])
        spans = _spans_of(r, sid)
        # 空虚なテストにしない: そもそも配置が起きていることを確かめる
        assert spans, "1件も配置されていない（週上限以前に何も置かれておらず検証にならない）"
        hit = exceeds_weekly_cap(minutes_by_day(spans))
        assert hit is None, f"週28hを超える配置が生成された: {hit}"

    def test_engine_counts_previous_month_shifts(self):
        """月をまたぐ7日窓のため、生成範囲の6日前からの確定シフトを見ること。

        7/29-7/31 に各8h（計24h）の確定シフトがあるとき、8/1 に8h入れると
        7/29〜8/4 の窓が32hになる。前月末を見ない実装ではこれを見逃す。
        """
        from db import execute
        shop_id = _shop_with_daily_pattern()
        sid = insert_staff(shop_id, "FW11", "外国籍B", "foreign_worker", 1100, 0, 160)
        for d in ("2026-07-29", "2026-07-30", "2026-07-31"):
            execute(
                "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, "
                "break_time_minutes, status) VALUES (?,?,?,?,60,'confirmed')",
                (shop_id, sid, f"{d}T09:00:00", f"{d}T18:00:00"))
        for d in AUG[:7]:
            insert_wish(shop_id, sid, d, "09:00", "18:00")
        r = shift_engine.auto_generate(shop_id, SETTINGS, "2026-08-01", "2026-08-07")
        early = [c for c in r["confirmed"] if c["staff_id"] == sid and c["start"][:10] <= "2026-08-04"]
        # 7/29〜7/31 で24h使っているので、7/29を含む窓に入る8/1〜8/4 には
        # 8hシフトを1本も入れられない（入れると32h）
        assert not early, f"前月末の実績を見ずに配置した: {[c['start'] for c in early]}"

    def test_other_roles_are_not_limited_by_28h(self):
        """part_time は週28hの制約を受けないこと（他ロールの挙動は変えない）。"""
        from weekly_hours import minutes_by_day, exceeds_weekly_cap
        shop_id = _shop_with_daily_pattern()
        sid = insert_staff(shop_id, "PT10", "パートA", "part_time", 1100, 0, 160)
        for d in AUG:
            insert_wish(shop_id, sid, d, "09:00", "18:00")
        r = shift_engine.auto_generate(shop_id, SETTINGS, AUG[0], AUG[-1])
        spans = _spans_of(r, sid)
        assert spans, "1件も配置されていない"
        hit = exceeds_weekly_cap(minutes_by_day(spans))
        assert hit is not None, \
            "パートにも週28hがかかっている（このロールは制約対象外のはず）"


class TestManualShiftWeeklyCap:
    """手動でシフトを入れるときの週28h検査と、店長の承諾による強行。"""

    def _setup(self):
        shop_id = insert_shop(settings=SETTINGS)
        sid = insert_staff(shop_id, "FW20", "外国籍C", "foreign_worker", 1100, 0, 160)
        tok = make_session("shop", shop_id, shop_id)
        return shop_id, sid, tok

    def _add_confirmed(self, shop_id, sid, day, start="09:00", end="18:00", brk=60):
        from db import execute
        return execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, "
            "break_time_minutes, status) VALUES (?,?,?,?,?,'confirmed')",
            (shop_id, sid, f"{day}T{start}:00", f"{day}T{end}:00", brk))["last_row_id"]

    def test_post_rejects_shift_over_28h(self, client):
        """既に24h入っている週に、さらに8hを手で足すと拒否されること。"""
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)   # 8h × 3 = 24h
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 400, r.get_data(as_text=True)
        body = r.get_json()
        assert body.get("weekly_cap_exceeded") is True
        # 店長が判断できるよう、どの7日間が何時間になるのかを返すこと
        assert body["detail"]["window_start"] <= "2026-08-13" <= body["detail"]["window_end"]
        assert body["detail"]["minutes"] == 32 * 60

    def test_post_allows_shift_within_28h(self, client):
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11"):
            self._add_confirmed(shop_id, sid, d)   # 16h
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)

    def test_post_with_confirmation_saves_and_marks_ack(self, client):
        """店長が承諾すれば保存でき、承諾した印が残ること。"""
        from db import query_one
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
            "weekly_cap_confirmed": True,
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)
        row = query_one("SELECT weekly_cap_ack FROM shifts WHERE id=?", (r.get_json()["id"],))
        assert row["weekly_cap_ack"] == 1, "承諾の印が残っていない"

    def test_within_cap_does_not_mark_ack(self, client):
        """上限内で保存したシフトに承諾の印が付かないこと（印の意味が薄まる）。"""
        from db import query_one
        shop_id, sid, tok = self._setup()
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
            "weekly_cap_confirmed": True,
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)
        row = query_one("SELECT weekly_cap_ack FROM shifts WHERE id=?", (r.get_json()["id"],))
        assert row["weekly_cap_ack"] == 0

    def test_put_rejects_extending_over_28h(self, client):
        """更新で長さを伸ばして超える場合も拒否されること。"""
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)
        target = self._add_confirmed(shop_id, sid, "2026-08-13", "09:00", "12:00", brk=0)  # 3h
        # 3h → 8h に伸ばすと窓が32hになる。
        # allow_confirmed_edit は確定シフトを直接編集するためのフラグ（:2726）。
        # これが無いと週上限より手前の locked チェックで 409 になり、
        # 週28hの検査に到達しない。
        r = client.put(f"/api/shop/shifts/{target}", json={
            "start_datetime": "2026-08-13T09:00:00", "end_datetime": "2026-08-13T18:00:00",
            "allow_confirmed_edit": True,
        }, headers=auth(tok))
        assert r.status_code == 400, r.get_data(as_text=True)
        assert r.get_json().get("weekly_cap_exceeded") is True

    def test_put_keeping_same_length_is_allowed(self, client):
        """自分自身を二重に数えていたら、変更なしの更新すら弾かれてしまう。"""
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)   # 24h
        target = self._add_confirmed(shop_id, sid, "2026-08-14", "09:00", "13:00", brk=0)  # 4h
        r = client.put(f"/api/shop/shifts/{target}", json={
            "start_datetime": "2026-08-14T09:00:00", "end_datetime": "2026-08-14T13:00:00",
            "allow_confirmed_edit": True,
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)

    def test_other_roles_are_not_blocked(self, client):
        """part_time は週28hで弾かれないこと。"""
        shop_id = insert_shop(settings=SETTINGS)
        sid = insert_staff(shop_id, "PT20", "パートB", "part_time", 1100, 0, 160)
        tok = make_session("shop", shop_id, shop_id)
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)
