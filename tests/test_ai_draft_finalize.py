"""tests test_ai_draft_finalize.py - AIドラフト保存と一括確定のテスト。"""
import pytest

import db as dbmod
import shift_engine
from helpers import (
    insert_shop, insert_staff, insert_pattern, insert_fixed,
    insert_wish, make_session, auth,
)

MON, TUE = "2026-08-03", "2026-08-04"
SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6, "default_hourly_wage": 1100}


class TestAIDraftMode:
    """AI生成のドラフトモードのテスト。"""

    def _setup_shop(self):
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        emp_id = insert_staff(shop_id, "E1", "社員", "employee", 2000)
        insert_wish(shop_id, emp_id, MON, "09:00", "18:00")
        return shop_id, emp_id

    def test_ai_generate_draft_saves_as_requested(self, client):
        """AI生成（draft=True）は status='requested', reason='AIドラフト' で保存。"""
        shop_id, emp_id = self._setup_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": MON, "draft": True,
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["draft"] is True
        assert body["confirmed_count"] > 0
        # shifts は requested で保存されている
        rows = dbmod.query_all(
            "SELECT status, reason FROM shifts WHERE shop_id=? AND staff_id=?",
            (shop_id, emp_id))
        assert any(r["status"] == "requested" and "AIドラフト" in (r["reason"] or "") for r in rows)
        # 通知は飛んでいない
        notifs = dbmod.query_all(
            "SELECT * FROM notifications WHERE shop_id=? AND staff_id=?", (shop_id, emp_id))
        assert all("確定" not in (n.get("title") or "") for n in notifs)

    def test_ai_generate_immediate_finalize_sends_notification(self, client):
        """AI生成（draft=False）は即座に confirmed + 通知送信。"""
        shop_id, emp_id = self._setup_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": MON, "draft": False,
        }, headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert body["draft"] is False
        # confirmed で保存
        rows = dbmod.query_all(
            "SELECT status FROM shifts WHERE shop_id=? AND staff_id=? AND status='confirmed'",
            (shop_id, emp_id))
        assert len(rows) > 0
        # 通知が飛んでいる
        notifs = dbmod.query_all(
            "SELECT * FROM notifications WHERE shop_id=? AND staff_id=? AND title LIKE '%確定%'",
            (shop_id, emp_id))
        assert len(notifs) > 0

    def test_ai_default_is_draft(self, client):
        """APIのデフォルトは draft=False（後方互換）。UI が draft=true を明示送信する。"""
        shop_id, emp_id = self._setup_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": MON,
        }, headers=auth(tok))
        assert r.status_code == 200
        # APIのデフォルトは draft=False（後方互換・即確定）
        assert r.get_json()["draft"] is False


class TestFinalize:
    """ドラフトを一括確定するAPIのテスト。"""

    def _setup_draft(self):
        """AIドラフト状態のシフトを準備。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000)
        emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000)
        # AIドラフトで保存
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, emp1, f"{MON}T09:00:00", f"{MON}T18:00:00", "requested", "AIドラフト: 希望シフト"))
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, emp2, f"{MON}T09:00:00", f"{MON}T18:00:00", "requested", "AIドラフト: 不足補填"))
        return shop_id, emp1, emp2

    def test_finalize_drafts(self, client):
        """ドラフトを一括確定して通知送信。"""
        shop_id, emp1, emp2 = self._setup_draft()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/finalize", json={
            "start_date": MON, "end_date": MON,
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["finalized"] >= 2
        assert body["notified_staff"] >= 2
        # 全シフトが confirmed になっている
        rows = dbmod.query_all(
            "SELECT status FROM shifts WHERE shop_id=? AND status='confirmed'",
            (shop_id,))
        assert len(rows) >= 2
        # スタッフに通知が飛んでいる
        for sid in (emp1, emp2):
            notifs = dbmod.query_all(
                "SELECT * FROM notifications WHERE shop_id=? AND staff_id=? AND title LIKE '%確定%'",
                (shop_id, sid))
            assert len(notifs) > 0

    def test_finalize_empty_returns_zero(self, client):
        """対象ドラフト無し時は0確定。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/finalize", json={
            "start_date": MON, "end_date": MON,
        }, headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["finalized"] == 0

    def test_finalize_includes_staff_requests(self, client):
        """確定時には期間内のスタッフ希望も含めて全て confirmed にする。

        これにより「希望表カード」が確定後に消える（シフト完全確定）。
        """
        shop_id, emp1, _ = self._setup_draft()
        # スタッフ希望も混ぜる
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, emp1, f"{TUE}T10:00:00", f"{TUE}T15:00:00", "requested", "スタッフ希望提出"))
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/finalize", json={
            "start_date": MON, "end_date": TUE,
        }, headers=auth(tok))
        assert r.status_code == 200
        # 月曜のドラフトも火曜のスタッフ希望も両方 confirmed になる
        mon_drafts = dbmod.query_all(
            "SELECT status FROM shifts WHERE shop_id=? AND start_datetime LIKE ?",
            (shop_id, f"{MON}%"))
        assert all(r["status"] == "confirmed" for r in mon_drafts)
        tue_staff_req = dbmod.query_all(
            "SELECT status FROM shifts WHERE shop_id=? AND start_datetime LIKE ? AND reason=?",
            (shop_id, f"{TUE}%", "スタッフ希望提出"))
        # スタッフ希望も confirmed に変換されている（希望表カードが消える）
        assert all(r["status"] == "confirmed" for r in tue_staff_req)


class TestEngineExcludesDrafts:
    """エンジン: AIドラフトを次回入力から除外。"""

    def test_draft_not_used_as_engine_input(self):
        """AIドラフト保存後の再生成で、ドラフトを希望入力として使わない。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        emp_id = insert_staff(shop_id, "E1", "社員", "employee", 2000)
        # AIドラフトを直接INSERT
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, emp_id, f"{MON}T09:00:00", f"{MON}T18:00:00", "requested", "AIドラフト: 希望シフト"))
        # エンジン呼び出し
        result = shift_engine.auto_generate(shop_id, SETTINGS, MON, MON)
        # ドラフトが入力に入っていて二重に配置されることはない
        placed = [c for c in result["confirmed"] if c["staff_id"] == emp_id]
        # 1日1シフトまで（同日内重複なし）
        days = [c["start"][:10] for c in placed]
        assert len(days) == len(set(days))


class TestDefaultEndFromShiftHours:
    """「いつでも」希望時のデフォルト終了時刻が shift_hours から取得される。"""

    def test_default_end_time_uses_shift_hours(self, client):
        """shift_hours で設定した時間が「いつでも」希望の終了時刻に使われる。"""
        import json
        shop_id = insert_shop(settings={
            "shift_hours": {
                "bulk_mode": True,
                "bulk": {"start_time": "04:00", "end_time": "02:00", "is_closed": False},
                "days": {},
            },
        })
        staff_id = insert_staff(shop_id, "P1", "x", "part_time")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-31", "2099-12-31"))
        tok = make_session("staff", staff_id, shop_id)
        # 「いつでも」希望（end_datetime 未指定）
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{MON}T04:00:00", "availability": "any"}],
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        row = dbmod.query_one(
            "SELECT end_datetime FROM shifts WHERE staff_id=? AND status='requested'",
            (staff_id,))
        # shift_hours.bulk.end_time = "02:00" が使われている
        assert row["end_datetime"].endswith("T02:00:00")

    def test_default_end_time_fallback_to_patterns(self, client):
        """shift_hours 無し時は shift_patterns の最遅終了を使う。"""
        shop_id = insert_shop()
        insert_pattern(shop_id, "日", "09:00", "18:00", 1)
        insert_pattern(shop_id, "夜", "17:00", "23:00", 1)
        staff_id = insert_staff(shop_id, "P1", "x", "part_time")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-31", "2099-12-31"))
        tok = make_session("staff", staff_id, shop_id)
        client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "availability": "any"}],
        }, headers=auth(tok))
        row = dbmod.query_one(
            "SELECT end_datetime FROM shifts WHERE staff_id=? AND status='requested'",
            (staff_id,))
        # shift_patterns の最遅 = "23:00" が使われる
        assert row["end_datetime"].endswith("T23:00:00")
