"""tests/test_rest_request.py - 休希望（availability='rest'）のテスト。"""
import pytest

import db as dbmod
import shift_engine
from helpers import (
    insert_shop, insert_staff, insert_pattern, insert_fixed,
    insert_wish, make_session, auth,
)

MON, TUE = "2026-08-03", "2026-08-04"
SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6, "default_hourly_wage": 1100}


class TestRestRequest:
    """休希望機能のテスト。"""

    def test_manager_can_submit_rest_request(self, client):
        """管理者が休希望（availability='rest'）を提出できる。"""
        shop_id = insert_shop()
        mgr_id = insert_staff(shop_id, "mgr", "店長", "manager")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-31", "2099-12-31"))
        tok = make_session("shop", mgr_id, shop_id)
        r = client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T00:00:00", "end_datetime": f"{MON}T23:59:59",
                        "availability": "rest"}],
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["submitted"] == 1
        # shifts テーブルに rest 希望として保存
        row = dbmod.query_one(
            "SELECT availability, reason FROM shifts WHERE staff_id=? AND status='requested'",
            (mgr_id,))
        assert row["availability"] == "rest"
        assert "休希望" in row["reason"]

    def test_rest_request_allows_overlap_with_other_requests(self, client):
        """休希望は既存希望との重複を許可（同日に複数の希望が出せる）。"""
        shop_id = insert_shop()
        mgr_id = insert_staff(shop_id, "mgr", "店長", "manager")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-31", "2099-12-31"))
        tok = make_session("shop", mgr_id, shop_id)
        # 1件目: 時間指定
        client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(tok))
        # 2件目: 休希望（重複するが許可される）
        r = client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T00:00:00", "end_datetime": f"{MON}T23:59:59",
                        "availability": "rest"}],
        }, headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["submitted"] == 1
        assert r.get_json()["skipped_overlap"] == 0

    def test_engine_skips_staff_on_rest_day(self):
        """エンジン: 休希望のスタッフはその日に配置しない。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        emp_id = insert_staff(shop_id, "E1", "社員", "employee", 2000)
        # 月曜に休希望
        insert_wish(shop_id, emp_id, MON, "00:00", "23:59", availability="rest")
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, availability) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, emp_id, f"{MON}T00:00:00", f"{MON}T23:59:59", "requested", "rest"))
        result = shift_engine.auto_generate(shop_id, SETTINGS, MON, MON)
        # 社員は月曜に配置されない（休希望尊重）
        placed_days = [c["start"][:10] for c in result["confirmed"] if c["staff_id"] == emp_id]
        assert MON not in placed_days

    def test_engine_places_staff_on_non_rest_day(self):
        """エンジン: 休希望でない日は通常通り配置される。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        emp_id = insert_staff(shop_id, "E1", "社員", "employee", 2000)
        # 月曜は休希望、火曜は時間指定希望
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, availability) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, emp_id, f"{MON}T00:00:00", f"{MON}T23:59:59", "requested", "rest"))
        insert_wish(shop_id, emp_id, TUE, "09:00", "18:00")
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,?)",
            (shop_id, emp_id, f"{TUE}T09:00:00", f"{TUE}T18:00:00", "requested"))
        result = shift_engine.auto_generate(shop_id, SETTINGS, MON, TUE)
        placed_days = [c["start"][:10] for c in result["confirmed"] if c["staff_id"] == emp_id]
        assert MON not in placed_days  # 月曜は休希望 → 配置されない
        assert TUE in placed_days      # 火曜は希望通り配置

    def test_engine_skips_fixed_shift_on_rest_day(self):
        """エンジン: 休希望の日は固定シフトも配置しない。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        pt_id = insert_staff(shop_id, "P1", "バイト", "part_time", 1100)
        # 月曜固定シフト
        insert_fixed(pt_id, 1, "09:00", "18:00")  # weekday=1 = 月曜
        # 月曜に休希望
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, availability) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, pt_id, f"{MON}T00:00:00", f"{MON}T23:59:59", "requested", "rest"))
        result = shift_engine.auto_generate(shop_id, SETTINGS, MON, MON)
        placed_days = [c["start"][:10] for c in result["confirmed"] if c["staff_id"] == pt_id]
        assert MON not in placed_days
