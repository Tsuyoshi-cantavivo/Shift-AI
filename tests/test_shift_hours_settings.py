"""tests/test_shift_hours_settings.py - シフト時間設定（シフト作成可能時間）のテスト。

【対象要件】
  ② 機能追加: 設定タブ「シフト時間設定」
     - 曜日別設定（月-日・祝日）
     - 一括設定（全曜日共通）
     - 定休日チェックボックス（時間入力を無効化）
"""
import json

import pytest

import db as dbmod
from helpers import insert_shop, make_session, auth

MON, TUE, WED = "2026-08-03", "2026-08-04", "2026-08-05"


class TestShiftHoursSettings:
    """シフト時間設定 API のテスト。"""

    def test_get_default_shift_hours(self, client):
        """設定未保存時はデフォルト値が返される。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/shift-hours", headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert "bulk_mode" in body
        assert "bulk" in body
        assert "days" in body
        assert "holidays" in body
        # デフォルト: 8曜日分（0-6 + holiday）
        for k in ("0", "1", "2", "3", "4", "5", "6", "holiday"):
            assert k in body["days"]
            assert "start_time" in body["days"][k]
            assert "end_time" in body["days"][k]
            assert "is_closed" in body["days"][k]

    def test_put_bulk_mode_settings(self, client):
        """一括設定を保存できる。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": True,
                "bulk": {"start_time": "10:00", "end_time": "20:00", "is_closed": False},
                "days": {},
            },
        }, headers=auth(tok))
        assert r.status_code == 200
        # 再取得して確認
        r = client.get("/api/shop/shift-hours", headers=auth(tok))
        body = r.get_json()
        assert body["bulk_mode"] is True
        assert body["bulk"]["start_time"] == "10:00"
        assert body["bulk"]["end_time"] == "20:00"

    def test_put_per_day_settings(self, client):
        """曜日別設定を保存できる（月曜の開始時間を変更）。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": False,
                "bulk": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
                "days": {
                    "1": {"start_time": "08:00", "end_time": "17:00", "is_closed": False},
                    "6": {"start_time": "10:00", "end_time": "15:00", "is_closed": False},
                    "0": {"start_time": "00:00", "end_time": "00:00", "is_closed": True},
                },
            },
        }, headers=auth(tok))
        assert r.status_code == 200
        # 再取得
        r = client.get("/api/shop/shift-hours", headers=auth(tok))
        body = r.get_json()
        assert body["days"]["1"]["start_time"] == "08:00"
        assert body["days"]["1"]["end_time"] == "17:00"
        assert body["days"]["0"]["is_closed"] is True
        assert body["days"]["6"]["start_time"] == "10:00"

    def test_put_holiday_settings(self, client):
        """祝日テンプレートを設定できる。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": False,
                "days": {
                    "holiday": {"start_time": "10:00", "end_time": "18:00", "is_closed": False},
                },
            },
        }, headers=auth(tok))
        assert r.status_code == 200
        r = client.get("/api/shop/shift-hours", headers=auth(tok))
        body = r.get_json()
        assert body["days"]["holiday"]["start_time"] == "10:00"
        assert body["days"]["holiday"]["end_time"] == "18:00"

    def test_invalid_time_falls_back_to_default(self, client):
        """不正な時刻フォーマットはデフォルトにフォールバック。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": True,
                "bulk": {"start_time": "99:99", "end_time": "XYZ", "is_closed": False},
            },
        }, headers=auth(tok))
        assert r.status_code == 200
        r = client.get("/api/shop/shift-hours", headers=auth(tok))
        body = r.get_json()
        # デフォルト値にフォールバック
        assert body["bulk"]["start_time"] == "09:00"
        assert body["bulk"]["end_time"] == "22:00"

    def test_settings_persisted_in_shop_settings_json(self, client):
        """保存内容が shops.settings JSON に永続化されている。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": False,
                "days": {"2": {"start_time": "07:00", "end_time": "16:00", "is_closed": False}},
            },
        }, headers=auth(tok))
        row = dbmod.query_one("SELECT settings FROM shops WHERE id=?", (shop_id,))
        s = json.loads(row["settings"])
        assert "shift_hours" in s
        assert s["shift_hours"]["days"]["2"]["start_time"] == "07:00"

    def test_non_shop_role_cannot_access(self, client):
        """店舗ロール以外はアクセス不可。"""
        from helpers import insert_admin
        admin_id = insert_admin()
        admin_tok = make_session("admin", admin_id)
        r = client.get("/api/shop/shift-hours", headers=auth(admin_tok))
        assert r.status_code == 403


class TestHolidayManagement:
    """祝日・特別休業日の CRUD テスト。"""

    def test_add_holiday(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/holidays", json={
            "holiday_date": "2026-01-01", "note": "元日",
        }, headers=auth(tok))
        assert r.status_code == 200
        r = client.get("/api/shop/holidays", headers=auth(tok))
        assert r.status_code == 200
        holidays = r.get_json()["holidays"]
        assert any(h["holiday_date"] == "2026-01-01" for h in holidays)

    def test_delete_holiday(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/holidays", json={
            "holiday_date": "2026-01-01",
        }, headers=auth(tok))
        r = client.delete("/api/shop/holidays/2026-01-01", headers=auth(tok))
        assert r.status_code == 200
        r = client.get("/api/shop/holidays", headers=auth(tok))
        holidays = r.get_json()["holidays"]
        assert not any(h["holiday_date"] == "2026-01-01" for h in holidays)

    def test_duplicate_holiday_silently_ignored(self, client):
        """同じ日付の二重追加はエラーにならない（INSERT OR IGNORE）。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r1 = client.post("/api/shop/holidays", json={"holiday_date": "2026-05-05"},
                         headers=auth(tok))
        r2 = client.post("/api/shop/holidays", json={"holiday_date": "2026-05-05"},
                         headers=auth(tok))
        assert r1.status_code == 200
        assert r2.status_code == 200
        # 1件のみ
        r = client.get("/api/shop/holidays", headers=auth(tok))
        holidays = r.get_json()["holidays"]
        assert sum(1 for h in holidays if h["holiday_date"] == "2026-05-05") == 1

    def test_holidays_isolated_per_shop(self, client):
        """店舗Aの祝日は店舗Bから見えない。"""
        shop_a = insert_shop(code="A")
        shop_b = insert_shop(code="B")
        tok_a = make_session("shop", shop_a, shop_a)
        tok_b = make_session("shop", shop_b, shop_b)
        client.post("/api/shop/holidays", json={"holiday_date": "2026-01-01"},
                    headers=auth(tok_a))
        r = client.get("/api/shop/holidays", headers=auth(tok_b))
        assert len(r.get_json()["holidays"]) == 0

    def test_holiday_missing_date_returns_400(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/holidays", json={}, headers=auth(tok))
        assert r.status_code == 400
