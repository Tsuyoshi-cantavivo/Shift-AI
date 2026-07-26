"""システム管理者アカウントの管理API。

背景: system_admins への UPDATE がコード全体でゼロで、/api/init が作る
初期パスワードを変更する正規の手段が存在しなかった。S4 で初期パスワードを
ランダム化したため、変更手段が無いままだと発行された値を一生使い続けることになる。

管理者アカウントの一覧/追加/削除（S3）は Phase 2 Task 5 で実装済み（TestAdminAccounts）。
"""
import os
import threading

import admin_api
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff

# tests/ の1つ上（プロジェクトルート）。schema.sql の場所を conftest.py と
# 同じ方法で解決する（一時ファイルDBへの切り替えテストで使う）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_PROJECT_ROOT, "schema.sql")


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

    def test_duplicate_admin_id_race_is_converted_to_400(self, client, monkeypatch):
        """事前の重複チェックをすり抜けても、INSERTのUNIQUE制約違反を400に変換すること。

        本番でこの状況が起きるのは2つの作成リクエストが競合したとき
        （事前チェックの時点ではどちらも「重複無し」と判定されてしまう）。
        ここでは admin_api.query_one を monkeypatch し、重複チェックのSQLだけ
        常に None を返すようにして、その競合状態を決定的に再現する。
        INSERT自体はUNIQUE制約に違反するため、admin_create_admin の
        try/except（UNIQUE違反を400に変換する処理）を削除するミューテーション
        を行うと、このテストは500になってFAILする（手動確認済み）。
        """
        insert_admin("admin", "Admin123")
        t = _token(client)

        real_query_one = admin_api.query_one
        dup_check_sql = "SELECT id FROM system_admins WHERE admin_id=?"

        def patched_query_one(sql, params=()):
            if sql == dup_check_sql:
                return None
            return real_query_one(sql, params)

        monkeypatch.setattr(admin_api, "query_one", patched_query_one)

        r = client.post("/api/admin/admins", headers=_hdr(t),
                        json={"admin_id": "admin", "name": "重複", "password": "OpsPass123"})
        assert r.status_code == 400
        assert "error" in r.get_json()

    def test_non_string_admin_id_does_not_500(self, client):
        """admin_id に数値等の非文字列が来ても500にならないこと。

        (body.get("admin_id") or "").strip() だと、真値の非文字列
        （例: 数値 12345）で AttributeError → 500 になる。
        sanitize_login_code は str() で吸収してから正規化するため、
        非文字列でも安全に作成できる。
        """
        insert_admin("admin", "Admin123")
        t = _token(client)
        r = client.post("/api/admin/admins", headers=_hdr(t),
                        json={"admin_id": 12345, "name": "数値ID", "password": "OpsPass123"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert dbmod.query_one("SELECT id FROM system_admins WHERE admin_id='12345'") is not None

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
        逐次リクエストに限れば不可能。並行リクエストでの「最後の1人ガード」
        単体の原子性は test_concurrent_delete_last_admin_guard_is_atomic で
        HTTP経由のまま検証する。
        """
        insert_admin("admin", "Admin123")
        t = _token(client)
        me = dbmod.query_one("SELECT id FROM system_admins WHERE admin_id='admin'")["id"]
        r = client.delete(f"/api/admin/admins/{me}", headers=_hdr(t))
        assert r.status_code == 400

    def test_concurrent_delete_last_admin_guard_is_atomic(self, tmp_path, monkeypatch):
        """2人の管理者が同時に互いを削除しようとしても、片方だけ成功し必ず1人残る。

        レビュー指摘: 旧実装は SELECT COUNT(*) してから DELETE する2段構え
        （check-then-act）で、2人の管理者が同時に互いを削除しようとすると
        両方が「まだ2人いる」を見た状態のまま削除を実行してしまい、管理者が
        0人になる事故が起こり得た（本番はD1 REST経由で1文ごとにネットワーク
        往復があり、この競合窓がローカルSQLiteより桁違いに広い）。

        【なぜ一時ファイルDBに切り替えるか】
          通常のテストは DB_PATH=":memory:" で、プロセス全体が1つの共有
          sqlite3.Connection を使い回す設計（高速化のため。conftest.py参照）。
          その同一コネクションオブジェクトを2スレッドから本当に同時実行すると、
          Pythonのsqlite3モジュールが公式に「呼び出し側で直列化する責任がある」
          と明記している領域に踏み込み、実際に試したところセグメンテーション
          違反でプロセスごと落ちることを確認した（校閲時に検証済み）。
          そのため、このテストだけは一時ファイルDBに切り替え、DBアクセスの
          たびに新しいコネクションを開く経路（db.py の file モード）を使わせる
          ことで、本物の別コネクションによる安全な同時アクセスを再現する。

        本テストは admin_delete_admin を実HTTP経由でそのまま呼び出し、
        「最後の1人ガード」のDELETE文が実行される直前に
        threading.Barrier(2) で2スレッドを同期させ、実際に同時到達する
        状況を再現する。逐次リクエストでは「総数1人のとき削除対象は
        必ず自分自身」になり自分自身チェックに隠れて検証できないが
        （test_cannot_delete_last_admin 参照）、並行リクエストでは互いに
        「自分ではない対象」を削除しようとするため、最後の1人ガードだけを
        独立して検証できる。

        ガード条件（AND (SELECT COUNT(*) FROM system_admins) > 1）を削除する
        ミューテーションを行うと、このテストは「残り0人」または「両方200」で
        FAILする（手動確認済み。詳細は task-5-report.md 参照）。
        """
        db_path = str(tmp_path / "concurrent_delete_test.db")
        monkeypatch.setattr(dbmod, "DB_PATH", db_path)
        dbmod.init_schema(_SCHEMA_PATH)

        import app as appmod
        client = appmod.app.test_client()

        a = insert_admin("admin", "Admin123")
        b = insert_admin("ops2", "OpsPass123", name="運営2")
        token_a = _token(client, "admin", "Admin123")
        token_b = _token(client, "ops2", "OpsPass123")

        barrier = threading.Barrier(2)
        real_execute = admin_api.execute
        guard_sql = ("DELETE FROM system_admins WHERE id=? AND "
                     "(SELECT COUNT(*) FROM system_admins) > 1")

        def patched_execute(sql, params=()):
            if sql == guard_sql:
                # 2スレッドがこのガードDELETEに実際に同時到達するまで待つ。
                barrier.wait(timeout=5)
            return real_execute(sql, params)

        monkeypatch.setattr(admin_api, "execute", patched_execute)

        results = {}

        def do_delete(key, token, target_id):
            r = client.delete(f"/api/admin/admins/{target_id}", headers=_hdr(token))
            results[key] = r.status_code

        t1 = threading.Thread(target=do_delete, args=("a_deletes_b", token_a, b))
        t2 = threading.Thread(target=do_delete, args=("b_deletes_a", token_b, a))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive() and not t2.is_alive(), "スレッドがタイムアウトした（デッドロックの疑い）"
        statuses = sorted(results.values())
        assert statuses == [200, 400], f"想定: 片方200・片方400。実際: {results}"
        remaining = dbmod.query_all("SELECT id FROM system_admins")
        assert len(remaining) == 1, f"管理者が1人残るはずが {len(remaining)} 人になった: {remaining}"

    def test_deleting_admin_revokes_their_sessions(self, client):
        insert_admin("admin", "Admin123")
        other = insert_admin("ops2", "OpsPass123", name="運営2")
        other_token = _token(client, "ops2", "OpsPass123")
        t = _token(client)
        assert client.delete(f"/api/admin/admins/{other}", headers=_hdr(t)).status_code == 200
        assert client.get("/api/me", headers=_hdr(other_token)).status_code == 401
