"""tests/test_design_tokens.py — デザイントークンの定義とコントラスト比を検証する。

実行: ./.venv/bin/python -m pytest tests/test_design_tokens.py -v

設計書: docs/superpowers/specs/2026-07-25-design-refresh-design.md
色の値はすべて設計書からの引き写し。ここを変えるときは設計書も直すこと。
"""
import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).resolve().parents[1] / "public" / "style.css"
# app.js もインラインスタイルでトークンを参照する。CSS しか見ないと
# `style="color:var(--indigo-l)"` のような削除済みトークンの参照を取り逃がす。
JS_PATH = Path(__file__).resolve().parents[1] / "public" / "app.js"

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
    # 意味色。設計書 4.1 / 5.1 に追記した確定値
    "success": "#4A7C59", "success-ink": "#3F6652",
    "warning": "#8F6718", "warning-ink": "#6F5724",
    "danger": "#B14A35", "danger-ink": "#874338",
    "info": "#4A6785", "info-ink": "#3F5770",
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
    "success": "#7FB08D", "success-ink": "#A2C4AA",
    "warning": "#D9B45E", "warning-ink": "#E1C78A",
    "danger": "#D97757", "danger-ink": "#E19C85",
    "info": "#8AA8C8", "info-ink": "#AABFD4",
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


def _read_js():
    return JS_PATH.read_text(encoding="utf-8")


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


def _to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _to_hex(rgb):
    return "#%02X%02X%02X" % tuple(int(round(max(0, min(255, c)))) for c in rgb)


_MIX_RE = re.compile(
    r"^color-mix\(\s*in\s+srgb\s*,\s*(.+?)\s+([\d.]+)%\s*,\s*(.+?)\s*\)$", re.S)


def _resolve(expr, tokens):
    """CSS の色式を 16 進数に解決する。

    実際に描画される色は `var()` と `color-mix()` の合成であることが多く、
    トークンの生値だけを見ていると「#fff をトークンの上に置いた」「薄い地を
    color-mix で作った」組み合わせが検査の網から丸ごと漏れる。ここで解決する。

    対応: `#RRGGBB` / `var(--token)` / `color-mix(in srgb, <A> P%, <B>)`（不透明同士）
    """
    expr = expr.strip()
    m = _MIX_RE.match(expr)
    if m:
        a = _to_rgb(_resolve(m.group(1), tokens))
        p = float(m.group(2)) / 100.0
        b = _to_rgb(_resolve(m.group(3), tokens))
        return _to_hex(tuple(a[i] * p + b[i] * (1 - p) for i in range(3)))
    m = re.match(r"^var\(\s*--([\w-]+)\s*\)$", expr)
    if m:
        name = m.group(1)
        assert name in tokens, f"未定義のトークン: --{name}"
        return _resolve(tokens[name], tokens)
    assert re.match(r"^#[0-9A-Fa-f]{6}$", expr), f"解決できない色式: {expr}"
    return expr.upper()


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

    @pytest.mark.parametrize("token", REMOVED_TOKENS)
    def test_old_token_is_gone_from_app_js(self, token):
        """app.js のインラインスタイルにも削除済みトークンの参照が無いこと。

        app.js は `style="color:var(--indigo-l)"` の形でトークンを参照する。
        削除済みトークンを参照しても CSS エラーにはならず、継承値や currentColor に
        黙って落ちるだけなので、実行しても気づけない。ここで機械的に止める。
        """
        assert f"var(--{token})" not in _read_js(), \
            f"var(--{token}) の参照が public/app.js に残っている"

    def test_no_hardcoded_indigo(self):
        """インディゴ系の生の16進数が残っていないこと。"""
        for path, text in (("style.css", _read_css()), ("app.js", _read_js())):
            up = text.upper()
            for dead in ["#6366F1", "#818CF8", "#4F46E5", "#10B981", "#0F172A", "#1F2937"]:
                assert dead not in up, f"{dead} が {path} に残っている"


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


class TestRenderedContrast:
    """実際に描画される前景／背景の組み合わせを守る。

    上の TestContrast は設計書 5.4 節が測ったトークン同士の 8 ペアだけを見ており、
    「トークンの上に置いた抜き文字」も「color-mix() で作った薄い地」も原理的に
    見えなかった。意味色 4 種が設計書のトークン表に無いまま実装で発明され、
    コントラスト実測を一度も通らなかったのはこの穴が原因。ここで塞ぐ。

    (説明, 前景の式, 背景の式) — 式は style.css の宣言をそのまま写す。
    """

    PAIRS = [
        # 意味色の薄い地に載せる文字（.badge-soft.* / .badge.bg-* / .ei-icon.*）
        ("badge-soft success", "var(--success-ink)",
         "color-mix(in srgb, var(--success) 14%, var(--surface))"),
        ("badge-soft warning", "var(--warning-ink)",
         "color-mix(in srgb, var(--warning) 14%, var(--surface))"),
        ("badge-soft danger", "var(--danger-ink)",
         "color-mix(in srgb, var(--danger) 14%, var(--surface))"),
        ("badge-soft info", "var(--info-ink)",
         "color-mix(in srgb, var(--info) 14%, var(--surface))"),
        ("badge-soft muted", "var(--ink-2)", "var(--paper)"),
        # shortage-chip は地が半透明なので、下地が --surface のときが最悪値
        ("shortage-chip", "var(--danger-ink)",
         "color-mix(in srgb, var(--danger) 12%, var(--surface))"),
        # 意味色を文字色として紙の上に置く（.text-*）
        ("text-success", "var(--success)", "var(--paper)"),
        ("text-warning", "var(--warning)", "var(--paper)"),
        ("text-danger", "var(--danger)", "var(--paper)"),
        ("text-info", "var(--info)", "var(--paper)"),
        # 意味色をベタ地にして抜き文字を載せる。#fff ではなく --paper を使うこと。
        # #fff だとダークで地と同化する（例: #fff on --warning = 1.97:1）。
        ("cal-pending-badge", "var(--paper)", "var(--warning)"),
        ("tl-gap-bar", "var(--paper)", "var(--alert)"),
        ("side-item-badge", "var(--paper)", "var(--alert)"),
        ("chat-ai-avatar", "var(--paper)", "var(--success)"),
        ("chat-bubble-warn avatar", "var(--paper)", "var(--warning)"),
        ("gen-step active icon", "var(--paper)", "var(--info)"),
        ("gen-step done icon", "var(--paper)", "var(--success)"),
        ("side-item active icon", "var(--paper)", "var(--ink)"),
        ("wmark any/evening", "var(--paper)", "var(--info)"),
        ("wmark morning", "var(--paper)", "var(--warning)"),
        ("wmark rest", "var(--paper)", "var(--ink-2)"),
        ("wmark time", "var(--paper)", "var(--success)"),
        ("chat-bubble-user", "var(--paper)", "var(--ink)"),
        ("chip count", "var(--ink)", "var(--rule)"),
    ]

    @pytest.mark.parametrize("label,fg,bg", PAIRS)
    @pytest.mark.parametrize("scope", [":root", 'html[data-theme="dark"]'])
    def test_rendered_pair_meets_aa(self, scope, label, fg, bg):
        t = _tokens_in_scope(scope)
        ratio = _contrast(_resolve(fg, t), _resolve(bg, t))
        theme = "ライト" if scope == ":root" else "ダーク"
        assert ratio >= 4.5, f"{theme} {label}: {ratio:.2f}:1（4.5:1 未満）"

    def test_no_white_literal_on_token_fill(self):
        """意味色・--ink のベタ地に #fff を直接置いていないこと。

        --paper はライトで明・ダークで暗と反転するので両テーマで地から離れるが、
        #fff はダークで地と同化する。地に var(--token) を使う宣言では #fff を禁じる。
        """
        css = _read_css()
        # @media print は常にライトのトークン値で固定されるため対象外
        screen_css = css.split("@media print")[0]
        offenders = []
        for line in screen_css.splitlines():
            if re.search(r"background(-color)?:\s*(var\(--|linear-gradient)", line) \
                    and re.search(r"color:\s*#fff\b", line, re.I):
                offenders.append(line.strip())
        assert not offenders, "トークン地の上に #fff を直接置いている: " + "; ".join(offenders)


class TestDraftBarNotOverridden:
    """AIドラフトのバーが申請中の質感に後勝ちで潰されないこと。

    AIドラフトは shifts.status='requested' で保存されるため .tl-bar-draft には
    必ず .tl-st-requested が同居する。同じ詳細度(0,2,0)で先に書くと後勝ちで死に、
    「live に見えて実は効いていない CSS」が残る。
    """

    def test_draft_rule_has_higher_specificity_and_comes_after(self):
        css = _read_css()
        draft = css.find(".tl-bar.tl-bar-draft.tl-st-requested")
        requested = css.find(".tl-bar.tl-st-requested {")
        assert draft != -1, ".tl-bar.tl-bar-draft.tl-st-requested の規則が無い"
        assert requested != -1, ".tl-bar.tl-st-requested の規則が無い"
        assert draft > requested, \
            ".tl-bar-draft の規則が .tl-st-requested より前にあり、後勝ちで無効化される"

    def test_no_lower_specificity_draft_appearance_rule(self):
        """詳細度(0,2,0) の .tl-bar.tl-bar-draft で見た目を指定していないこと。"""
        css = _read_css()
        m = re.search(r"\n\.tl-bar\.tl-bar-draft\s*\{(.*?)\n\}", css, re.S)
        assert m is None, \
            ".tl-bar.tl-bar-draft（詳細度0,2,0）の規則は .tl-st-requested に負ける"
