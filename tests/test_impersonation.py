"""代理閲覧（impersonation）のテスト。

管理者はサポート時に顧客の画面を見る必要があるが、書き込みは許さない。
運営者が顧客の確定シフトを壊す事故を構造的に防ぐため、GET のみ許可する。
"""
from flask import g, request_finished

import app as appmod
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff, make_session


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    assert r.status_code == 200
    return r.get_json()["token"]


def _second_admin_token(client, admin_id="admin2"):
    """別の管理者アカウントでログインする。

    login() の "admin" はマジックワードなので、admin_id が "admin" 以外のときは
    店舗コード側に "admin" を入れ、ユーザーコード側に admin_id を入れる。
    """
    insert_admin(admin_id, "Admin123")
    r = client.post("/api/login", json={"shop_code": "admin", "user_code": admin_id,
                                        "password": "Admin123"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["token"]


def _capture_impersonating(client, method, path, headers=None):
    """リクエスト終了時点の g.impersonating を捕捉する。

    g はリクエストコンテキスト内でしか読めないため、request_finished シグナル
    （finalize_request 内で送出＝コンテキストがまだ生きている）で拾う。
    未設定なら "UNSET" が入るので「常に定義済み」であることまで検証できる。
    """
    seen = []

    def _rec(sender, response, **extra):
        seen.append(getattr(g, "impersonating", "UNSET"))

    with request_finished.connected_to(_rec, appmod.app):
        getattr(client, method)(path, headers=headers or {})
    return seen


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _shop_with_staff():
    sid = insert_shop("SHOP1", "pw12345678", name="レイクタウン店")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    insert_staff(sid, "p1", "アルバイト太郎")
    return sid


class TestImpersonateStart:
    def test_start_and_read_shop_data(self, client):
        """代理開始後、店舗APIのGETが通ること。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        # 代理前は403
        assert client.get("/api/shop/staffs", headers=_hdr(t)).status_code == 403

        r = client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        assert r.status_code == 200
        assert r.get_json()["shop"]["shop_name"] == "レイクタウン店"

        r = client.get("/api/shop/staffs", headers=_hdr(t))
        assert r.status_code == 200
        names = [s["name"] for s in r.get_json()["staffs"]]
        assert "アルバイト太郎" in names

    def test_unknown_shop_returns_404(self, client):
        t = _admin_token(client)
        assert client.post("/api/admin/impersonate/99999", headers=_hdr(t)).status_code == 404

    def test_shop_role_cannot_impersonate(self, client):
        sid = _shop_with_staff()
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        assert client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t)).status_code == 403


class TestImpersonateReadOnly:
    def test_write_is_forbidden(self, client):
        """代理中は POST/PUT/DELETE が 403 になること。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))

        r = client.post("/api/shop/staffs", headers=_hdr(t),
                        json={"staff_code": "new1", "name": "新人", "role": "part_time",
                              "password": "pw12345678"})
        assert r.status_code == 403, "代理中に書き込みができてしまう"

        staff = dbmod.query_one("SELECT id FROM staffs WHERE staff_code='p1'")
        r = client.delete(f"/api/shop/staffs/{staff['id']}", headers=_hdr(t))
        assert r.status_code == 403
        assert dbmod.query_one("SELECT id FROM staffs WHERE staff_code='p1'") is not None


class TestImpersonateScope:
    def test_admin_api_still_works_during_impersonation(self, client):
        """代理中でも /api/admin/* は管理者として動くこと（戻れなくならないため）。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        r = client.get("/api/admin/shops", headers=_hdr(t))
        assert r.status_code == 200

    def test_staff_api_is_not_impersonated(self, client):
        """代理中でもスタッフ用APIには化けないこと。

        brief は /api/staff/myshift を挙げていたが実在しないため、
        require_auth(["staff"]) で守られた実在の GET（/api/staff/dashboard）を使う。
        """
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        r = client.get("/api/staff/dashboard", headers=_hdr(t))
        assert r.status_code == 403

    def test_me_reports_impersonating(self, client):
        sid = _shop_with_staff()
        t = _admin_token(client)
        assert client.get("/api/me", headers=_hdr(t)).get_json().get("impersonating") is None
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        me = client.get("/api/me", headers=_hdr(t)).get_json()
        assert me["role"] == "admin", "代理中でも /api/me は管理者のまま返すこと"
        assert me["impersonating"]["shop_id"] == sid
        assert me["impersonating"]["shop_name"] == "レイクタウン店"


class TestImpersonateEnd:
    def test_stop_restores_admin(self, client):
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        assert client.get("/api/shop/staffs", headers=_hdr(t)).status_code == 200

        r = client.delete("/api/admin/impersonate", headers=_hdr(t))
        assert r.status_code == 200
        assert client.get("/api/shop/staffs", headers=_hdr(t)).status_code == 403
        assert client.get("/api/me", headers=_hdr(t)).get_json().get("impersonating") is None

    def test_deleted_shop_during_impersonation_returns_409(self, client):
        """代理中の店舗が消えた場合、別テナントに着地せず 409 になること。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        dbmod.execute("UPDATE sessions SET acting_shop_id=99999 WHERE role='admin'")
        r = client.get("/api/shop/staffs", headers=_hdr(t))
        assert r.status_code == 409


class TestImpersonateIsolation:
    """代理状態が「自分のセッション行」だけに閉じていること。

    UPDATE の WHERE を token=? から role='admin' に緩めると、押していない運営者が
    勝手に代理状態になったり、逆に他人の代理を解除してしまう。管理者アカウントは
    複数持てる（Task 5）ので、これは現実的な回帰。
    """

    def test_other_admin_is_not_impersonated(self, client):
        sid = _shop_with_staff()
        ta = _admin_token(client)
        tb = _second_admin_token(client)
        assert client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(ta)).status_code == 200
        assert client.get("/api/shop/staffs", headers=_hdr(ta)).status_code == 200
        assert client.get("/api/shop/staffs", headers=_hdr(tb)).status_code == 403, \
            "代理を開始していない別の管理者まで代理状態になっている"
        assert client.get("/api/me", headers=_hdr(tb)).get_json().get("impersonating") is None

    def test_other_admin_stop_does_not_end_my_impersonation(self, client):
        sid = _shop_with_staff()
        ta = _admin_token(client)
        tb = _second_admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(ta))
        assert client.delete("/api/admin/impersonate", headers=_hdr(tb)).status_code == 200
        assert client.get("/api/shop/staffs", headers=_hdr(ta)).status_code == 200, \
            "別の管理者の解除操作で自分の代理まで解除されている"

    def test_shop_session_ignores_acting_shop_id(self, client):
        """shop セッションに acting_shop_id が乗っても別テナントに着地しないこと。

        現状 non-admin に acting_shop_id を立てるコードは無いが、require_auth は
        全APIの唯一の認可関門で、role == "admin" の条件を落とすと店舗セッションが
        他店のデータを読める状態になる（多層防御の要）。
        """
        sid1 = insert_shop("SHOP1", "pw12345678", name="レイクタウン店")
        insert_staff(sid1, "p1", "自店スタッフ")
        mgr = insert_staff(sid1, "mgr", "店長", role="manager", password="pw12345678")
        sid2 = insert_shop("SHOP2", "pw12345678", name="別テナント店")
        insert_staff(sid2, "p2", "他店スタッフ")

        t = make_session("shop", mgr, sid1)
        dbmod.execute("UPDATE sessions SET acting_shop_id=? WHERE token=?", (sid2, t))

        r = client.get("/api/shop/staffs", headers=_hdr(t))
        assert r.status_code == 200
        names = [s["name"] for s in r.get_json()["staffs"]]
        assert "自店スタッフ" in names
        assert "他店スタッフ" not in names, \
            "shop セッションの acting_shop_id で別テナントに着地している"


class TestImpersonatingFlag:
    """g.impersonating（brief の Produces で公開したインタフェース）の検証。"""

    def test_true_during_impersonated_get(self, client):
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        assert _capture_impersonating(client, "get", "/api/shop/staffs", _hdr(t)) == [True]

    def test_false_on_normal_auth(self, client):
        _shop_with_staff()
        t = _admin_token(client)
        assert _capture_impersonating(client, "get", "/api/admin/shops", _hdr(t)) == [False]

    def test_false_when_require_auth_is_not_reached(self, client):
        """require_auth を通らない／abort する経路でも未設定にならないこと。

        audit() は login() / logout() から require_auth を経ずに呼ばれ、しかも
        例外を握り潰すため、未設定だと監査ログが静かに欠落する。
        """
        # require_auth を一度も通らない経路
        assert _capture_impersonating(client, "get", "/api/health") == [False]
        # require_auth が 401 で abort する経路
        assert _capture_impersonating(client, "get", "/api/shop/staffs") == [False]


class TestImpersonateAudit:
    def test_start_and_end_are_audited(self, client):
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        client.delete("/api/admin/impersonate", headers=_hdr(t))
        actions = [r["action"] for r in dbmod.query_all(
            "SELECT action FROM audit_logs WHERE action LIKE 'admin.impersonate%'")]
        assert "admin.impersonate_start" in actions
        assert "admin.impersonate_end" in actions

    def test_switching_shops_closes_previous(self, client):
        """解除せず別店舗へ乗り換えたとき、旧店舗の end が記録されること。"""
        sid1 = _shop_with_staff()
        sid2 = insert_shop("SHOP2", "pw12345678", name="越谷店")
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid1}", headers=_hdr(t))
        client.post(f"/api/admin/impersonate/{sid2}", headers=_hdr(t))
        rows = dbmod.query_all(
            "SELECT action, target_id FROM audit_logs "
            "WHERE action LIKE 'admin.impersonate%' ORDER BY id")
        assert [(r["action"], r["target_id"]) for r in rows] == [
            ("admin.impersonate_start", sid1),
            ("admin.impersonate_end", sid1),
            ("admin.impersonate_start", sid2),
        ], "乗り換え時に旧店舗の終了が記録されていない"

    def test_inactive_shop_is_marked_in_audit_detail(self, client):
        """停止中の店舗に入ったことがログから読めること（入れること自体は許容）。"""
        sid = _shop_with_staff()
        dbmod.execute("UPDATE shops SET is_active=0 WHERE id=?", (sid,))
        t = _admin_token(client)
        assert client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t)).status_code == 200
        row = dbmod.query_one(
            "SELECT detail FROM audit_logs WHERE action='admin.impersonate_start'")
        assert "（停止中）" in row["detail"]


class TestImpersonateSafeMethods:
    def test_head_is_allowed(self, client):
        """HEAD はセーフメソッドなので GET と同じく通ること。"""
        sid = _shop_with_staff()
        t = _admin_token(client)
        client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        assert client.head("/api/shop/staffs", headers=_hdr(t)).status_code == 200
