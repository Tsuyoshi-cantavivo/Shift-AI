"""店舗のアーカイブ・復元・設定編集。"""
import json

import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff, make_session


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


class TestArchive:
    def test_archive_hides_from_default_list(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        insert_shop("SHOP2", name="店2")

        assert client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t)).status_code == 200

        codes = [s["shop_code"] for s in client.get("/api/admin/shops", headers=_hdr(t)).get_json()["shops"]]
        assert "SHOP1" not in codes
        assert "SHOP2" in codes

        codes = [s["shop_code"] for s in client.get("/api/admin/shops?include_archived=1",
                                                    headers=_hdr(t)).get_json()["shops"]]
        assert "SHOP1" in codes

    def test_archive_sets_flags_and_deactivates(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        row = dbmod.query_one("SELECT is_archived, archived_at, is_active FROM shops WHERE id=?", (sid,))
        assert row["is_archived"] == 1
        assert row["archived_at"]
        assert row["is_active"] == 0

    def test_archive_revokes_sessions(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", "pw12345678", name="店1")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        shop_token = r.get_json()["token"]
        assert client.get("/api/me", headers=_hdr(shop_token)).status_code == 200

        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert client.get("/api/me", headers=_hdr(shop_token)).status_code == 401

    def test_archived_shop_cannot_login(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", "pw12345678", name="店1")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        assert r.status_code == 400

    def test_unarchive_restores(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert client.post(f"/api/admin/shops/{sid}/unarchive", headers=_hdr(t)).status_code == 200
        row = dbmod.query_one("SELECT is_archived, archived_at, is_active FROM shops WHERE id=?", (sid,))
        assert row["is_archived"] == 0
        assert row["archived_at"] is None
        # 復元しても有効化はしない（明示的に有効化させる）
        assert row["is_active"] == 0

    def test_archive_is_audited(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert dbmod.query_one("SELECT id FROM audit_logs WHERE action='shop.archive'") is not None

    def test_require_auth_rejects_archived_shop_even_with_live_session(self, client):
        """アーカイブは archive() 内のセッション全削除を主防御にしているが、それだけに
        頼らない多層防御として require_auth 側にも is_archived チェックがある。
        ここではセッション行が（何らかの理由で）生き残ったケースを直接再現し、
        require_auth 単体でも 401 に落ちることを確認する。"""
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        # archive() の DELETE FROM sessions を経由せず、直接セッションを作る
        token = make_session("shop", sid, shop_id=sid)
        assert client.get("/api/me", headers=_hdr(token)).status_code == 401

    def test_impersonate_archived_shop_audit_detail_combines_suffixes(self, client):
        """アーカイブは is_active=0 も同時にセットするため、代理閲覧の監査ログには
        「（停止中）」と「（アーカイブ済み）」の両方が付く。文字列連結の順序が
        崩れて壊れていないことをリグレッションとして固定する。"""
        t = _admin_token(client)
        sid = insert_shop("SHOPBOTH", name="店1")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t))
        assert r.status_code == 200
        row = dbmod.query_one(
            "SELECT detail FROM audit_logs WHERE action='admin.impersonate_start' "
            "ORDER BY id DESC LIMIT 1")
        assert row["detail"] == "SHOPBOTH の代理閲覧を開始（閲覧のみ）（停止中）（アーカイブ済み）"


class TestShopSettings:
    def test_update_settings_merges(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1", settings={"default_hourly_wage": 1100,
                                                          "max_daily_hours": 8})
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=_hdr(t),
                       json={"default_hourly_wage": 1200})
        assert r.status_code == 200
        # レスポンス本体（{"ok": True, "settings": {...}}）自体もマージ後の値を
        # 返していること（DB行だけでなくAPI応答の正常系も自動テストで守る）。
        body = r.get_json()
        assert body["settings"]["default_hourly_wage"] == 1200
        assert body["settings"]["max_daily_hours"] == 8
        s = json.loads(dbmod.query_one("SELECT settings FROM shops WHERE id=?", (sid,))["settings"])
        assert s["default_hourly_wage"] == 1200
        assert s["max_daily_hours"] == 8, "既存キーが消えている"

    def test_unknown_key_is_rejected(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=_hdr(t),
                       json={"evil_key": 1})
        assert r.status_code == 400

    def test_non_dict_body_is_rejected(self, client):
        """JSON配列などdict以外のボディは、生のPython例外文字列が漏れる500ではなく
        400として拒否されること（admin_update_shop と同じ「型不正は400」の慣習）。"""
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=_hdr(t),
                       json=["not", "a", "dict"])
        assert r.status_code == 400

    def test_unknown_shop_returns_404(self, client):
        t = _admin_token(client)
        r = client.put("/api/admin/shops/99999/settings", headers=_hdr(t),
                       json={"default_hourly_wage": 1200})
        assert r.status_code == 404
