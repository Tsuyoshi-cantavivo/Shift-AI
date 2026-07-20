"""tests/test_integration_flow.py - 結合テスト: 店舗作成からシフト作成まで。

【対象要件】
  結合テスト: 店舗作成からシフト作成までの一連の操作が正常に行えることを確認。
"""
import pytest

import db as dbmod
from helpers import insert_admin, make_session, auth


class TestIntegrationFlow:
    """店舗作成 → ログイン → 設定 → スタッフ作成 → シフト作成 の全体フロー。"""

    def test_full_flow_admin_creates_shop_manager_logs_in(self, client):
        """[統合] 管理者が店舗+責任者作成 → 責任者がログイン → 設定→スタッフ→シフト。"""
        # Step1: 管理者初期化
        admin_id = insert_admin()
        admin_tok = make_session("admin", admin_id)

        # Step2: 管理者が店舗 + 店舗責任者を同時作成
        r = client.post("/api/admin/shops", json={
            "shop_code": "INTEG01", "shop_name": "統合テスト店",
            "password": "ShopPass1",
            "manager_code": "manager", "manager_password": "Mgr12345",
            "manager_name": "山田店長",
        }, headers=auth(admin_tok))
        assert r.status_code == 200, r.get_json()
        sid = r.get_json()["id"]
        assert r.get_json()["manager_code"] == "manager"

        # Step3: 店舗責任者としてログイン（shop_code + manager_code + password）
        r = client.post("/api/login", json={
            "shop_code": "INTEG01", "user_code": "manager",
            "password": "Mgr12345",
        })
        assert r.status_code == 200
        shop_tok = r.get_json()["token"]
        assert r.get_json()["role"] == "shop"
        # 認証ヘッダは以後これを使う
        hdr = auth(shop_tok)

        # Step4: シフト時間設定を保存
        r = client.put("/api/shop/shift-hours", json={
            "shift_hours": {
                "bulk_mode": False,
                "bulk": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
                "days": {
                    "0": {"start_time": "10:00", "end_time": "20:00", "is_closed": False},
                    "6": {"start_time": "10:00", "end_time": "20:00", "is_closed": False},
                },
            },
        }, headers=hdr)
        assert r.status_code == 200

        # Step5: スタッフを作成（社員 + 学生アルバイト + 通常アルバイト）
        # 社員
        r = client.post("/api/shop/staffs", json={
            "staff_code": "EMP001", "name": "社員一郎",
            "password": "Emp12345", "role": "employee",
            "hourly_wage": 2000, "max_hours_per_month": 200,
        }, headers=hdr)
        assert r.status_code == 200
        emp_id = r.get_json()["id"]

        # 学生アルバイト
        r = client.post("/api/shop/staffs", json={
            "staff_code": "STU001", "name": "学生花子",
            "password": "Stu12345", "role": "student",
            "hourly_wage": 1100, "max_hours_per_month": 80,
        }, headers=hdr)
        assert r.status_code == 200
        stu_id = r.get_json()["id"]

        # 通常アルバイト
        r = client.post("/api/shop/staffs", json={
            "staff_code": "PT001", "name": "バイト太郎",
            "password": "Pt123456", "role": "part_time",
            "hourly_wage": 1200, "max_hours_per_month": 160,
        }, headers=hdr)
        assert r.status_code == 200

        # Step6: シフトパターンを設定
        r = client.post("/api/shop/patterns", json={
            "pattern_name": "通し", "start_time": "09:00",
            "end_time": "18:00", "required_staff": 2,
        }, headers=hdr)
        assert r.status_code == 200

        # Step7: シフト作成（社員 + 学生）
        r = client.post("/api/shop/shifts", json={
            "staff_id": emp_id,
            "start_datetime": "2026-08-03T09:00:00",
            "end_datetime": "2026-08-03T18:00:00",
        }, headers=hdr)
        assert r.status_code == 200
        # 学生を追加（社員が既にいるのでOK）
        r = client.post("/api/shop/shifts", json={
            "staff_id": stu_id,
            "start_datetime": "2026-08-03T09:00:00",
            "end_datetime": "2026-08-03T18:00:00",
        }, headers=hdr)
        assert r.status_code == 200

        # Step8: シフト一覧取得
        r = client.get("/api/shop/shifts?start=2026-08-03&end=2026-08-03", headers=hdr)
        assert r.status_code == 200
        shifts = r.get_json()["shifts"]
        assert len(shifts) == 2

        # Step9: 集計を取得
        r = client.get("/api/shop/summary?start=2026-08-03&end=2026-08-03", headers=hdr)
        assert r.status_code == 200
        summary = r.get_json()
        assert summary["total_hours"] > 0

        # Step10: 学生のみシフトが拒否されることを確認（別日）
        # 社員のいない別の日に学生を配置しようとする → 拒否
        r = client.post("/api/shop/shifts", json={
            "staff_id": stu_id,
            "start_datetime": "2026-08-04T09:00:00",
            "end_datetime": "2026-08-04T18:00:00",
        }, headers=hdr)
        assert r.status_code == 400
        assert r.get_json().get("student_only") is True

    def test_admin_login_with_magic_word(self, client):
        """管理者ログイン: user_code に 'admin' を入れる。"""
        insert_admin()
        r = client.post("/api/login", json={
            "shop_code": "anything", "user_code": "admin",
            "password": "admin123",
        })
        assert r.status_code == 200
        assert r.get_json()["role"] == "admin"

    def test_old_fallback_login_still_works(self, client):
        """後方互換: user_code == shop_code の旧店主ログイン。"""
        from helpers import insert_shop
        shop_id = insert_shop(code="LEGACY", password="LegacyPw1")
        r = client.post("/api/login", json={
            "shop_code": "LEGACY", "user_code": "LEGACY",
            "password": "LegacyPw1",
        })
        assert r.status_code == 200
        assert r.get_json()["role"] == "shop"

    def test_dashboard_works_after_full_setup(self, client):
        """ダッシュボードが正常に取得できる。"""
        admin_id = insert_admin()
        admin_tok = make_session("admin", admin_id)
        client.post("/api/admin/shops", json={
            "shop_code": "DASH01", "shop_name": "ダッシュ店",
            "password": "ShopPass1",
            "manager_code": "mgr", "manager_password": "Mgr12345",
            "manager_name": "店長",
        }, headers=auth(admin_tok))
        # ログイン
        r = client.post("/api/login", json={
            "shop_code": "DASH01", "user_code": "mgr",
            "password": "Mgr12345",
        })
        shop_tok = r.get_json()["token"]
        # ダッシュボード取得
        r = client.get("/api/shop/dashboard", headers=auth(shop_tok))
        assert r.status_code == 200
        body = r.get_json()
        assert "today_attendance" in body
        assert "staff_count" in body
