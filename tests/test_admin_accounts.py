"""システム管理者アカウントの管理API。

背景: system_admins への UPDATE がコード全体でゼロで、/api/init が作る
初期パスワードを変更する正規の手段が存在しなかった。S4 で初期パスワードを
ランダム化したため、変更手段が無いままだと発行された値を一生使い続けることになる。

管理者アカウントの一覧/追加/削除（S3）は Phase 2 Task 5 で実装済み（TestAdminAccounts）。
"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _token(client, admin_id="admin", pw="Admin123"):
    # shop_code に "admin" を明示するのは、login() の既存マジックワード分岐
    # （どちらかの欄に "admin"）を確実に通すため。user_code だけ送ると、
    # admin_id が "admin" 以外の管理者では shop_code が空のまま「店舗コードと
    # ユーザーコードを入力してください」400になり、login() の設計（店舗・
    # スタッフは必ず shop_code を要する）と正しく整合しない。
    r = client.post("/api/login", json={"shop_code": "admin", "user_code": admin_id, "password": pw})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


class TestAdminPasswordChange:
    def test_change_password(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "Admin123", "new_password": "NewPass456"})
        assert r.status_code == 200
        # 新パスワードでログインできる
        assert client.post("/api/login", json={"user_code": "admin",
                                               "password": "NewPass456"}).status_code == 200
        # 旧パスワードでは不可
        assert client.post("/api/login", json={"user_code": "admin",
                                               "password": "Admin123"}).status_code == 400

    def test_wrong_current_password_is_rejected(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "wrong", "new_password": "NewPass456"})
        assert r.status_code == 400
        assert client.post("/api/login", json={"user_code": "admin",
                                               "password": "Admin123"}).status_code == 200

    def test_weak_password_is_rejected(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "Admin123", "new_password": "short"})
        assert r.status_code == 400

    def test_other_sessions_are_revoked(self, client):
        """変更後、自分の現在のセッションは生き、他のセッションは失効すること。"""
        insert_admin("admin", "Admin123")
        old = _token(client)
        cur = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(cur),
                       json={"current_password": "Admin123", "new_password": "NewPass456"})
        assert r.status_code == 200
        assert client.get("/api/me", headers=_hdr(cur)).status_code == 200
        assert client.get("/api/me", headers=_hdr(old)).status_code == 401

    def test_requires_admin_role(self, client):
        sid = insert_shop("SHOP1", "pw12345678")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "pw12345678", "new_password": "NewPass456"})
        assert r.status_code == 403

    def test_requires_auth(self, client):
        r = client.put("/api/admin/password",
                       json={"current_password": "x", "new_password": "NewPass456"})
        assert r.status_code == 401

    def test_change_is_audited(self, client):
        admin_id = insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.put("/api/admin/password", headers=_hdr(t),
                       json={"current_password": "Admin123", "new_password": "NewPass456"})
        assert r.status_code == 200
        row = dbmod.query_one("SELECT * FROM audit_logs WHERE action='admin.password_change' "
                              "ORDER BY id DESC LIMIT 1")
        assert row is not None, "パスワード変更が監査ログに残っていない"
        assert row["actor_role"] == "admin"
        assert row["target_type"] == "system_admin"
        assert row["target_id"] == admin_id
        # 入力されたパスワードが記録されていないこと
        joined = f"{row['actor_name']} {row['detail']}"
        assert "NewPass456" not in joined and "Admin123" not in joined


class TestAdminAccounts:
    def test_list_admins(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.get("/api/admin/admins", headers=_hdr(t))
        assert r.status_code == 200
        admins = r.get_json()["admins"]
        assert len(admins) == 1
        assert admins[0]["admin_id"] == "admin"
        assert "password_hash" not in admins[0], "パスワードハッシュが漏れている"

    def test_create_admin(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.post("/api/admin/admins", headers=_hdr(t),
                        json={"admin_id": "ops2", "name": "運営2", "password": "OpsPass123"})
        assert r.status_code == 200
        # 作った管理者でログインできる（既存のマジックワード分岐を使うため
        # shop_code="admin" を明示。詳細は _token のコメント参照）
        assert client.post("/api/login", json={"shop_code": "admin", "user_code": "ops2",
                                               "password": "OpsPass123"}).status_code == 200

    def test_create_admin_wrong_password_is_rejected(self, client):
        """作成した管理者でも、パスワードを間違えればログインできないこと。"""
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.post("/api/admin/admins", headers=_hdr(t),
                        json={"admin_id": "ops2", "name": "運営2", "password": "OpsPass123"})
        assert r.status_code == 200
        assert client.post("/api/login", json={"shop_code": "admin", "user_code": "ops2",
                                               "password": "wrongpass"}).status_code == 400

    def test_duplicate_admin_id_is_rejected(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.post("/api/admin/admins", headers=_hdr(t),
                        json={"admin_id": "admin", "name": "重複", "password": "OpsPass123"})
        assert r.status_code == 400

    def test_admin_id_too_long_is_rejected(self, client):
        """login() は64文字超のuser_codeを常に400で弾くため、作成時点でも同じ上限で弾く

        （さもないと作成はできるが二度とログインできない行が生まれる）。
        """
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.post("/api/admin/admins", headers=_hdr(t),
                        json={"admin_id": "a" * 65, "name": "長すぎ", "password": "OpsPass123"})
        assert r.status_code == 400

    def test_delete_admin(self, client):
        insert_admin("admin", "Admin123")
        other = insert_admin("ops2", "OpsPass123", name="運営2")
        t = _token(client)
        r = client.delete(f"/api/admin/admins/{other}", headers=_hdr(t))
        assert r.status_code == 200
        assert dbmod.query_one("SELECT id FROM system_admins WHERE id=?", (other,)) is None

    def test_delete_nonexistent_admin_returns_404(self, client):
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.delete("/api/admin/admins/99999", headers=_hdr(t))
        assert r.status_code == 404

    def test_cannot_delete_self(self, client):
        insert_admin("admin", "Admin123")
        insert_admin("ops2", "OpsPass123", name="運営2")
        t = _token(client)
        me = dbmod.query_one("SELECT id FROM system_admins WHERE admin_id='admin'")["id"]
        r = client.delete(f"/api/admin/admins/{me}", headers=_hdr(t))
        assert r.status_code == 400

    def test_cannot_delete_last_admin(self, client):
        """管理者が1人しかいない状態では、削除リクエストは400になること。

        NOTE: この削除APIは必ず認証済み本人が呼ぶため、「総数1人」の状態で
        存在する削除対象は理論上つねに呼び出し本人と一致する（他の行が無い）。
        よってここでの400は「自分自身チェック」と「最後の1人ガード」の
        両方が同時に満たされた結果であり、両者を実HTTP経由で分離することは
        できない。「最後の1人ガード」のSQLが実際にアトミックに機能する
        ことは test_last_admin_guard_sql_is_atomic で単体検証する。
        """
        insert_admin("admin", "Admin123")
        t = _token(client)
        me = dbmod.query_one("SELECT id FROM system_admins WHERE admin_id='admin'")["id"]
        r = client.delete(f"/api/admin/admins/{me}", headers=_hdr(t))
        assert r.status_code == 400

    def test_last_admin_guard_sql_is_atomic(self, client):
        """admin_delete_admin が使う「最後の1人ガード」のDELETE文自体を直接検証する。

        レビュー指摘: SELECT COUNT(*) してから DELETE する2段構え（check-then-act）
        だと、2人の管理者が同時に互いを削除した場合、両方が「まだ2人いる」を
        見た状態のまま削除してしまい管理者が0人になる事故が起こり得る
        （本番はD1 REST経由で1文ごとにネットワーク往復があり競合窓が広い）。
        1文のDELETEに条件を埋め込めば、DBが判定と実行を原子的に行うため
        この競合が起きない。ここではそのSQL自体を直接呼び、
          - 管理者が1人のときは0行削除・行は残る
          - 管理者が2人のときは1行削除される
        ことを検証する（HTTP経由では自分自身チェックに埋もれて確認できない）。
        """
        aid = insert_admin("admin", "Admin123")
        meta = dbmod.execute(
            "DELETE FROM system_admins WHERE id=? AND (SELECT COUNT(*) FROM system_admins) > 1",
            (aid,))
        assert meta["changes"] == 0, "管理者が1人しかいないのに削除されてしまった"
        assert dbmod.query_one("SELECT id FROM system_admins WHERE id=?", (aid,)) is not None

        other = insert_admin("ops2", "OpsPass123", name="運営2")
        meta = dbmod.execute(
            "DELETE FROM system_admins WHERE id=? AND (SELECT COUNT(*) FROM system_admins) > 1",
            (other,))
        assert meta["changes"] == 1
        assert dbmod.query_one("SELECT id FROM system_admins WHERE id=?", (other,)) is None

    def test_deleting_admin_revokes_their_sessions(self, client):
        insert_admin("admin", "Admin123")
        other = insert_admin("ops2", "OpsPass123", name="運営2")
        other_token = _token(client, "ops2", "OpsPass123")
        t = _token(client)
        assert client.delete(f"/api/admin/admins/{other}", headers=_hdr(t)).status_code == 200
        assert client.get("/api/me", headers=_hdr(other_token)).status_code == 401
