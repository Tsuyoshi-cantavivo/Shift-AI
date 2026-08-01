"""tests/test_wish_image_resize.py — 希望表画像取り込みの送信前リサイズ、
サイズ計算部分（純関数）のテスト。

実行: ./.venv/bin/python -m pytest tests/test_wish_image_resize.py -v

public/app.js の reqImageResizeDims() を Node で直接実行して検証する
（helpers.run_js）。canvas/Image に依存する reqImageResize() 本体は
ブラウザでしか動かないため対象外（e2e/wish_image_import.spec.js 側で
実ブラウザ経由の疎通を見る）。DOM に触らない純関数として切り出して
あるので、Task5 brief の「サイズ計算部分だけでもよい」に対応する
（Phase 2 の座標計算純関数化（tests/test_required_bar_geometry.py）と
同じ方針）。
"""
import json

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


def _fn():
    return extract_js_function(_src(), "reqImageResizeDims")


def _dims(w, h, maxEdge):
    out = run_js([_fn()], f"JSON.stringify(reqImageResizeDims({w},{h},{maxEdge}))")
    return json.loads(out)


class TestReqImageResizeDims:
    def test_smaller_than_max_edge_is_unchanged(self):
        """長辺がmaxEdge以下ならリサイズしない（劣化させない）。"""
        d = _dims(800, 600, 1600)
        assert d == {"width": 800, "height": 600}

    def test_equal_to_max_edge_is_unchanged(self):
        d = _dims(1600, 900, 1600)
        assert d == {"width": 1600, "height": 900}

    def test_landscape_shrinks_to_long_edge(self):
        """横長画像は幅がmaxEdgeに合わせて縮む（アスペクト比維持）。"""
        d = _dims(3200, 1600, 1600)
        assert d["width"] == 1600
        assert d["height"] == 800

    def test_portrait_shrinks_to_long_edge(self):
        """縦長画像は高さがmaxEdgeに合わせて縮む（幅ではなく高さが長辺）。"""
        d = _dims(1200, 4800, 1600)
        assert d["height"] == 1600
        assert d["width"] == 400

    def test_aspect_ratio_is_preserved(self):
        d = _dims(3000, 2000, 1500)
        # 元のアスペクト比 3:2 が維持されること
        assert abs(d["width"] / d["height"] - 3000 / 2000) < 0.01

    def test_non_integer_result_is_rounded(self):
        """縮小後のpxは整数（canvas.width/heightは非整数を切り捨てるため、
        ここで丸めておかないと計算した比率と実際のcanvasサイズがズレる）。"""
        d = _dims(3333, 1000, 1600)
        assert isinstance(d["width"], int)
        assert isinstance(d["height"], int)

    def test_zero_or_negative_dims_return_zero(self):
        """壊れた画像（幅/高さ0）でcanvas描画がエラーにならないよう、
        呼び出し側が判定できる {0,0} を返す。"""
        assert _dims(0, 100, 1600) == {"width": 0, "height": 0}
        assert _dims(100, 0, 1600) == {"width": 0, "height": 0}
        assert _dims(-10, 100, 1600) == {"width": 0, "height": 0}

    def test_result_never_rounds_down_to_zero(self):
        """極端に細長い画像（例: 1px×10000px）でも、縮小後の短辺が
        Math.round で 0 に潰れないこと（0pxのcanvasはエラーになる）。"""
        d = _dims(1, 10000, 1600)
        assert d["width"] >= 1
        assert d["height"] == 1600
