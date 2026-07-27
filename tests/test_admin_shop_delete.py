"""店舗のエクスポートと完全削除。"""
import json
import re

import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff, insert_pattern


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _populate_dependent_rows(sid, staff_id, pattern_id):
    """店舗に紐づく全テーブルへ1件ずつ行を作る。

    削除される側にも FK 依存のある行を必ず置くこと。staffs と shift_patterns しか
    無いと、shifts / wish_history / change_requests を staffs より後ろへ動かす
    「削除順の変異」が FK 違反を起こさず素通りしてしまい、テストが順序の回帰を
    検出できなくなる。
    """
    dbmod.execute("INSERT INTO shift_pattern_weekday_required "
                  "(pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
                  (pattern_id, sid, 1, 3))
    dbmod.execute("INSERT INTO fixed_shifts (staff_id, weekday, start_time, end_time) "
                  "VALUES (?,?,?,?)", (staff_id, 2, "10:00", "18:00"))
    dbmod.execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
                  "VALUES (?,?,?,?,?)",
                  (sid, staff_id, "2026-08-01T10:00:00", "2026-08-01T18:00:00", "confirmed"))
    dbmod.execute("INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime) "
                  "VALUES (?,?,?,?)",
                  (sid, staff_id, "2026-08-02T10:00:00", "2026-08-02T18:00:00"))
    dbmod.execute("INSERT INTO change_requests (shop_id, staff_id, request_type) VALUES (?,?,?)",
                  (sid, staff_id, "cancel"))
    dbmod.execute("INSERT INTO notifications (shop_id, staff_id, type, title) VALUES (?,?,?,?)",
                  (sid, staff_id, "info", "お知らせ"))
    dbmod.execute("INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline) "
                  "VALUES (?,?,?,?)", (sid, "2026-08-01", "2026-08-31", "2026-07-25"))
    dbmod.execute("INSERT INTO shop_holidays (shop_id, holiday_date) VALUES (?,?)",
                  (sid, "2026-08-11"))
    dbmod.execute("INSERT INTO sessions (token, role, user_id, shop_id) VALUES (?,?,?,?)",
                  (f"tok_{sid}", "shop", staff_id, sid))


def _shop_with_data():
    """削除対象の店舗。削除対象になる全テーブルに行を持つ。

    export のテストが件数を見ているので staffs は2名・shift_patterns は1件に保つ
    （weekday_required は新しいパターンを足さず既存のものにぶら下げる）。
    """
    sid = insert_shop("SHOP1", "pw12345678", name="レイクタウン店")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    staff = insert_staff(sid, "p1", "太郎")
    pid = insert_pattern(sid, "早番", "09:00", "17:00", 2)
    _populate_dependent_rows(sid, staff, pid)
    return sid


def _staff_id(sid, code="p1"):
    return dbmod.query_one("SELECT id FROM staffs WHERE shop_id=? AND staff_code=?",
                           (sid, code))["id"]


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

    def test_export_filename_is_sanitized(self, client):
        """shop_code は運営の自由入力なので filename に生で入れないこと。

        " が入るとヘッダのクォートが壊れ、添付ファイル名が意図しない形になる。
        """
        t = _admin_token(client)
        sid = insert_shop('EV"IL/横浜 店', "pw12345678", name="記号入り店")
        r = client.get(f"/api/admin/shops/{sid}/export", headers=_hdr(t))
        assert r.status_code == 200
        cd = r.headers.get("Content-Disposition", "")
        m = re.fullmatch(r'attachment; filename="([^"]*)"', cd)
        assert m, f"ヘッダが壊れている: {cd!r}"
        assert re.fullmatch(r"shop-[A-Za-z0-9_-]+-\d{8}\.json", m.group(1)), m.group(1)
        # 監査ログには元のコードがそのまま残ること（証跡までは落とさない）
        row = dbmod.query_one("SELECT detail FROM audit_logs WHERE action='shop.export'")
        assert 'EV"IL/横浜 店' in (row["detail"] or "")


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

    def test_delete_requires_exact_confirm_code(self, client):
        """大小違い・前方一致・部分一致では消えないこと。

        「完全一致」を緩めると、隣の店舗コードの打ち間違いで別の店が消える。
        """
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        for bad in ("shop1", "Shop1", "SHOP", "SHOP12", "HOP1", "SHOP 1", ""):
            r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                              json={"confirm_code": bad})
            assert r.status_code == 400, f"{bad!r} で削除が通ってしまう"
            assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is not None, \
                f"{bad!r} で店舗が消えた"
        # 前後の空白だけは許容する（コピペ由来の失敗を無駄に増やさないため）
        assert client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                             json={"confirm_code": "  SHOP1  "}).status_code == 200

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
        """監査ログは運営の記録なので消さないこと。

        削除処理自身が書いた行だけを見ると、audit_logs を削除対象に加える変異を
        検出できない（自己成就テストになる）。削除「前」に存在した行が残ることを見る。
        """
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        before = dbmod.query_all("SELECT id, action FROM audit_logs ORDER BY id")
        assert any(r["action"] == "shop.archive" for r in before), before

        client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t), json={"confirm_code": "SHOP1"})

        after_ids = {r["id"] for r in dbmod.query_all("SELECT id FROM audit_logs")}
        missing = [r["action"] for r in before if r["id"] not in after_ids]
        assert not missing, f"削除前から存在した監査ログが消えている: {missing}"
        assert dbmod.query_all("SELECT id FROM audit_logs WHERE shop_id=?", (sid,)), \
            "監査ログまで消えている"
        row = dbmod.query_one("SELECT detail FROM audit_logs WHERE action='shop.delete'")
        assert row is not None
        assert "SHOP1" in (row["detail"] or ""), "店舗コードが記録に残っていない"

    def test_delete_records_row_counts(self, client):
        """何件消したかを監査に残すこと（店舗行が消えた後の唯一の規模の証跡）。"""
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.get_json()["counts"]["staffs"] == 2
        row = dbmod.query_one("SELECT detail FROM audit_logs WHERE action='shop.delete_done'")
        assert row is not None, "完了の監査記録が無い"
        assert "staffs=2" in row["detail"] and "shops=1" in row["detail"], row["detail"]

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
        ここでは1回目が shifts まで進んで落ちた状況（実装の削除順の前半）を作り、
        再実行が0件 DELETE を素通りして完了することを確認する。
        """
        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        dbmod.execute("DELETE FROM fixed_shifts WHERE staff_id IN "
                      "(SELECT id FROM staffs WHERE shop_id=?)", (sid,))
        for table in ("sessions", "notifications", "change_requests", "wish_history", "shifts"):
            dbmod.execute(f"DELETE FROM {table} WHERE shop_id=?", (sid,))

        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 200, r.get_data(as_text=True)
        deleted = r.get_json()["deleted"]
        # どこまで消したかが呼び出し側に返ること（再実行の判断材料）
        assert deleted[0] == "fixed_shifts" and deleted[-1] == "shops", deleted
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is None
        assert dbmod.query_one("SELECT id FROM staffs WHERE shop_id=?", (sid,)) is None

    def test_delete_completes_with_orphan_rows(self, client):
        """shop_id から辿れない孤児行があっても完走すること。

        staffs を参照するテーブルを shop_id だけで絞ると、shop_id が NULL / 不整合の
        行が消し残り、直後の DELETE FROM staffs が FK 違反で落ちる。子テーブルは
        既に commit 済みなので「顧客データの大半は消えたのに API では二度と完了
        できない（手動SQLでしか直せない）」状態になる。
        """
        t = _admin_token(client)
        sid = _shop_with_data()
        staff = _staff_id(sid)
        dbmod.execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime) "
                      "VALUES (NULL,?,?,?)",
                      (staff, "2026-09-01T10:00:00", "2026-09-01T18:00:00"))
        dbmod.execute("INSERT INTO change_requests (shop_id, staff_id, request_type) "
                      "VALUES (NULL,?,?)", (staff, "cancel"))
        # wish_history.shop_id は NOT NULL なので「別店舗の id が入った不整合行」で再現
        dbmod.execute("INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime) "
                      "VALUES (?,?,?,?)",
                      (99999, staff, "2026-09-02T10:00:00", "2026-09-02T18:00:00"))
        dbmod.execute("INSERT INTO notifications (shop_id, staff_id, type, title) "
                      "VALUES (NULL,?,?,?)", (staff, "info", "孤児通知"))

        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is None
        assert dbmod.query_one("SELECT id FROM staffs WHERE shop_id=?", (sid,)) is None
        # 孤児ゴミも同時に掃除されること（消えたスタッフを参照する行を残さない）
        for table in ("shifts", "change_requests", "wish_history", "notifications"):
            assert dbmod.query_one(f"SELECT id FROM {table} WHERE staff_id=?", (staff,)) is None, \
                f"{table} に消えたスタッフを参照する孤児行が残っている"


class TestDeleteAudit:
    """破壊の「前」に記録が残ること。

    トランザクションが張れない以上、完了後にしか記録しないと、途中で失敗した
    ときに「誰がいつどの店舗を消し始めたか」が消えたデータごと残らなくなる。
    """

    def test_audit_is_written_before_any_delete(self, client, monkeypatch):
        import admin_api

        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))

        real = admin_api.execute

        def boom(sql, params=()):
            if sql.strip().upper().startswith("DELETE"):
                raise RuntimeError("D1 down")
            return real(sql, params)

        monkeypatch.setattr(admin_api, "execute", boom)
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 500

        row = dbmod.query_one("SELECT detail FROM audit_logs WHERE action='shop.delete' "
                              "AND target_id=?", (sid,))
        assert row is not None, "破壊が始まる前の監査記録が無い"
        assert "SHOP1" in (row["detail"] or "")
        # 完了記録は出ていないこと（＝途中で失敗したことが監査だけで判別できる）
        assert dbmod.query_one("SELECT id FROM audit_logs WHERE action='shop.delete_done'") is None

    def test_delete_aborts_when_audit_cannot_be_recorded(self, client, monkeypatch):
        """監査を書けないときは1行も消さないこと（記録の無い破壊を作らない）。

        audit() は失敗しても業務を止めない設計（src/app.py）なので、
        呼びっぱなしにすると記録ゼロのまま破壊が進む。
        """
        import app as appmod

        t = _admin_token(client)
        sid = _shop_with_data()
        client.post(f"/api/admin/shops/{sid}/archive", headers=_hdr(t))

        def boom(table, row):
            raise RuntimeError("audit down")

        monkeypatch.setattr(appmod, "insert_row", boom)
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(t),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 500
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is not None
        assert dbmod.query_one("SELECT id FROM staffs WHERE shop_id=?", (sid,)) is not None
        assert dbmod.query_one("SELECT id FROM shifts WHERE shop_id=?", (sid,)) is not None


class TestAuthorization:
    """新2エンドポイントが管理者専用であること。"""

    def _manager_token(self, client):
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    def test_export_requires_admin(self, client):
        sid = _shop_with_data()
        assert client.get(f"/api/admin/shops/{sid}/export").status_code == 401
        mt = self._manager_token(client)
        assert client.get(f"/api/admin/shops/{sid}/export", headers=_hdr(mt)).status_code == 403, \
            "店舗ロールが店舗の全データをエクスポートできてしまう"

    def test_delete_requires_admin(self, client):
        sid = _shop_with_data()
        assert client.delete(f"/api/admin/shops/{sid}",
                             json={"confirm_code": "SHOP1"}).status_code == 401
        mt = self._manager_token(client)
        r = client.delete(f"/api/admin/shops/{sid}", headers=_hdr(mt),
                          json={"confirm_code": "SHOP1"})
        assert r.status_code == 403, "店舗ロールが自店を完全削除できてしまう"
        assert dbmod.query_one("SELECT id FROM shops WHERE id=?", (sid,)) is not None


class TestDeleteTenantIsolation:
    """他テナントのデータに一切触れないこと。

    DELETE から WHERE shop_id=? が1つでも落ちると、無関係の顧客のデータが
    道連れで消える。テーブル単位で「他店の行が残っている」ことを固定する。
    """

    def _other_shop_with_every_table(self):
        """削除対象と同じ全テーブルに行を持つ別店舗を作る。"""
        other = insert_shop("SHOP2", "pw12345678", name="別テナント店")
        staff = insert_staff(other, "p9", "別店の人")
        pid = insert_pattern(other, "遅番", "13:00", "21:00", 1)
        _populate_dependent_rows(other, staff, pid)
        return other, staff

    def test_delete_leaves_every_other_shop_table_intact(self, client):
        t = _admin_token(client)
        sid = _shop_with_data()
        other, staff = self._other_shop_with_every_table()
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
