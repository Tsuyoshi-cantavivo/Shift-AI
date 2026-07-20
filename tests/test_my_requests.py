"""tests test_my_requests.py - 店舗管理者自身の希望提出機能のテスト。"""
import pytest

import db as dbmod
from helpers import (
    insert_shop, insert_staff, insert_pattern, make_session, auth,
)

MON = "2026-08-03"


class TestShopMyRequests:
    """店舗管理者自身の希望提出APIのテスト。"""

    def _setup_manager(self):
        """manager ロールのスタッフがいる店舗を準備。"""
        shop_id = insert_shop()
        # manager ロールのスタッフを作成
        mgr_id = insert_staff(shop_id, "mgr", "店長", "manager", 2000)
        # 募集期間を作成
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-31", "2099-12-31"))
        return shop_id, mgr_id

    def test_get_me_returns_manager_staff_info(self, client):
        """shop ロールで /api/shop/me が自分のスタッフ情報を返す。"""
        shop_id, mgr_id = self._setup_manager()
        # shop ロールでログイン（session.user_id = staffs.id, shop_id = shops.id）
        tok = make_session("shop", mgr_id, shop_id)
        r = client.get("/api/shop/me", headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert body["staff"] is not None
        assert body["staff"]["id"] == mgr_id
        assert body["staff"]["role"] == "manager"
        assert body["staff"]["name"] == "店長"

    def test_get_me_returns_null_for_legacy_shop_login(self, client):
        """旧仕様（shops.id を user_id）のログインでは staff=null。"""
        shop_id = insert_shop()
        # 旧仕様: user_id = shops.id（manager ロールのスタッフが無い）
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/me", headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["staff"] is None

    def test_manager_can_submit_request(self, client):
        """manager が自分の希望を提出できる。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        r = client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["submitted"] == 1
        # shifts テーブルに requested として登録
        row = dbmod.query_one(
            "SELECT * FROM shifts WHERE staff_id=? AND status='requested'",
            (mgr_id,))
        assert row is not None
        assert row["start_datetime"] == f"{MON}T09:00:00"
        # wish_history にも保存
        wh = dbmod.query_one(
            "SELECT * FROM wish_history WHERE staff_id=?",
            (mgr_id,))
        assert wh is not None
        assert wh["note"] == "管理者希望提出"

    def test_manager_submit_with_flex_availability(self, client):
        """柔軟希望（availability）で提出できる。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        r = client.post("/api/shop/my-requests", json={
            "shifts": [{
                "start_datetime": f"{MON}T09:00:00",
                "end_datetime": f"{MON}T22:00:00",
                "availability": "morning",
            }],
        }, headers=auth(tok))
        assert r.status_code == 200
        row = dbmod.query_one(
            "SELECT availability FROM shifts WHERE staff_id=? AND status='requested'",
            (mgr_id,))
        assert row["availability"] == "morning"

    def test_legacy_login_cannot_submit(self, client):
        """旧仕様の店主ログインでは希望提出できない。"""
        shop_id = insert_shop()
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-31", "2099-12-31"))
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(tok))
        assert r.status_code == 400
        assert "manager ロール" in r.get_json()["error"]

    def test_outside_period_rejected(self, client):
        """募集期間外の日付は拒否される。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        r = client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": "2099-12-31T09:00:00", "end_datetime": "2099-12-31T18:00:00"}],
        }, headers=auth(tok))
        assert r.status_code == 400
        assert "募集期間外" in r.get_json()["error"]

    def test_list_my_requests(self, client):
        """自分の希望一覧を取得。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        # 2件提出
        client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(tok))
        client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": "2026-08-04T09:00:00", "end_datetime": "2026-08-04T18:00:00"}],
        }, headers=auth(tok))
        r = client.get("/api/shop/my-requests", headers=auth(tok))
        assert r.status_code == 200
        reqs = r.get_json()["requests"]
        assert len(reqs) == 2

    def test_delete_my_request(self, client):
        """自分の希望を削除できる。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(tok))
        row = dbmod.query_one(
            "SELECT id FROM shifts WHERE staff_id=? AND status='requested'", (mgr_id,))
        rid = row["id"]
        # 削除
        r = client.delete(f"/api/shop/my-requests/{rid}", headers=auth(tok))
        assert r.status_code == 200
        # 削除確認
        row2 = dbmod.query_one("SELECT id FROM shifts WHERE id=?", (rid,))
        assert row2 is None
        # wish_history からも削除
        wh = dbmod.query_one(
            "SELECT id FROM wish_history WHERE staff_id=? AND start_datetime=?",
            (mgr_id, f"{MON}T09:00:00"))
        assert wh is None

    def test_overlap_request_skipped(self, client):
        """同日内時間重複の希望はスキップ。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        # 1件目
        client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(tok))
        # 2件目（重複）
        r = client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T10:00:00", "end_datetime": f"{MON}T20:00:00"}],
        }, headers=auth(tok))
        assert r.status_code == 200
        assert r.get_json()["skipped_overlap"] == 1
        assert r.get_json()["submitted"] == 0

    def test_my_wishes_history(self, client):
        """希望履歴を取得（確定/却下問わず全て）。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(tok))
        r = client.get("/api/shop/my-wishes", headers=auth(tok))
        assert r.status_code == 200
        wishes = r.get_json()["wishes"]
        assert len(wishes) >= 1

    def test_my_shifts_list(self, client):
        """自分の確定シフトを取得。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        # 確定シフトを直接INSERT
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, mgr_id, f"{MON}T09:00:00", f"{MON}T18:00:00", "confirmed", "テスト"))
        r = client.get(f"/api/shop/my-shifts?start={MON}&end={MON}", headers=auth(tok))
        assert r.status_code == 200
        shifts = r.get_json()["shifts"]
        assert len(shifts) == 1
        assert shifts[0]["status"] == "confirmed"

    def test_non_shop_role_cannot_access(self, client):
        """shop ロール以外はアクセス不可。"""
        shop_id, mgr_id = self._setup_manager()
        # staff ロールのスタッフを作って staff トークンで
        staff_id = insert_staff(shop_id, "P1", "バイト", "part_time")
        staff_tok = make_session("staff", staff_id, shop_id)
        r = client.get("/api/shop/my-requests", headers=auth(staff_tok))
        assert r.status_code == 403

    def test_submitted_request_is_visible_in_shop_admin_requests(self, client):
        """manager が提出した希望は「希望休管理」画面（shop/shifts）からも見える。"""
        shop_id, mgr_id = self._setup_manager()
        tok = make_session("shop", mgr_id, shop_id)
        client.post("/api/shop/my-requests", json={
            "shifts": [{"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T18:00:00"}],
        }, headers=auth(tok))
        # shop/shifts で requested として取得できる
        r = client.get(f"/api/shop/shifts?start={MON}&end={MON}", headers=auth(tok))
        assert r.status_code == 200
        shifts = r.get_json()["shifts"]
        requested = [s for s in shifts if s["status"] == "requested"]
        assert len(requested) >= 1
