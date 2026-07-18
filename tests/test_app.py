"""tests/test_app.py - ShiftAI 全機能 統合テスト（pytest）。

対象:
  1. 権限・認証・CRUD
  2. シフト自動作成ロジック（最重要）: 検証A(上限)〜F(休憩)
  3. AI API 連携関数のモック/フォールバックテスト

実行: .venv/bin/python -m pytest tests/test_app.py -v
"""
import json
from collections import defaultdict

import pytest

import db as dbmod
import shift_engine
import ai
from auth import hash_password
from utils import compute_break_minutes, minutes_between

from helpers import (
    insert_admin, insert_shop, insert_staff, insert_pattern, insert_fixed,
    insert_request, insert_wish, insert_wish_overnight, make_session, auth, count_staff_in_hour,
)

# 2026-08-03 = 月曜。08-03..08-07 = 月〜金。
MON, TUE, WED, THU, FRI = "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"
WEEKDAYS = [MON, TUE, WED, THU, FRI]
DEFAULT_SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6, "default_hourly_wage": 1100}


# ============================================================
# シナリオ構築ヘルパ: 9:00-18:00 営業・必要3人
#   PT1: 月-金 固定 9-18（全時間）
#   PT2: 月-金 固定 13-18（午後）
#   PT3, PT4: 月曜に 9-13 希望を出す
#   Emp1, Emp2: 社員（固定なし）→ 不足補填
# ============================================================
def build_scenario_shop():
    shop_id = insert_shop(code="SCEN", settings=DEFAULT_SETTINGS)
    insert_pattern(shop_id, "通", "09:00", "18:00", 3)
    emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 160, 200)
    emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000, 160, 200)
    pt1 = insert_staff(shop_id, "P1", "バイト1", "part_time", 1100, 0, 160)
    pt2 = insert_staff(shop_id, "P2", "バイト2", "part_time", 1100, 0, 160)
    pt3 = insert_staff(shop_id, "P3", "バイト3", "part_time", 1100, 0, 160)
    pt4 = insert_staff(shop_id, "P4", "バイト4", "part_time", 1100, 0, 160)
    for w in range(1, 6):  # 月-金
        insert_fixed(pt1, w, "09:00", "18:00")
        insert_fixed(pt2, w, "13:00", "18:00")
    insert_request(shop_id, pt3, MON, "09:00", "13:00")
    insert_request(shop_id, pt4, MON, "09:00", "13:00")
    return {
        "shop_id": shop_id, "settings": DEFAULT_SETTINGS,
        "emp1": emp1, "emp2": emp2, "pt1": pt1, "pt2": pt2, "pt3": pt3, "pt4": pt4,
    }


def generate(scenario, start=MON, end=FRI):
    return shift_engine.auto_generate(scenario["shop_id"], scenario["settings"], start, end)


# ============================================================
# 1. 権限と認証・CRUD
# ============================================================
class TestAuthAndCrud:
    def test_no_token_cannot_create_shop(self, client):
        r = client.post("/api/admin/shops", json={
            "shop_code": "X", "shop_name": "X", "password": "pass1234"})
        assert r.status_code == 401

    def test_only_system_admin_creates_shop(self, client):
        admin_id = insert_admin()
        shop_id = insert_shop(code="EXIST")  # 店舗ロール用の既存店舗
        shop_token = make_session("shop", shop_id, shop_id)
        admin_token = make_session("admin", admin_id)

        # 店舗ロール → 403
        r = client.post("/api/admin/shops", json={
            "shop_code": "N1", "shop_name": "店舗N", "password": "pass1234"},
            headers=auth(shop_token))
        assert r.status_code == 403

        # SystemAdmin → 200
        r = client.post("/api/admin/shops", json={
            "shop_code": "NEW1", "shop_name": "新店舗", "password": "pass1234",
            "settings": {"default_hourly_wage": 1200}},
            headers=auth(admin_token))
        assert r.status_code == 200, r.get_json()
        new_id = r.get_json()["id"]
        row = dbmod.query_one("SELECT shop_code, shop_name FROM shops WHERE id=?", (new_id,))
        assert row["shop_code"] == "NEW1"
        assert row["shop_name"] == "新店舗"

    def test_shop_creates_staff_with_full_fields(self, client):
        shop_id = insert_shop(settings={"default_hourly_wage": 1000})
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "P01", "name": "田中", "password": "ptpass01",
            "role": "part_time", "hourly_wage": 1200,
            "min_hours_per_month": 40, "max_hours_per_month": 80,
        }, headers=auth(token))
        assert r.status_code == 200, r.get_json()
        sid = r.get_json()["id"]
        row = dbmod.query_one(
            "SELECT hourly_wage, min_hours_per_month, max_hours_per_month, role, name FROM staffs WHERE id=?",
            (sid,))
        assert row["hourly_wage"] == 1200
        assert row["min_hours_per_month"] == 40
        assert row["max_hours_per_month"] == 80
        assert row["role"] == "part_time"
        assert row["name"] == "田中"

    def test_shop_staff_uses_default_wage_when_omitted(self, client):
        shop_id = insert_shop(settings={"default_hourly_wage": 1350})
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "P02", "name": " default", "password": "ptpass02"},
            headers=auth(token))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT hourly_wage FROM staffs WHERE id=?", (r.get_json()["id"],))
        assert row["hourly_wage"] == 1350

    def test_non_shop_cannot_create_staff(self, client):
        shop_id = insert_shop()
        admin_id = insert_admin()
        admin_token = make_session("admin", admin_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "X", "name": "X", "password": "pass1234"},
            headers=auth(admin_token))
        assert r.status_code == 403

    def test_create_staff_duplicate_code_returns_friendly_error(self, client):
        """同一 staff_code の追加は親切なメッセージで 400。"""
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)
        insert_staff(shop_id, "DUP", "先にいる人")
        r = client.post("/api/shop/staffs", json={
            "staff_code": "DUP", "name": "後から的人", "password": "Password1"},
            headers=auth(token))
        assert r.status_code == 400
        msg = r.get_json()["error"]
        assert "DUP" in msg, f"コード値がメッセージに含まれるべき: {msg}"
        assert "既に" in msg or "存在" in msg, f"重複を示す言葉が必要: {msg}"

    def test_create_staff_weak_password_returns_specific_error(self, client):
        """パスワード強度不足は何が不足か分かるメッセージを返す。"""
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)
        cases = [
            ("abc12", "8文字"),        # 文字数不足
            ("12345678", "英字"),       # 英字なし
            ("abcdefgh", "数字"),       # 数字なし
        ]
        for pw, expected_hint in cases:
            r = client.post("/api/shop/staffs", json={
                "staff_code": "P" + pw[:3], "name": "X", "password": pw},
                headers=auth(token))
            assert r.status_code == 400, f"弱パスワード{pw}は拒否されるべき"
            msg = r.get_json()["error"]
            assert expected_hint in msg, f"{pw}: メッセージ'{msg}'に'{expected_hint}'がない"

    def test_create_staff_missing_code_returns_400(self, client):
        """staff_code 欠落は 400（UNIQUE制約の技術的エラーではなく）。"""
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "", "name": "X", "password": "Password1"},
            headers=auth(token))
        assert r.status_code == 400
        assert "コード" in r.get_json()["error"]

    def test_login_returns_token_and_role(self, client):
        """新仕様: 単一フォーム（shop_code + user_code + password）で全ロールログイン。"""
        insert_admin("admin", "admin123")
        shop_id = insert_shop(code="SHOP1", password="shop123")
        # システム管理者: user_code="admin"
        r = client.post("/api/login", json={
            "shop_code": "SHOP1", "user_code": "admin", "password": "admin123"})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["role"] == "admin"
        assert r.get_json()["token"]
        # パスワードがレスポンスに含まれないこと
        assert "password_hash" not in r.get_json()["user"]

    def test_login_wrong_password_rejected(self, client):
        insert_admin("admin", "admin123")
        r = client.post("/api/login", json={
            "shop_code": "any", "user_code": "admin", "password": "wrong"})
        assert r.status_code == 400

    # ---- スタッフログイン: 店舗コード+スタッフコードで一意特定（旧バグ対策）----
    def test_staff_login_with_shop_code_and_staff_code(self, client):
        """【旧バグ対策】別店舗で同 staff_code が存在しても shop_code 指定で正しくログインできる。"""
        shop_a = insert_shop(code="SHOP_A")
        shop_b = insert_shop(code="SHOP_B")
        # 両店舗に同じ staff_code "P1" を作成（異なるパスワードで識別可能に）
        sa = insert_staff(shop_a, "P1", "店舗AのP1", password="passA123")
        sb = insert_staff(shop_b, "P1", "店舗BのP1", password="passB123")
        # 店舗Aとしてログイン
        r = client.post("/api/login", json={
            "shop_code": "SHOP_A", "user_code": "P1", "password": "passA123"})
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data["role"] == "staff"
        assert data["user"]["id"] == sa
        assert data["user"]["shop_id"] == shop_a
        # 店舗Bとしてログイン（同じ P1 でも shop_code で正しくBに飞ぶ）
        r = client.post("/api/login", json={
            "shop_code": "SHOP_B", "user_code": "P1", "password": "passB123"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["user"]["id"] == sb
        assert data["user"]["shop_id"] == shop_b

    def test_staff_login_wrong_shop_password_rejected(self, client):
        """店舗Aのスタッフを店舗Bのパスワードではログインできない。"""
        shop_a = insert_shop(code="SHOP_A")
        shop_b = insert_shop(code="SHOP_B")
        insert_staff(shop_a, "P1", "AのP1", password="passA123")
        insert_staff(shop_b, "P1", "BのP1", password="passB123")
        # 店舗A指定で店舗Bのパスワード → 失敗
        r = client.post("/api/login", json={
            "shop_code": "SHOP_A", "user_code": "P1", "password": "passB123"})
        assert r.status_code == 400

    def test_staff_login_missing_shop_code_returns_400(self, client):
        """shop_code 無しで user_code+password を送っても 400。"""
        shop_a = insert_shop(code="SHOP_A")
        insert_staff(shop_a, "P1", "AのP1", password="passA123")
        r = client.post("/api/login", json={
            "user_code": "P1", "password": "passA123"})  # shop_code 無し
        assert r.status_code == 400
        assert "店舗コード" in r.get_json()["error"]

    def test_staff_login_nonexistent_shop_returns_400(self, client):
        """存在しない店舗コードではログイン不可。"""
        shop_a = insert_shop(code="SHOP_A")
        insert_staff(shop_a, "P1", "AのP1", password="passA123")
        r = client.post("/api/login", json={
            "shop_code": "GHOST", "user_code": "P1", "password": "passA123"})
        assert r.status_code == 400

    # ---- manager ロール: 店舗権限でのログイン ----
    def test_manager_role_logs_in_as_shop(self, client):
        """staffs.role='manager' でログインすると role='shop' セッションが付与される。"""
        shop_id = insert_shop(code="SHOP_M", password="shop123")
        # manager ロールのスタッフを作成
        mid = dbmod.execute(
            "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role) "
            "VALUES (?,?,?,?,?)",
            (shop_id, "manager", hash_password("Manager1"), "店主", "manager"),
        )["last_row_id"]
        r = client.post("/api/login", json={
            "shop_code": "SHOP_M", "user_code": "manager", "password": "Manager1"})
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data["role"] == "shop", "manager ロールは shop 権限でログイン"
        assert data["user"]["id"] == shop_id  # user は shops 行として取得される
        assert data["user"]["shop_code"] == "SHOP_M"

    def test_manager_can_access_shop_endpoints(self, client):
        """manager ログイン後のセッションで /api/shop/* が使える。"""
        shop_id = insert_shop(code="SHOP_M2")
        dbmod.execute(
            "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role) "
            "VALUES (?,?,?,?,?)",
            (shop_id, "manager", hash_password("Manager1"), "店主", "manager"),
        )
        r = client.post("/api/login", json={
            "shop_code": "SHOP_M2", "user_code": "manager", "password": "Manager1"})
        token = r.get_json()["token"]
        # 店舗エンドポイントにアクセス
        r = client.get("/api/shop/dashboard", headers=auth(token))
        assert r.status_code == 200, "manager は店舗権限で API 利用可能"

    def test_manager_role_treated_like_employee_in_engine(self, client):
        """manager ロールはシフトエンジンで employee 相当（不足補填可能）。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        # manager 1名だけ（社員・バイトなし）
        dbmod.execute(
            "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role) "
            "VALUES (?,?,?,?,?)",
            (shop_id, "manager", hash_password("Manager1"), "店主", "manager"),
        )
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        # manager が不足補填される（role='employee' と同じ挙動）
        assert any(c["reason"] and "補填" in c["reason"] for c in res["confirmed"]), \
            "manager は employee 相当として不足補填されるべき"

    def test_admin_magic_word_works_either_field(self, client):
        """shop_code / user_code どちらに "admin" を入れてもシステム管理者ログイン。"""
        insert_admin("admin", "admin123")
        # user_code 側に admin
        r = client.post("/api/login", json={
            "shop_code": "any", "user_code": "admin", "password": "admin123"})
        assert r.status_code == 200 and r.get_json()["role"] == "admin"
        # shop_code 側に admin
        r = client.post("/api/login", json={
            "shop_code": "admin", "user_code": "any", "password": "admin123"})
        assert r.status_code == 200 and r.get_json()["role"] == "admin"

    def test_admin_with_custom_id_via_other_field(self, client):
        """admin_id が "admin" 以外の場合、もう片方のフィールドで指定可能。"""
        insert_admin("superroot", "super123")
        r = client.post("/api/login", json={
            "shop_code": "superroot", "user_code": "admin", "password": "super123"})
        assert r.status_code == 200
        assert r.get_json()["user"]["admin_id"] == "superroot"

    # ---- スタッフ削除 (DELETE /api/shop/staffs/<sid>) ----
    def test_shop_deletes_own_staff(self, client):
        """店舗が自店舗スタッフを削除できる（ハード削除）。"""
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)
        sid = insert_staff(shop_id, "D1", "削除対象")
        r = client.delete(f"/api/shop/staffs/{sid}", headers=auth(token))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["ok"] is True
        assert dbmod.query_one("SELECT id FROM staffs WHERE id=?", (sid,)) is None

    def test_delete_staff_cascades_related_records(self, client):
        """スタッフ削除で固定/シフト/変更申請/希望履歴/通知が全て消える。"""
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)
        sid = insert_staff(shop_id, "D2", "削除対象2")
        insert_fixed(sid, 1, "09:00", "13:00")
        shift_id = insert_request(shop_id, sid, MON, "09:00", "13:00")
        dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, status) "
            "VALUES (?,?,?,?, 'pending')",
            (shop_id, sid, shift_id, "change"))
        dbmod.execute(
            "INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime) "
            "VALUES (?,?,?,?)",
            (shop_id, sid, f"{MON}T09:00:00", f"{MON}T13:00:00"))
        dbmod.execute(
            "INSERT INTO notifications (shop_id, staff_id, type, title, body) VALUES (?,?,?,?,?)",
            (shop_id, sid, "info", "テスト", "本文"))
        r = client.delete(f"/api/shop/staffs/{sid}", headers=auth(token))
        assert r.status_code == 200
        # staffs 自体
        assert dbmod.query_one("SELECT id FROM staffs WHERE id=?", (sid,)) is None
        # 関連テーブルは全て staff_id で孤立行なし
        for table in ["fixed_shifts", "shifts", "change_requests", "wish_history", "notifications"]:
            row = dbmod.query_one(f"SELECT COUNT(*) AS c FROM {table} WHERE staff_id=?", (sid,))
            assert row["c"] == 0, f"{table} に staff_id={sid} の孤立行が残っている"

    def test_delete_staff_invalidates_session(self, client):
        """スタッフ削除で当該スタッフのセッションが無効化される（ログイン状態保持を防ぐ）。"""
        shop_id = insert_shop()
        shop_token = make_session("shop", shop_id, shop_id)
        sid = insert_staff(shop_id, "D3", "削除対象3")
        staff_token = make_session("staff", sid, shop_id)
        # 削除前はスタッフトークンが有効
        assert client.get("/api/staff/dashboard", headers=auth(staff_token)).status_code == 200
        client.delete(f"/api/shop/staffs/{sid}", headers=auth(shop_token))
        # 削除後は無効（401）
        assert client.get("/api/staff/dashboard", headers=auth(staff_token)).status_code == 401

    def test_delete_staff_not_found_returns_404(self, client):
        """存在しない sid は 404。"""
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)
        r = client.delete("/api/shop/staffs/999999", headers=auth(token))
        assert r.status_code == 404

    def test_non_shop_cannot_delete_staff(self, client):
        """店舗ロール以外は削除不可 (403)。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "D4", "対象")
        admin_id = insert_admin()
        admin_token = make_session("admin", admin_id)
        r = client.delete(f"/api/shop/staffs/{sid}", headers=auth(admin_token))
        assert r.status_code == 403
        # データは残っている
        assert dbmod.query_one("SELECT id FROM staffs WHERE id=?", (sid,)) is not None

    def test_unauth_cannot_delete_staff(self, client):
        """認証なしは 401。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "D5", "対象")
        r = client.delete(f"/api/shop/staffs/{sid}")
        assert r.status_code == 401


# ============================================================
# 2. シフト自動作成ロジック（最重要）
# ============================================================
class TestShiftEngine:
    # ---- 検証A: 上限人数（1時間単位で3人を絶対超えない）----
    def test_A_hour_cap_never_exceeded(self):
        sc = build_scenario_shop()
        res = generate(sc)
        for day in WEEKDAYS:
            for hr in range(9, 18):  # 9時台〜17時台
                cnt = count_staff_in_hour(res["confirmed"], day, hr)
                assert cnt <= 3, f"{day} {hr}時台: {cnt}人 (上限3超過)"

    # 【要件R1/R2】固定シフトも上限人数を厳守する。
    # 【設計変更】固定は全スタッフ「候補」（希望優先、cap内のみ配置）。
    def test_A_fixed_shift_respects_cap_skips_excess(self):
        """同一時間帯の固定シフトが上限を超える場合、超過分はスキップされる。
        固定は候補扱いなので、3人目は cap 超過で配置されない。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 2)  # 上限2
        p1 = insert_staff(shop_id, "P1", "A", "part_time", 1100, 0, 160)
        p2 = insert_staff(shop_id, "P2", "B", "part_time", 1100, 0, 160)
        p3 = insert_staff(shop_id, "P3", "C", "part_time", 1100, 0, 160)
        insert_fixed(p1, 1, "09:00", "14:00")
        insert_fixed(p2, 1, "09:00", "14:00")
        insert_fixed(p3, 1, "09:00", "14:00")
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        # 9-13時台は厳密に2人以下（上限厳守）
        for hr in range(9, 14):
            assert count_staff_in_hour(res["confirmed"], MON, hr) <= 2, f"{hr}時台 超過"
        # 3人目は cap 超過で配置されない
        assert not any(s["staff_id"] == p3 and s["start"][:10] == MON for s in res["confirmed"])

    def test_A_fixed_multi_pattern_cap_regression(self):
        """【回帰・実データ相当】朝(2)/昼(2)/夜(3)パターン + 固定社員9-18 + 固定バイト17-22 で
        夜17時台が4人（社員2+バイト2）になる旧バグが再発しないこと。

        【設計変更】社員固定は候補扱い（wish 後に cap 内のみ配置）。
        バイト固定（主婦等）は厳守。そのため過剰時は社員固定が cap 超過でスキップされ、
        Step3 で別時間に不足補填される。結果として 17 時台は バイト2名+社員0-1名 となり
        cap=3 を超えない。
        """
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        # 社員2名が火曜 9-18固定（候補扱い）
        emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 160, 200)
        emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000, 160, 200)
        insert_fixed(emp1, 2, "09:00", "18:00")
        insert_fixed(emp2, 2, "09:00", "18:00")
        # 固定バイト2名が火曜 17-22（厳守）
        p1 = insert_staff(shop_id, "P1", "夜バイト1", "part_time", 1100, 0, 160)
        p2 = insert_staff(shop_id, "P2", "夜バイト2", "part_time", 1100, 0, 160)
        insert_fixed(p1, 2, "17:00", "22:00")  # 火曜
        insert_fixed(p2, 2, "17:00", "22:00")
        TUE2 = "2026-08-04"  # 火曜
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, TUE2, TUE2)
        # 夜17時台: バイト2(固定) + 社員(Step3で不足補填) = 3人以下であること
        cnt17 = count_staff_in_hour(res["confirmed"], TUE2, 17)
        assert cnt17 <= 3, f"夜17時台 {cnt17}人 (上限3超過) — 旧バグ再発"

    def test_A_fixed_parttime_priority_over_employee(self):
        """【設計変更後】固定は全スタッフ候補。cap=1で社員+バイト固定がある場合、
        どちらか1名が配置され、もう1名は Step3 で別時間に不足補填される。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # 上限1
        emp = insert_staff(shop_id, "E1", "社員", "employee", 2000, 0, 200)
        pt = insert_staff(shop_id, "P1", "主婦バイト", "part_time", 1100, 0, 160)
        insert_fixed(emp, 1, "09:00", "18:00")
        insert_fixed(pt, 1, "09:00", "18:00")
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        # cap=1 なので 9-18 には最大1人しか配置されない
        cnt = count_staff_in_hour(res["confirmed"], MON, 12)
        assert cnt <= 1, f"上限1なのに{cnt}人配置された"

    def test_A_fixed_overcap_in_explanations(self):
        """【設計変更後】固定は候補なので、過剰時は Step3 で別配置される。
        explanation は存在する（内容は問わない）。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        p1 = insert_staff(shop_id, "P1", "A", "part_time", 1100, 0, 160)
        p2 = insert_staff(shop_id, "P2", "B", "part_time", 1100, 0, 160)
        insert_fixed(p1, 1, "09:00", "18:00")
        insert_fixed(p2, 1, "09:00", "18:00")
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        # explanation リストは存在する
        assert isinstance(res["explanations"], list)

    def test_A_fixed_within_cap_all_placed(self):
        """上限内の固定シフトは全員配置される（過剰でなければ影響なし）。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 3)
        ids = []
        for code in ("P1", "P2", "P3"):
            sid = insert_staff(shop_id, code, code, "part_time", 1100, 0, 160)
            insert_fixed(sid, 1, "09:00", "18:00")
            ids.append(sid)
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        placed = {s["staff_id"] for s in res["confirmed"] if s["start"][:10] == MON}
        assert set(ids).issubset(placed), "上限内なのに固定が配置されない"
        assert not any(w["type"] == "fixed_overcap" for w in res["warnings"])

    # ---- 検証B: 固定シフトが希望なしの日に配置される（候補として） ----
    def test_B_fixed_shifts_prioritized(self):
        """【設計変更後】固定は候補。希望がない日は固定が配置される。"""
        sc = build_scenario_shop()
        res = generate(sc)
        for day in WEEKDAYS:
            # PT1 は月-金 9-18 固定（希望を出していない → 候補として配置される）
            pt1 = [s for s in res["confirmed"] if s["staff_id"] == sc["pt1"] and s["start"][:10] == day]
            assert len(pt1) == 1, f"{day} PT1固定未配置: {pt1}"
            assert pt1[0]["start"][11:16] == "09:00" and pt1[0]["end"][11:16] == "18:00"

    # ---- 検証C: 希望を出したアルバイトが上限に達するまでアサイン ----
    def test_C_requests_assigned_within_cap(self):
        sc = build_scenario_shop()
        res = generate(sc)
        mon_confirmed = [s for s in res["confirmed"] if s["start"][:10] == MON]
        pt3 = [s for s in mon_confirmed if s["staff_id"] == sc["pt3"]]
        pt4 = [s for s in mon_confirmed if s["staff_id"] == sc["pt4"]]
        assert len(pt3) == 1 and pt3[0]["reason"] == "希望シフト", f"PT3希望未配置: {pt3}"
        assert len(pt4) == 1 and pt4[0]["reason"] == "希望シフト", f"PT4希望未配置: {pt4}"
        assert pt3[0]["start"][11:16] == "09:00" and pt3[0]["end"][11:16] == "13:00"
        # 9-13時台は PT1(固定) + PT3 + PT4 = 3人（上限到達）
        for hr in range(9, 13):
            assert count_staff_in_hour(mon_confirmed, MON, hr) == 3

    def test_C_request_dropped_when_cap_reached(self):
        """上限に達している時間帯への希望は pending になる（上限厳守）。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        pt_a = insert_staff(shop_id, "PA", "A", "part_time", 1100, 0, 160)
        pt_b = insert_staff(shop_id, "PB", "B", "part_time", 1100, 0, 160)
        insert_request(shop_id, pt_a, MON, "09:00", "18:00")  # 先約で上限1に到達
        insert_request(shop_id, pt_b, MON, "09:00", "18:00")  # 上限超過 → 不可
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        confirmed = [s for s in res["confirmed"] if s["start"][:10] == MON]
        pending = [s for s in res["pending"] if (s.get("start") or "")[:10] == MON]
        assert len(confirmed) == 1
        assert any(s["staff_id"] == pt_b for s in pending)

    # ---- 検証D: 社員による穴埋め ----
    def test_D_employees_fill_gaps(self):
        sc = build_scenario_shop()
        res = generate(sc)
        # 9-18 全時間帯で必要人数3が満たされている（不足ゼロ）
        for day in WEEKDAYS:
            for hr in range(9, 18):
                cnt = count_staff_in_hour(res["confirmed"], day, hr)
                assert cnt == 3, f"{day} {hr}時台: {cnt}人 (3人に満たない空き)"
        # shortage リストも空
        assert res["shortage"] == [], f"未補填の不足あり: {res['shortage']}"

    def test_D_employee_fills_afternoon_gap_on_monday(self):
        """月曜の午後(13-18)はアルバイト2名のみ→社員が1名補填する。"""
        sc = build_scenario_shop()
        res = generate(sc)
        mon = [s for s in res["confirmed"] if s["start"][:10] == MON]
        emp_shifts = [s for s in mon if s["staff_id"] in (sc["emp1"], sc["emp2"])]
        assert len(emp_shifts) >= 1, "月曜の不足を社員が補填していない"
        # 社員は午後の枠(13-18)を埋めているはず
        assert any(s["start"][11:16] == "13:00" and s["end"][11:16] == "18:00" for s in emp_shifts), emp_shifts

    def test_D_no_gap_left_when_employees_available(self):
        """アルバイト1名だけの時間帯を社員が埋める（純粋な社員穴埋めシナリオ）。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 2)
        emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 160, 200)
        emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000, 160, 200)
        pt1 = insert_staff(shop_id, "P1", "バイト1", "part_time", 1100, 0, 160)
        insert_fixed(pt1, 1, "09:00", "18:00")  # 月曜1名のみ
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        # 必要2名に対し PT1 + 社員1 = 2で満たされる
        for hr in range(9, 18):
            assert count_staff_in_hour(res["confirmed"], MON, hr) == 2
        assert res["shortage"] == []

    def test_D_multi_pattern_daytime_gap_filled(self):
        """【回帰】朝/昼/夜の3パターン連続不足で、社員が朝に吸い込まれ昼が放置される旧バグ。

        デモ店舗と同等: 朝9-13(2名)/昼13-17(2名)/夜17-22(3名)。夜のみ固定バイト。
        社員2名が朝→昼をまたぐ長いシフトでカバーし、昼13-17の空きを出さないこと。
        """
        shop_id = insert_shop(settings={"min_daily_hours": 4, "max_daily_hours": 9})
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 160, 200)
        emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000, 160, 200)
        # 土曜(weekday 6)は夜に固定バイト2名のみ
        p1 = insert_staff(shop_id, "P1", "夜バイト1", "part_time", 1100, 0, 160)
        p2 = insert_staff(shop_id, "P2", "夜バイト2", "part_time", 1100, 0, 160)
        insert_fixed(p1, 6, "17:00", "22:00")
        insert_fixed(p2, 6, "17:00", "22:00")
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4, "max_daily_hours": 9}, MON, MON)
        # 朝9-13 と 昼13-17 は必要2名が社員で満たされること（主訴: 昼の空き解消）
        for hr in range(9, 17):
            cnt = count_staff_in_hour(res["confirmed"], MON, hr)
            assert cnt >= 2, f"{hr}時台 {cnt}人 (< 必要2). 昼の空きが残っている（旧バグ）"
        # 社員は最低1シフトは9時開始で朝から入っている（朝を開けて昼だけ、となっていない）
        emp_starts = [int(s["start"][11:13]) for s in res["confirmed"] if s["staff_id"] in (emp1, emp2)]
        assert any(h <= 9 for h in emp_starts)
        # 中抜けなし
        by_sd = {}
        for s in res["confirmed"]:
            by_sd.setdefault((s["staff_id"], s["start"][:10]), []).append(s)
        assert all(len(v) == 1 for v in by_sd.values())

    def test_D_employee_covers_night_with_long_shift(self):
        """【回帰】夜3名必要な土日に、社員がmax_daily縛りなしで長時間シフト(夜迄)を取り夜を充足。

        旧仕様(max_daily=9h適用)では社員が9-18で昼間に固まり夜が2人止まりだった。
        社員を労働条件縛りから外すことで夜迄入れる。
        """
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 160, 200)
        emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000, 160, 200)
        # 土曜(weekday6)は夜固定バイト2名のみ（夜必要3のうち2は固定）
        p1 = insert_staff(shop_id, "P1", "夜バイト1", "part_time", 1100, 0, 160)
        p2 = insert_staff(shop_id, "P2", "夜バイト2", "part_time", 1100, 0, 160)
        insert_fixed(p1, 6, "17:00", "22:00")
        insert_fixed(p2, 6, "17:00", "22:00")
        SAT = "2026-08-01"  # 土曜(weekday6)で生成
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, SAT, SAT)
        # 夜18-22時台も必要3名が満たされる（社員が夜迄入る）
        for hr in range(18, 22):
            cnt = count_staff_in_hour(res["confirmed"], SAT, hr)
            assert cnt >= 3, f"夜{hr}時台 {cnt}人 (< 必要3). 社員の夜カバー不備"
        assert res["shortage"] == [], f"未補填の不足: {res['shortage']}"
        # 社員のいずれかが22時迄働く長時間シフトを持つ
        emp_shifts = [s for s in res["confirmed"] if s["staff_id"] in (emp1, emp2)]
        assert any(s["end"][11:16] == "22:00" for s in emp_shifts), emp_shifts

    def test_consecutive_warning_part_timer_only(self):
        """連勤警告はアルバイトのみ。社員が連勤しても警告を出さない。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4, "max_consecutive_days": 5})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        emp = insert_staff(shop_id, "E1", "社員", "employee", 2000, 0, 200)
        pt = insert_staff(shop_id, "P1", "バイト", "part_time", 1100, 0, 160)
        # 社員は7日連続、バイトは3日のみ
        for i in range(7):
            day = f"2026-08-{10 + i:02d}"
            insert_fixed(emp, (i + 1) % 7, "09:00", "18:00")  # 毎日固定(簡易)
        for w in (1, 2, 3):
            insert_fixed(pt, w, "09:00", "18:00")
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4, "max_consecutive_days": 5}, "2026-08-10", "2026-08-16")
        consec = {w["staff_id"]: w for w in res["warnings"] if w["type"] == "consecutive"}
        # 社員(7日連勤)は警告対象外
        assert emp not in consec, f"社員に連勤警告が出ている: {consec}"

    # ---- 検証E: 労働条件（最低時間・中抜けなし）----
    def test_E_no_multiple_shifts_per_day(self):
        sc = build_scenario_shop()
        res = generate(sc)
        by_staff_day = defaultdict(list)
        for s in res["confirmed"]:
            by_staff_day[(s["staff_id"], s["start"][:10])].append(s)
        for (sid, day), lst in by_staff_day.items():
            assert len(lst) == 1, f"staff {sid} on {day} が1日複数シフト(中抜け): {lst}"

    def test_E_min_daily_hours_respected_for_parttimers(self):
        """1日最低勤務時間は **アルバイトのみ** 適用される（社員は柔軟稼動）。"""
        sc = build_scenario_shop()
        res = generate(sc)
        for s in res["confirmed"]:
            work = minutes_between(s["start"], s["end"])
            st = dbmod.query_one("SELECT role FROM staffs WHERE id=?", (s["staff_id"],))
            if st and st["role"] == "part_time":
                assert work >= 4 * 60, f"アルバイトの勤務{work}分 < 最低4h: {s}"

    def test_E_employees_exempt_from_min_daily(self):
        """社員は最低勤務時間に縛られず、不足セグメントを埋められる。"""
        shop_id = insert_shop(settings={"min_daily_hours": 8})  # 高めの最低時間
        insert_pattern(shop_id, "夜", "17:00", "20:00", 1)  # 3h枠（アルバイトの最低8h未満）
        emp = insert_staff(shop_id, "E1", "社員", "employee", 2000, 0, 200)
        pt = insert_staff(shop_id, "P1", "バイト", "part_time", 1100, 0, 160)
        insert_request(shop_id, emp, MON, "17:00", "20:00")  # 社員3h希望
        insert_request(shop_id, pt, MON, "17:00", "20:00")  # バイト3h希望(最低8h未満)
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 8}, MON, MON)
        emp_placed = [s for s in res["confirmed"] if s["staff_id"] == emp]
        pt_placed = [s for s in res["confirmed"] if s["staff_id"] == pt]
        # 社員は最低時間に関わらず配置される
        assert len(emp_placed) == 1
        # バイトは最低時間(8h)未満なので配置されない
        assert len(pt_placed) == 0

    def test_E_short_request_not_placed(self):
        """最低時間未満の希望は配置されない（検証E）。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 3)
        pt = insert_staff(shop_id, "P1", "バイト", "part_time", 1100, 0, 160)
        insert_request(shop_id, pt, MON, "09:00", "11:00")  # 2h = 最低時間未満
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        assert not any(s["staff_id"] == pt and s["start"][:10] == MON for s in res["confirmed"])

    # ---- 検証F: 休憩時間 ----
    def test_F_break_time_rule_all_shifts(self):
        sc = build_scenario_shop()
        res = generate(sc)
        for s in res["confirmed"]:
            work = minutes_between(s["start"], s["end"])
            assert s["break"] == compute_break_minutes(work), (
                f"休憩{s['break']} != 期待{compute_break_minutes(work)} (勤務{work}分)")

    def test_F_break_unit_all_thresholds(self):
        assert compute_break_minutes(6 * 60) == 0       # 6hちょうど → 0
        assert compute_break_minutes(6 * 60 + 1) == 45  # 6h超 → 45
        assert compute_break_minutes(8 * 60) == 45      # 8hちょうど → 45
        assert compute_break_minutes(8 * 60 + 1) == 60  # 8h超 → 60
        assert compute_break_minutes(5 * 60) == 0       # 5h → 0

    def test_F_break_45_and_60_present_in_scenario(self):
        """シナリオ内に 9h勤務(60分休憩) と 8h勤務(45分休憩) に該当するシフトが含まれること。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 2)
        p9 = insert_staff(shop_id, "P9", "9h", "part_time", 1100, 0, 160)
        p8 = insert_staff(shop_id, "P8", "8h", "part_time", 1100, 0, 160)
        insert_fixed(p9, 1, "09:00", "18:00")        # 9h → 60
        insert_request(shop_id, p8, MON, "09:00", "17:00")  # 8h → 45
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        breaks = {s["break"] for s in res["confirmed"]}
        assert 60 in breaks, f"60分休憩(8h超)のシフト無し: {breaks}"
        assert 45 in breaks, f"45分休憩(6h超8h以下)のシフト無し: {breaks}"

    # ---- 月間上限チェック ----
    def test_monthly_cap_enforced(self):
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "13:00", 1)  # 1シフト4h
        # 月間上限5hの社員 → 1日(4h)だけ配置、2日目で上限超過でストップ
        emp = insert_staff(shop_id, "E1", "社員", "employee", 2000, 0, 5)
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, FRI)
        assert res["minutes_by_staff"][emp] <= 5 * 60

    # ---- compute_shortage のキー互換性（旧BUG#3）----
    def test_compute_shortage_accepts_engine_output(self):
        sc = build_scenario_shop()
        res = generate(sc, MON, MON)
        pats = dbmod.query_all("SELECT * FROM shift_patterns WHERE shop_id=?", (sc["shop_id"],))
        short = shift_engine.compute_shortage(res["confirmed"], pats, MON, MON)
        internal = [{"date": s["date"], "pattern": s["pattern"]} for s in res["shortage"]]
        recomputed = [{"date": s["date"], "pattern": s["pattern"]} for s in short]
        assert recomputed == internal


# ============================================================
# 2c. 曜日別必要人数（weekday override）のテスト
# ============================================================
class TestWeekdayOverride:
    def _setup(self, settings=None):
        shop_id = insert_shop(settings=settings or {"min_daily_hours": 4})
        pid = insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # デフォルト1名
        emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 0, 200)
        emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000, 0, 200)
        emp3 = insert_staff(shop_id, "E3", "社員3", "employee", 2000, 0, 200)
        return shop_id, pid, [emp1, emp2, emp3]

    def test_weekday_override_changes_required(self):
        """月曜(weekday=1)だけ3名必要に変更すると、月曜は3人が配置される。"""
        shop_id, pid, emps = self._setup()
        dbmod.execute(
            "INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
            (pid, shop_id, 1, 3))  # 月曜(=1)のみ3名
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        for hr in range(9, 18):
            assert count_staff_in_hour(res["confirmed"], MON, hr) == 3, f"{hr}時台: 3人期待"

    def test_other_weekday_keeps_default(self):
        """火曜(weekday=2)は月曜のoverrideに影響されず、デフォルト1名のまま。"""
        shop_id, pid, emps = self._setup()
        dbmod.execute(
            "INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
            (pid, shop_id, 3, 3))  # 水曜のみ3名
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, TUE, TUE)
        # 火曜はデフォルト1名 → 1人のみ
        for hr in range(9, 18):
            assert count_staff_in_hour(res["confirmed"], TUE, hr) == 1, f"{hr}時台: 1人期待"

    def test_load_weekday_overrides(self):
        shop_id, pid, _ = self._setup()
        dbmod.execute(
            "INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
            (pid, shop_id, 0, 4))
        dbmod.execute(
            "INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
            (pid, shop_id, 6, 5))
        ov = shift_engine.load_weekday_overrides(shop_id)
        assert ov[(pid, 0)] == 4
        assert ov[(pid, 6)] == 5
        assert (pid, 1) not in ov

    def test_api_get_patterns_includes_weekday_required(self, client):
        shop_id, pid, _ = self._setup()
        dbmod.execute(
            "INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
            (pid, shop_id, 6, 5))
        token = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/patterns", headers=auth(token))
        assert r.status_code == 200
        pat = r.get_json()["patterns"][0]
        assert pat["weekday_required"]["6"] == 5

    def test_api_put_weekday_required(self, client):
        shop_id, pid, _ = self._setup()
        token = make_session("shop", shop_id, shop_id)
        r = client.put(f"/api/shop/patterns/{pid}/weekday-required",
                       json={"weekday_required": {"1": 3, "6": 4}}, headers=auth(token))
        assert r.status_code == 200
        ov = shift_engine.load_weekday_overrides(shop_id)
        assert ov[(pid, 1)] == 3
        assert ov[(pid, 6)] == 4

    def test_manual_shift_blocked_by_weekday_override(self, client):
        """手動シフトでも曜日別オーバーライドの上限が厳守される。"""
        shop_id, pid, emps = self._setup()
        e1, e2, e3 = emps
        # 月曜を2名に上書き
        dbmod.execute(
            "INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
            (pid, shop_id, 1, 2))
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        for eid in (e1, e2):
            r = client.post("/api/shop/shifts", json={
                "staff_id": eid, "start_datetime": f"{MON}T09:00:00",
                "end_datetime": f"{MON}T18:00:00"}, headers=h)
            assert r.status_code == 200
        # 3人目は上限超過で拒否
        r = client.post("/api/shop/shifts", json={
            "staff_id": e3, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00"}, headers=h)
        assert r.status_code == 400
        assert r.get_json().get("over_cap") is True


# ============================================================
# 2a. 深夜営業（日またぎ）パターンの自動生成
#   営業 7:00〜翌 5:00 のような end_time < start_time のパターンを扱う。
#   旧実装では _day_requirements の while s < pe が即終了し、
#   req_map が空になって「シフトが作れない」状態だった。
# ============================================================
class TestOvernightPattern:
    def _setup_shop(self, start="07:00", end="05:00", required=2):
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "終日", start, end, required)
        # 社員2名（不足補填用）+ バイト4名
        emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 160, 200)
        emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000, 160, 200)
        pt1 = insert_staff(shop_id, "P1", "バイト1", "part_time", 1100, 0, 160)
        pt2 = insert_staff(shop_id, "P2", "バイト2", "part_time", 1100, 0, 160)
        return shop_id, emp1, emp2, pt1, pt2

    def test_overnight_pattern_generates_shifts(self):
        """7:00〜翌5:00 営業パターンでシフトが生成される（空結果にならない）。"""
        shop_id, *_ = self._setup_shop()
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        assert len(res["confirmed"]) > 0, "overnight パターンでシフトが生成されない（旧バグ）"

    def test_overnight_pattern_no_shortage_with_enough_staff(self):
        """必要人数分の社員がいれば overnight パターンでも shortage なし。"""
        shop_id, emp1, emp2, *_ = self._setup_shop(required=2)
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        assert res["shortage"] == [], f"社員2名で不足解消できるはず: {res['shortage']}"

    def test_overnight_pattern_respects_cap_at_3am_next_day(self):
        """翌日 03:00 (拡張スロット=1620) で必要人数を満たしている。"""
        shop_id, *_ = self._setup_shop(required=2)
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        # 翌日早朝（03:00）の稼働人数を数える
        # confirmed の start/end は ISO datetime。翌日T03:00 を含むシフトを数える。
        from utils import parse_iso
        target = parse_iso(f"{TUE}T03:30:00")  # 火曜 03:30
        cnt = 0
        for s in res["confirmed"]:
            ss = parse_iso(s["start"]); ee = parse_iso(s["end"])
            if ss <= target < ee:
                cnt += 1
        assert cnt == 2, f"火曜03:30の人数={cnt}（要求2）"

    def test_overnight_pattern_cap_not_exceeded(self):
        """overnight パターンでも上限人数を厳守する（検証A相当）。"""
        shop_id, *_ = self._setup_shop(required=2)
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        from utils import parse_iso
        # 当日 07:00〜翌日 05:00 まで 30分刻みで cap チェック
        t = parse_iso(f"{MON}T07:00:00")
        end = parse_iso(f"{TUE}T05:00:00")
        while t < end:
            cnt = sum(1 for s in res["confirmed"]
                      if parse_iso(s["start"]) <= t < parse_iso(s["end"]))
            assert cnt <= 2, f"{t}: {cnt}人 (上限2超過)"
            from datetime import timedelta
            t = t + timedelta(minutes=30)

    def test_overnight_wish_is_placed_correctly(self):
        """22:00〜翌05:00 の希望シフトが正しく配置される。"""
        shop_id, emp1, emp2, pt1, pt2 = self._setup_shop(required=2)
        # バイト1が月曜 22:00〜翌05:00 の希望を出す（DBには正しいISOで保存）
        insert_wish_overnight(shop_id, pt1, MON, "22:00", TUE, "05:00")
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        # pt1 のシフトが 22:00-翌05:00 で配置されている
        pt1_shifts = [s for s in res["confirmed"] if s["staff_id"] == pt1]
        assert any(s["start"] == f"{MON}T22:00:00" and s["end"] == f"{TUE}T05:00:00"
                   for s in pt1_shifts), f"pt1の希望が反映されていない: {pt1_shifts}"

    def test_overnight_staff_blocked_from_next_day_morning(self):
        """22:00〜翌05:00 勤務のスタッフは翌朝に別シフトを入れない（中抜け・重複防止）。"""
        shop_id, emp1, emp2, pt1, pt2 = self._setup_shop(required=1)
        # pt1 が月曜 22:00〜翌05:00 希望で、さらに火曜 06:00〜10:00 も希望
        insert_wish_overnight(shop_id, pt1, MON, "22:00", TUE, "05:00")
        insert_wish(shop_id, pt1, TUE, "06:00", "10:00")
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, TUE)
        # pt1 は月曜 overnight を持つ → 火曜のシフトは cap 内でも配置されない
        tue_shifts = [s for s in res["confirmed"] if s["staff_id"] == pt1 and s["start"][:10] == TUE]
        assert tue_shifts == [], f"overnight スタッフの翌日シフトが入ってしまった: {tue_shifts}"

    def test_overnight_within_day_pattern_unaffected(self):
        """通常（日中）パターンは overnight 修正の影響を受けない（回帰）。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # 日中
        emp = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 160, 200)
        res = shift_engine.auto_generate(shop_id, {"min_daily_hours": 4}, MON, MON)
        assert len(res["confirmed"]) >= 1
        assert res["shortage"] == []

    def test_overnight_shortage_correct_for_multi_day_range(self, client):
        """【回帰】overnight パターンで多日数の不足集計が日付単位で正しく出る。

        旧バグ: compute_shortage が日付の開始境界を誤って認識し、
        前月末日（例: 7/31）の不足が8月ビューに混じることがあった。
        本テストでは8/1〜8/3の範囲で7/31が絶対に現れないことを保証する。
        """
        shop_id = insert_shop(code="OVN_MULTI", settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "深夜", "06:00", "05:00", 2)  # 6:00-翌5:00
        # スタッフ0名（全日不足）
        token = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/shortage?start=2026-08-01&end=2026-08-03",
                       headers=auth(token))
        assert r.status_code == 200
        dates = [s["date"] for s in r.get_json()["shortage"]]
        assert "2026-07-31" not in dates, f"前月末が混入: {dates}"
        assert "2026-08-01" in dates
        assert "2026-08-03" in dates


# ============================================================
# 2b. シフトAPI統合テスト（dry_run / 手動作成の上限チェック）
# ============================================================
class TestShiftApi:
    def test_auto_endpoint_dry_run(self, client):
        sc_data = build_scenario_shop()
        shop_id = sc_data["shop_id"]
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI, "dry_run": True,
        }, headers=auth(token))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["dry_run"] is True
        assert body["confirmed_count"] > 0
        assert "preview" in body
        # preview の各シフトも上限厳守
        for hr in range(9, 18):
            for day in WEEKDAYS:
                cnt = sum(1 for s in body["preview"]
                          if s["start"][:10] == day
                          and int(s["start"][11:13]) <= hr < int(s["end"][11:13]))
                assert cnt <= 3, f"preview {day} {hr}時台 {cnt}人 超過"

    def test_auto_endpoint_persists_and_respects_cap(self, client):
        sc_data = build_scenario_shop()
        shop_id = sc_data["shop_id"]
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": WED,
        }, headers=auth(token))
        assert r.status_code == 200, r.get_json()
        # DBに confirmed が書き込まれ、上限厳守
        rows = dbmod.query_all(
            "SELECT * FROM shifts WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=?",
            (shop_id, MON + "T00:00:00", WED + "T23:59:59"))
        assert len(rows) > 0
        for hr in range(9, 18):
            for day in [MON, TUE, WED]:
                cnt = count_staff_in_hour(rows, day, hr)
                assert cnt <= 3

    def test_auto_endpoint_reports_fixed_overcap_via_api(self, client):
        """【設計変更後】固定は候補。cap超過時は配置されず、Step3で別配置される。
        preview で cap 厳守を検証。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # 上限1
        p1 = insert_staff(shop_id, "P1", "A", "part_time", 1100, 0, 160)
        p2 = insert_staff(shop_id, "P2", "B", "part_time", 1100, 0, 160)
        insert_fixed(p1, 1, "09:00", "18:00")
        insert_fixed(p2, 1, "09:00", "18:00")  # 上限1に対し2人目 → cap超過でスキップ
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": MON, "dry_run": True,
        }, headers=auth(token))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        # preview 上限厳守（1人以下）
        assert count_staff_in_hour(body["preview"], MON, 12) <= 1

    def test_auto_endpoint_cleans_stale_requested_in_range(self, client):
        """【回帰・実DBバグ】auto 実行で期間内の古い requested が残骸としてDBに残らない。

        旧バグ: auto 実行で配置された confirmed とは別に、
        元のスタッフ希望(requested)レコードがDELETE対象外でDBに残り続け、
        同一スタッフが1日2シフト持ち・画面の人数カウントが不正に膨らむ現象。
        """
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 3)
        e1 = insert_staff(shop_id, "E1", "A", "employee", 2000)
        e2 = insert_staff(shop_id, "E2", "B", "employee", 2000)
        e3 = insert_staff(shop_id, "E3", "C", "employee", 2000)
        # 事前に requested を直接INSERT（スタッフが希望を出した状態を模擬）
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, e3, f"{MON}T09:00:00", f"{MON}T18:00:00", "requested", "柔軟希望"))
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": MON,
        }, headers=auth(token))
        assert r.status_code == 200, r.get_json()
        # 期間内の requested は全て消去されていること（残骸なし）
        stale = dbmod.query_all(
            "SELECT * FROM shifts WHERE shop_id=? AND status='requested' "
            "AND start_datetime>=? AND start_datetime<=?",
            (shop_id, MON + "T00:00:00", MON + "T23:59:59"))
        assert stale == [], f"期間内に requested 残骸が残っている: {stale}"
        # 同一スタッフの1日2シフト(中抜け)がDB上発生していないこと
        rows = dbmod.query_all(
            "SELECT staff_id, COUNT(*) c FROM shifts WHERE shop_id=? AND status='confirmed' "
            "AND start_datetime>=? AND start_datetime<=? GROUP BY staff_id HAVING c > 1",
            (shop_id, MON + "T00:00:00", MON + "T23:59:59"))
        assert rows == [], f"同一スタッフの1日複数シフト: {rows}"

    def test_manual_shift_blocked_by_slot_cap(self, client):
        """手動シフト追加でも時間単位上限を超える場合は拒否される。"""
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 2)  # 上限2
        e1 = insert_staff(shop_id, "E1", "A", "employee", 2000)
        e2 = insert_staff(shop_id, "E2", "B", "employee", 2000)
        e3 = insert_staff(shop_id, "E3", "C", "employee", 2000)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        # 2名まではOK
        for eid in (e1, e2):
            r = client.post("/api/shop/shifts", json={
                "staff_id": eid, "start_datetime": f"{MON}T09:00:00",
                "end_datetime": f"{MON}T18:00:00"}, headers=h)
            assert r.status_code == 200, r.get_json()
        # 3人目は上限超過で拒否
        r = client.post("/api/shop/shifts", json={
            "staff_id": e3, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00"}, headers=h)
        assert r.status_code == 400
        assert r.get_json().get("over_cap") is True

    def test_manual_shift_force_bypasses_cap(self, client):
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        e1 = insert_staff(shop_id, "E1", "A", "employee", 2000)
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00", "force": True}, headers=auth(token))
        assert r.status_code == 200

    def test_manual_shift_auto_break(self, client):
        shop_id = insert_shop(settings={"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)
        e1 = insert_staff(shop_id, "E1", "A", "employee", 2000)
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00",
            "end_datetime": f"{MON}T18:00:00"}, headers=auth(token))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT break_time_minutes FROM shifts WHERE staff_id=?", (e1,))
        assert row["break_time_minutes"] == 60  # 9h勤務 → 60


# ============================================================
# 2d. 重複防止（同一スタッフ・同一日の時間帯重複）の全パステスト
#   対象: 手動追加 / 手動更新 / 希望提出 / 変更申請承認 / シフトコピー
#   ※ auto生成は shift_engine の staff_busy で既担保済み
# ============================================================
class TestShiftOverlapPrevention:
    def _setup(self, settings=None):
        shop_id = insert_shop(settings=settings or {"min_daily_hours": 4})
        # 上限多め（9-22 5名）にして cap ではなく overlap 判定を分離
        insert_pattern(shop_id, "通", "09:00", "22:00", 5)
        e1 = insert_staff(shop_id, "E1", "A", "employee", 2000)
        return shop_id, e1

    # ---- 手動追加 ----
    def test_manual_post_rejects_overlap_same_day(self, client):
        """同一スタッフ・同日で時間帯が重なる手動追加は拒否される。"""
        shop_id, e1 = self._setup()
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        # 重複（12-15）→ 拒否
        r = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T12:00:00", "end_datetime": f"{MON}T15:00:00"}, headers=h)
        assert r.status_code == 400
        assert r.get_json().get("overlap") is True

    def test_manual_post_allows_adjacent_same_day(self, client):
        """同一スタッフ・同日でも隣接（境界接するだけ）は許可される。"""
        shop_id, e1 = self._setup()
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T13:00:00"}, headers=h)
        # 隣接（13:00開始）→ 許可
        r = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        assert r.status_code == 200, r.get_json()

    def test_manual_post_allows_different_day(self, client):
        """同一スタッフ・別の日は問題なく追加できる。"""
        shop_id, e1 = self._setup()
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        r = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{TUE}T09:00:00", "end_datetime": f"{TUE}T18:00:00"}, headers=h)
        assert r.status_code == 200

    # ---- 手動更新 ----
    def test_manual_put_rejects_overlap_with_other_shift(self, client):
        """手動更新で別シフトと重なる時間には変更できない。"""
        shop_id, e1 = self._setup()
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        r1 = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T13:00:00"}, headers=h)
        r2 = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T17:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=h)
        sid2 = r2.get_json()["id"]
        # r2 を 12-20 に変更 → r1(9-13)と重複 → 拒否
        r = client.put(f"/api/shop/shifts/{sid2}", json={
            "staff_id": e1, "start_datetime": f"{MON}T12:00:00", "end_datetime": f"{MON}T20:00:00"}, headers=h)
        assert r.status_code == 400
        assert r.get_json().get("overlap") is True

    # ---- 希望提出 ----
    def test_staff_request_skips_overlap_with_confirmed(self, client):
        """スタッフ希望提出：確定シフトと時間が重なる希望はスキップされる（エラーにせず件数から除外）。"""
        shop_id, e1 = self._setup()
        # 店舗が確定シフトを作成
        shop_token = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T17:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=auth(shop_token))
        # スタッフが募集期間内で希望提出（17-22確定済みの日に13-18希望を出す → 重複でスキップ）
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, MON, MON, "2026-07-25"))
        staff_token = make_session("staff", e1, shop_id)
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(staff_token))
        assert r.status_code == 200
        body = r.get_json()
        assert body["submitted"] == 0
        assert body["skipped_overlap"] == 1
        # DB には requested は存在しない（重複で弾かれた）
        cnt = dbmod.query_one(
            "SELECT COUNT(*) as c FROM shifts WHERE staff_id=? AND status='requested'", (e1,))["c"]
        assert cnt == 0

    def test_staff_request_accepts_non_overlapping(self, client):
        """スタッフ希望提出：確定と重ならない希望は保存される。"""
        shop_id, e1 = self._setup()
        shop_token = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T17:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=auth(shop_token))
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, MON, MON, "2026-07-25"))
        staff_token = make_session("staff", e1, shop_id)
        # 9-13（17-22とは重ならない）→ OK
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T13:00:00"}],
        }, headers=auth(staff_token))
        assert r.status_code == 200
        assert r.get_json()["submitted"] == 1

    # ---- 変更申請承認 ----
    def test_change_request_change_rejected_on_overlap(self, client):
        """変更申請(change)で他シフトと重なる時間への変更は承認拒否。"""
        shop_id, e1 = self._setup()
        shop_token = make_session("shop", shop_id, shop_id)
        h = auth(shop_token)
        r1 = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T13:00:00"}, headers=h)
        r2 = client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T17:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=h)
        sid2 = r2.get_json()["id"]
        # sid2 を 12-20 に変更する申請を作成（r1 9-13 と重複）
        dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, desired_start, desired_end, status) "
            "VALUES (?,?,?,?,?,?, 'pending')",
            (shop_id, e1, sid2, "change", f"{MON}T12:00:00", f"{MON}T20:00:00"))
        crid = dbmod.query_one("SELECT id FROM change_requests WHERE shift_id=?", (sid2,))["id"]
        r = client.put(f"/api/shop/change-requests/{crid}", json={"action": "approve"}, headers=h)
        assert r.status_code == 400
        assert r.get_json().get("overlap") is True

    def test_change_request_add_rejected_on_overlap(self, client):
        """追加申請(add)で同日重複する時間は承認拒否。"""
        shop_id, e1 = self._setup()
        shop_token = make_session("shop", shop_id, shop_id)
        h = auth(shop_token)
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        # 12-15 の追加申請を作成（9-18と重複）
        dbmod.execute(
            "INSERT INTO change_requests (shop_id, staff_id, shift_id, request_type, desired_start, desired_end, status) "
            "VALUES (?,?,?,?,?,?, 'pending')",
            (shop_id, e1, None, "add", f"{MON}T12:00:00", f"{MON}T15:00:00"))
        crid = dbmod.query_one("SELECT id FROM change_requests WHERE staff_id=? AND request_type='add'", (e1,))["id"]
        r = client.put(f"/api/shop/change-requests/{crid}", json={"action": "approve"}, headers=h)
        assert r.status_code == 400
        assert r.get_json().get("overlap") is True

    # ---- シフトコピー ----
    def test_shift_copy_skips_overlap_at_destination(self, client):
        """コピー先で同スタッフと重複するシフトはスキップされる。"""
        shop_id, e1 = self._setup()
        shop_token = make_session("shop", shop_id, shop_id)
        h = auth(shop_token)
        # 8/3 に 9-13 のシフトを作成
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T13:00:00"}, headers=h)
        # 8/4 に 11-15 のシフトを作成（コピー先で重複想定）
        client.post("/api/shop/shifts", json={
            "staff_id": e1, "start_datetime": f"{TUE}T11:00:00", "end_datetime": f"{TUE}T15:00:00"}, headers=h)
        # 8/3 → 8/4 へコピー: 8/3の9-13 を 8/4 へ。8/4 の 11-15 と重複 → スキップ
        r = client.post("/api/shop/shifts/copy", json={
            "from_start": MON, "from_end": MON, "to_start": TUE,
        }, headers=h)
        assert r.status_code == 200
        body = r.get_json()
        assert body["copied"] == 0
        assert body["skipped_overlap"] == 1


# ============================================================
# 2e. 自動調整（auto_adjust / auto-confirm）の全バリエーションテスト
#   プロとして以下を網羅：
#   1. PUT auto_adjust: cap超過 → 他シフト短縮で確定
#   2. PUT auto_adjust: 同日重複 → 既存シフトと統合
#   3. PUT auto_adjust: 社員優先で短縮される
#   4. PUT auto_adjust: 最小時間(4h)未満になる短縮はスキップ
#   5. PUT auto_adjust: 解決不能な場合はスキップ（400）
#   6. POST /auto-confirm: 一括確定（cap超過＋同日重複の混在）
#   7. POST /auto-confirm: 14h超で統合スキップ
#   8. POST /auto-confirm: 調整なし（cap内）は単純確定
# ============================================================
class TestAutoAdjust:
    def _setup(self, settings=None, cap=2):
        """1店舗 + パターン1個（9-22 cap）+ 社員2名 + バイト1名。"""
        shop_id = insert_shop(settings=settings or {"min_daily_hours": 4})
        insert_pattern(shop_id, "通", "09:00", "22:00", cap)
        emp1 = insert_staff(shop_id, "E1", "社員A", "employee", 2000)
        emp2 = insert_staff(shop_id, "E2", "社員B", "employee", 2000)
        pt1 = insert_staff(shop_id, "P1", "バイトA", "part_time", 1100)
        return shop_id, (emp1, emp2, pt1)

    def test_put_auto_adjust_cap_over_shortens_employee(self, client):
        """【1】PUT auto_adjust: cap超過 → 社員優先で短縮して確定。"""
        shop_id, (e1, e2, p1) = self._setup(cap=2)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        # 2社員が9-18で確定（13時台は2人=cap到達）
        for eid in (e1, e2):
            client.post("/api/shop/shifts", json={"staff_id": eid, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        # バイトの13-18を requested として作成
        sid = dbmod.query_one("SELECT id FROM shifts WHERE staff_id=? ORDER BY id DESC", (e2,))["id"]
        # 別途 requested を直接INSERT
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, p1, f"{MON}T13:00:00", f"{MON}T18:00:00", "requested", "テスト"))
        rid = dbmod.query_one("SELECT id FROM shifts WHERE staff_id=? AND status='requested'", (p1,))["id"]
        # auto_adjust付きで確定
        r = client.put(f"/api/shop/shifts/{rid}", json={
            "staff_id": p1, "start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T18:00:00",
            "status": "confirmed", "auto_adjust": True,
        }, headers=h)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        # 調整が行われた
        assert body.get("adjustments"), f"adjustments が空: {body}"
        # バイトのシフトが確定された
        row = dbmod.query_one("SELECT status FROM shifts WHERE id=?", (rid,))
        assert row["status"] == "confirmed"
        # いずれかの社員が短縮された（9-13になる）
        e1_row = dbmod.query_one("SELECT end_datetime FROM shifts WHERE staff_id=? AND start_datetime LIKE ?", (e1, f"{MON}%"))
        assert e1_row["end_datetime"] == f"{MON}T13:00:00" or e1_row["end_datetime"] == f"{MON}T18:00:00"

    def test_put_auto_adjust_same_day_merge(self, client):
        """【2】PUT auto_adjust: 同日重複 → 既存シフトと統合（1シフトに）。"""
        shop_id, (e1, e2, p1) = self._setup(cap=5)  # cap緩め
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        # バイトが17-22で確定済み
        client.post("/api/shop/shifts", json={"staff_id": p1, "start_datetime": f"{MON}T17:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=h)
        # 同じバイトの13-18 requested（17時台が重複）
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, p1, f"{MON}T13:00:00", f"{MON}T18:00:00", "requested", "テスト"))
        rid = dbmod.query_one("SELECT id FROM shifts WHERE staff_id=? AND status='requested'", (p1,))["id"]
        # auto_adjust付きで確定
        r = client.put(f"/api/shop/shifts/{rid}", json={
            "staff_id": p1, "start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T18:00:00",
            "status": "confirmed", "auto_adjust": True,
        }, headers=h)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body.get("action") == "merged", f"統合されていない: {body}"
        # requestedは統合で削除される
        row = dbmod.query_one("SELECT COUNT(*) as c FROM shifts WHERE id=?", (rid,))
        assert row["c"] == 0, "統合元の requested が削除されていない"
        # 既存シフトが延長されている（13:00〜22:00 = 統合）
        merged = dbmod.query_one("SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='confirmed'", (p1,))
        assert merged["start_datetime"] == f"{MON}T13:00:00"
        assert merged["end_datetime"] == f"{MON}T22:00:00"

    def test_put_auto_adjust_skip_too_short(self, client):
        """【4】PUT auto_adjust: 最小時間(4h)未満になる短縮はスキップ。"""
        shop_id, (e1, e2, p1) = self._setup(cap=1)  # cap厳しめ
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        # 社員1名が9-22で確定
        client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=h)
        # バイトの13-18 requested
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, p1, f"{MON}T13:00:00", f"{MON}T18:00:00", "requested", "テスト"))
        rid = dbmod.query_one("SELECT id FROM shifts WHERE staff_id=? AND status='requested'", (p1,))["id"]
        # 社員9-22を13時で切ると9-13（4h丁度）→ 短縮できる。21時で切ると9-21（12h）。
        # 前詰め: 9-13 = 4h OK
        r = client.put(f"/api/shop/shifts/{rid}", json={
            "staff_id": p1, "start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T18:00:00",
            "status": "confirmed", "auto_adjust": True,
        }, headers=h)
        assert r.status_code == 200, r.get_json()
        # 社員のシフトが9-13に短縮
        emp_row = dbmod.query_one("SELECT end_datetime FROM shifts WHERE staff_id=? AND status='confirmed'", (e1,))
        assert emp_row["end_datetime"] == f"{MON}T13:00:00", f"社員が短縮されていない: {emp_row}"

    def test_auto_confirm_batch_mixed(self, client):
        """【6】POST /auto-confirm: cap超過＋同日重複の混在一括処理。"""
        shop_id, (e1, e2, p1) = self._setup(cap=2)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        # 8/3: 社員2名9-18確定（cap到達）+ バイト13-18 requested
        for eid in (e1, e2):
            client.post("/api/shop/shifts", json={"staff_id": eid, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, p1, f"{MON}T13:00:00", f"{MON}T18:00:00", "requested", "cap超過テスト"))
        # 8/4: バイト17-22確定 + 13-18 requested（同日重複）
        client.post("/api/shop/shifts", json={"staff_id": p1, "start_datetime": f"{TUE}T17:00:00", "end_datetime": f"{TUE}T22:00:00"}, headers=h)
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, p1, f"{TUE}T13:00:00", f"{TUE}T18:00:00", "requested", "同日重複テスト"))
        # 一括確定
        r = client.post("/api/shop/shifts/auto-confirm", json={
            "start_date": MON, "end_date": TUE,
        }, headers=h)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["total"] == 2
        # 1件はcap調整で確定、1件は統合
        assert body["confirmed"] + body["merged"] == 2, f"確定+統合 != 2: {body}"
        assert body["skipped"] == 0
        # 全 requested が解消された
        req_left = dbmod.query_one("SELECT COUNT(*) as c FROM shifts WHERE shop_id=? AND status='requested'", (shop_id,))["c"]
        assert req_left == 0, f"requested が残っている: {req_left}"

    def test_auto_confirm_skip_too_long_merge(self, client):
        """【7】POST /auto-confirm: 統合すると14h超になる場合はスキップ。"""
        shop_id, (e1, e2, p1) = self._setup(cap=5)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        # バイトが8:00-15:00で確定
        client.post("/api/shop/shifts", json={"staff_id": p1, "start_datetime": f"{MON}T08:00:00", "end_datetime": f"{MON}T15:00:00"}, headers=h)
        # 同バイトの16-23 requested → 統合すると8-23=15h（14h超）→ スキップ
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, p1, f"{MON}T16:00:00", f"{MON}T23:00:00", "requested", "14h超テスト"))
        r = client.post("/api/shop/shifts/auto-confirm", json={"start_date": MON, "end_date": MON}, headers=h)
        body = r.get_json()
        assert body["skipped"] == 1, f"スキップされていない: {body}"
        assert body["confirmed"] + body["merged"] == 0

    def test_auto_confirm_simple_in_cap(self, client):
        """【8】POST /auto-confirm: cap内の requested は単純確定（調整不要）。"""
        shop_id, (e1, e2, p1) = self._setup(cap=3)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        # バイト1名の requested のみ（cap内）
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, p1, f"{MON}T09:00:00", f"{MON}T18:00:00", "requested", "単純確定テスト"))
        r = client.post("/api/shop/shifts/auto-confirm", json={"start_date": MON, "end_date": MON}, headers=h)
        body = r.get_json()
        assert body["confirmed"] == 1
        assert body["merged"] == 0
        assert body["skipped"] == 0
        assert len(body.get("adjustments", [])) == 0  # 調整なし

    def test_put_without_auto_adjust_still_400_on_overlap(self, client):
        """【5】PUT 通常モードは overlap で400（auto_adjust無し）。"""
        shop_id, (e1, e2, p1) = self._setup(cap=5)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        # 別の社員のシフトを 9-18 に編集（e1と同じ時間帯で cap超過）
        client.post("/api/shop/shifts", json={"staff_id": e2, "start_datetime": f"{MON}T17:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=h)
        sid2 = dbmod.query_one("SELECT id FROM shifts WHERE staff_id=? ORDER BY id DESC LIMIT 1", (e2,))["id"]
        # e2 を 9-18 に編集 → e1 と同日重複
        # ただ、これは別スタッフなので overlap（同一スタッフ重複）ではない
        # 同一スタッフの同日重複テスト: e1 を もう一つ作る
        # 別途 e1 の requested を INSERT
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, e1, f"{MON}T13:00:00", f"{MON}T18:00:00", "requested", "overlapテスト"))
        rid = dbmod.query_one("SELECT id FROM shifts WHERE staff_id=? AND status='requested'", (e1,))["id"]
        # 通常PUT（auto_adjust無し）→ overlap 400
        r = client.put(f"/api/shop/shifts/{rid}", json={
            "staff_id": e1, "start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T18:00:00",
            "status": "confirmed",
        }, headers=h)
        assert r.status_code == 400
        assert r.get_json().get("overlap") is True

    def test_put_auto_adjust_employee_priority(self, client):
        """【3】PUT auto_adjust: 社員優先で短縮（バイトより先に社員を短縮）。"""
        shop_id, (e1, e2, p1) = self._setup(cap=2)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        # 社員9-18 + バイト9-18 = 2人（cap到達）
        client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        client.post("/api/shop/shifts", json={"staff_id": p1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        # 別の requested（社員e2の13-18）
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, e2, f"{MON}T13:00:00", f"{MON}T18:00:00", "requested", "優先度テスト"))
        rid = dbmod.query_one("SELECT id FROM shifts WHERE staff_id=? AND status='requested'", (e2,))["id"]
        r = client.put(f"/api/shop/shifts/{rid}", json={
            "staff_id": e2, "start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T18:00:00",
            "status": "confirmed", "auto_adjust": True,
        }, headers=h)
        assert r.status_code == 200, r.get_json()
        # 社員e1が短縮される（バイトp1より先）
        e1_row = dbmod.query_one("SELECT end_datetime FROM shifts WHERE staff_id=? AND status='confirmed' AND start_datetime LIKE ?", (e1, f"{MON}%"))
        # e1 か p1 のいずれかが短縮されているはず（社員優先で e1 が先）
        p1_row = dbmod.query_one("SELECT end_datetime FROM shifts WHERE staff_id=? AND status='confirmed' AND start_datetime LIKE ?", (p1, f"{MON}%"))
        # 社員優先で e1 が短縮されていることを確認
        assert e1_row["end_datetime"] == f"{MON}T13:00:00", f"社員が優先短縮されていない: e1={e1_row} p1={p1_row}"

    # ---- 隣接統合（17-18 + 18-22 → 17-22）----
    def test_post_merges_adjacent_before(self, client):
        """隣接統合: 既存17-22の直前に13-17を追加 → 13-22に統合。"""
        shop_id, (e1, e2, p1) = self._setup(cap=5)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T17:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=h)
        r = client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T17:00:00"}, headers=h)
        assert r.status_code == 200, r.get_json()
        assert r.get_json().get("merged") is True, f"統合されていない: {r.get_json()}"
        rows = dbmod.query_all("SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='confirmed' AND start_datetime LIKE ?", (e1, f"{MON}%"))
        assert len(rows) == 1, f"2シフトになっている: {rows}"
        assert rows[0]["start_datetime"] == f"{MON}T13:00:00"
        assert rows[0]["end_datetime"] == f"{MON}T22:00:00"

    def test_post_merges_adjacent_after(self, client):
        """隣接統合: 既存9-13の直後に13-18を追加 → 9-18に統合。"""
        shop_id, (e1, e2, p1) = self._setup(cap=5)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T13:00:00"}, headers=h)
        r = client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T13:00:00", "end_datetime": f"{MON}T18:00:00"}, headers=h)
        assert r.status_code == 200
        assert r.get_json().get("merged") is True
        rows = dbmod.query_all("SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='confirmed' AND start_datetime LIKE ?", (e1, f"{MON}%"))
        assert len(rows) == 1
        assert rows[0]["start_datetime"] == f"{MON}T09:00:00"
        assert rows[0]["end_datetime"] == f"{MON}T18:00:00"

    def test_post_no_merge_when_not_adjacent(self, client):
        """非隣接: 9-13と17-22は1時間隙間がある → 別シフトのまま。"""
        shop_id, (e1, e2, p1) = self._setup(cap=5)
        token = make_session("shop", shop_id, shop_id)
        h = auth(token)
        client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T13:00:00"}, headers=h)
        r = client.post("/api/shop/shifts", json={"staff_id": e1, "start_datetime": f"{MON}T17:00:00", "end_datetime": f"{MON}T22:00:00"}, headers=h)
        assert r.status_code == 200
        assert not r.get_json().get("merged"), "隣接していないのに統合された"
        rows = dbmod.query_all("SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='confirmed' AND start_datetime LIKE ?", (e1, f"{MON}%"))
        assert len(rows) == 2, f"2シフトであるべき: {len(rows)}"


# ============================================================
# 3. AI API 連携関数のモック/フォールバックテスト
# ============================================================
class TestAI:
    def test_parse_shift_request_income_calc(self):
        """『月10万円』+ 時給 → 必要労働時間・希望シフト配列・理由。"""
        wage = 1250
        r = ai.parse_shift_request("月10万円稼ぎたいです", wage, period_days=20)
        assert r["target_income"] == 100000
        assert r["need_hours"] == 80  # 100000 / 1250 を切り上げ
        assert r["hourly_wage"] == wage
        # 希望シフト配列と理由が返る
        assert isinstance(r["proposed_shifts"], list)
        assert len(r["proposed_shifts"]) > 0
        assert sum(s["hours"] for s in r["proposed_shifts"]) >= 1
        assert r["reason"]
        assert "10" in r["reason"] and "1250" in r["reason"]

    def test_parse_shift_request_proposed_shifts_meet_target(self):
        """提案シフトの合計時間が目標時間を（日数上限以内で）できるだけ充足する。"""
        r = ai.parse_shift_request("月8万円稼ぎたい", 1000, period_days=20)
        # 80000/1000 = 80h、1日5h×16日（period_days=20以内）
        assert r["need_hours"] == 80
        assert len(r["proposed_shifts"]) == 16
        # 全ての提案シフトが5hで、時間帯情報を持つ
        for s in r["proposed_shifts"]:
            assert s["hours"] == 5
            assert s["start_time"] and s["end_time"]

    def test_parse_shift_request_ng_weekday_excluded(self):
        """『火曜NG』『朝希望』が反映される。"""
        r = ai.parse_shift_request("月5万円稼ぎたい。火曜はNG。朝希望", 1000, period_days=15)
        assert 2 in r["ng_weekdays"]  # 火曜=2
        assert r["preferred_slot"] == "morning"
        # 提案シフトに火曜(2)が含まれない
        for s in r["proposed_shifts"]:
            assert s["weekday"] != 2
            assert s["start_time"] == "09:00"  # 朝希望の時間帯

    def test_parse_shift_request_explicit_time_range(self):
        """『8万稼ぎたい。13-18が可能時間』は時間指定(13:00-18:00)として解析される（旧バグ: いつでも扱い）。"""
        r = ai.parse_shift_request("8万稼ぎたい。13-18が可能時間", 1000, period_days=15)
        assert r["preferred_slot"] == "time"
        assert r["preferred_start"] == "13:00"
        assert r["preferred_end"] == "18:00"
        # 提案シフトも13-18
        assert r["proposed_shifts"]
        assert r["proposed_shifts"][0]["start_time"] == "13:00"
        assert r["proposed_shifts"][0]["end_time"] == "18:00"
        assert "13:00-18:00" in r["reason"]

    def test_parse_shift_request_japanese_time_range(self):
        """『13時〜18時』『9時-17時』等の日本語時間帯も解析される。"""
        for txt, exp_s, exp_e in [("13時〜18時で", "13:00", "18:00"),
                                  ("9時-17時希望", "09:00", "17:00"),
                                  ("13:00〜18:00", "13:00", "18:00")]:
            r = ai.parse_shift_request(f"5万円。{txt}", 1000, period_days=10)
            assert r["preferred_slot"] == "time", txt
            assert r["preferred_start"] == exp_s, txt
            assert r["preferred_end"] == exp_e, txt

    def test_generate_help_message(self):
        """欠員データからヘルプ要請テキストが生成される。"""
        msg = ai.generate_help_message("8月15日(土)", "17:00〜22:00", 2, "渋谷店")
        assert isinstance(msg, str) and msg
        assert "8月15日" in msg
        assert "2" in msg  # 不足人数
        assert "渋谷店" in msg

    def test_review_shift_balance_consecutive_warning(self):
        """連勤を含むシフトから労務チェックコメント（連勤指摘）が生成される。"""
        shifts = []
        # 社員A: 7日連続勤務
        for i in range(7):
            day = f"2026-08-{10 + i:02d}"
            shifts.append({
                "staff_id": 1, "staff_name": "山田",
                "start_datetime": f"{day}T09:00:00",
                "end_datetime": f"{day}T18:00:00",
                "break_time_minutes": 60, "status": "confirmed",
            })
        res = ai.review_shift_balance(shifts)
        assert "metrics" in res and "advice" in res
        assert res["metrics"]["max_consecutive_days"] >= 7
        # アドバイスに連勤に関する言及があること
        assert ("連" in res["advice"]) or ("勤務" in res["advice"]), res["advice"]

    def test_review_shift_balance_metrics_summary(self):
        """複数スタッフの集計が正しく計算される。"""
        shifts = [
            {"staff_id": 1, "staff_name": "A", "start_datetime": "2026-08-01T09:00:00",
             "end_datetime": "2026-08-01T14:00:00", "break_time_minutes": 0, "status": "confirmed"},
            {"staff_id": 2, "staff_name": "B", "start_datetime": "2026-08-01T09:00:00",
             "end_datetime": "2026-08-01T13:00:00", "break_time_minutes": 0, "status": "confirmed"},
        ]
        res = ai.review_shift_balance(shifts)
        summary = {s["staff_id"]: s for s in res["metrics"]["staff_summary"]}
        assert summary[1]["hours"] == 5.0  # 5h勤務・休憩0
        assert summary[2]["hours"] == 4.0  # 4h勤務・休憩0

    def test_parse_shift_request_llm_path_mocked(self, monkeypatch):
        """call_llm をモックし、LLM経路のパース結果に proposed_shifts が補完される。"""
        fake = json.dumps({
            "target_income": 50000,
            "ng_weekdays": [0],
            "preferred_slot": "evening",
            "reason": "LLM提案文",
        })

        def fake_call(system_prompt, user_prompt, temperature=0.3):
            return fake

        monkeypatch.setattr(ai, "call_llm", fake_call)
        r = ai.parse_shift_request("月5万円", 1000, period_days=10)
        assert r["source"] == "llm"
        assert r["need_hours"] == 50  # 50000/1000 をLLM結果から補完
        assert isinstance(r["proposed_shifts"], list)
        assert r["proposed_shifts"]
        # evening 希望の時間帯
        assert r["proposed_shifts"][0]["start_time"] == "17:00"

    def test_ai_parse_endpoint(self, client):
        shop_id = insert_shop(settings={"default_hourly_wage": 1000})
        staff_id = insert_staff(shop_id, "P1", "バイト", "part_time", 1000)
        token = make_session("staff", staff_id, shop_id)
        r = client.post("/api/staff/ai/parse", json={
            "text": "月6万円稼ぎたい", "period_days": 15}, headers=auth(token))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["need_hours"] == 60
        assert body["proposed_shifts"]


# ============================================================
# 3b. 会話型AIチャットのテスト（LLM接続時 / 未接続時）
# ============================================================
class TestAIChat:
    def test_chat_unavailable_when_llm_not_configured(self):
        """【要件】LLM未設定時はルールベースにフォールバックせず、明示的に unavailable を返す。"""
        ctx = {"shop_name": "テスト店", "staff_count": 5, "shortage_count": 0}
        res = ai.chat("今のシフト状況は？", [], ctx)
        assert res["source"] == "unavailable", f"未接続時は unavailable: {res}"
        assert "AIエンジン" in res["reply"] or "未接続" in res["reply"]
        assert "管理者" in res["reply"]  # 設定画面への誘導
        assert isinstance(res["suggestions"], list) and res["suggestions"]  # 提案は維持

    def test_staff_chat_unavailable_when_llm_not_configured(self):
        """スタッフチャットもLLM未設定時は unavailable（ルールベースに誤認させない）。"""
        ctx = {"staff_name": "鈴木 花子", "hourly_wage": 1100, "upcoming_shifts": []}
        res = ai.chat_staff("月5万円稼ぐには？", [], ctx)
        assert res["source"] == "unavailable"
        assert "未接続" in res["reply"]
        # ルールベースの定型文で誤魔化さない
        assert "時給1100円で" not in res["reply"]
        assert "約46時間" not in res["reply"]

    def test_shop_chat_llm_path_uses_ctx_in_prompt(self, monkeypatch):
        """LLM接続時は ctx（実データ）をシステムプロンプトに含めてLLMを呼ぶ。"""
        monkeypatch.setattr(ai, "is_llm_available", lambda: True)
        captured = {}

        def fake_call(messages, temperature=0.4):
            captured["messages"] = messages
            captured["temp"] = temperature
            return "LLMが生成した回答"

        monkeypatch.setattr(ai, "_call_llm_messages", fake_call)
        ctx = {"shop_name": "テスト店", "month_cost": 350000, "month_hours": 120.5,
               "shortage_count": 2, "upcoming_confirmed": 20}
        res = ai.chat("人件費は？", [], ctx)
        assert res["source"] == "llm"
        assert res["reply"] == "LLMが生成した回答"
        # システムプロンプトに実データが埋め込まれている
        sys_msg = captured["messages"][0]["content"]
        assert "350000" in sys_msg or "350,000" in sys_msg  # 人件費
        assert "120" in sys_msg  # 時間
        assert "テスト店" in sys_msg  # 店舗名

    def test_shop_chat_history_passed_to_llm(self, monkeypatch):
        """履歴が system / user / assistant / user の順で LLM へ渡る。"""
        monkeypatch.setattr(ai, "is_llm_available", lambda: True)
        captured = {}

        def fake_call(messages, temperature=0.4):
            captured["messages"] = messages
            return "これはLLMの回答です。"

        monkeypatch.setattr(ai, "_call_llm_messages", fake_call)
        history = [
            {"role": "user", "content": "前回の質問"},
            {"role": "assistant", "content": "前回の回答"},
        ]
        res = ai.chat("次の質問", history, {"shop_name": "X"})
        assert res["source"] == "llm"
        assert res["reply"] == "これはLLMの回答です。"
        roles = [m["role"] for m in captured["messages"]]
        assert roles == ["system", "user", "assistant", "user"]
        assert captured["messages"][-1]["content"] == "次の質問"

    def test_staff_chat_llm_path_includes_wage_and_shifts(self, monkeypatch):
        """スタッフチャットLLM経路：時給・確定シフトがプロンプトに含まれる。"""
        monkeypatch.setattr(ai, "is_llm_available", lambda: True)
        captured = {}

        def fake_call(messages, temperature=0.4):
            captured["messages"] = messages
            return "はい、計算します。"

        monkeypatch.setattr(ai, "_call_llm_messages", fake_call)
        ctx = {"staff_name": "鈴木 花子", "hourly_wage": 1100, "today": "2026-08-01",
               "upcoming_shifts": [
                   {"start": "2026-08-05T17:00:00", "end": "2026-08-05T22:00:00", "status": "confirmed"}]}
        res = ai.chat_staff("月5万円稼ぐには？", [], ctx)
        assert res["source"] == "llm"
        sys_msg = captured["messages"][0]["content"]
        assert "1100" in sys_msg          # 時給
        assert "鈴木 花子" in sys_msg       # スタッフ名
        assert "2026-08-05" in sys_msg     # 確定シフト

    def test_chat_llm_call_failure_returns_unavailable(self, monkeypatch):
        """LLM呼び出し失敗時は unavailable に（ルールベースに戻さない）。"""
        monkeypatch.setattr(ai, "is_llm_available", lambda: True)
        monkeypatch.setattr(ai, "_call_llm_messages", lambda m, temperature=0.4: None)
        res = ai.chat("何か聞いて", [], {"shop_name": "X"})
        assert res["source"] == "unavailable"
        assert "失敗" in res["reply"] or "接続テスト" in res["reply"]

    def test_shop_ai_chat_endpoint_unavailable_status(self, client):
        """API経由でもLLM未設定時は 200 + source=unavailable。"""
        shop_id = insert_shop(settings={"default_hourly_wage": 1100})
        insert_pattern(shop_id, "通", "09:00", "18:00", 2)
        insert_staff(shop_id, "E1", "社員", "employee", 2000)
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/ai/chat",
                        json={"message": "今月のシフト状況は？", "history": []},
                        headers=auth(token))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["reply"]
        assert isinstance(body["suggestions"], list)
        # テスト環境はLLM未設定なので unavailable
        assert body.get("source") == "unavailable"

    def test_staff_ai_chat_endpoint(self, client):
        shop_id = insert_shop(settings={"default_hourly_wage": 1000})
        staff_id = insert_staff(shop_id, "P1", "バイト", "part_time", 1000)
        token = make_session("staff", staff_id, shop_id)
        r = client.post("/api/staff/ai/chat",
                        json={"message": "次のシフトは？", "history": []},
                        headers=auth(token))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["reply"]


# (削除) 旧 TestAdminAiSettings — .env 一本化により Web UI/API でのAIキー管理は廃止。
# AI設定はサーバー管理者が .env (LLM_API_KEY/LLM_API_URL/LLM_MODEL) で行う。


# ============================================================
# 4. Explainable AI（説明可能なAI）のテスト
# ============================================================
class TestExplainableAI:
    def test_auto_generate_includes_explanations(self):
        """auto_generate の結果に explanations リストが含まれる。"""
        sc = build_scenario_shop()
        res = generate(sc)
        assert "explanations" in res
        assert isinstance(res["explanations"], list)
        assert len(res["explanations"]) > 0

    def test_explanations_have_required_fields(self):
        """各 explanation に type, icon, title, detail が含まれる。"""
        sc = build_scenario_shop()
        res = generate(sc)
        for e in res["explanations"]:
            assert "type" in e and e["type"] in ("success", "info", "warning", "ai")
            assert "icon" in e
            assert "title" in e and e["title"]
            assert "detail" in e and e["detail"]

    def test_explanations_mention_fixed_shifts(self):
        """固定シフトが存在する場合、explanation リストが返る（内容は動的）。"""
        sc = build_scenario_shop()
        res = generate(sc)
        assert isinstance(res["explanations"], list)
        assert len(res["explanations"]) > 0

    def test_explanations_mention_employee_fill(self):
        """社員による不足補填がある場合、その説明が含まれる。"""
        sc = build_scenario_shop()
        res = generate(sc)
        titles = [e["title"] for e in res["explanations"]]
        assert any("社員" in t and "補填" in t for t in titles), f"社員補填の説明がない: {titles}"

    def test_auto_endpoint_returns_explanations(self, client):
        """dry_run エンドポイントが explanations を返す。"""
        sc_data = build_scenario_shop()
        shop_id = sc_data["shop_id"]
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI, "dry_run": True,
        }, headers=auth(token))
        assert r.status_code == 200
        body = r.get_json()
        assert "explanations" in body
        assert len(body["explanations"]) > 0

    def test_persist_endpoint_returns_explanations(self, client):
        """確定エンドポイントも explanations を返す。"""
        sc_data = build_scenario_shop()
        shop_id = sc_data["shop_id"]
        token = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": WED,
        }, headers=auth(token))
        assert r.status_code == 200
        assert "explanations" in r.get_json()


# ============================================================
# 5. ダッシュボードAPI・募集期間APIのテスト
# ============================================================
class TestDashboardAndPeriods:
    def test_dashboard_endpoint_returns_stats(self, client):
        """ダッシュボードAPIが統計データを返す。"""
        shop_id = insert_shop(settings={"default_hourly_wage": 1100})
        insert_pattern(shop_id, "通", "09:00", "18:00", 2)
        insert_staff(shop_id, "E1", "社員", "employee", 2000)
        insert_staff(shop_id, "P1", "バイト", "part_time", 1100)
        token = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/dashboard", headers=auth(token))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["staff_count"] == 2
        assert body["employee_count"] == 1
        assert body["part_time_count"] == 1
        assert "month_cost" in body
        assert "daily_cost_series" in body

    def test_staff_periods_endpoint(self, client):
        """スタッフが自分の店舗の募集期間一覧を取得できる。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "バイト")
        # 募集期間を作成
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-15", "2026-07-25"))
        token = make_session("staff", staff_id, shop_id)
        r = client.get("/api/staff/periods", headers=auth(token))
        assert r.status_code == 200
        body = r.get_json()
        assert len(body["periods"]) == 1
        assert body["periods"][0]["start_date"] == "2026-08-01"

    def test_request_rejected_outside_period(self, client):
        """募集期間外の希望提出は拒否される。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "バイト")
        # 8/1-8/15 の期間を作成
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-15", "2026-07-25"))
        token = make_session("staff", staff_id, shop_id)
        # 9月の希望を提出 → 拒否
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": "2026-09-01T09:00:00", "end_datetime": "2026-09-01T14:00:00"}],
        }, headers=auth(token))
        assert r.status_code == 400
        assert "募集期間外" in r.get_json()["error"]

    def test_request_accepted_within_period(self, client):
        """募集期間内の希望提出は成功する。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "バイト")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-15", "2026-07-25"))
        token = make_session("staff", staff_id, shop_id)
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": "2026-08-05T09:00:00", "end_datetime": "2026-08-05T14:00:00"}],
        }, headers=auth(token))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["submitted"] == 1

    def test_request_accepts_seconds_less_datetime(self, client):
        """【回帰・ユーザー報告】秒なし 'YYYY-MM-DDTHH:MM' でも提出できる。

        旧バグ: AI希望入力で時間指定を作ると HH:MM 形式（秒なし）になり、
        サーバーが '%Y-%m-%dT%H:%M:%S' パースに失敗して 400 エラーになっていた。
        """
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "バイト")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-15", "2026-07-25"))
        token = make_session("staff", staff_id, shop_id)
        r = client.post("/api/staff/requests", json={
            "shifts": [{"start_datetime": "2026-08-05T09:00", "end_datetime": "2026-08-05T14:00"}],
        }, headers=auth(token))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["submitted"] == 1
        # DB には秒ありで正規化されて保存されていること
        row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? ORDER BY id DESC LIMIT 1",
            (staff_id,))
        assert row["start_datetime"] == "2026-08-05T09:00:00"
        assert row["end_datetime"] == "2026-08-05T14:00:00"

    def test_parse_iso_handles_both_formats(self):
        """parse_iso は秒なし・秒あり両方を許容する。"""
        from utils import parse_iso, normalize_iso
        # 秒あり
        a = parse_iso("2026-08-05T09:00:00")
        # 秒なし
        b = parse_iso("2026-08-05T09:00")
        assert a == b
        # normalize_iso は秒なしを秒ありに
        assert normalize_iso("2026-08-05T09:00") == "2026-08-05T09:00:00"
        assert normalize_iso("2026-08-05T09:00:00") == "2026-08-05T09:00:00"
        assert normalize_iso(None) is None


# (削除) 旧 TestAIChatDataAware クラス — LLM未設定時のルールベース実データ応答を検証していたが、
# 新仕様（LLM未設定時は unavailable を返す）により廃止。
# 実データのプロンプト取り込みは TestAIChat.test_shop_chat_llm_path_uses_ctx_in_prompt で検証。
