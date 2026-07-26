"""管理者による店舗更新（部分更新）のテスト。

背景: 有効/無効トグルが shop_name:'' を送り、店舗名が空文字で潰れる事故があった。
"""
import db as dbmod
from helpers import insert_admin, insert_shop


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    assert r.status_code == 200
    return r.get_json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def test_toggle_active_preserves_shop_name(client):
    """is_active だけを送ったとき、店舗名が変わらないこと。"""
    token = _admin_token(client)
    sid = insert_shop("SHOP1", name="レイクタウン店")

    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token),
                   json={"is_active": False})
    assert r.status_code == 200

    row = dbmod.query_one("SELECT shop_name, is_active FROM shops WHERE id=?", (sid,))
    assert row["shop_name"] == "レイクタウン店", "店舗名が消えている"
    assert row["is_active"] == 0


def test_empty_shop_name_is_rejected(client):
    """空の店舗名は 400 で拒否されること。"""
    token = _admin_token(client)
    sid = insert_shop("SHOP1", name="レイクタウン店")

    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token),
                   json={"shop_name": "   "})
    assert r.status_code == 400
    row = dbmod.query_one("SELECT shop_name FROM shops WHERE id=?", (sid,))
    assert row["shop_name"] == "レイクタウン店"


def test_rename_shop(client):
    """店舗名を変更できること。is_active は変わらないこと。"""
    token = _admin_token(client)
    sid = insert_shop("SHOP1", name="旧名")

    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token),
                   json={"shop_name": "新名"})
    assert r.status_code == 200
    row = dbmod.query_one("SELECT shop_name, is_active FROM shops WHERE id=?", (sid,))
    assert row["shop_name"] == "新名"
    assert row["is_active"] == 1


def test_change_shop_code(client):
    """店舗コードを変更できること。"""
    token = _admin_token(client)
    sid = insert_shop("OLD1", name="店")

    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token),
                   json={"shop_code": "NEW1"})
    assert r.status_code == 200
    row = dbmod.query_one("SELECT shop_code FROM shops WHERE id=?", (sid,))
    assert row["shop_code"] == "NEW1"


def test_duplicate_shop_code_is_rejected(client):
    """既に使われている店舗コードへの変更は 400 で拒否されること。"""
    token = _admin_token(client)
    sid1 = insert_shop("SHOP1", name="店1")
    insert_shop("SHOP2", name="店2")

    r = client.put(f"/api/admin/shops/{sid1}", headers=_hdr(token),
                   json={"shop_code": "SHOP2"})
    assert r.status_code == 400
    row = dbmod.query_one("SELECT shop_code FROM shops WHERE id=?", (sid1,))
    assert row["shop_code"] == "SHOP1"


def test_unknown_shop_returns_404(client):
    token = _admin_token(client)
    r = client.put("/api/admin/shops/99999", headers=_hdr(token),
                   json={"is_active": True})
    assert r.status_code == 404


def test_requires_admin_role(client):
    """shop ロールでは呼べないこと。"""
    from helpers import insert_staff
    sid = insert_shop("SHOP1", "pw12345678", name="店")
    insert_staff(sid, "mgr", "店長", role="manager", password="pw12345678")
    r = client.post("/api/login", json={"shop_code": "SHOP1", "user_code": "mgr",
                                        "password": "pw12345678"})
    token = r.get_json()["token"]
    r = client.put(f"/api/admin/shops/{sid}", headers=_hdr(token), json={"is_active": False})
    assert r.status_code == 403
