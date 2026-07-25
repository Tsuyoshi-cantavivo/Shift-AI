"""tests/test_design_tokens.py — デザイントークンの定義とコントラスト比を検証する。

実行: ./.venv/bin/python -m pytest tests/test_design_tokens.py -v

設計書: docs/superpowers/specs/2026-07-25-design-refresh-design.md
色の値はすべて設計書からの引き写し。ここを変えるときは設計書も直すこと。
"""
import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).resolve().parents[1] / "public" / "style.css"

# --- 設計書 4章・5章の確定値 ---
LIGHT_EXPECTED = {
    "paper": "#FBFAF6", "surface": "#FFFFFF", "zebra": "#F5F3EC",
    "ink": "#243240", "ink-2": "#5A6472", "ink-3": "#8A8272",
    "rule": "#E4E1D6", "grid-1h": "#EEEBE1", "grid-4h": "#DCD6C4",
    "daybreak": "#B9AF98", "alert": "#B14A35",
    "role-manager": "#B9CBE2", "role-manager-ink": "#22364F",
    "role-employee": "#C9DFE0", "role-employee-ink": "#1D3D42",
    "role-part-time": "#F3DFA4", "role-part-time-ink": "#4A3A12",
    "role-student": "#F2DADD", "role-student-ink": "#4A2A2E",
}
DARK_EXPECTED = {
    "paper": "#262624", "surface": "#30302E", "zebra": "#2B2A27",
    "ink": "#F5F4EF", "ink-2": "#B7B4AC", "ink-3": "#8A867E",
    "rule": "#373633", "grid-1h": "#31302D", "grid-4h": "#3E3D3A",
    "daybreak": "#585754", "alert": "#D97757",
    "role-manager": "#3B4A5B", "role-manager-ink": "#C6D6E8",
    "role-employee": "#2F4A4D", "role-employee-ink": "#BFDCDE",
    "role-part-time": "#514526", "role-part-time-ink": "#EDD89E",
    "role-student": "#4E373B", "role-student-ink": "#F0D5D9",
}
# 現行の AI SaaS 配色。1つでも残っていたら差し替え漏れ。
REMOVED_TOKENS = [
    "navy", "card", "card-2", "card-3", "indigo", "indigo-l", "indigo-d",
    "ai-green", "ai-green-l", "t-primary", "t-secondary", "t-muted", "t-dim",
    "line", "line-2", "line-3", "sh-indigo", "grad-indigo", "grad-ai",
    "grad-card", "radius-xl",
]


def _read_css():
    return CSS_PATH.read_text(encoding="utf-8")


def _tokens_in_scope(selector):
    """指定セレクタのブロックから CSS 変数を辞書で取り出す。"""
    text = _read_css()
    pattern = re.escape(selector) + r"\s*\{(.*?)\n\}"
    m = re.search(pattern, text, re.S)
    assert m, f"セレクタが見つからない: {selector}"
    body = m.group(1)
    return {k: v.strip() for k, v in re.findall(r"--([\w-]+)\s*:\s*([^;]+);", body)}


def _luminance(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(fg, bg):
    l1, l2 = sorted([_luminance(fg), _luminance(bg)], reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


class TestTokensDefined:
    def test_light_tokens_are_in_root(self):
        """ライトが正。:root に全トークンが定義されていること。"""
        tokens = _tokens_in_scope(":root")
        for name, expected in LIGHT_EXPECTED.items():
            assert name in tokens, f"--{name} が :root に無い"
            assert tokens[name].upper() == expected.upper(), \
                f"--{name} は {expected} であるべき（実際は {tokens[name]}）"

    def test_dark_tokens_override_root(self):
        """ダークは派生。html[data-theme=\"dark\"] で上書きすること。"""
        tokens = _tokens_in_scope('html[data-theme="dark"]')
        for name, expected in DARK_EXPECTED.items():
            assert name in tokens, f"--{name} がダークに無い"
            assert tokens[name].upper() == expected.upper(), \
                f"--{name} は {expected} であるべき（実際は {tokens[name]}）"

    def test_fonts_defined(self):
        tokens = _tokens_in_scope(":root")
        assert "Zen Kaku Gothic New" in tokens.get("font-ui", "")
        assert "BIZ UDGothic" in tokens.get("font-num", "")


class TestOldTokensRemoved:
    @pytest.mark.parametrize("token", REMOVED_TOKENS)
    def test_old_token_is_gone(self, token):
        """旧AI SaaS配色のトークンが定義にも参照にも残っていないこと。"""
        text = _read_css()
        assert f"--{token}:" not in text, f"--{token} の定義が残っている"
        assert f"var(--{token})" not in text, f"var(--{token}) の参照が残っている"

    def test_no_hardcoded_indigo(self):
        """インディゴ系の生の16進数が残っていないこと。"""
        text = _read_css().upper()
        for dead in ["#6366F1", "#818CF8", "#4F46E5", "#10B981", "#0F172A", "#1F2937"]:
            assert dead not in text, f"{dead} が style.css に残っている"


class TestContrast:
    """設計書 5.4 節の実測値を機械で守る。すべて WCAG AA (4.5:1) 以上。"""

    LIGHT_PAIRS = [
        ("本文", "ink", "paper"), ("副次文字", "ink-2", "paper"),
        ("要対応", "alert", "paper"), ("ゼブラ上の本文", "ink", "zebra"),
        ("店長バー", "role-manager-ink", "role-manager"),
        ("社員バー", "role-employee-ink", "role-employee"),
        ("パートバー", "role-part-time-ink", "role-part-time"),
        ("学生バー", "role-student-ink", "role-student"),
    ]

    @pytest.mark.parametrize("label,fg,bg", LIGHT_PAIRS)
    def test_light_meets_aa(self, label, fg, bg):
        t = _tokens_in_scope(":root")
        ratio = _contrast(t[fg], t[bg])
        assert ratio >= 4.5, f"ライト {label}: {ratio:.2f}:1（4.5:1 未満）"

    @pytest.mark.parametrize("label,fg,bg", LIGHT_PAIRS)
    def test_dark_meets_aa(self, label, fg, bg):
        t = _tokens_in_scope('html[data-theme="dark"]')
        ratio = _contrast(t[fg], t[bg])
        assert ratio >= 4.5, f"ダーク {label}: {ratio:.2f}:1（4.5:1 未満）"

    def test_muted_text_meets_aa_large(self):
        """補助・目盛りは小さいが 3:1 は下回らないこと。"""
        for scope in (":root", 'html[data-theme="dark"]'):
            t = _tokens_in_scope(scope)
            assert _contrast(t["ink-3"], t["paper"]) >= 3.0
