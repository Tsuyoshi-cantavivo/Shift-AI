"""tests/test_admin_role_change.py - 管理者によるスタッフのロール変更・PWリセット。"""
import pytest

import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff, make_session, auth


class TestAdminRoleChange:
    """システム管理者によるスタッフの role 変更 API。"""

    def test_change_role_to_manager(self, client):
        """employee を manager に変更できる。"""
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee")
        tok = make_session("admin", admin_id)
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/role",
                       json={"role": "manager"}, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["old_role"] == "employee"
        assert body["new_role"] == "manager"
        # DB反映確認
        row = dbmod.query_one("SELECT role FROM staffs WHERE id=?", (emp_id,))
        assert row["role"] == "manager"

    def test_change_role_invalid_value_rejected(self, client):
        """無効な role 値は拒否。"""
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee")
        tok = make_session("admin", admin_id)
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/role",
                       json={"role": "invalid_role"}, headers=auth(tok))
        assert r.status_code == 400

    def test_change_role_invalidates_session(self, client):
        """role 変更後は既存セッションが無効化される（再ログイン強制）。"""
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee")
        # emp が staff ログイン
        emp_tok = make_session("staff", emp_id, shop_id)
        # staff としてアクセスできることを確認
        r = client.get("/api/staff/dashboard", headers=auth(emp_tok))
        assert r.status_code == 200
        # 管理者が role を変更
        admin_tok = make_session("admin", admin_id)
        client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/role",
                   json={"role": "manager"}, headers=auth(admin_tok))
        # 古いトークンは無効化されている
        r = client.get("/api/staff/dashboard", headers=auth(emp_tok))
        assert r.status_code == 401

    def test_change_role_to_student_enforces_80h(self, client):
        """student に変更時は月80h上限を強制。"""
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee", 2000, 0, 200)
        tok = make_session("admin", admin_id)
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/role",
                       json={"role": "student"}, headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json().get("max_hours_per_month") == 80
        row = dbmod.query_one("SELECT role, max_hours_per_month FROM staffs WHERE id=?", (emp_id,))
        assert row["role"] == "student"
        assert row["max_hours_per_month"] == 80

    def test_non_admin_cannot_change_role(self, client):
        """管理者以外は role 変更不可。"""
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee")
        # shop ロール（店舗管理者）でも不可
        shop_tok = make_session("shop", shop_id, shop_id)
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/role",
                       json={"role": "manager"}, headers=auth(shop_tok))
        assert r.status_code == 403

    def test_role_change_nonexistent_staff_404(self, client):
        """存在しないスタッフIDは 404。"""
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        tok = make_session("admin", admin_id)
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/9999/role",
                       json={"role": "manager"}, headers=auth(tok))
        assert r.status_code == 404

    def test_role_change_wrong_shop_404(self, client):
        """別店舗のスタッフIDは 404（IDOR防止）。"""
        admin_id = insert_admin()
        shop_a = insert_shop(code="A")
        shop_b = insert_shop(code="B")
        emp_b = insert_staff(shop_b, "E1", "社員B", "employee")
        tok = make_session("admin", admin_id)
        # shop_a のスタッフとして shop_b のスタッフを指定 → 404
        r = client.put(f"/api/admin/shops/{shop_a}/staffs/{emp_b}/role",
                       json={"role": "manager"}, headers=auth(tok))
        assert r.status_code == 404


class TestAdminPasswordReset:
    """システム管理者によるパスワードリセット。"""

    def test_reset_password_success(self, client):
        """管理者がスタッフのPWをリセットできる。"""
        from auth import verify_password
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee", password="OldPass1")
        tok = make_session("admin", admin_id)
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/password",
                       json={"new_password": "NewPass123"}, headers=auth(tok))
        assert r.status_code == 200
        # 新PWで認証できる
        row = dbmod.query_one("SELECT password_hash FROM staffs WHERE id=?", (emp_id,))
        assert verify_password("NewPass123", row["password_hash"])

    def test_reset_password_weak_rejected(self, client):
        """弱PWは拒否。"""
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee")
        tok = make_session("admin", admin_id)
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/password",
                       json={"new_password": "weak"}, headers=auth(tok))
        assert r.status_code == 400

    def test_reset_password_invalidates_session(self, client):
        """PWリセット後は既存セッション無効化。"""
        admin_id = insert_admin()
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee", password="OldPass1")
        emp_tok = make_session("staff", emp_id, shop_id)
        # 管理者がリセット
        admin_tok = make_session("admin", admin_id)
        client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/password",
                   json={"new_password": "NewPass123"}, headers=auth(admin_tok))
        # 古いトークンは無効化
        r = client.get("/api/staff/dashboard", headers=auth(emp_tok))
        assert r.status_code == 401

    def test_reset_password_non_admin_forbidden(self, client):
        """管理者以外はPWリセット不可。"""
        shop_id = insert_shop(code="S1")
        emp_id = insert_staff(shop_id, "E1", "社員", "employee")
        shop_tok = make_session("shop", shop_id, shop_id)
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/{emp_id}/password",
                       json={"new_password": "NewPass123"}, headers=auth(shop_tok))
        assert r.status_code == 403
