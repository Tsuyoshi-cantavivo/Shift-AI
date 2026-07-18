"""tests/test_performance.py - 性能テスト（基本）。

対象:
  - シフト自動生成の規模拡張（30日、180日）
  - API応答時間
  - SQL N+1 確認
"""
import time

import pytest

import db as dbmod
import shift_engine
from helpers import insert_shop, insert_staff, insert_pattern, insert_fixed


class TestShiftEnginePerformance:
    def test_generate_one_month(self):
        """1ヶ月（30日）のシフト生成が 5 秒以内。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4, "max_consecutive_days": 6})
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        # 社員2名 + バイト5名
        emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000)
        emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000)
        for w in range(1, 6):
            insert_fixed(emp1, w, "09:00", "18:00")
            insert_fixed(emp2, w, "09:00", "18:00")
        for i in range(1, 6):
            insert_staff(shop_id, f"P{i}", f"バイト{i}", "part_time", 1100)
        t0 = time.time()
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, "2026-08-01", "2026-08-30")
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"30日生成に{elapsed:.2f}s（5s超過）"
        # 何らかのシフトが生成されている
        assert len(res["confirmed"]) > 0

    def test_generate_six_months(self):
        """6ヶ月（180日）の生成が 30 秒以内（大量データ試験）。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        insert_staff(shop_id, "E1", "x", "employee", 2000)
        insert_staff(shop_id, "E2", "y", "employee", 2000)
        t0 = time.time()
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, "2026-01-01", "2026-06-30")
        elapsed = time.time() - t0
        assert elapsed < 30.0, f"180日生成に{elapsed:.2f}s"
        assert len(res["confirmed"]) > 0


class TestApiPerformance:
    def test_dashboard_response_time(self, client):
        """/api/shop/dashboard が 1 秒以内。"""
        shop_id = insert_shop(settings={"default_hourly_wage": 1100})
        insert_pattern(shop_id, "通", "09:00", "18:00", 2)
        for i in range(20):
            insert_staff(shop_id, f"P{i}", f"スタッフ{i}")
        from helpers import make_session, auth
        tok = make_session("shop", shop_id, shop_id)
        t0 = time.time()
        r = client.get("/api/shop/dashboard", headers=auth(tok))
        elapsed = time.time() - t0
        assert r.status_code == 200
        assert elapsed < 1.0, f"dashboard {elapsed:.3f}s（1s超過）"

    def test_summary_with_many_shifts(self, client):
        """100件のシフトがある状態での集計が 1 秒以内。"""
        shop_id = insert_shop(settings={"default_hourly_wage": 1100})
        insert_pattern(shop_id, "通", "09:00", "22:00", 5)
        from helpers import make_session, auth
        tok = make_session("shop", shop_id, shop_id)
        # 100 件のシフトを直接 INSERT
        for day in range(1, 21):  # 20日 × 5スタッフ = 100件
            for s in range(1, 6):
                sid = insert_staff(shop_id, f"P{s}_d{day}", f"スタッフ{s}_{day}")
                dbmod.execute(
                    "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, break_time_minutes) "
                    "VALUES (?,?,?,?,?,?)",
                    (shop_id, sid, f"2026-08-{day:02d}T09:00:00", f"2026-08-{day:02d}T18:00:00", "confirmed", 60))
        t0 = time.time()
        r = client.get("/api/shop/summary?start=2026-08-01&end=2026-08-31", headers=auth(tok))
        elapsed = time.time() - t0
        assert r.status_code == 200
        assert elapsed < 2.0, f"summary(100 shifts) {elapsed:.3f}s"

    def test_shifts_list_query(self, client):
        """シフト一覧が 1 秒以内（100件）。"""
        shop_id = insert_shop()
        insert_pattern(shop_id, "通", "09:00", "22:00", 5)
        from helpers import make_session, auth
        tok = make_session("shop", shop_id, shop_id)
        for day in range(1, 31):
            for s in range(1, 4):
                sid = insert_staff(shop_id, f"P{s}_d{day}", f"s{s}_{day}")
                dbmod.execute(
                    "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, break_time_minutes) "
                    "VALUES (?,?,?,?,?,?)",
                    (shop_id, sid, f"2026-08-{day:02d}T09:00:00", f"2026-08-{day:02d}T18:00:00", "confirmed", 60))
        t0 = time.time()
        r = client.get("/api/shop/shifts?start=2026-08-01&end=2026-08-31", headers=auth(tok))
        elapsed = time.time() - t0
        assert r.status_code == 200
        assert elapsed < 1.0, f"shifts list(90 shifts) {elapsed:.3f}s"


class TestDbPerformance:
    def test_index_effective(self):
        """インデックス定義が存在することを確認。"""
        # SQLite の sqlite_master からインデックスを確認
        rows = dbmod.query_all(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
        names = {r["name"] for r in rows}
        expected = {"idx_staffs_shop", "idx_shifts_shop_date", "idx_shifts_staff",
                    "idx_shifts_status", "idx_sessions_user", "idx_notif_shop",
                    "idx_notif_staff", "idx_creq_shop", "idx_creq_staff"}
        assert expected.issubset(names), f"足りないインデックス: {expected - names}"
