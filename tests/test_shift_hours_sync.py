"""tests test_shift_hours_sync.py - シフト時間設定とパターンの同期テスト。"""
import pytest

import db as dbmod
from helpers import insert_shop, insert_pattern, make_session, auth


class TestShiftHoursPatternSync:
    """shift_hours 保存時の shift_patterns 自動同期テスト。"""

    def test_sync_updates_existing_patterns(self, client):
        """既存パターンがある場合、保存時に時間が一括更新される。"""
        shop_id = insert_shop()
        # 既存パターン: 9-18 と 17-22
        p1 = insert_pattern(shop_id, "日中", "09:00", "18:00", 2)
        p2 = insert_pattern(shop_id, "夜", "17:00", "22:00", 1)
        tok = make_session("shop", shop_id, shop_id)
        # 04:00-02:00（翌日）で保存 + sync_patterns
        r = client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": True,
                "bulk": {"start_time": "04:00", "end_time": "02:00", "is_closed": False},
                "days": {},
            },
            "sync_patterns": True,
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert len(body["sync_log"]) > 0
        # 両パターンが 04:00-02:00 に更新されている
        for pid in (p1, p2):
            row = dbmod.query_one("SELECT start_time, end_time FROM shift_patterns WHERE id=?", (pid,))
            assert row["start_time"] == "04:00"
            assert row["end_time"] == "02:00"

    def test_sync_creates_pattern_when_none(self, client):
        """パターンが1つも無ければ「通し」パターンを新規作成。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": True,
                "bulk": {"start_time": "10:00", "end_time": "20:00", "is_closed": False},
                "days": {},
            },
            "sync_patterns": True,
        }, headers=auth(tok))
        assert r.status_code == 200
        # パターンが1件作成されている
        rows = dbmod.query_all("SELECT * FROM shift_patterns WHERE shop_id=?", (shop_id,))
        assert len(rows) == 1
        assert rows[0]["start_time"] == "10:00"
        assert rows[0]["end_time"] == "20:00"
        assert "通し" in rows[0]["pattern_name"]

    def test_no_sync_when_flag_off(self, client):
        """sync_patterns 未指定時はパターン更新しない。"""
        shop_id = insert_shop()
        p1 = insert_pattern(shop_id, "日中", "09:00", "18:00", 2)
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": True,
                "bulk": {"start_time": "04:00", "end_time": "02:00", "is_closed": False},
                "days": {},
            },
            # sync_patterns 無し
        }, headers=auth(tok))
        assert r.status_code == 200
        # パターンは変更されていない
        row = dbmod.query_one("SELECT start_time, end_time FROM shift_patterns WHERE id=?", (p1,))
        assert row["start_time"] == "09:00"
        assert row["end_time"] == "18:00"

    def test_sync_preserves_required_staff_and_name(self, client):
        """同期時は必要人数とパターン名は保持（時間のみ更新）。"""
        shop_id = insert_shop()
        p1 = insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        tok = make_session("shop", shop_id, shop_id)
        client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": True,
                "bulk": {"start_time": "04:00", "end_time": "02:00", "is_closed": False},
                "days": {},
            },
            "sync_patterns": True,
        }, headers=auth(tok))
        row = dbmod.query_one("SELECT pattern_name, required_staff FROM shift_patterns WHERE id=?", (p1,))
        assert row["pattern_name"] == "夜"
        assert row["required_staff"] == 3

    def test_sync_per_day_mode_uses_monday_as_representative(self, client):
        """曜日別モード時は月曜の時間で全パターン更新（代表時間として）。"""
        shop_id = insert_shop()
        p1 = insert_pattern(shop_id, "通", "09:00", "18:00", 2)
        tok = make_session("shop", shop_id, shop_id)
        client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": False,
                "days": {
                    "1": {"start_time": "05:00", "end_time": "01:00", "is_closed": False},  # 月曜
                    "2": {"start_time": "10:00", "end_time": "20:00", "is_closed": False},  # 火曜
                },
            },
            "sync_patterns": True,
        }, headers=auth(tok))
        # 月曜(05:00-01:00)で更新されている
        row = dbmod.query_one("SELECT start_time, end_time FROM shift_patterns WHERE id=?", (p1,))
        assert row["start_time"] == "05:00"
        assert row["end_time"] == "01:00"
