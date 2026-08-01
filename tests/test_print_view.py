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
