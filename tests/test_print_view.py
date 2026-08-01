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
        assert "#appView" in _print_css()

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
        assert re.search(r"printPayload\s*=", _read_appjs()), \
            "印刷内容を appState.printPayload に保持していない"
