"""tests/test_staff_attention_api.py — 気にかけたい人の API。

実行: ./.venv/bin/python -m pytest tests/test_staff_attention_api.py -v
"""
from datetime import timedelta

import pytest

from db import execute
from helpers import insert_shop, insert_staff, make_session, auth
from utils import jst_today


def _day(days_ago):
    return (jst_today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _add_shift(shop_id, staff_id, days_ago):
    d = _day(days_ago)
    execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
        "VALUES (?,?,?,?,'confirmed')",
        (shop_id, staff_id, f"{d}T09:00:00", f"{d}T17:00:00"))


class TestStaffAttentionApi:
    def _tok(self, shop_id):
        return make_session("shop", shop_id, shop_id)

    def test_requires_shop_role(self, client):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        tok = make_session("staff", sid, shop_id)
        r = client.post("/api/shop/staff-attention", headers=auth(tok))
        assert r.status_code in (401, 403)

    def test_returns_empty_when_nothing_changed(self, client):
        """安定して出ている人は挙がらない（平常時は何も出さない）。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        # 直近も基準期間も同じくらい出ている
        for x in list(range(1, 30, 3)) + list(range(31, 89, 3)):
            _add_shift(shop_id, sid, x)
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_id)))
        assert r.status_code == 200
        assert r.get_json()["items"] == []

    def test_detects_attendance_drop(self, client):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        for x in range(31, 71):        # 基準期間に多く出勤
            _add_shift(shop_id, sid, x)
        _add_shift(shop_id, sid, 3)    # 直近は1日だけ
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_id)))
        d = r.get_json()
        assert len(d["items"]) == 1
        item = d["items"][0]
        assert item["staff_id"] == sid
        assert item["name"] == "田中太郎"
        assert item["reasons"][0]["type"] == "attendance_drop"
        # 店長が判断できる材料（事実）と声かけの例が入っていること
        assert item["headline"]
        assert item["detail"]
        assert item["message"]

    def test_falls_back_without_llm(self, client):
        """conftest が LLM_API_KEY="" にするため、既定でフォールバック経路。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        for x in range(31, 71):
            _add_shift(shop_id, sid, x)
        _add_shift(shop_id, sid, 3)
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_id)))
        assert r.get_json()["source"] == "rule_based"

    def test_does_not_include_other_shops(self, client):
        """他店舗のスタッフが混ざらないこと。"""
        shop_a = insert_shop(code="SHOPA")
        shop_b = insert_shop(code="SHOPB", name="別店舗")
        sid_b = insert_staff(shop_b, "P9", "他店の人")
        for x in range(31, 71):
            _add_shift(shop_b, sid_b, x)
        _add_shift(shop_b, sid_b, 3)
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_a)))
        assert r.get_json()["items"] == []

    def test_no_diagnosis_words_in_response(self, client):
        """レスポンスのどこにも断定的な語が出ないこと。"""
        import json
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        for x in range(31, 71):
            _add_shift(shop_id, sid, x)
        _add_shift(shop_id, sid, 3)
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_id)))
        body = json.dumps(r.get_json(), ensure_ascii=False)
        for w in ("離職", "退職", "メンタル", "やる気"):
            assert w not in body, f"レスポンスに断定的な語が含まれる: {w}"
