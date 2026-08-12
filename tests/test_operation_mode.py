"""運用モード（shops.settings.operation_mode）のテスト。

店長だけがアプリを使う店舗では、スタッフがログインする前提の画面
（マイシフト・希望、通知、変更申請、募集期間）が意味を持たない。
フロントはこの値でナビと画面を出し分けるので、

- 未設定の店舗が既定の "staff" に倒れること
- 2値以外を保存できないこと（不正値がフロントに届いて分岐が壊れるのを防ぐ）
- /api/me が返すこと（ナビ描画がこの1本だけを待てばよい）

の3点を固定する。サーバ側の権限や通知の挙動はモードで変えない
（/api/shop/shifts/finalize は manager_only でも通知行を作り続ける）ので、
ここではその不変も併せて確認する。
"""
import pytest

from utils import operation_mode_of, validate_known_settings_values
from helpers import insert_shop, insert_staff, make_session, auth


# ---------- utils.operation_mode_of ----------

@pytest.mark.parametrize("settings,expected", [
    ({}, "staff"),
    ({"operation_mode": "staff"}, "staff"),
    ({"operation_mode": "manager_only"}, "manager_only"),
    # 検証が入る前に保存された古い行や、admin コンソールから未知キー扱いで
    # 素通りした値でも、読み出し側で既定に丸める（画面が壊れるより既定で出す）
    ({"operation_mode": "bogus"}, "staff"),
    ({"operation_mode": None}, "staff"),
    ({"operation_mode": 1}, "staff"),
    (None, "staff"),
    ("not-a-dict", "staff"),
])
def test_operation_mode_of_falls_back_to_staff(settings, expected):
    assert operation_mode_of(settings) == expected


# ---------- 保存時の検証 ----------

def test_validate_accepts_both_modes():
    for mode in ("staff", "manager_only"):
        assert validate_known_settings_values({"operation_mode": mode})


@pytest.mark.parametrize("bad", ["", "STAFF", "manager", "manager_only ", 1, True, None, [], {}])
def test_validate_rejects_unknown_mode(bad):
    with pytest.raises(ValueError, match="operation_mode"):
        validate_known_settings_values({"operation_mode": bad})


@pytest.mark.parametrize("bad", [[], {}])
def test_validate_rejects_unhashable_period_mode(bad):
    """list/dict を `in frozenset` に渡すと unhashable で TypeError になり、
    @errorhandler(ValueError) に拾われず 400 のはずが 500 になっていた。
    operation_mode を足すついでに period_mode 側も塞いだので、退行を止める。"""
    with pytest.raises(ValueError, match="period_mode"):
        validate_known_settings_values({"period_mode": bad})


def test_put_shop_settings_rejects_unknown_mode(client):
    shop_id = insert_shop()
    token = make_session("shop", shop_id, shop_id)
    r = client.put("/api/shop/settings", json={"settings": {"operation_mode": "manager"}},
                   headers=auth(token))
    assert r.status_code == 400
    assert "operation_mode" in r.get_json()["error"]


def test_put_shop_settings_saves_manager_only(client):
    shop_id = insert_shop()
    token = make_session("shop", shop_id, shop_id)
    r = client.put("/api/shop/settings", json={"settings": {"operation_mode": "manager_only"}},
                   headers=auth(token))
    assert r.status_code == 200
    r2 = client.get("/api/shop/settings", headers=auth(token))
    assert r2.get_json()["settings"]["operation_mode"] == "manager_only"


# ---------- /api/me ----------

def test_me_returns_default_mode_when_unset(client):
    shop_id = insert_shop()
    token = make_session("shop", shop_id, shop_id)
    r = client.get("/api/me", headers=auth(token))
    assert r.status_code == 200
    assert r.get_json()["operation_mode"] == "staff"


def test_me_returns_saved_mode(client):
    shop_id = insert_shop(settings={"operation_mode": "manager_only"})
    token = make_session("shop", shop_id, shop_id)
    r = client.get("/api/me", headers=auth(token))
    assert r.get_json()["operation_mode"] == "manager_only"


def test_me_for_staff_role_has_no_operation_mode(client):
    """スタッフ画面にはモードによる出し分けが無いので返さない。"""
    shop_id = insert_shop(settings={"operation_mode": "manager_only"})
    staff_id = insert_staff(shop_id, "P001", "山田")
    token = make_session("staff", staff_id, shop_id)
    r = client.get("/api/me", headers=auth(token))
    assert r.status_code == 200
    assert "operation_mode" not in r.get_json()
