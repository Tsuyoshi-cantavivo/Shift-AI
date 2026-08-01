"""tests/test_required_bar_geometry.py — 必要人数バーの座標計算（純関数）。

実行: ./.venv/bin/python -m pytest tests/test_required_bar_geometry.py -v

public/app.js の関数を Node で直接実行して検証する（helpers.run_js）。
DOM に触らない純関数として切り出しているので、描画を変えてもここは壊れない。
"""
import json

import pytest

from helpers import extract_js_function, run_js

SRC = None


def _fns(*names):
    global SRC
    if SRC is None:
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "public/app.js"), encoding="utf-8") as f:
            SRC = f.read()
    return [extract_js_function(SRC, n) for n in names]


class TestReqBarRange:
    def test_range_covers_all_patterns(self):
        pats = [{"start_time": "09:00", "end_time": "17:00"},
                {"start_time": "17:00", "end_time": "22:00"}]
        out = run_js(_fns("reqBarRange"),
                     f"JSON.stringify(reqBarRange({json.dumps(pats)}))")
        r = json.loads(out)
        assert r["minH"] == 9
        assert r["maxH"] == 22
        assert r["rangeMin"] == 540
        assert r["rangeLen"] == 780

    def test_overnight_extends_past_24(self):
        pats = [{"start_time": "22:00", "end_time": "02:00"}]
        out = run_js(_fns("reqBarRange"),
                     f"JSON.stringify(reqBarRange({json.dumps(pats)}))")
        r = json.loads(out)
        assert r["maxH"] == 26, "日またぎが翌日側に伸びていない"

    def test_empty_patterns_fall_back(self):
        out = run_js(_fns("reqBarRange"), "JSON.stringify(reqBarRange([]))")
        r = json.loads(out)
        assert r["rangeLen"] > 0, "パターン0件で幅0になると全バーが消える"


class TestReqBarPosition:
    def test_full_range_is_zero_to_hundred(self):
        rng = {"minH": 9, "maxH": 22, "rangeMin": 540, "rangeLen": 780}
        out = run_js(_fns("reqBarPosition"),
                     f"JSON.stringify(reqBarPosition('09:00','22:00',{json.dumps(rng)}))")
        p = json.loads(out)
        assert abs(p["left"]) < 0.01
        assert abs(p["width"] - 100) < 0.01

    def test_half_range(self):
        rng = {"minH": 0, "maxH": 24, "rangeMin": 0, "rangeLen": 1440}
        out = run_js(_fns("reqBarPosition"),
                     f"JSON.stringify(reqBarPosition('06:00','18:00',{json.dumps(rng)}))")
        p = json.loads(out)
        assert abs(p["left"] - 25) < 0.01
        assert abs(p["width"] - 50) < 0.01

    def test_overnight_wraps_to_extended_slot(self):
        rng = {"minH": 9, "maxH": 26, "rangeMin": 540, "rangeLen": 1020}
        out = run_js(_fns("reqBarPosition"),
                     f"JSON.stringify(reqBarPosition('22:00','02:00',{json.dumps(rng)}))")
        p = json.loads(out)
        # 22:00 = 1320分 → (1320-540)/1020 = 76.47%
        assert abs(p["left"] - 76.47) < 0.1
        # 翌02:00 = 1560分 → 幅 240/1020 = 23.53%
        assert abs(p["width"] - 23.53) < 0.1


class TestReqBarHeight:
    def test_height_is_proportional_to_count(self):
        out = run_js(_fns("reqBarHeightPx"),
                     "JSON.stringify([1,2,3].map(reqBarHeightPx))")
        h = json.loads(out)
        assert h[1] - h[0] == h[2] - h[1], "1人あたりの高さが一定でない"
        assert h[0] > 0

    def test_zero_still_has_visible_height(self):
        out = run_js(_fns("reqBarHeightPx"), "String(reqBarHeightPx(0))")
        assert float(out.strip()) > 0, "0人のバーが高さ0だと画面から消えて操作できない"

    def test_round_trip(self):
        """高さ → 人数 → 高さ で元に戻ること。"""
        out = run_js(_fns("reqBarHeightPx", "reqBarCountFromPx"),
                     "JSON.stringify([0,1,2,5,10].map((n) => reqBarCountFromPx(reqBarHeightPx(n))))")
        assert json.loads(out) == [0, 1, 2, 5, 10]

    def test_negative_px_clamps_to_zero(self):
        out = run_js(_fns("reqBarCountFromPx"), "String(reqBarCountFromPx(-50))")
        assert int(out.strip()) == 0, "マイナスにドラッグしたときに負の人数になる"


class TestReqBarEffective:
    def test_override_wins(self):
        pat = {"required_staff": 2, "weekday_required": {"6": 5}}
        out = run_js(_fns("reqBarEffective"),
                     f"JSON.stringify(reqBarEffective({json.dumps(pat)}, 6))")
        e = json.loads(out)
        assert e["count"] == 5
        assert e["isOverride"] is True

    def test_falls_back_to_base(self):
        pat = {"required_staff": 2, "weekday_required": {"6": 5}}
        out = run_js(_fns("reqBarEffective"),
                     f"JSON.stringify(reqBarEffective({json.dumps(pat)}, 1))")
        e = json.loads(out)
        assert e["count"] == 2
        assert e["isOverride"] is False

    def test_override_zero_is_not_treated_as_absent(self):
        """0 の上書きが「未設定」と誤判定されないこと（0 は falsy）。"""
        pat = {"required_staff": 3, "weekday_required": {"0": 0}}
        out = run_js(_fns("reqBarEffective"),
                     f"JSON.stringify(reqBarEffective({json.dumps(pat)}, 0))")
        e = json.loads(out)
        assert e["count"] == 0, "0 の上書きが基本値にフォールバックしている"
        assert e["isOverride"] is True
