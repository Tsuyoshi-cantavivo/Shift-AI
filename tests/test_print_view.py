"""tests/test_print_view.py — 印刷ビューの構造的な回帰を防ぐ静的検査。

実行: ./.venv/bin/python -m pytest tests/test_print_view.py -v

印刷は「押してブラウザのダイアログが出る」性質上ユニットテストしにくい。
ここでは白紙バグの再発につながる構造だけをソース検査で固定し、
実挙動は e2e/print_view.spec.js で見る。
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _read_css():
    return _read("public/style.css")


def _read_appjs():
    return _read("public/app.js")


_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _print_css():
    """style.css の @media print ブロックの中身をすべて連結して返す。

    コメント文中の "@media print"（style.css:998 など）をブロック開始と
    誤認しないよう、先にコメントを落とす。
    """
    css = _CSS_COMMENT_RE.sub("", _read_css())
    parts = []
    i = 0
    while True:
        j = css.find("@media print", i)
        if j < 0:
            break
        open_at = css.find("{", j)
        if open_at < 0:
            break
        depth, m = 0, open_at
        while m < len(css):
            if css[m] == "{":
                depth += 1
            elif css[m] == "}":
                depth -= 1
                if depth == 0:
                    break
            m += 1
        parts.append(css[open_at + 1:m])
        i = m + 1
    return "\n".join(parts)


class TestPrintCssStructure:
    """印刷ビューが「表示される」ための最低条件を固定する。"""

    def test_print_view_is_shown_in_print(self):
        assert re.search(r"\.print-view\s*\{[^}]*display:\s*block", _print_css())

    def test_app_view_is_hidden_in_print(self):
        """#appView を含むCSSルールが、実際に display:none を持つこと。

        以前は "#appView" という文字列が印刷CSSに存在するかしか見ておらず、
        `display: none !important` を `display: block` に変えても緑のままだった
        （#appView が言及されてさえいれば通ってしまうため）。
        """
        css = _print_css()
        m = re.search(r"([^{}]*#appView[^{}]*)\{([^}]*)\}", css)
        assert m, "#appView を含むCSSルールが印刷CSSに無い"
        assert re.search(r"display:\s*none", m.group(2)), \
            "#appView を含むルールに display:none が無い（画面アプリを隠せていない）"

    def test_page_rule_exists(self):
        assert "@page" in _print_css()


class TestPrintDomLifecycle:
    """白紙バグの再発を構造レベルで防ぐ。

    #printView は印刷ボタン押下時にしか組み立てられず afterprint で破棄されて
    いたため、プレビューの再描画・Ctrl+P・2回目の印刷がすべて白紙になっていた。
    """

    def test_print_view_is_not_cleared_after_print(self):
        """afterprint リスナの中で printView を破棄していないこと。

        リスナ自体を置かない実装も条件を満たす（そちらが本タスクの正解）。
        「printView を空文字にする記述が afterprint の中にある」場合だけ落とす。
        リスナが無いときに素通りするのを補うのが下の 2 テスト。
        """
        js = _read_appjs()
        offenders = []
        for m in re.finditer(r"addEventListener\(\s*['\"]afterprint['\"]", js):
            body = js[m.start():m.start() + 400]
            if re.search(r"printView[\s\S]{0,200}innerHTML\s*=\s*(''|\"\")", body):
                offenders.append(body.splitlines()[0])
        assert not offenders, \
            "afterprint で printView を破棄している（向き変更や2回目の印刷が白紙になる）: " \
            + "; ".join(offenders)

    def test_beforeprint_listener_exists(self):
        assert re.search(r"addEventListener\(\s*['\"]beforeprint['\"]", _read_appjs())

    def test_print_payload_is_retained(self):
        """appState.printPayload に印刷内容が保持されていること。

        単に "printPayload\\s*=" だけを見ると、clearPrintView() 内の
        `appState.printPayload = null;`（public/app.js の破棄処理）にも一致して
        しまい、openPrintView() 側の実際の保持コード
        （`appState.printPayload = { start, end, html: pagesHtml };`）を
        消しても緑のままだった。保持側の形（オブジェクト代入）に絞る。
        """
        assert re.search(r"appState\.printPayload\s*=\s*\{", _read_appjs()), \
            "印刷内容を appState.printPayload に保持していない"


class TestPrintTimelineNotClipped:
    """印刷時にタイムラインが切り捨てられないこと。

    画面用の .tl-wrap は overflow-x:auto、.tl-row は min-width:480px を持つ。
    印刷でこれが残ると、用紙幅や縮小率によって帯の右側が丸ごと消える。
    """

    def test_overflow_is_released_in_print(self):
        assert re.search(
            r"\.print-page\s+\.tl-wrap\s*\{[^}]*overflow-x:\s*visible", _print_css()
        ), "印刷で .tl-wrap の overflow-x を解除していない"

    def test_min_width_is_released_in_print(self):
        """.tl-row と .tl-axis-row の両方で min-width が解除されていること。

        以前は or 連結だったため、どちらか一方（.tl-axis-row 側）さえ残って
        いれば .tl-row 側の min-width:0 を消しても緑のままだった。シフト行
        本体である .tl-row は軸行と別セレクタなので、個別に検証する。
        """
        css = _print_css()
        assert re.search(r"\.print-page\s+\.tl-row[^{]*\{[^}]*min-width:\s*0", css), \
            "印刷で .tl-row の min-width を解除していない"
        assert re.search(r"\.print-page\s+\.tl-axis-row[^{]*\{[^}]*min-width:\s*0", css), \
            "印刷で .tl-axis-row の min-width を解除していない"


class TestPrintOrientation:
    """用紙の向き切替が実装されていること。"""

    def test_orientation_helpers_exist(self):
        js = _read_appjs()
        # 呼び出しだけ残って定義が消えた場合も落とすため、定義の形で見る
        for name in ("getPrintOrientation", "setPrintOrientation", "applyPrintOrientation"):
            assert re.search(r"function\s+" + name + r"\s*\(", js), f"{name} が定義されていない"

    def test_orientation_toggle_button_exists(self):
        # addEventListener 側だけ残ってボタンHTMLが消えた場合も落とすため、id 属性で見る
        assert 'id="printOrientBtn"' in _read_appjs()

    def test_orientation_applied_at_load(self):
        """スクリプト読み込み時にも向きを適用していること。

        シフト管理画面を一度も開かずに Ctrl+P したとき、@page が保存値ではなく
        style.css の既定（landscape）に落ちてしまうのを防ぐ。
        """
        assert re.search(r"^applyPrintOrientation\(\);", _read_appjs(), re.M), \
            "トップレベルで applyPrintOrientation() を呼んでいない"

    def test_portrait_layout_rules_exist(self):
        assert 'data-orientation="portrait"' in _print_css(), \
            "縦向き用のレイアウト調整が印刷CSSに無い"
