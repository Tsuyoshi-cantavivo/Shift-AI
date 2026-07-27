"""店舗のエクスポートと完全削除。"""
import json

import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff, insert_pattern


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _shop_with_data():
    sid = insert_shop("SHOP1", "pw12345678", name="レイクタウン店")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    insert_staff(sid, "p1", "太郎")
    insert_pattern(sid, "早番", "09:00", "17:00", 2)
    return sid


class TestExport:
    def test_export_contains_shop_data(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        r = client.get(f"/api/admin/shops/{sid}/export", headers=_hdr(t))
        assert r.status_code == 200
        assert "attachment" in r.headers.get("Content-Disposition", "")
        data = json.loads(r.get_data(as_text=True))
        assert data["shop"]["shop_code"] == "SHOP1"
        assert len(data["staffs"]) == 2
        assert len(data["shift_patterns"]) == 1

    def test_export_excludes_password_hash(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        r = client.get(f"/api/admin/shops/{sid}/export", headers=_hdr(t))
        raw = r.get_data(as_text=True)
        assert "password_hash" not in raw, "パスワードハッシュがエクスポートに含まれている"

    def test_export_is_audited(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        client.get(f"/api/admin/shops/{sid}/export", headers=_hdr(t))
        assert dbmod.query_one("SELECT id FROM audit_logs WHERE action='shop.export'") is not None

    def test_unknown_shop_returns_404(self, client):
        t = _admin_token(client)
        assert client.get("/api/admin/shops/99999/export", headers=_hdr(t)).status_code == 404


class TestDelete:
    def test_delete_requires_archived(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 400, "アーカイブ前に削除できてしまう"
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is not None

    def test_delete_requires_matching_confirm_code(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "WRONG"})
        assert r.status_code == 400
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is not None

    def test_delete_removes_all_dependent_rows(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is None
        assert dbmod.query_one("SELECT id FROM staffs WHERE shop_id=?", (sid,)) is None
        assert dbmod.query_one("SELECT id FROM shift_patterns WHERE shop_id=?", (sid,)) is None
        assert dbmod.query_one("SELECT token FROM sessions WHERE shop_id=?", (sid,)) is None

    def test_delete_keeps_audit_logs(self, client):
        """監査ログは運営の記録なので消さないこと。"""
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t), json={"confirm_code": "SHOP1"})
        rows = dbmod.query_all("SELECT id FROM audit_logs WHERE shop_id=?", (sid,))
        assert rows, "監査ログまで消えている"
        row = dbmod.query_one("SELECT detail FROM audit_logs WHERE action='shop.delete'")
        assert row is not None
        assert "SHOP1" in (row["detail"] or ""), "店舗コードが記録に残っていない"

    def test_delete_does_not_touch_other_shops(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        other = insert_shop("SHOP2", name="店2")
        insert_staff(other, "p9", "別店の人")
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t), json={"confirm_code": "SHOP1"})
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (other,)) is not None
        assert dbmod.query_one("SELECT id FROM staffs WHERE shop_id=?", (other,)) is not None

    def test_delete_is_idempotent_on_retry(self, client):
        """再実行しても壊れないこと（execute が毎回commitしロールバックできないため）。"""
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                             json={"confirm_code": "SHOP1"}).status_code == 200
        # 店舗が消えたので2回目は404
        assert client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                             json={"confirm_code": "SHOP1"}).status_code == 404

    def test_delete_resumes_after_partial_failure(self, client):
        """途中で失敗した後、同じリクエストの再実行で最後まで進むこと。

        本番の D1 は1文ごとにネットワーク往復するため途中で落ち得る。shops を
        最後に消すので、途中失敗の状態は「子テーブルだけ消えて店舗行は残る」。
        ここでは1回目が fixed_shifts / shift_patterns まで進んで落ちた状況を作り、
        再実行が0件 DELETE を素通りして完了することを確認する。
        """
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        dbmod.execute("DELETE FROM fixed_shifts WHERE staff_id IN "
                      "(SELECT id FROM staffs WHERE shop_id=?)", (sid,))
        dbmod.execute("DELETE FROM shift_patterns WHERE shop_id=?", (sid,))

        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 200, r.get_data(as_text=True)
        deleted = r.get_json()["deleted"]
        # どこまで消したかが呼び出し側に返ること（再実行の判断材料）
        assert deleted[0] == "fixed_shifts" and deleted[-1] == "shops", deleted
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is None
        assert dbmod.query_one("SELECT id FROM staffs WHERE shop_id=?", (sid,)) is None


class TestDeleteTenantIsolation:
    """他テナントのデータに一切触れないこと。

    DELETE から WHERE shop_id=? が1つでも落ちると、無関係の顧客のデータが
    道連れで消える。テーブル単位で「他店の行が残っている」ことを固定する。
    """

    def _other_shop_with_every_table(self, t, client):
        """削除対象と同じ全テーブルに行を持つ別店舗を作る。"""
        other = insert_shop("SHOP2", "pw12345678", name="別テナント店")
        staff = insert_staff(other, "p9", "別店の人")
        pid = insert_pattern(other, "遅番", "13:00", "21:00", 1)
        dbmod.execute("INSERT INTO shift_pattern_weekday_required "
                      "(pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
                      (pid, other, 1, 3))
        dbmod.execute("INSERT INTO fixed_shifts (staff_id, weekday, start_time, end_time) "
                      "VALUES (?,?,?,?)", (staff, 2, "10:00", "18:00"))
        dbmod.execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
                      "VALUES (?,?,?,?,?)",
                      (other, staff, "2026-08-01T10:00:00", "2026-08-01T18:00:00", "confirmed"))
        dbmod.execute("INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime) "
                      "VALUES (?,?,?,?)",
                      (other, staff, "2026-08-02T10:00:00", "2026-08-02T18:00:00"))
        dbmod.execute("INSERT INTO change_requests (shop_id, staff_id, request_type) VALUES (?,?,?)",
                      (other, staff, "cancel"))
        dbmod.execute("INSERT INTO notifications (shop_id, staff_id, type, title) VALUES (?,?,?,?)",
                      (other, staff, "info", "別店へのお知らせ"))
        dbmod.execute("INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline) "
                      "VALUES (?,?,?,?)", (other, "2026-08-01", "2026-08-31", "2026-07-25"))
        dbmod.execute("INSERT INTO shop_holidays (shop_id, holiday_date) VALUES (?,?)",
                      (other, "2026-08-11"))
        dbmod.execute("INSERT INTO sessions (token, role, user_id, shop_id) VALUES (?,?,?,?)",
                      ("tok_other", "shop", staff, other))
        return other, staff

    def test_delete_leaves_every_other_shop_table_intact(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        other, staff = self._other_shop_with_every_table(t, client)
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 200, r.get_data(as_text=True)

        for table in ("shift_patterns", "shift_pattern_weekday_required", "shifts",
                      "wish_history", "change_requests", "notifications",
                      "shift_request_periods", "shop_holidays", "sessions", "staffs"):
            row = dbmod.query_one(f"SELECT * FROM {table} WHERE shop_id=?", (other,))
            assert row is not None, f"他店の {table} が巻き添えで消えている"
        assert dbmod.query_one("SELECT id FROM fixed_shifts WHERE staff_id=?", (staff,)) is not None, \
            "他店スタッフの fixed_shifts が巻き添えで消えている"
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (other,)) is not None


class TestDeleteDuringImpersonation:
    def test_impersonating_admin_escapes_with_409(self, client):
        """代理中の店舗を完全削除したら、代理していた管理者が 409 で脱出できること。

        require_auth の 409 防御は「SELECT * FROM shops WHERE id=? が行を引けない」
        ことに依存している（src/app.py の代理閲覧経路）。完全削除を論理削除に
        置き換えるとこの防御が効かなくなり、削除済み店舗を代理閲覧し続けられて
        しまうため、物理削除であることをテストで固定する。
        """
        t = _admin_token(client)
        sid = _shop_with_data()
        assert client.post(f"/api/admin/impersonate/{sid}", headers=_hdr(t)).status_code == 200
        assert client.get("/api/shop/staffs", headers=_hdr(t)).status_code == 200

        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        assert client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                             json={"confirm_code": "SHOP1"}).status_code == 200

        r = client.get("/api/shop/staffs", headers=_hdr(t))
        assert r.status_code == 409, "削除済み店舗の代理閲覧が続いている"
        # 管理者に戻る導線（/api/admin/*）は生きていること
        assert client.delete("/api/admin/impersonate", headers=_hdr(t)).status_code == 200
        assert client.get("/api/shop/staffs", headers=_hdr(t)).status_code == 403
