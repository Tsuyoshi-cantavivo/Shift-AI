"""tests/test_patterns_bulk.py — 必要人数の一括保存API。

実行: ./.venv/bin/python -m pytest tests/test_patterns_bulk.py -v

従来はパターンごとに PUT を2発（本体 + 曜日別）直列で投げており、
しかもフロントが画面の文字列 "09:00 - 22:00" を再パースして送っていた。
表示を変えると保存が壊れる構造だったため、state から一括で送る形に変える。
"""
import pytest

from helpers import insert_shop, insert_pattern, make_session, auth


def _token(shop_id):
    return make_session("shop", shop_id, shop_id)


class TestBulkSave:
    def test_saves_base_and_weekday(self, client):
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": "夜番", "start_time": "18:00", "end_time": "23:00",
            "required_staff": 3, "weekday_required": {"0": 4, "6": 5},
        }]}, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        p = d["patterns"][0]
        assert p["pattern_name"] == "夜番"
        assert p["start_time"] == "18:00"
        assert p["end_time"] == "23:00"
        assert p["required_staff"] == 3
        assert p["weekday_required"] == {"0": 4, "6": 5}

    def test_weekday_map_is_replaced_not_merged(self, client):
        """曜日別は置換方式（送らなかった曜日は削除される）。"""
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)
        base = {"id": pid, "pattern_name": "夜", "start_time": "17:00",
                "end_time": "22:00", "required_staff": 2}

        client.put("/api/shop/patterns/bulk",
                   json={"patterns": [dict(base, weekday_required={"0": 4, "6": 5})]},
                   headers=auth(tok))
        client.put("/api/shop/patterns/bulk",
                   json={"patterns": [dict(base, weekday_required={"6": 5})]},
                   headers=auth(tok))

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        assert d["patterns"][0]["weekday_required"] == {"6": 5}

    def test_zero_is_saved(self, client):
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "昼", "09:00", "17:00", 2)
        tok = _token(shop_id)

        client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": "昼", "start_time": "09:00", "end_time": "17:00",
            "required_staff": 0, "weekday_required": {"0": 0},
        }]}, headers=auth(tok))

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        assert d["patterns"][0]["required_staff"] == 0
        assert d["patterns"][0]["weekday_required"] == {"0": 0}


class TestBulkValidation:
    def test_invalid_hours_rolls_back_everything(self, client):
        """1件でも検証に失敗したら全体をロールバックする。"""
        shop_id = insert_shop()
        p1 = insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        p2 = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [
            {"id": p1, "pattern_name": "朝", "start_time": "09:00", "end_time": "14:00",
             "required_staff": 9, "weekday_required": {}},
            # 16時間 → _validate_pattern_hours が 400 で弾く
            {"id": p2, "pattern_name": "長すぎ", "start_time": "06:00", "end_time": "22:00",
             "required_staff": 2, "weekday_required": {}},
        ]}, headers=auth(tok))
        assert r.status_code == 400

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        pats = {p["pattern_name"]: p for p in d["patterns"]}
        assert "朝" in pats
        assert pats["朝"]["end_time"] == "13:00", "1件目が保存されたまま残っている（ロールバックされていない）"
        assert pats["朝"]["required_staff"] == 2

    def test_failure_reports_which_pattern(self, client):
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": "長すぎ", "start_time": "06:00", "end_time": "22:00",
            "required_staff": 2, "weekday_required": {},
        }]}, headers=auth(tok))
        assert r.status_code == 400
        body = r.get_json()
        assert "長すぎ" in str(body), f"どのパターンが原因か分からない: {body}"

    def test_warning_is_returned_per_pattern(self, client):
        """9h/13h 超の警告がパターンごとに返ること（拒否はしない）。"""
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "通し", "09:00", "17:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": "通し", "start_time": "09:00", "end_time": "21:00",
            "required_staff": 2, "weekday_required": {},
        }]}, headers=auth(tok))
        assert r.status_code == 200
        warnings = r.get_json().get("warnings") or []
        assert warnings, "12時間のパターンに警告が返っていない"
        assert warnings[0]["pattern_name"] == "通し"

    def test_other_shop_pattern_is_rejected(self, client):
        """他店舗のパターンIDを混ぜても更新されないこと（IDOR対策）。"""
        shop_a = insert_shop(code="SHOPA")
        shop_b = insert_shop(code="SHOPB", name="別店舗")
        pid_b = insert_pattern(shop_b, "他店", "10:00", "15:00", 9)
        tok_a = _token(shop_a)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid_b, "pattern_name": "乗っ取り", "start_time": "10:00", "end_time": "15:00",
            "required_staff": 1, "weekday_required": {},
        }]}, headers=auth(tok_a))
        assert r.status_code in (400, 404)

        tok_b = _token(shop_b)
        d = client.get("/api/shop/patterns", headers=auth(tok_b)).get_json()
        assert d["patterns"][0]["pattern_name"] == "他店", "他店舗のパターンを書き換えられた"
        assert d["patterns"][0]["required_staff"] == 9


class TestBulkPatternNameValidation:
    """レビュー Minor M1: pattern_name が検証を素通りして 500 + 部分書き込みになる。

    name = it.get("pattern_name") を取るだけで検証していなかったため、
    UPDATE の NOT NULL 制約違反で書き込みフェーズの途中で 500 になり、
    「1件でも失敗したら何も書かない」(docstring) に反して、先に処理された
    正常なパターンだけが書き込まれた状態が残っていた（実測確認済み）。
    """

    def test_empty_name_does_not_leave_partial_write(self, client):
        """正常なパターンと不正なパターン（空の名前）を混ぜても、
        正常な方だけが先に書き込まれてしまわないこと。"""
        shop_id = insert_shop()
        p1 = insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        p2 = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [
            {"id": p1, "pattern_name": "朝番", "start_time": "09:00", "end_time": "14:00",
             "required_staff": 3, "weekday_required": {}},
            # pattern_name が空文字列。以前は検証を素通りし、NOT NULL制約違反で
            # 書き込みフェーズの途中に 500 になっていた（p1 は先に書き込まれた後）。
            {"id": p2, "pattern_name": "", "start_time": "17:00", "end_time": "22:00",
             "required_staff": 2, "weekday_required": {}},
        ]}, headers=auth(tok))
        assert r.status_code == 400

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        pats = {p["id"]: p for p in d["patterns"]}
        assert pats[p1]["pattern_name"] == "朝", "検証失敗時に他パターンが書き込まれてしまった（部分書き込み）"
        assert pats[p1]["end_time"] == "13:00"
        assert pats[p1]["required_staff"] == 2

    def test_non_string_name_is_rejected(self, client):
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": None, "start_time": "17:00", "end_time": "22:00",
            "required_staff": 2, "weekday_required": {},
        }]}, headers=auth(tok))
        assert r.status_code == 400
