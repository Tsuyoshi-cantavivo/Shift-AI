"""tests/test_required_bar_geometry.py — 必要人数バーの座標計算（純関数）。

実行: ./.venv/bin/python -m pytest tests/test_required_bar_geometry.py -v

public/app.js の関数を Node で直接実行して検証する（helpers.run_js）。
DOM に触らない純関数として切り出しているので、描画を変えてもここは壊れない。

reqBarRange/reqBarPosition は既存の _parseTimeParts に、
reqBarHeightPx/reqBarCountFromPx はモジュールスコープの定数
（REQ_BAR_UNIT_PX/REQ_BAR_MIN_PX）に依存する。helpers.extract_js_function は
関数本体しか抜き出さないため、これらは _fns() とは別に明示的に読み込む
（DRY を優先し、実装側に複製を持たせない代わりに、テスト側で依存関係を
 自分で組み立てる）。
"""
import json
import re

import pytest

from helpers import extract_js_function, run_js

SRC = None


def _src():
    global SRC
    if SRC is None:
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "public/app.js"), encoding="utf-8") as f:
            SRC = f.read()
    return SRC


def _fns(*names):
    return [extract_js_function(_src(), n) for n in names]


def _parse_time_fn():
    """reqBarRange/reqBarPosition が使う _parseTimeParts を Node に渡す。"""
    return extract_js_function(_src(), "_parseTimeParts")


def _consts():
    """定数を実ソースから読み出して Node に渡す。
    テスト側に 14 / 6 を直書きすると、実装が変わってもテストが気づかない。"""
    src = _src()
    out = []
    for name in ("REQ_BAR_UNIT_PX", "REQ_BAR_MIN_PX"):
        m = re.search(name + r"\s*=\s*(\d+)", src)
        assert m, f"{name} が public/app.js に見つからない"
        out.append(f"const {name} = {m.group(1)};")
    return "\n".join(out)


class TestReqBarRange:
    def test_range_covers_all_patterns(self):
        pats = [{"start_time": "09:00", "end_time": "17:00"},
                {"start_time": "17:00", "end_time": "22:00"}]
        out = run_js(_fns("reqBarRange") + [_parse_time_fn()],
                     f"JSON.stringify(reqBarRange({json.dumps(pats)}))")
        r = json.loads(out)
        assert r["minH"] == 9
        assert r["maxH"] == 22
        assert r["rangeMin"] == 540
        assert r["rangeLen"] == 780

    def test_overnight_extends_past_24(self):
        pats = [{"start_time": "22:00", "end_time": "02:00"}]
        out = run_js(_fns("reqBarRange") + [_parse_time_fn()],
                     f"JSON.stringify(reqBarRange({json.dumps(pats)}))")
        r = json.loads(out)
        assert r["maxH"] == 26, "日またぎが翌日側に伸びていない"

    def test_empty_patterns_fall_back(self):
        out = run_js(_fns("reqBarRange") + [_parse_time_fn()],
                     "JSON.stringify(reqBarRange([]))")
        r = json.loads(out)
        assert r["rangeLen"] > 0, "パターン0件で幅0になると全バーが消える"


class TestReqBarPosition:
    def test_full_range_is_zero_to_hundred(self):
        rng = {"minH": 9, "maxH": 22, "rangeMin": 540, "rangeLen": 780}
        out = run_js(_fns("reqBarPosition") + [_parse_time_fn()],
                     f"JSON.stringify(reqBarPosition('09:00','22:00',{json.dumps(rng)}))")
        p = json.loads(out)
        assert abs(p["left"]) < 0.01
        assert abs(p["width"] - 100) < 0.01

    def test_half_range(self):
        rng = {"minH": 0, "maxH": 24, "rangeMin": 0, "rangeLen": 1440}
        out = run_js(_fns("reqBarPosition") + [_parse_time_fn()],
                     f"JSON.stringify(reqBarPosition('06:00','18:00',{json.dumps(rng)}))")
        p = json.loads(out)
        assert abs(p["left"] - 25) < 0.01
        assert abs(p["width"] - 50) < 0.01

    def test_overnight_wraps_to_extended_slot(self):
        rng = {"minH": 9, "maxH": 26, "rangeMin": 540, "rangeLen": 1020}
        out = run_js(_fns("reqBarPosition") + [_parse_time_fn()],
                     f"JSON.stringify(reqBarPosition('22:00','02:00',{json.dumps(rng)}))")
        p = json.loads(out)
        # 22:00 = 1320分 → (1320-540)/1020 = 76.47%
        assert abs(p["left"] - 76.47) < 0.1
        # 翌02:00 = 1560分 → 幅 240/1020 = 23.53%
        assert abs(p["width"] - 23.53) < 0.1


class TestReqBarInvalidTime:
    """parseTime のロジックを reqBarRange/reqBarPosition が複製していた頃、
    不正な時刻を渡すケースが1件も無かったため、複製がズレても
    13件全PASSのままという穴があった（レビュー指摘）。
    複製をやめて既存の _parseTimeParts を直接使う形に直したうえで、
    不正入力時の挙動そのものをここで固定する。"""

    def test_range_ignores_invalid_patterns(self):
        pats = [{"start_time": "invalid", "end_time": "17:00"},
                {"start_time": "09:00", "end_time": "22:00"}]
        out = run_js(_fns("reqBarRange") + [_parse_time_fn()],
                     f"JSON.stringify(reqBarRange({json.dumps(pats)}))")
        r = json.loads(out)
        # 不正なパターンは集計から除外され、正常なほうだけで軸が決まる
        assert r["minH"] == 9
        assert r["maxH"] == 22

    def test_position_returns_zero_width_for_invalid_time(self):
        rng = {"minH": 9, "maxH": 22, "rangeMin": 540, "rangeLen": 780}
        out = run_js(_fns("reqBarPosition") + [_parse_time_fn()],
                     f"JSON.stringify(reqBarPosition('invalid','22:00',{json.dumps(rng)}))")
        p = json.loads(out)
        assert p["width"] == 0, "不正な時刻でバーを描いてしまう"


class TestReqBarHeight:
    def test_height_is_proportional_to_count(self):
        out = run_js([_consts()] + _fns("reqBarHeightPx"),
                     "JSON.stringify([1,2,3].map(reqBarHeightPx))")
        h = json.loads(out)
        assert h[1] - h[0] == h[2] - h[1], "1人あたりの高さが一定でない"
        assert h[0] > 0

    def test_zero_still_has_visible_height(self):
        out = run_js([_consts()] + _fns("reqBarHeightPx"), "String(reqBarHeightPx(0))")
        assert float(out.strip()) > 0, "0人のバーが高さ0だと画面から消えて操作できない"

    def test_round_trip(self):
        """高さ → 人数 → 高さ で元に戻ること。"""
        out = run_js([_consts()] + _fns("reqBarHeightPx", "reqBarCountFromPx"),
                     "JSON.stringify([0,1,2,5,10].map((n) => reqBarCountFromPx(reqBarHeightPx(n))))")
        assert json.loads(out) == [0, 1, 2, 5, 10]

    def test_negative_px_clamps_to_zero(self):
        out = run_js([_consts()] + _fns("reqBarCountFromPx"), "String(reqBarCountFromPx(-50))")
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


class TestReqBarLanes:
    """重なる時間帯を段（レーン）に振り分ける reqBarLanes（レビュー指摘 I5）。

    不透明な .rq-bar を同一トラックに絶対配置で重ねると、後勝ちのバーが
    前のバーを隠して読めなくなり、Task5以降のドラッグでも掴めなくなる。
    重ならないバー同士は段を共有して縦を節約する（区間スケジューリングの貪欲法）。
    """

    def _lanes(self, pats):
        out = run_js(_fns("reqBarLanes") + [_parse_time_fn()],
                     f"JSON.stringify(reqBarLanes({json.dumps(pats)}))")
        return json.loads(out)

    def test_non_overlapping_share_lane_zero(self):
        pats = [{"id": 1, "start_time": "09:00", "end_time": "12:00"},
                {"id": 2, "start_time": "12:00", "end_time": "17:00"}]
        r = self._lanes(pats)
        assert r["lane"]["1"] == 0
        assert r["lane"]["2"] == 0
        assert r["laneCount"] == 1

    def test_overlapping_get_different_lanes(self):
        pats = [{"id": 1, "start_time": "09:00", "end_time": "17:00"},
                {"id": 2, "start_time": "12:00", "end_time": "20:00"}]
        r = self._lanes(pats)
        assert r["lane"]["1"] != r["lane"]["2"], "重なる時間帯が同じ段に置かれている"
        assert r["laneCount"] == 2

    def test_three_way_overlap_needs_three_lanes(self):
        """3件が同時刻に重なるときは3段になる。"""
        pats = [{"id": 1, "start_time": "09:00", "end_time": "15:00"},
                {"id": 2, "start_time": "10:00", "end_time": "16:00"},
                {"id": 3, "start_time": "11:00", "end_time": "17:00"}]
        r = self._lanes(pats)
        assert r["laneCount"] == 3
        assert len({r["lane"]["1"], r["lane"]["2"], r["lane"]["3"]}) == 3

    def test_overnight_does_not_conflict_with_unrelated_daytime(self):
        """日またぎ（22:00-02:00）が翌日側（+24h）に伸びて扱われ、
        時間的に無関係な日中の時間帯と誤って重なり扱いにならないこと。"""
        pats = [{"id": 1, "start_time": "22:00", "end_time": "02:00"},
                {"id": 2, "start_time": "09:00", "end_time": "17:00"}]
        r = self._lanes(pats)
        assert r["lane"]["1"] == 0
        assert r["lane"]["2"] == 0
        assert r["laneCount"] == 1

    def test_invalid_time_pattern_is_excluded(self):
        pats = [{"id": 1, "start_time": "invalid", "end_time": "17:00"},
                {"id": 2, "start_time": "09:00", "end_time": "17:00"}]
        r = self._lanes(pats)
        assert "1" not in r["lane"], "不正な時刻のパターンが段に含まれている"
        assert r["lane"]["2"] == 0
        assert r["laneCount"] == 1
