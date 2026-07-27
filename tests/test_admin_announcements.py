"""全店舗一斉通知。"""
import db as dbmod
from helpers import insert_admin, insert_shop, insert_staff


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


def _hdr(t):
    return {"Authorization": f"Bearer {t}"}


def _two_shops():
    a = insert_shop("A", name="店A")
    insert_staff(a, "mgrA", "店長A", role="manager")
    insert_staff(a, "p1", "太郎")
    b = insert_shop("B", name="店B")
    insert_staff(b, "mgrB", "店長B", role="manager")
    return a, b


class TestAnnounce:
    def test_to_managers_of_all_shops(self, client):
        t = _admin_token(client)
        a, b = _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "managers",
                              "title": "メンテナンス", "body": "8/1 に実施します"})
        assert r.status_code == 200
        assert r.get_json()["shops"] == 2
        rows = dbmod.query_all("SELECT shop_id, staff_id FROM notifications WHERE type='announcement'")
        assert len(rows) == 2
        assert all(x["staff_id"] is None for x in rows)

    def test_to_all_staff(self, client):
        t = _admin_token(client)
        a, b = _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "all",
                              "title": "お知らせ", "body": "本文"})
        assert r.status_code == 200
        rows = dbmod.query_all("SELECT shop_id, staff_id FROM notifications WHERE type='announcement'")
        # 店舗向け2件 + スタッフ3人分
        assert len(rows) == 5
        assert r.get_json()["recipients"] == 3

    def test_selected_shops_only(self, client):
        t = _admin_token(client)
        a, b = _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": [a], "audience": "managers",
                              "title": "個別", "body": "本文"})
        assert r.status_code == 200
        rows = dbmod.query_all("SELECT shop_id FROM notifications WHERE type='announcement'")
        assert {x["shop_id"] for x in rows} == {a}

    def test_archived_shops_are_excluded(self, client):
        t = _admin_token(client)
        a, b = _two_shops()
        client.post(f"/api/admin/shops/{b}/archive", headers=_hdr(t))
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "managers",
                              "title": "お知らせ", "body": "本文"})
        assert r.get_json()["shops"] == 1

    def test_title_is_required(self, client):
        t = _admin_token(client)
        _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "managers", "title": "", "body": "本文"})
        assert r.status_code == 400

    def test_empty_shop_ids_list_is_rejected(self, client):
        """shop_ids:[] は「店舗を選ぶ」で1件も選ばなかった誤操作の可能性が高い。

        None（未指定）は全店舗配信の意味だが、[] を同じ falsy 扱いにして
        全店舗配信へフォールバックさせると、誤操作で全社配信になり得るため
        400 で明確に拒否する（レビュー指摘）。
        """
        t = _admin_token(client)
        _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": [], "audience": "managers",
                              "title": "空配列テスト", "body": "本文"})
        assert r.status_code == 400
        assert dbmod.query_all(
            "SELECT id FROM notifications WHERE type='announcement'") == []

    def test_shop_ids_null_still_targets_all_shops(self, client):
        """shop_ids:[] を400にする変更が、shop_ids:null（全店舗)の挙動を
        壊していないことの回帰確認。"""
        t = _admin_token(client)
        _two_shops()
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "managers",
                              "title": "全店舗テスト", "body": "本文"})
        assert r.status_code == 200
        assert r.get_json()["shops"] == 2

    def test_same_created_at_within_batch(self, client):
        """配信履歴のグルーピングのため、同一バッチは created_at が揃うこと。"""
        t = _admin_token(client)
        _two_shops()
        client.post("/api/admin/announcements", headers=_hdr(t),
                    json={"shop_ids": None, "audience": "all", "title": "お知らせ", "body": "本文"})
        stamps = {r["created_at"] for r in
                  dbmod.query_all("SELECT created_at FROM notifications WHERE type='announcement'")}
        assert len(stamps) == 1, "バッチ内で created_at がばらついている"

    def test_is_audited(self, client):
        t = _admin_token(client)
        _two_shops()
        client.post("/api/admin/announcements", headers=_hdr(t),
                    json={"shop_ids": None, "audience": "managers", "title": "お知らせ", "body": "本文"})
        assert dbmod.query_one("SELECT id FROM audit_logs WHERE action='admin.announce'") is not None


class TestHistory:
    def test_history_groups_by_batch(self, client):
        t = _admin_token(client)
        _two_shops()
        client.post("/api/admin/announcements", headers=_hdr(t),
                    json={"shop_ids": None, "audience": "managers", "title": "1回目", "body": "本文"})
        r = client.get("/api/admin/notifications", headers=_hdr(t))
        assert r.status_code == 200
        items = r.get_json()["announcements"]
        assert len(items) == 1
        assert items[0]["title"] == "1回目"
        assert items[0]["shops"] == 2

    def test_repeated_same_title_batches_are_not_merged(self, client):
        """同一件名を連続配信しても、独立した2回の配信が履歴で2行になること。

        created_at は秒精度でバッチ内は1つの値に揃えているため、同一秒に
        同一件名で2回配信すると (created_at, title) だけでは1件に統合されて
        しまう（レビュー指摘）。batch_id で区別されることを確認する。
        """
        t = _admin_token(client)
        _two_shops()
        for _ in range(2):
            r = client.post("/api/admin/announcements", headers=_hdr(t),
                            json={"shop_ids": None, "audience": "managers",
                                  "title": "衝突テスト", "body": "本文"})
            assert r.status_code == 200
        r = client.get("/api/admin/notifications", headers=_hdr(t))
        items = r.get_json()["announcements"]
        assert len(items) == 2
        assert all(i["title"] == "衝突テスト" for i in items)

    def test_history_requires_admin(self, client):
        sid = insert_shop("SHOP1", "pw12345678")
        insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
        r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                            "password": "pw12345678"})
        t = r.get_json()["token"]
        assert client.get("/api/admin/notifications", headers=_hdr(t)).status_code == 403


# ============================================================
# Cloudflare D1 のバインドパラメータ上限（1クエリあたり100個）
# ============================================================
# 本番は DB_MODE=d1（REST API）。D1 は「1クエリあたりのバインドパラメータ100個」を
# 上限としており、超えると D1 がクエリ自体を拒否する。
# 一斉通知は1行あたり (?,?,?,?,?,0,?,?) = 7バインドのまとめ INSERT なので、
# チャンク分割しないと約14行（= 稼働2店舗・スタッフ17名の audience=all で 19行）で
# 破綻する。ローカル SQLite は 500,000 変数まで許すため、この壁は
# 「行数を増やしたテスト」では絶対に検出できない。バインド数そのものを数える。
D1_MAX_BINDS = 100


class _BindSpy:
    """admin_api が呼ぶ execute / query_all をラップし、1回あたりのバインド数を記録する。"""

    def __init__(self, monkeypatch):
        import admin_api
        self.counts = []
        self.insert_counts = []
        for name in ("execute", "query_all"):
            orig = getattr(admin_api, name)
            monkeypatch.setattr(admin_api, name, self._wrap(orig))

    def _wrap(self, orig):
        def spy(sql, params=()):
            n = len(params or ())
            self.counts.append(n)
            if "INSERT INTO notifications" in sql:
                self.insert_counts.append(n)
            return orig(sql, params)
        return spy


def _many_shops(shop_count, staff_per_shop):
    ids = []
    for i in range(shop_count):
        sid = insert_shop(f"S{i}", name=f"店{i}")
        for j in range(staff_per_shop):
            insert_staff(sid, f"p{i}_{j}", f"スタッフ{i}_{j}")
        ids.append(sid)
    return ids


class TestD1BindLimit:
    def test_announce_to_all_never_exceeds_d1_bind_limit(self, client, monkeypatch):
        """本番規模（稼働2店舗・スタッフ17名 = 19行 = 133バインド）を超える規模で、
        1回の execute / query_all あたりのバインド数が D1 の上限を超えないこと。"""
        t = _admin_token(client)
        _many_shops(6, 5)  # 店舗6 + スタッフ30 = 36行 = 252バインド
        spy = _BindSpy(monkeypatch)
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "all",
                              "title": "大規模配信", "body": "本文"})
        assert r.status_code == 200
        assert spy.insert_counts, "notifications への INSERT が観測できていない"
        assert max(spy.counts) <= D1_MAX_BINDS, (
            f"D1のバインド上限({D1_MAX_BINDS})を超えるクエリがある: "
            f"最大{max(spy.counts)}バインド")

    def test_announce_with_many_shop_ids_never_exceeds_d1_bind_limit(self, client, monkeypatch):
        """shop_ids の IN (...) 展開も同じ壁に当たる（店舗数が100を超えると破綻）。"""
        t = _admin_token(client)
        ids = _many_shops(120, 0)
        spy = _BindSpy(monkeypatch)
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": ids, "audience": "managers",
                              "title": "全店配信", "body": "本文"})
        assert r.status_code == 200
        assert r.get_json()["shops"] == 120
        assert max(spy.counts) <= D1_MAX_BINDS, (
            f"IN (...) の展開が上限を超えている: 最大{max(spy.counts)}バインド")

    def test_staff_in_clause_never_exceeds_d1_bind_limit(self, client, monkeypatch):
        """audience=all のスタッフ取得 `shop_id IN (...)` も店舗数ぶん展開される。"""
        t = _admin_token(client)
        _many_shops(120, 1)
        spy = _BindSpy(monkeypatch)
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "all",
                              "title": "全店配信", "body": "本文"})
        assert r.status_code == 200
        assert r.get_json()["recipients"] == 120
        assert max(spy.counts) <= D1_MAX_BINDS, (
            f"IN (...) の展開が上限を超えている: 最大{max(spy.counts)}バインド")

    def test_all_rows_are_still_inserted_after_chunking(self, client):
        """チャンク分割しても配信件数が欠けないこと（分割の実装ミス検出）。"""
        t = _admin_token(client)
        _many_shops(6, 5)
        r = client.post("/api/admin/announcements", headers=_hdr(t),
                        json={"shop_ids": None, "audience": "all",
                              "title": "大規模配信", "body": "本文"})
        assert r.status_code == 200
        rows = dbmod.query_all(
            "SELECT shop_id, staff_id, batch_id FROM notifications WHERE type='announcement'")
        assert len(rows) == 36, "店舗6件 + スタッフ30件 = 36行にならない"
        assert len({x["batch_id"] for x in rows}) == 1, "チャンクごとに batch_id が分かれている"
