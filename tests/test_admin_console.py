"""管理者コンソールのAPI（マイグレーション適用・ダッシュボード等）。"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    assert r.status_code == 200
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


class TestMigrationsApi:
    def test_status_lists_migrations(self, client):
        t = _admin_token(client)
        r = client.get("/api/admin/migrations", headers=_hdr(t))
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data["migrations"], list)
        assert data["migrations"], "マイグレーションが1件も返っていない"
        assert isinstance(data["pending"], int)

    def test_apply_returns_result_shape(self, client):
        """適用が「全部成功した」ことまで検証する。

        【なぜ failed の中身まで見るのか】src/admin_api.py の適用ハンドラは
        失敗しても 200 + failed 埋めで返す（途中失敗を画面に出すため）。
        キーの存在だけを見ていると、migrator を作った動機そのもの
        （本番D1で 0003 が前半失敗・後半成功の部分適用になった事故）が
        再発してもテストはグリーンのまま通る。
        """
        t = _admin_token(client)
        r = client.post("/api/admin/migrations/apply", headers=_hdr(t))
        assert r.status_code == 200
        data = r.get_json()
        assert set(["applied", "skipped", "failed"]).issubset(data.keys())
        assert data["failed"] is None, f"マイグレーションが途中で失敗している: {data['failed']}"
        # 適用後は未適用が残っていないこと（部分適用のまま「成功」に見えないように）
        st = client.get("/api/admin/migrations", headers=_hdr(t)).get_json()
        assert st["pending"] == 0, f"適用後も未適用が残っている: {st['pending']}件"


class TestRemovedOrphanEndpoints:
    """本番D1で「ローカルSQLiteに DROP TABLE staffs を実行して ok:true を返す」
    孤児エンドポイントを削除したことを固定する。

    db.get_conn() → _get_local_conn() は DB_MODE を無視して常に
    sqlite3.connect(DB_PATH) を返すため、d1 モードでも本番D1ではなくコンテナ内の
    ローカルSQLiteを壊していた。スキーマ変更の正規経路は /api/admin/migrations。
    """

    def _rules(self):
        import app as appmod
        return {str(r) for r in appmod.app.url_map.iter_rules()}

    def test_db_migrate_endpoint_is_gone(self, client):
        # 静的配信のキャッチオール /<path:path> が GET だけを持つため、消えた
        # POST ルートは 404 ではなく 405 になる。どちらでも「ハンドラに届かない」
        # ことに変わりはないので、ルーティング表そのものからも消えていることを見る。
        assert "/api/admin/db/migrate" not in self._rules()
        t = _admin_token(client)
        assert client.post("/api/admin/db/migrate", headers=_hdr(t)).status_code in (404, 405)

    def test_db_restore_staffs_endpoint_is_gone(self, client):
        assert "/api/admin/db/restore-staffs" not in self._rules()
        t = _admin_token(client)
        assert client.post("/api/admin/db/restore-staffs",
                           headers=_hdr(t)).status_code in (404, 405)

    def test_readonly_diagnostic_endpoint_is_kept(self, client):
        """システム画面のDB診断タブが使っている読み取り専用APIは残すこと。"""
        t = _admin_token(client)
        r = client.get("/api/admin/db/diagnostic", headers=_hdr(t))
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_module_no_longer_exposes_raw_connection_helpers(self):
        """DB_MODE を無視する生コネクション取得ヘルパが残っていないこと。"""
        import admin_api
        assert not hasattr(admin_api, "db_module_get_conn")
        assert not hasattr(admin_api, "_restore_staffs_table_internal")

    def test_apply_is_audited(self, client):
        t = _admin_token(client)
        client.post("/api/admin/migrations/apply", headers=_hdr(t))
        row = dbmod.query_one("SELECT action FROM audit_logs WHERE action='admin.migrate' "
                              "ORDER BY id DESC LIMIT 1")
        assert row is not None, "マイグレーション適用が監査ログに残っていない"

    def test_requires_admin_role(self, client):
        sid = insert_shop("SHOP1", "pw12345678")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        assert client.get("/api/admin/migrations", headers=_hdr(t)).status_code == 403
        assert client.post("/api/admin/migrations/apply", headers=_hdr(t)).status_code == 403
