"""tests/test_student_role.py - 学生アルバイトロールのテスト。

【対象要件】
  ③ 機能追加: スタッフ管理に「学生アルバイト」追加
     - 月80時間を超えるシフトは禁止
     - 学生アルバイトのみで構成されたシフトは禁止
     - 違反時は分かりやすいエラーメッセージ
"""
import pytest

import db as dbmod
import shift_engine
from helpers import (
    insert_shop, insert_staff, insert_pattern, insert_fixed,
    insert_request, insert_wish, make_session, auth,
)

MON, TUE, WED, THU, FRI = "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"
SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6, "default_hourly_wage": 1100}


class TestStudentRole:
    """学生アルバイトロールのCRUD・バリデーション。"""

    def test_create_student_role_staff(self, client):
        """学生アルバイト作成成功（max<=80）。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "S001", "name": "学生太郎",
            "password": "Stu12345", "role": "student",
            "hourly_wage": 1000, "max_hours_per_month": 80,
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        sid = r.get_json()["id"]
        row = dbmod.query_one("SELECT role, max_hours_per_month FROM staffs WHERE id=?", (sid,))
        assert row["role"] == "student"
        assert row["max_hours_per_month"] == 80

    def test_student_max_hours_enforced_at_80_on_create(self, client):
        """学生アルバイトは月80h超えの設定は拒否。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "S002", "name": "学生次郎",
            "password": "Stu12345", "role": "student",
            "max_hours_per_month": 120,  # 80超過
        }, headers=auth(tok))
        assert r.status_code == 400
        assert "80時間" in r.get_json()["error"]

    def test_student_default_max_is_80(self, client):
        """学生を max 指定なしで作成 → 自動的に80。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "S003", "name": "学生三郎",
            "password": "Stu12345", "role": "student",
        }, headers=auth(tok))
        assert r.status_code == 200
        sid = r.get_json()["id"]
        row = dbmod.query_one("SELECT max_hours_per_month FROM staffs WHERE id=?", (sid,))
        assert row["max_hours_per_month"] == 80

    def test_update_student_above_80_rejected(self, client):
        """学生を 80h 超 に更新しようとして拒否される。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "S004", "name": "学生四郎",
            "password": "Stu12345", "role": "student", "max_hours_per_month": 60,
        }, headers=auth(tok))
        sid = r.get_json()["id"]
        # 80超に更新しようとする
        r = client.put(f"/api/shop/staffs/{sid}", json={
            "name": "学生四郎", "hourly_wage": 1100,
            "min_hours_per_month": 0, "max_hours_per_month": 100,
        }, headers=auth(tok))
        assert r.status_code == 400

    def test_non_student_can_have_higher_max(self, client):
        """学生以外は 80h 超も設定可能。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "P001", "name": "バイト",
            "password": "Pt123456", "role": "part_time",
            "max_hours_per_month": 160,
        }, headers=auth(tok))
        assert r.status_code == 200

    def test_engine_enforces_student_80h_monthly_cap(self):
        """エンジン: 学生の月間上限は80hを強制（cap=100指定でも80に切詰め）。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        # 学生を max=100 で直接INSERT（API経由せず）→ エンジンは80hに切詰め
        stu_id = insert_staff(shop_id, "S1", "学生1", "student", 1000, 0, 100)
        # 30日分の希望（1日9h × 10日 = 90h → 80hで止まるはず）
        days = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
                "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
        for d in days:
            insert_wish(shop_id, stu_id, d, "09:00", "18:00")
        result = shift_engine.auto_generate(shop_id, SETTINGS, "2026-08-03", "2026-08-14")
        # 9h × N日 の合計が 80h を超えない（min(80, 9*days)）
        total_h = result["minutes_by_staff"][stu_id] / 60
        assert total_h <= 80.01, f"学生の月間時間 {total_h}h が80hを超過"


class TestStudentOnlyShiftPrevention:
    """学生のみで構成されるシフトの防止。"""

    def test_post_rejects_student_only_shift(self, client):
        """学生スタッフを配置すると社会人がいない場合、拒否される。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 3)
        stu_id = insert_staff(shop_id, "S1", "学生1", "student", 1000, 0, 80)
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts", json={
            "staff_id": stu_id,
            "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 400
        body = r.get_json()
        assert body.get("student_only") is True
        assert "学生アルバイトのみ" in body["error"]

    def test_post_allows_student_when_employee_present(self, client):
        """社会人が既に配置されている日は学生を追加できる。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 3)
        emp_id = insert_staff(shop_id, "E1", "社員1", "employee", 1500, 0, 200)
        stu_id = insert_staff(shop_id, "S1", "学生1", "student", 1000, 0, 80)
        tok = make_session("shop", shop_id, shop_id)
        # 先に社員を配置
        client.post("/api/shop/shifts", json={
            "staff_id": emp_id,
            "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(tok))
        # その後に学生を配置 → 成功
        r = client.post("/api/shop/shifts", json={
            "staff_id": stu_id,
            "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 200

    def test_engine_warns_on_student_only_day(self):
        """エンジン: 学生のみの日がある場合は warnings に報告される。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        # 学生のみ配置
        stu_id = insert_staff(shop_id, "S1", "学生1", "student", 1000, 0, 80)
        insert_wish(shop_id, stu_id, MON, "09:00", "18:00")
        result = shift_engine.auto_generate(shop_id, SETTINGS, MON, MON)
        # warnings に student_only_day が含まれる
        student_warnings = [w for w in result["warnings"] if w.get("type") == "student_only_day"]
        assert len(student_warnings) >= 1, f"学生のみ日の警告が出るべき: {result['warnings']}"

    def test_engine_no_student_only_warning_when_employee_present(self):
        """エンジン: 社員も配置されている日は student_only_day 警告なし。"""
        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 2)
        stu_id = insert_staff(shop_id, "S1", "学生1", "student", 1000, 0, 80)
        emp_id = insert_staff(shop_id, "E1", "社員1", "employee", 1500, 0, 200)
        # 学生と社員両方の希望 → 両方配置される
        insert_wish(shop_id, stu_id, MON, "09:00", "18:00")
        insert_wish(shop_id, emp_id, MON, "09:00", "18:00")
        result = shift_engine.auto_generate(shop_id, SETTINGS, MON, MON)
        student_warnings = [w for w in result["warnings"] if w.get("type") == "student_only_day"]
        assert len(student_warnings) == 0
