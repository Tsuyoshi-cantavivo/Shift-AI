"""tests/test_admin_staff_apis.py - 未カバーAPIの統合テスト。

対象 (既存 test_app.py がカバーしていない部分):
  - 管理者API全般
  - スタッフAPI（マイシフト・ダッシュボード・ICS・パスワード変更）
  - 店舗API（通知・設定・コピー・一括確定・CSVエクスポート）
  - 変更申請（承認/却下）
  - 申請バリデーション
"""
import json

import pytest

import db as dbmod
from helpers import (
    insert_admin, insert_shop, insert_staff, insert_pattern, insert_fixed,
    insert_request, make_session, auth,
)

MON, TUE, WED = "2026-08-03", "2026-08-04", "2026-08-05"
SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6,
            "default_hourly_wage": 1100, "night_premium_rate": 1.25,
            "transport_per_day": 300, "business_hours": "9:00-22:00"}


# ============================================================
# 管理者
# ============================================================
class TestAdminApis:
    def test_admin_list_shops(self, client):
        admin_id = insert_admin()
        insert_shop(code="S1", name="店舗1")
        insert_shop(code="S2", name="店舗2")
        tok = make_session("admin", admin_id)
        r = client.get("/api/admin/shops", headers=auth(tok))
        assert r.status_code == 200
        assert len(r.get_json()["shops"]) == 2

    def test_admin_create_and_update_shop(self, client):
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.post("/api/admin/shops", json={
            "shop_code": "NEW", "shop_name": "新店舗",
            "password": "ShopPass1", "settings": {"x": 1},
        }, headers=auth(tok))
        assert r.status_code == 200
        sid = r.get_json()["id"]
        # 更新
        r = client.put(f"/api/admin/shops/{sid}", json={
            "shop_name": "改名", "is_active": False,
        }, headers=auth(tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT shop_name, is_active FROM shops WHERE id=?", (sid,))
        assert row["shop_name"] == "改名"
        assert row["is_active"] == 0

    def test_admin_shop_stats(self, client):
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        e1 = insert_staff(shop_id, "E1", "x", "employee", 2000)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        tok = make_session("admin", admin_id)
        # スタッフ数
        r = client.get(f"/api/admin/shops/stats/{shop_id}", headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert body["staff_count"] == 1
        assert body["confirmed_count"] == 0

    def test_admin_shop_staffs(self, client):
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        insert_staff(shop_id, "P1", "バイト1")
        insert_staff(shop_id, "P2", "バイト2")
        tok = make_session("admin", admin_id)
        r = client.get(f"/api/admin/shops/staffs/{shop_id}", headers=auth(tok))
        assert r.status_code == 200
        assert len(r.get_json()["staffs"]) == 2

    def test_admin_shop_next_period_existing(self, client):
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        # 既存の募集期間を作成
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2099-01-01", "2099-01-15", "2098-12-25"))
        tok = make_session("admin", admin_id)
        r = client.get(f"/api/admin/shops/{shop_id}/periods/next", headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["start_date"] == "2099-01-01"

    def test_admin_shop_next_period_fallback(self, client):
        """募集期間が無ければ calc_next_period で生成。"""
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        tok = make_session("admin", admin_id)
        r = client.get(f"/api/admin/shops/{shop_id}/periods/next", headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert body["start_date"]
        assert body["end_date"]
        assert body["deadline"]

    def test_admin_shop_summary(self, client):
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1", settings=SETTINGS)
        e1 = insert_staff(shop_id, "E1", "x", "employee", 2000)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        shop_tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(shop_tok))
        tok = make_session("admin", admin_id)
        r = client.get(f"/api/admin/shops/summary/{shop_id}?start={MON}&end={MON}",
                       headers=auth(tok))
        assert r.status_code == 200
        # 9:00-18:00 = 9h, 休憩60分(8h超) → 実働 8h
        assert r.get_json()["total_hours"] >= 8.0

    def test_admin_notifications_empty(self, client):
        """管理者向け通知APIは空リストを返す（仕様）。"""
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.get("/api/admin/notifications", headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["notifications"] == []
        assert r.get_json()["unread"] == 0

    def test_admin_read_all_notifications(self, client):
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.put("/api/admin/notifications/read-all", headers=auth(tok))
        assert r.status_code == 200


# ============================================================
# 店舗: 設定 / 通知 / パスワード変更
# ============================================================
class TestShopSettings:
    def test_get_settings(self, client):
        shop_id = insert_shop(code="S1", settings={"default_hourly_wage": 1234})
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/settings", headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert body["settings"]["default_hourly_wage"] == 1234

    def test_update_shop_name(self, client):
        shop_id = insert_shop(code="S1", name="旧店舗名")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/settings", json={
            "shop_name": "新店舗名", "settings": {"new_key": 1},
        }, headers=auth(tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT shop_name, settings FROM shops WHERE id=?", (shop_id,))
        assert row["shop_name"] == "新店舗名"
        s = json.loads(row["settings"])
        assert s["new_key"] == 1

    def test_password_change_success(self, client):
        shop_id = insert_shop(code="S1", password="OldPass1")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/password", json={
            "current_password": "OldPass1", "new_password": "NewPass2",
        }, headers=auth(tok))
        assert r.status_code == 200

    def test_password_change_wrong_current(self, client):
        shop_id = insert_shop(code="S1", password="OldPass1")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/password", json={
            "current_password": "wrong", "new_password": "NewPass2",
        }, headers=auth(tok))
        assert r.status_code == 400

    def test_password_change_weak_new(self, client):
        shop_id = insert_shop(code="S1", password="OldPass1")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/password", json={
            "current_password": "OldPass1", "new_password": "short",
        }, headers=auth(tok))
        assert r.status_code == 400


class TestShopNotifications:
    def test_get_notifications_empty(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/notifications", headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["notifications"] == []
        assert r.get_json()["unread"] == 0

    def test_read_all_notifications(self, client):
        shop_id = insert_shop()
        dbmod.execute(
            "INSERT INTO notifications (shop_id, staff_id, type, title, body, is_read) VALUES (?,?,?,?,?,?)",
            (shop_id, None, "info", "通知1", "本文", 0))
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/notifications/read-all", headers=auth(tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT is_read FROM notifications WHERE shop_id=?", (shop_id,))
        assert row["is_read"] == 1


# ============================================================
# スタッフAPI
# ============================================================
class TestStaffApis:
    def test_staff_periods(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-15", "2026-07-25"))
        tok = make_session("staff", staff_id, shop_id)
        r = client.get("/api/staff/periods", headers=auth(tok))
        assert r.status_code == 200
        assert len(r.get_json()["periods"]) == 1

    def test_staff_shifts_list(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x")
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        shop_tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": staff_id, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(shop_tok))
        tok = make_session("staff", staff_id, shop_id)
        r = client.get(f"/api/staff/shifts?start={MON}&end={MON}", headers=auth(tok))
        assert r.status_code == 200
        assert len(r.get_json()["shifts"]) == 1

    def test_staff_notifications(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x")
        dbmod.execute(
            "INSERT INTO notifications (shop_id, staff_id, type, title, body, is_read) VALUES (?,?,?,?,?,?)",
            (shop_id, staff_id, "info", "スタッフ通知", "本文", 0))
        tok = make_session("staff", staff_id, shop_id)
        r = client.get("/api/staff/notifications", headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["unread"] == 1

    def test_staff_read_all_notifications(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x")
        dbmod.execute(
            "INSERT INTO notifications (shop_id, staff_id, type, title, body, is_read) VALUES (?,?,?,?,?,?)",
            (shop_id, staff_id, "info", "通知", "本文", 0))
        tok = make_session("staff", staff_id, shop_id)
        r = client.put("/api/staff/notifications/read-all", headers=auth(tok))
        assert r.status_code == 200

    def test_staff_dashboard(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x")
        tok = make_session("staff", staff_id, shop_id)
        r = client.get("/api/staff/dashboard", headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert "pending_requests" in body
        assert "pending_approvals" in body
        assert "next_shift" in body

    def test_staff_summary(self, client):
        shop_id = insert_shop(settings=SETTINGS)
        staff_id = insert_staff(shop_id, "P1", "x", "employee", 2000)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        shop_tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": staff_id, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(shop_tok))
        tok = make_session("staff", staff_id, shop_id)
        r = client.get(f"/api/staff/summary?start={MON}&end={MON}", headers=auth(tok))
        assert r.status_code == 200
        assert len(r.get_json()["staff"]) == 1

    def test_staff_password_change(self, client):
        from helpers import insert_staff
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x", password="OldPass1")
        tok = make_session("staff", staff_id, shop_id)
        r = client.put("/api/staff/password", json={
            "current_password": "OldPass1", "new_password": "NewPass2",
        }, headers=auth(tok))
        assert r.status_code == 200


# ============================================================
# 変更申請
# ============================================================
class TestChangeRequests:
    def _setup(self):
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "22:00", 3)
        e1 = insert_staff(shop_id, "E1", "x", "employee", 2000)
        return shop_id, e1

    def test_staff_create_change_request(self, client):
        shop_id, e1 = self._setup()
        shop_tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(shop_tok))
        sid = r.get_json()["id"]
        staff_tok = make_session("staff", e1, shop_id)
        # 変更申請作成
        r = client.post("/api/staff/change-requests", json={
            "shift_id": sid, "request_type": "change",
            "desired_start": f"{MON}T10:00:00", "desired_end": f"{MON}T19:00:00",
            "reason": "体調不良",
        }, headers=auth(staff_tok))
        assert r.status_code == 200

    def test_staff_change_request_invalid_type(self, client):
        shop_id, e1 = self._setup()
        staff_tok = make_session("staff", e1, shop_id)
        r = client.post("/api/staff/change-requests", json={
            "request_type": "invalid", "desired_start": f"{MON}T10:00:00",
            "desired_end": f"{MON}T19:00:00",
        }, headers=auth(staff_tok))
        assert r.status_code == 400

    def test_staff_change_request_list(self, client):
        shop_id, e1 = self._setup()
        staff_tok = make_session("staff", e1, shop_id)
        # 直接 INSERT
        dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, desired_start, desired_end, status) "
            "VALUES (?,?,?,?,?,?, 'pending')",
            (shop_id, e1, None, "add", f"{MON}T10:00:00", f"{MON}T15:00:00"))
        r = client.get("/api/staff/change-requests", headers=auth(staff_tok))
        assert r.status_code == 200
        assert len(r.get_json()["change_requests"]) == 1

    def test_shop_approve_change_request(self, client):
        shop_id, e1 = self._setup()
        shop_tok = make_session("shop", shop_id, shop_id)
        sid = dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, e1, f"{MON}T09:00:00", f"{MON}T18:00:00", "confirmed", "テスト")
        )["last_row_id"]
        crid = dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, desired_start, desired_end, status) "
            "VALUES (?,?,?,?,?,?, 'pending')",
            (shop_id, e1, sid, "change", f"{MON}T10:00:00", f"{MON}T19:00:00")
        )["last_row_id"]
        r = client.put(f"/api/shop/change-requests/{crid}", json={"action": "approve"},
                       headers=auth(shop_tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT status FROM change_requests WHERE id=?", (crid,))
        assert row["status"] == "approved"
        # シフトも更新されている
        sh = dbmod.query_one("SELECT start_datetime FROM shifts WHERE id=?", (sid,))
        assert sh["start_datetime"] == f"{MON}T10:00:00"

    def test_shop_reject_change_request(self, client):
        shop_id, e1 = self._setup()
        shop_tok = make_session("shop", shop_id, shop_id)
        crid = dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, desired_start, desired_end, status) "
            "VALUES (?,?,?,?,?,?, 'pending')",
            (shop_id, e1, None, "add", f"{MON}T10:00:00", f"{MON}T15:00:00")
        )["last_row_id"]
        r = client.put(f"/api/shop/change-requests/{crid}", json={"action": "reject"},
                       headers=auth(shop_tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT status FROM change_requests WHERE id=?", (crid,))
        assert row["status"] == "rejected"

    def test_shop_resolve_already_processed(self, client):
        shop_id, e1 = self._setup()
        shop_tok = make_session("shop", shop_id, shop_id)
        crid = dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, desired_start, desired_end, status) "
            "VALUES (?,?,?,?,?,?, 'approved')",
            (shop_id, e1, None, "add", f"{MON}T10:00:00", f"{MON}T15:00:00")
        )["last_row_id"]
        r = client.put(f"/api/shop/change-requests/{crid}", json={"action": "approve"},
                       headers=auth(shop_tok))
        assert r.status_code == 400

    def test_shop_approve_cancel_request(self, client):
        """cancel 申請を承認 → 対象シフト削除。"""
        shop_id, e1 = self._setup()
        shop_tok = make_session("shop", shop_id, shop_id)
        sid = dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, e1, f"{MON}T09:00:00", f"{MON}T18:00:00", "confirmed", "テスト")
        )["last_row_id"]
        crid = dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, status) "
            "VALUES (?,?,?,?, 'pending')",
            (shop_id, e1, sid, "cancel")
        )["last_row_id"]
        r = client.put(f"/api/shop/change-requests/{crid}", json={"action": "approve"},
                       headers=auth(shop_tok))
        assert r.status_code == 200
        # シフト削除確認
        row = dbmod.query_one("SELECT id FROM shifts WHERE id=?", (sid,))
        assert row is None

    def test_shop_approve_add_request(self, client):
        """add 申請を承認 → 新シフト追加。"""
        shop_id, e1 = self._setup()
        shop_tok = make_session("shop", shop_id, shop_id)
        crid = dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, desired_start, desired_end, status) "
            "VALUES (?,?,?,?,?,?, 'pending')",
            (shop_id, e1, None, "add", f"{MON}T10:00:00", f"{MON}T15:00:00")
        )["last_row_id"]
        r = client.put(f"/api/shop/change-requests/{crid}", json={"action": "approve"},
                       headers=auth(shop_tok))
        assert r.status_code == 200
        # 新しい confirmed シフトが存在
        row = dbmod.query_one(
            "SELECT id FROM shifts WHERE staff_id=? AND start_datetime=? AND status='confirmed'",
            (e1, f"{MON}T10:00:00"))
        assert row is not None


# ============================================================
# シフトコピー
# ============================================================
class TestShiftCopy:
    def test_copy_simple(self, client):
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "22:00", 3)
        e1 = insert_staff(shop_id, "E1", "x", "employee", 2000)
        tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(tok))
        # MON → TUE へコピー
        r = client.post("/api/shop/shifts/copy", json={
            "from_start": MON, "from_end": MON, "to_start": TUE,
        }, headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["copied"] == 1

    def test_copy_missing_fields(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/copy", json={
            "from_start": MON, "from_end": MON,  # to_start 無し
        }, headers=auth(tok))
        assert r.status_code == 400


# ============================================================
# CSVエクスポート
# ============================================================
class TestCsvExport:
    def test_csv_export_basic(self, client):
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "22:00", 1)
        e1 = insert_staff(shop_id, "E1", "テスト社員", "employee", 2000)
        tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(tok))
        r = client.get(f"/api/shop/shifts/export?start={MON}&end={MON}",
                       headers=auth(tok))
        assert r.status_code == 200
        assert "text/csv" in r.content_type
        body = r.get_data(as_text=True)
        assert "日付" in body
        assert "テスト社員" in body
        assert "社員" in body
        # BOM
        assert body.startswith("\ufeff")

    def test_csv_export_missing_dates(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/shifts/export", headers=auth(tok))
        assert r.status_code == 400


# ============================================================
# ICS エクスポート
# ============================================================
class TestIcsExport:
    def test_ics_basic(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x", "employee", 2000)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        shop_tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": staff_id, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(shop_tok))
        staff_tok = make_session("staff", staff_id, shop_id)
        r = client.get(f"/api/staff/shifts/ics?t={staff_tok}")
        assert r.status_code == 200
        assert "text/calendar" in r.content_type
        body = r.get_data(as_text=True)
        assert "BEGIN:VCALENDAR" in body
        assert "VEVENT" in body

    def test_ics_invalid_token(self, client):
        r = client.get("/api/staff/shifts/ics?t=invalidtoken")
        assert r.status_code == 401

    def test_ics_missing_token(self, client):
        r = client.get("/api/staff/shifts/ics")
        assert r.status_code == 401


# ============================================================
# 固定シフト CRUD
# ============================================================
class TestFixedShifts:
    def test_create_fixed(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x", "employee", 2000)
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/fixed-shifts", json={
            "staff_id": staff_id, "weekday": 1, "start_time": "09:00", "end_time": "18:00",
        }, headers=auth(tok))
        assert r.status_code == 200

    def test_list_fixed(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x", "employee", 2000)
        insert_fixed(staff_id, 1, "09:00", "18:00")
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/fixed-shifts", headers=auth(tok))
        assert r.status_code == 200
        assert len(r.get_json()["fixed_shifts"]) == 1

    def test_update_fixed(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x", "employee", 2000)
        fid = insert_fixed(staff_id, 1, "09:00", "18:00")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put(f"/api/shop/fixed-shifts/{fid}", json={
            "weekday": 2, "start_time": "10:00", "end_time": "19:00",
        }, headers=auth(tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT weekday FROM fixed_shifts WHERE id=?", (fid,))
        assert row["weekday"] == 2

    def test_delete_fixed(self, client):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "x", "employee", 2000)
        fid = insert_fixed(staff_id, 1, "09:00", "18:00")
        tok = make_session("shop", shop_id, shop_id)
        r = client.delete(f"/api/shop/fixed-shifts/{fid}", headers=auth(tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT id FROM fixed_shifts WHERE id=?", (fid,))
        assert row is None
