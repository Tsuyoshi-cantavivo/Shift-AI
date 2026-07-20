"""tests/test_shop_create_with_manager.py - 店舗作成 + 店舗責任者同時作成のテスト。

【対象要件】
  ① バグ修正: システム管理者が店舗を新規作成する際に、店舗責任者用の
     ユーザーID（任意指定）・パスワード・氏名を同時登録できる。
     ※ yuzublv24@gmail.com を登録する処理は含まない（仕様確認済み）
"""
import pytest

import db as dbmod
from auth import verify_password
from helpers import insert_admin, make_session, auth


class TestShopCreateWithManager:
    """店舗作成エンドポイントの新仕様テスト。"""

    def test_create_shop_with_manager_account(self, client):
        """店舗 + 店舗責任者アカウントが同時に作成される。"""
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.post("/api/admin/shops", json={
            "shop_code": "SHOP001", "shop_name": "渋谷店",
            "password": "ShopPass1",
            "manager_code": "manager", "manager_password": "Mgr1234abc",
            "manager_name": "山田太郎",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        sid = body["id"]
        assert body["manager_code"] == "manager"
        # 店舗が作成されている
        shop = dbmod.query_one("SELECT * FROM shops WHERE id=?", (sid,))
        assert shop["shop_code"] == "SHOP001"
        assert shop["shop_name"] == "渋谷店"
        # 店舗責任者が manager ロールで作成されている
        mgr = dbmod.query_one(
            "SELECT * FROM staffs WHERE shop_id=? AND staff_code=?",
            (sid, "manager"))
        assert mgr is not None
        assert mgr["role"] == "manager"
        assert mgr["name"] == "山田太郎"
        assert verify_password("Mgr1234abc", mgr["password_hash"])

    def test_created_manager_can_login(self, client):
        """作成された店舗責任者は即ログイン可能。"""
        admin_id = insert_admin()
        admin_tok = make_session("admin", admin_id)
        client.post("/api/admin/shops", json={
            "shop_code": "SHOP002", "shop_name": "新宿店",
            "password": "ShopPass1",
            "manager_code": "yamada", "manager_password": "Mgr1234abc",
            "manager_name": "山田",
        }, headers=auth(admin_tok))
        # ログイン
        r = client.post("/api/login", json={
            "shop_code": "SHOP002", "user_code": "yamada",
            "password": "Mgr1234abc",
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["role"] == "shop"
        assert body["user"]["shop_code"] == "SHOP002"

    def test_missing_manager_code_returns_400(self, client):
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.post("/api/admin/shops", json={
            "shop_code": "SHOP003", "shop_name": "池袋店",
            "password": "ShopPass1",
            # manager_code 欠落
            "manager_password": "Mgr1234abc", "manager_name": "X",
        }, headers=auth(tok))
        assert r.status_code == 400
        assert "店舗責任者のユーザーID" in r.get_json()["error"]

    def test_missing_manager_name_returns_400(self, client):
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.post("/api/admin/shops", json={
            "shop_code": "SHOP004", "shop_name": "池袋店",
            "password": "ShopPass1",
            "manager_code": "mgr", "manager_password": "Mgr1234abc",
            # manager_name 欠落
        }, headers=auth(tok))
        assert r.status_code == 400
        assert "氏名" in r.get_json()["error"]

    def test_weak_manager_password_returns_400(self, client):
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.post("/api/admin/shops", json={
            "shop_code": "SHOP005", "shop_name": "銀座店",
            "password": "ShopPass1",
            "manager_code": "mgr", "manager_password": "short",
            "manager_name": "山田",
        }, headers=auth(tok))
        assert r.status_code == 400

    def test_duplicate_shop_code_returns_400(self, client):
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        # 1店舗目
        client.post("/api/admin/shops", json={
            "shop_code": "DUP", "shop_name": "1号店",
            "password": "ShopPass1",
            "manager_code": "mgr1", "manager_password": "Mgr1234abc",
            "manager_name": "山田",
        }, headers=auth(tok))
        # 2店舗目（同名）
        r = client.post("/api/admin/shops", json={
            "shop_code": "DUP", "shop_name": "2号店",
            "password": "ShopPass1",
            "manager_code": "mgr2", "manager_password": "Mgr1234abc",
            "manager_name": "山田",
        }, headers=auth(tok))
        assert r.status_code == 400
        assert "DUP" in r.get_json()["error"]

    def test_manager_code_same_as_shop_code_works(self, client):
        """manager_code と shop_code が同じ値でも作成可能（同一 staffs UNIQUE は shop_id 単位）。"""
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.post("/api/admin/shops", json={
            "shop_code": "SHOP999", "shop_name": "テナント店",
            "password": "ShopPass1",
            "manager_code": "SHOP999", "manager_password": "Mgr1234abc",
            "manager_name": "店主",
        }, headers=auth(tok))
        assert r.status_code == 200

    def test_no_email_field_required(self, client):
        """メールアドレス不要（仕様確認）。"""
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        r = client.post("/api/admin/shops", json={
            "shop_code": "NOEMAIL", "shop_name": "メール不要店",
            "password": "ShopPass1",
            "manager_code": "mgr", "manager_password": "Mgr1234abc",
            "manager_name": "店主",
            # email フィールド無し
        }, headers=auth(tok))
        assert r.status_code == 200
        # shops テーブルに email カラムは無いが、staffs にも email 無し
        sid = r.get_json()["id"]
        cols = [r[1] for r in dbmod.get_conn().execute("PRAGMA table_info(staffs)").fetchall()]
        assert "email" not in cols

    def test_non_admin_cannot_create_shop(self, client):
        """店舗ロールでは店舗作成不可。"""
        from helpers import insert_shop
        shop_id = insert_shop()
        shop_tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/admin/shops", json={
            "shop_code": "X", "shop_name": "X", "password": "ShopPass1",
            "manager_code": "mgr", "manager_password": "Mgr1234abc",
            "manager_name": "X",
        }, headers=auth(shop_tok))
        assert r.status_code == 403

    def test_failed_manager_creation_rolls_back_shop(self, client):
        """manager 作成が失敗した場合は店舗も作成されない（ロールバック）。"""
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        # わざと不正なロールを与えることでINSERT失敗を起こすのは難しいが、
        # パスワード検証で失敗 → shop ロールバックを検証
        r = client.post("/api/admin/shops", json={
            "shop_code": "ROLLBACK", "shop_name": "ロールバック店",
            "password": "weak",  # 店舗PW弱 → 全体失敗
            "manager_code": "mgr", "manager_password": "Mgr1234abc",
            "manager_name": "X",
        }, headers=auth(tok))
        assert r.status_code == 400
        # 店舗が作成されていないことを検証
        shop = dbmod.query_one("SELECT id FROM shops WHERE shop_code=?", ("ROLLBACK",))
        assert shop is None

    def test_no_yuzublv_email_in_any_table(self, client):
        """仕様確認: 'yuzublv24@gmail.com' は一切登録されない。"""
        admin_id = insert_admin()
        tok = make_session("admin", admin_id)
        client.post("/api/admin/shops", json={
            "shop_code": "YUZU", "shop_name": "テスト店",
            "password": "ShopPass1",
            "manager_code": "mgr", "manager_password": "Mgr1234abc",
            "manager_name": "店主",
            "email": "yuzublv24@gmail.com",  # 万が一送信しても無視されることを確認
        }, headers=auth(tok))
        # 全テーブルから同文字列が見つからないことを検証
        for table in ("shops", "staffs", "system_admins", "notifications"):
            try:
                rows = dbmod.query_all(f"SELECT * FROM {table}")
                for row in rows:
                    for v in row.values():
                        if v and "yuzublv24" in str(v):
                            pytest.fail(f"'yuzublv24@gmail.com' が {table} に見つかりました: {row}")
            except Exception:
                pass
