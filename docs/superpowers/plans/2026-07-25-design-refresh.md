# ShiftAI デザイン刷新 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 現行の「Deep Navy × Indigo」なAI SaaSテンプレ外観を全廃し、小売現場の帳票を出自とする視覚言語（紙の地・和色・24時間の配置帯）に全画面を差し替える。

**Architecture:** `public/style.css` の `:root` トークンを差し替えることで全画面に波及させる。Bootstrap 5 は挙動とユーティリティのみ利用し、外観は自前CSSで上書きする。`public/app.js` の DOM 構造・クラス名・セレクタは原則変更せず、色の決定ロジック（`slotClass`）と Chart.js のハードコード色のみ差し替える。ライトを `:root` に置き、ダークを `html[data-theme="dark"]` で上書きする構造に反転させる。

**Tech Stack:** 素の CSS（CSS変数）／Vanilla JS／Bootstrap 5.3.3（CDN・外観は上書き）／Chart.js 4.4.1／Google Fonts（Zen Kaku Gothic New, BIZ UDGothic）／pytest／Playwright

**設計の根拠:** `docs/superpowers/specs/2026-07-25-design-refresh-design.md`。トークンの値はすべてこの設計書からの引き写しであり、実装中に色を発明しない。

## Global Constraints

- **`app.js` の DOM 構造・クラス名・`data-*` 属性・要素 ID を変更しない。** e2e はこれらをセレクタに使っている（`#sStart`、`button[data-screen="shifts"]` 等）。例外は本計画が明示的に指示する箇所のみ
- **Bootstrap のクラス名を剥がさない。** `btn` / `form-control` / `input-group` / `d-none` 等はそのまま残し、見た目だけ CSS で上書きする
- **色は設計書の値を一字一句コピーする。** 近い色を目分量で置かない
- **ライトが正。** `:root` にライト値、`html[data-theme="dark"]` にダーク値を書く（現行と逆）
- **フォント:** UI は `'Zen Kaku Gothic New', system-ui, sans-serif`、数値・時刻・表内は `'BIZ UDGothic', ui-monospace, monospace`
- **廃止するトークン:** `--navy` `--surface`(旧) `--card` `--card-2` `--card-3` `--indigo` `--indigo-l` `--indigo-d` `--ai-green` `--ai-green-l` `--t-primary` `--t-secondary` `--t-muted` `--t-dim` `--line` `--line-2` `--line-3` `--sh-indigo` `--grad-indigo` `--grad-ai` `--grad-card` `--radius-xl`
- **すべての前景／背景の組み合わせが WCAG AA（4.5:1）を満たすこと。** Task 1 で導入する `tests/test_design_tokens.py` が機械的に検証する
- **テスト実行コマンド:** Python は `./.venv/bin/python -m pytest tests/ -v`、e2e は `npx playwright test`

---

## File Structure

| ファイル | 責務 | 変更種別 |
|---|---|---|
| `public/style.css` | デザイントークンと全コンポーネントの外観。33セクション構成は維持する | 大幅改修 |
| `public/index.html` | フォント読み込み、`theme-color`、ログイン見出しの文言 | 3箇所修正 |
| `public/app.js` | 色の決定ロジック（`slotClass`→`roleClass`）、Chart.js の色、日付境界の挿入、ダッシュボード構成 | 限定的修正 |
| `tests/test_design_tokens.py` | トークンの定義・廃止・コントラスト比を機械検証する | 新規作成 |

`style.css` は1108行あるが、セクション見出しコメント（`/* ---------- N. 名前 ---------- */`）で区切られている。**この区切りを保ったまま各セクションの中身を差し替える。** ファイル分割は行わない（現行の運用に合わせる。分割すると `index.html` の読み込みとキャッシュ戦略まで波及し、今回の目的に対して過剰）。

---

## Task 1: デザイントークンの差し替えと検証テスト

**Files:**
- Modify: `public/style.css:1-93`（セクション1・1b）
- Modify: `public/index.html:6`（theme-color）, `public/index.html:10`（フォント）
- Create: `tests/test_design_tokens.py`

**Interfaces:**
- Produces: 以降の全タスクが使う CSS 変数群。`--paper` `--surface` `--zebra` `--ink` `--ink-2` `--ink-3` `--rule` `--grid-1h` `--grid-4h` `--daybreak` `--alert` `--alert-fill` `--role-manager` `--role-manager-ink` `--role-employee` `--role-employee-ink` `--role-part-time` `--role-part-time-ink` `--role-student` `--role-student-ink` `--font-ui` `--font-num` `--radius-xs` `--radius-sm` `--radius` `--radius-lg` `--sh-card` `--ease` `--dur` `--dur-slow` `--header-h` `--side-w` `--bottom-h`

- [ ] **Step 1: 検証テストを書く（まだ通らない）**

`tests/test_design_tokens.py` を新規作成する。

```python
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
```

- [ ] **Step 2: テストを実行して落ちることを確認する**

Run: `./.venv/bin/python -m pytest tests/test_design_tokens.py -v`
Expected: FAIL。`--paper が :root に無い` 等のアサーションエラーが出る（まだトークンを書いていないため）。

- [ ] **Step 3: `style.css` の 1-93 行を新トークンに差し替える**

先頭のコメントブロックごと、1行目から93行目（`html[data-theme="light"]` ブロックの終わりまで）を次で置き換える。

```css
/* ============================================================
   ShiftAI Design System
   配置表 — 現場の帳票の系譜
   小売バックヤードの紙のシフト表・複写伝票・タイムカードに出自を持つ。
   ライトが正、ダークはその変換として導く。
   設計書: docs/superpowers/specs/2026-07-25-design-refresh-design.md
   ============================================================ */

/* ---------- 1. Design Tokens（ライト＝正） ---------- */
:root {
  /* 地と面 */
  --paper:       #FBFAF6;   /* アプリ背景。蛍光灯下の紙 */
  --surface:     #FFFFFF;   /* カード・パネル */
  --zebra:       #F5F3EC;   /* 横に長い表の交互行 */

  /* 文字 */
  --ink:         #243240;   /* 主文字・プライマリボタン地 */
  --ink-2:       #5A6472;   /* 副次文字 */
  --ink-3:       #8A8272;   /* 補助文字・軸の目盛り */

  /* 線 */
  --rule:        #E4E1D6;   /* 区切り罫・入力枠 */
  --grid-1h:     #EEEBE1;   /* 配置帯の1時間グリッド */
  --grid-4h:     #DCD6C4;   /* 配置帯の4時間グリッド */
  --daybreak:    #B9AF98;   /* 24:00 の日付境界 */

  /* 要対応 */
  --alert:       #B14A35;
  --alert-fill:  rgba(199,90,71,.18);

  /* ロール色（面 / 面上の文字）。色温度で常勤・非常勤を分ける */
  --role-manager:        #B9CBE2;  /* 薄縹・常勤 */
  --role-manager-ink:    #22364F;
  --role-employee:       #C9DFE0;  /* 浅葱・常勤 */
  --role-employee-ink:   #1D3D42;
  --role-part-time:      #F3DFA4;  /* 山吹・非常勤 */
  --role-part-time-ink:  #4A3A12;
  --role-student:        #F2DADD;  /* 桜・非常勤 */
  --role-student-ink:    #4A2A2E;

  /* 意味色（要対応以外。和色の系統に合わせて彩度を落とす） */
  --success:     #4A7C59;
  --warning:     #B8862B;
  --danger:      #B14A35;
  --info:        #4A6785;

  /* 書体 */
  --font-ui:     'Zen Kaku Gothic New', system-ui, -apple-system, sans-serif;
  --font-num:    'BIZ UDGothic', ui-monospace, SFMono-Regular, monospace;

  /* レイアウト */
  --header-h:    56px;
  --side-w:      248px;
  --bottom-h:    64px;

  /* 形 */
  --radius-xs:   3px;   /* ロールバッジなど小さなラベル */
  --radius-sm:   6px;   /* 配置帯のバー */
  --radius:      8px;   /* ボタン・入力 */
  --radius-lg:   10px;  /* カード */

  /* 影。帳票の質感を壊すため最小限にとどめる */
  --sh-card:     0 1px 2px rgba(36,50,64,.04), 0 8px 24px rgba(36,50,64,.05);
  --sh-modal:    0 12px 40px rgba(36,50,64,.14);

  /* モーション */
  --ease:        cubic-bezier(.4,0,.2,1);
  --dur:         200ms;
  --dur-slow:    350ms;

  /* ヘッダー背景 */
  --header-bg:   rgba(251,250,246,.85);
}

/* ---------- 1b. Dark Theme（ライトからの派生） ----------
   ロール色は色相を保ったまま面を暗くし、文字を明るくする。
   罫線・グリッドはライトでのコントラスト比を実測し同じ比率になる値を逆算した
   （罫線 1.25:1 / 1hグリッド 1.15:1 / 4hグリッド 1.40:1 / 日付境界 2.10:1）。
   ------------------------------------------------------- */
html[data-theme="dark"] {
  --paper:       #262624;
  --surface:     #30302E;
  --zebra:       #2B2A27;

  --ink:         #F5F4EF;
  --ink-2:       #B7B4AC;
  --ink-3:       #8A867E;

  --rule:        #373633;
  --grid-1h:     #31302D;
  --grid-4h:     #3E3D3A;
  --daybreak:    #585754;

  --alert:       #D97757;
  --alert-fill:  rgba(217,119,87,.20);

  --role-manager:        #3B4A5B;
  --role-manager-ink:    #C6D6E8;
  --role-employee:       #2F4A4D;
  --role-employee-ink:   #BFDCDE;
  --role-part-time:      #514526;
  --role-part-time-ink:  #EDD89E;
  --role-student:        #4E373B;
  --role-student-ink:    #F0D5D9;

  --success:     #7FB08D;
  --warning:     #D9B45E;
  --danger:      #D97757;
  --info:        #8AA8C8;

  --sh-card:     0 1px 2px rgba(0,0,0,.30), 0 8px 24px rgba(0,0,0,.36);
  --sh-modal:    0 12px 40px rgba(0,0,0,.50);

  --header-bg:   rgba(38,38,36,.85);
}
```

- [ ] **Step 4: テストを実行して通ることを確認する**

Run: `./.venv/bin/python -m pytest tests/test_design_tokens.py -v`
Expected: PASS。ただし `TestOldTokensRemoved` は `style.css` の残り（94行目以降）が旧トークンを参照しているため**まだ落ちる**。この時点で落ちてよいのは `test_old_token_is_gone` と `test_no_hardcoded_indigo` のみ。`TestTokensDefined` と `TestContrast` は全て PASS すること。

- [ ] **Step 5: `index.html` のフォントと theme-color を差し替える**

10行目の Google Fonts の行を置き換える。

```html
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=BIZ+UDGothic:wght@400;700&display=swap" rel="stylesheet" />
```

6行目の theme-color を紙の色に変える。

```html
  <meta name="theme-color" content="#FBFAF6" media="(prefers-color-scheme: light)" />
  <meta name="theme-color" content="#262624" media="(prefers-color-scheme: dark)" />
```

- [ ] **Step 6: コミット**

```bash
git add public/style.css public/index.html tests/test_design_tokens.py
git commit -m "feat(design): デザイントークンを配置表の配色に差し替え

ライトを:rootに、ダークをhtml[data-theme=dark]に置く構造へ反転。
インディゴ系トークンを廃止し、紙の地・和色・ロール色に置き換えた。
トークンの値とコントラスト比を機械検証するテストを追加。"
```

---

## Task 2: ベース・タイポグラフィ・レイアウトの追従

**Files:**
- Modify: `public/style.css:95-259`（セクション2 Reset & Base、3 Typography、4 Layout）

**Interfaces:**
- Consumes: Task 1 の全トークン
- Produces: `body` の地色と書体、`.app-header` / `.side-nav` / `.bottom-nav` / `.app-content` の外観

- [ ] **Step 1: セクション2〜4 の旧トークン参照を置換する**

95行目から259行目までを読み、旧トークンを次の対応で置き換える。**セレクタと構造は変えない。値だけを差し替える。**

| 旧 | 新 |
|---|---|
| `var(--navy)` | `var(--paper)` |
| `var(--surface)` （旧＝濃色面） | `var(--surface)` （新＝白面。意味が変わるので用途を見て `--paper` か `--surface` を選ぶ） |
| `var(--card)` / `var(--card-2)` / `var(--card-3)` | `var(--surface)` / `var(--paper)` / `var(--rule)` |
| `var(--t-primary)` | `var(--ink)` |
| `var(--t-secondary)` | `var(--ink-2)` |
| `var(--t-muted)` / `var(--t-dim)` | `var(--ink-3)` |
| `var(--line)` / `var(--line-2)` / `var(--line-3)` | `var(--rule)` |
| `var(--indigo)` | `var(--ink)`（強調）または `var(--role-manager)`（面） |
| `var(--sh)` / `var(--sh-lg)` | `var(--sh-card)` |
| `var(--radius-xl)` | `var(--radius-lg)` |
| `var(--app-bg)` | 削除（グラデーションの地は使わない。`--paper` の単色にする） |

> **`--zebra` は行が横に長い表の交互行にのみ使う。** 設計書の原則「ゼブラを使うのは行が横に長い表（配置帯、スタッフ一覧、希望表、監査ログ）に限る。カード・フォーム・ナビには使わない」に従うこと。上の対応表は機械的な変換の目安であり、この原則に優先しない。ボタン・入力・ナビ・カードでわずかに濃い地が必要な場合は `--paper` を使う。

`body` の `font-family` を `var(--font-ui)` にする。数値を等幅にするクラスがあれば `var(--font-num)` に向ける（現行の `.num` クラスがそれに当たる）。

- [ ] **Step 2: 目視で確認する**

Run: `bash e2e/run_server.sh &` してから `http://127.0.0.1:8000` を開く。
Expected: 背景が紙色になり、ヘッダー・サイドナビ・本文の文字が読める。この時点でボタンやカードは Bootstrap 既定のままでよい。

- [ ] **Step 3: コミット**

```bash
git add public/style.css
git commit -m "feat(design): ベース・タイポグラフィ・レイアウトを新トークンに追従"
```

---

## Task 3: Bootstrap コンポーネントの外観上書き

**Files:**
- Modify: `public/style.css:316-529`（6 Buttons / 7 Inputs / 8 Cards / 9 Badges / 10 Tables / 11 List rows / 12 Tabs / 13 Modal / 14 Toast / 15 Loading）

**Interfaces:**
- Consumes: Task 1 の全トークン
- Produces: `.btn` / `.form-control` / `.card` / `.badge` / `table` / `.modal` の外観

- [ ] **Step 1: ボタンを差し替える（セクション6）**

```css
/* ---------- 6. Components: Buttons ---------- */
.btn {
  font-family: var(--font-ui);
  font-weight: 500;
  border-radius: var(--radius);
  border: 1px solid transparent;
  transition: background-color var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.btn-primary {
  background: var(--ink);
  color: var(--paper);
  border-color: var(--ink);
}
.btn-primary:hover, .btn-primary:focus {
  background: color-mix(in srgb, var(--ink) 88%, var(--paper));
  color: var(--paper);
  border-color: var(--ink);
}
.btn-light, .btn-secondary {
  background: var(--surface);
  color: var(--ink);
  border-color: var(--rule);
}
.btn-light:hover, .btn-secondary:hover {
  background: var(--paper);
  color: var(--ink);
  border-color: var(--rule);
}
.btn-danger {
  background: var(--alert);
  color: var(--paper);
  border-color: var(--alert);
}
.btn:focus-visible {
  outline: 2px solid var(--ink);
  outline-offset: 2px;
  box-shadow: none;
}
.icon-btn {
  background: transparent;
  border: 1px solid transparent;
  color: var(--ink-2);
  border-radius: var(--radius);
}
.icon-btn:hover { background: var(--zebra); color: var(--ink); }
.icon-btn:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
```

- [ ] **Step 2: 入力を差し替える（セクション7）**

```css
/* ---------- 7. Components: Inputs ---------- */
.form-control, .form-select {
  font-family: var(--font-ui);
  background: var(--surface);
  color: var(--ink);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
}
.form-control::placeholder { color: var(--ink-3); opacity: .8; }
.form-control:focus, .form-select:focus {
  background: var(--surface);
  color: var(--ink);
  border-color: var(--ink-2);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ink) 12%, transparent);
}
/* 時刻・数値の入力は等幅にする。読み違いを防ぐため */
input[type="time"], input[type="number"], input[type="date"], .form-control.num {
  font-family: var(--font-num);
}
.input-group-text {
  background: var(--paper);
  color: var(--ink-3);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
}
.form-label { color: var(--ink-2); font-size: .78rem; font-weight: 500; }
```

- [ ] **Step 3: カード・バッジ・テーブルを差し替える（セクション8〜10）**

```css
/* ---------- 8. Components: Cards ---------- */
.card, .app-card {
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: var(--radius-lg);
  box-shadow: var(--sh-card);
}
.card-body { color: var(--ink); }

/* ---------- 9. Badges & Pills ---------- */
.badge {
  font-family: var(--font-ui);
  font-weight: 700;
  font-size: .68rem;
  border-radius: var(--radius-xs);
  padding: .2em .5em;
}
.badge.bg-success { background: color-mix(in srgb, var(--success) 22%, var(--surface)) !important; color: var(--success) !important; }
.badge.bg-warning { background: color-mix(in srgb, var(--warning) 22%, var(--surface)) !important; color: var(--warning) !important; }
.badge.bg-danger  { background: color-mix(in srgb, var(--alert) 18%, var(--surface)) !important; color: var(--alert) !important; }
.badge.bg-info    { background: color-mix(in srgb, var(--info) 22%, var(--surface)) !important; color: var(--info) !important; }

/* ---------- 10. Tables ---------- */
table { color: var(--ink); border-color: var(--rule); }
thead th {
  color: var(--ink-2);
  font-weight: 700;
  font-size: .78rem;
  border-bottom: 1px solid var(--rule);
}
/* 横に長い表はゼブラで行を分ける。数値セルは等幅 */
tbody tr:nth-child(odd) { background: var(--zebra); }
td.num, th.num, td .num { font-family: var(--font-num); }
```

- [ ] **Step 4: モーダル・トースト・ローディングを差し替える（セクション13〜15）**

```css
/* ---------- 13. Modal ---------- */
.modal-content {
  background: var(--surface);
  color: var(--ink);
  border: 1px solid var(--rule);
  border-radius: var(--radius-lg);
  box-shadow: var(--sh-modal);
}
.modal-header, .modal-footer { border-color: var(--rule); }
.modal-backdrop.show { opacity: .45; }

/* ---------- 14. Toast ---------- */
.toast-item {
  background: var(--ink);
  color: var(--paper);
  border-radius: var(--radius);
  box-shadow: var(--sh-card);
  font-size: .82rem;
}
```

セクション11（List rows）・12（Tabs）・15（Loading）も同じ対応表（Task 2 Step 1）で旧トークンを置換する。

- [ ] **Step 5: 旧トークン検証テストを実行する**

Run: `./.venv/bin/python -m pytest tests/test_design_tokens.py::TestOldTokensRemoved -v`
Expected: セクション16以降にまだ旧トークンが残っているため FAIL してよい。Task 8 完了時点で PASS すること。

- [ ] **Step 6: コミット**

```bash
git add public/style.css
git commit -m "feat(design): Bootstrapコンポーネントの外観を帳票の質感に上書き"
```

---

## Task 4: ログイン画面

**Files:**
- Modify: `public/style.css:260-315`（セクション5 Login）
- Modify: `public/index.html:35`（見出しの文言）

**Interfaces:**
- Consumes: Task 1 のトークン、Task 3 のボタン・入力

- [ ] **Step 1: ログインの CSS を差し替える**

背景に1時間グリッドを薄く敷き、カード上端に4つのロール色を帯として並べる。既存の `.login-bg-noise` と `.login-glow` は削除せず、中身だけ差し替える（`index.html` の DOM を触らないため）。

```css
/* ---------- 5. Login ---------- */
.login-wrap {
  min-height: 100dvh;
  display: flex; align-items: center; justify-content: center;
  padding: 24px 16px;
  background: var(--paper);
  position: relative; overflow: hidden;
}
/* 背景に配置帯の1時間グリッドを敷く。製品の署名を最初の画面で一度だけ見せる */
.login-bg-noise {
  position: absolute; inset: 0; opacity: .55; pointer-events: none;
  background-image: linear-gradient(90deg, var(--grid-1h) 1px, transparent 1px);
  background-size: 4.5455% 100%;
}
.login-glow { display: none; }
.login-card {
  position: relative; width: 100%; max-width: 380px;
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: var(--radius-lg);
  box-shadow: var(--sh-card);
  overflow: hidden;
}
/* カード上端のロール色の帯 */
.login-card::before {
  content: ''; display: block; height: 5px;
  background: linear-gradient(to right,
    var(--role-manager) 0 24%,
    var(--role-employee) 24% 55%,
    var(--role-part-time) 55% 82%,
    var(--role-student) 82% 100%);
}
.login-hero { padding: 24px 26px 6px; text-align: left; }
.login-hero h3 { font-size: 1.05rem; font-weight: 700; color: var(--ink); margin-top: 10px; }
.login-hero p { color: var(--ink-3); font-size: .78rem; }
.brand-logo { font-size: 1.3rem; color: var(--ink); }
.login-form { padding: 14px 26px 26px; }
.form-field { margin-bottom: 13px; }
```

- [ ] **Step 2: 見出しの文言を変える**

`public/index.html:35` を次に変える。ログインする人がこれからする行為を書く。

```html
        <p class="text-secondary small mb-0">シフトを確認する</p>
```

- [ ] **Step 3: e2e のログインテストが通ることを確認する**

Run: `npx playwright test e2e/fast_navigation.spec.js`
Expected: PASS。ログインのセレクタ（`#loginShopCode` 等）は変えていないため無傷。

- [ ] **Step 4: コミット**

```bash
git add public/style.css public/index.html
git commit -m "feat(design): ログイン画面を紙の地とロール色の帯で刷新"
```

---

## Task 5: 配置帯のロール色化

**Files:**
- Modify: `public/app.js:114-120`（`slotClass` を `roleClass` に）
- Modify: `public/app.js:729, 772, 853, 1169, 1197`（呼び出し）
- Modify: `public/app.js:888-891, 1231`（凡例）
- Modify: `public/style.css:599-601, 662-664, 695-697`（`.chip` / `.dot` / `.tl-bar` の色）

**Interfaces:**
- Consumes: Task 1 の `--role-*` トークン
- Produces: `roleClass(role)` — 引数は `staffs.role` の値（`'employee' | 'part_time' | 'manager' | 'student'`）、戻り値は CSS クラス名の文字列（`'role-employee'` 等）。以降のタスクはこの関数名と戻り値を前提にする

**背景:** 現行は `slotClass(iso)` が開始時刻から `morning` / `noon` / `evening` を返している。設計ではバーの色はロールを表す。`staff_role` は API が既に返している（`src/app.py:2802` の `s.role as staff_role`）。

- [ ] **Step 1: `roleClass` を実装する**

`public/app.js:114-120` の `slotClass` を次で置き換える。

```javascript
/** スタッフのロールから配置帯・チップの色クラスを決める。
 *  色は staffs.role を表す。寒色＝常勤（店長・社員）、暖色＝非常勤（パート・学生）。
 *  未知の値や欠損は社員扱いにフォールバックする（色が消えるより誤色のほうが害が小さい）。 */
function roleClass(role) {
  switch (role) {
    case 'manager':   return 'role-manager';
    case 'employee':  return 'role-employee';
    case 'part_time': return 'role-part-time';
    case 'student':   return 'role-student';
    default:          return 'role-employee';
  }
}

/** ロールの日本語表示名。バッジに出して色だけに依存しないようにする。 */
function roleLabel(role) {
  switch (role) {
    case 'manager':   return '店長';
    case 'employee':  return '社員';
    case 'part_time': return 'パート';
    case 'student':   return '学生';
    default:          return '';
  }
}
```

- [ ] **Step 2: 呼び出し5箇所を置き換える**

`slotClass(s.start_datetime)` を `roleClass(s.staff_role)` に置き換える。対象は 729, 772, 853, 1169, 1197 行（Step 1 で行番号がずれるので、`grep -n "slotClass(" public/app.js` で再確認してから直す）。

Run: `grep -n "slotClass(" public/app.js`
Expected: 置換後は0件。

- [ ] **Step 3: 凡例を差し替える**

`public/app.js:888-891`（印刷用）を次に変える。

```javascript
  <div class="tl-legend">
    <span><i class="lg-role-manager"></i>店長</span>
    <span><i class="lg-role-employee"></i>社員</span>
    <span><i class="lg-role-part-time"></i>パート</span>
    <span><i class="lg-role-student"></i>学生</span>
    <span><i class="lg-alert"></i>不足</span>
```

`public/app.js:1231`（画面用）の凡例も同様に、`style="background:#F59E0B"` 等のインライン色をやめて `class="lg-role-manager"` 形式に置き換える。`editable` 分岐や「バーをタップで編集」の文言はそのまま残す。

- [ ] **Step 4: CSS のロールクラスを追加し、時間帯クラスを削除する**

`style.css:599-601`（`.chip`）、`662-664`（`.dot`）、`695-697`（`.tl-bar`）の `.morning` / `.noon` / `.evening` を削除し、次を書く。

```css
/* ロール色。面と文字をセットで当てる（文字色を忘れると読めなくなる） */
.chip.role-manager,   .dot.role-manager,   .tl-bar.role-manager   { background: var(--role-manager);   color: var(--role-manager-ink); }
.chip.role-employee,  .dot.role-employee,  .tl-bar.role-employee  { background: var(--role-employee);  color: var(--role-employee-ink); }
.chip.role-part-time, .dot.role-part-time, .tl-bar.role-part-time { background: var(--role-part-time); color: var(--role-part-time-ink); }
.chip.role-student,   .dot.role-student,   .tl-bar.role-student   { background: var(--role-student);   color: var(--role-student-ink); }

/* 凡例のキー */
.tl-legend i { display: inline-block; width: 11px; height: 11px; border-radius: var(--radius-xs); vertical-align: -1px; margin-right: 4px; }
.tl-legend i.lg-role-manager   { background: var(--role-manager); }
.tl-legend i.lg-role-employee  { background: var(--role-employee); }
.tl-legend i.lg-role-part-time { background: var(--role-part-time); }
.tl-legend i.lg-role-student   { background: var(--role-student); }
.tl-legend i.lg-alert          { background: var(--alert-fill); border: 1px solid var(--alert); }
```

`.wmark`（829行）は希望の種類を表す別概念なので**触らない**。

- [ ] **Step 5: e2e を実行して回帰がないことを確認する**

Run: `npx playwright test e2e/timeline_visual.spec.js e2e/shift_detail_visual.spec.js`
Expected: PASS。これらは時刻テキストを検証しており色に依存しない。

- [ ] **Step 6: コミット**

```bash
git add public/app.js public/style.css
git commit -m "feat(design): 配置帯の色を時間帯からロール別に変更

slotClass(開始時刻)をroleClass(staffs.role)へ置き換えた。
確定後も色が意味を持ち続け、誰が入っているかが一目で分かる。"
```

---

## Task 6: 配置帯の1時間グリッド・ゼブラ・日付境界

**Files:**
- Modify: `public/style.css:671-723`（セクション20 Timeline）
- Modify: `public/app.js`（`buildPrintTimelineHtml` と画面用タイムラインの `.tl-track` に日付境界を挿入）

**Interfaces:**
- Consumes: Task 1 の `--grid-1h` `--grid-4h` `--daybreak` `--zebra`、Task 5 の `roleClass`
- Produces: `.tl-track` に `--tl-hours`（表示時間数）と `--tl-daybreak`（24:00 の位置％）の CSS 変数をインラインで持たせる

**背景:** 現行はトラックが単色（`background: var(--card-2)`）で、時間の目盛りは軸行の `.tl-hour` の `border-left` だけにある。バーの背後には線がないため、位置を目測できない。

- [ ] **Step 1: トラックにグリッドを敷く CSS を書く**

`style.css` のセクション20 で `.tl-track` の定義を次に差し替える。

```css
.tl-row { display: flex; align-items: center; min-width: 480px; }
/* 横に長いので行をゼブラで分ける。横罫は引かない（縦線と交差して格子になる） */
.tl-row:nth-of-type(odd) { background: var(--zebra); }
.tl-track {
  flex: 1 1 auto; position: relative; height: 32px; min-width: 0; cursor: pointer;
  /* 1時間ごとの細線と4時間ごとの濃線。--tl-hours は JS がインラインで与える */
  background-image:
    linear-gradient(90deg, var(--grid-1h) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid-4h) 1px, transparent 1px);
  background-size:
    calc(100% / var(--tl-hours, 22)) 100%,
    calc(400% / var(--tl-hours, 22)) 100%;
}
.tl-track:hover { background-color: color-mix(in srgb, var(--ink) 4%, transparent); }
/* 24:00 の日付境界。深夜をまたぐシフトがどこで日付を越えるかを示す */
.tl-track::after {
  content: '';
  position: absolute; top: 0; bottom: 0;
  left: var(--tl-daybreak, -1px);
  width: 1px; background: var(--daybreak);
  display: var(--tl-daybreak-display, none);
}
```

- [ ] **Step 2: JS から時間数と境界位置を渡す**

`buildPrintTimelineHtml`（`public/app.js:794`〜）の行生成部分で、`.tl-track` にインライン CSS 変数を付ける。`minH` / `maxH` / `rangeMin` / `rangeLen` は同関数内に既にある。

まず関数内、`const rows = order.map(...)` の直前に次を置く。

```javascript
  // 配置帯のグリッド用: 表示時間数と、24:00 の位置（範囲外なら線を出さない）
  const tlHours = Math.max(1, maxH - minH);
  const dayBreakPct = ((24 * 60 - rangeMin) / rangeLen) * 100;
  const showDayBreak = dayBreakPct > 0 && dayBreakPct < 100;
  const trackVars = `--tl-hours:${tlHours};`
    + (showDayBreak ? `--tl-daybreak:${dayBreakPct.toFixed(2)}%;--tl-daybreak-display:block;` : '');
```

次に、同関数の `.tl-track` を出している箇所（現在の855行）を書き換える。

```javascript
    return `<div class="tl-row"><div class="tl-name">${esc(st.name)}</div><div class="tl-track" style="${trackVars}">${bars}</div></div>`;
```

不足行（現在の872行）の `.tl-track` にも同じ `style="${trackVars}"` を付ける。

- [ ] **Step 3: 画面用タイムラインにも同じ変数を渡す**

画面用の描画関数（`public/app.js:1133`〜。`const order = []; const staffMap = {};` で始まる関数）にも Step 2 と同じ3行（`tlHours` / `dayBreakPct` / `showDayBreak` / `trackVars`）を追加し、`.tl-track` を出している箇所（現在の1200行）に `style="${trackVars}"` を足す。

**注意:** 画面用の `.tl-track` は既に `data-staff-id` と `title` を持っている。これらは消さずに `style` を追加する。

```javascript
    return `<div class="tl-row" data-staff-id="${sid}" data-staff-name="${esc(st.name)}"><div class="tl-name">${esc(st.name)}</div><div class="tl-track" data-staff-id="${sid}" style="${trackVars}" title="${editable ? '空き部分をクリックで追加' : ''}">${bars}</div></div>`;
```

- [ ] **Step 4: バーの見た目をトークンに合わせる**

セクション20 の `.tl-bar` を次に差し替える。影を落とし、白文字をやめる（ロール色の文字色は Task 5 の CSS が当てる）。

```css
.tl-bar {
  position: absolute; top: 4px; bottom: 4px;
  border-radius: var(--radius-sm);
  font-family: var(--font-num);
  font-size: .58rem; font-weight: 700;
  display: flex; align-items: center; padding: 0 5px;
  overflow: hidden; white-space: nowrap; cursor: pointer;
  transition: transform var(--dur) var(--ease);
}
.tl-bar:active { transform: scale(.98); }
.tl-bar.selected { outline: 2px solid var(--ink); outline-offset: 1px; z-index: 3; }
.tl-bar.tl-bar-continued { box-shadow: inset 3px 0 0 color-mix(in srgb, var(--ink) 45%, transparent); }
.tl-name { width: 52px; flex: 0 0 52px; font-size: .68rem; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding-left: 4px; }
.tl-hour { flex: 1 1 0; font-size: .5rem; font-family: var(--font-num); color: var(--ink-3); text-align: left; border-left: 1px solid var(--rule); padding-left: 1px; }
.tl-hour-next { color: var(--daybreak); font-weight: 700; }
```

- [ ] **Step 5: 画面で確認する**

Run: サーバを起動し、店長でログイン→シフト画面→日付をクリックしてタイムラインを開く。
Expected: バーの背後に1時間ごとの細線と4時間ごとの濃線が出る。営業時間が24時をまたぐ日は縦の境界線が1本出る。行が交互の地色で分かれている。

- [ ] **Step 6: コミット**

```bash
git add public/style.css public/app.js
git commit -m "feat(design): 配置帯に1時間グリッド・ゼブラ行・日付境界を追加"
```

---

## Task 7: 状態を質感で表す

**Files:**
- Modify: `public/app.js`（バー生成時に status クラスを付ける。853行付近と1197行付近）
- Modify: `public/style.css`（セクション20 に状態の質感を追加）

**Interfaces:**
- Consumes: Task 5 の `roleClass`、Task 6 のバー CSS
- Produces: `.tl-st-confirmed` / `.tl-st-modifying` / `.tl-st-requested` クラス

**背景:** 色はロールを表すため、状態（`shifts.status`）は質感で表す。既存の `.tl-bar-draft`（AIドラフト）は `requested` の一種であり、併存させる。

- [ ] **Step 1: status クラスを付ける関数を追加する**

`public/app.js` の `roleLabel` の直後に置く。

```javascript
/** シフトの状態を質感クラスに変換する。色はロールが担うため、状態は模様で表す。
 *  confirmed=ベタ塗り / modifying=斜線 / requested=淡く破線枠 */
function statusClass(status) {
  switch (status) {
    case 'confirmed': return 'tl-st-confirmed';
    case 'modifying': return 'tl-st-modifying';
    case 'requested': return 'tl-st-requested';
    default:          return 'tl-st-confirmed';
  }
}
```

- [ ] **Step 2: バー生成に status クラスを足す**

印刷用（853行付近）と画面用（1197行付近）の `class="tl-bar ..."` に `${statusClass(s.status)}` を追加する。既存の `contCls` / `draftCls` / `overCapCls` は消さない。

印刷用:
```javascript
      return `<div class="tl-bar ${roleClass(s.staff_role)} ${statusClass(s.status)}${contCls}${draftCls}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%">${lbl}</div>`;
```

画面用も同様に `${statusClass(s.status)}` を `roleClass(...)` の直後へ足す。

- [ ] **Step 3: 質感の CSS を書く**

セクション20 に追加する。

```css
/* 状態は色ではなく質感で表す（色はロールが担っているため） */
.tl-bar.tl-st-confirmed { /* ベタ塗り。追加の装飾なし */ }
.tl-bar.tl-st-modifying {
  background-image: repeating-linear-gradient(45deg,
    rgba(255,255,255,.55) 0 4px, transparent 4px 8px);
}
.tl-bar.tl-st-requested {
  opacity: .55;
  border: 1px dashed color-mix(in srgb, var(--ink) 45%, transparent);
}
html[data-theme="dark"] .tl-bar.tl-st-modifying {
  background-image: repeating-linear-gradient(45deg,
    rgba(255,255,255,.22) 0 4px, transparent 4px 8px);
}
```

- [ ] **Step 4: 凡例に質感の説明を足す**

`public/app.js` の印刷用凡例（Task 5 Step 3 で編集した箇所）の末尾に追加する。

```javascript
    <span class="tl-legend-note">ベタ塗り＝確定／斜線＝変更中／薄い破線＝申請中</span>
```

対応する CSS:
```css
.tl-legend-note { color: var(--ink-3); font-size: .62rem; }
```

- [ ] **Step 5: e2e を実行する**

Run: `npx playwright test e2e/draft_preserves_requests.spec.js e2e/timeline_visual.spec.js`
Expected: PASS。

- [ ] **Step 6: コミット**

```bash
git add public/app.js public/style.css
git commit -m "feat(design): シフトの状態を色から質感（ベタ/斜線/破線）へ移行"
```

---

## Task 8: カレンダー・KPI・その他セクションの追従

**Files:**
- Modify: `public/style.css:530-670`（16 KPI Cards / 17 Dashboard grid / 18 Chart container / 19 Calendar / 19b Staff cell）
- Modify: `public/style.css:724-871`（21 Shortage / 22 Notifications / 23 Shift Matrix / 24 Chat / 25 AI Generator / 26 Wish calendar / 27 Preview grid / 28 Empty state / 29 Animations / 30 Page title / 31 Utility / 32 Responsive）

**Interfaces:**
- Consumes: Task 1 の全トークン

- [ ] **Step 1: 残りのセクションの旧トークンを一掃する**

Task 2 Step 1 の対応表に従って、530行以降のすべての旧トークン参照とハードコードされたインディゴ系16進数を置き換える。

不足表示（セクション21 Shortage）は `--alert` と `--alert-fill` を使う。

```css
/* ---------- 21. Shortage ---------- */
.tl-gap-row .tl-name { color: var(--alert); font-weight: 700; }
.tl-gap-bar {
  position: absolute; top: 5px; bottom: 5px;
  border-radius: var(--radius-sm);
  background: var(--alert-fill);
  border: 1px solid var(--alert);
  color: var(--alert);
  font-family: var(--font-num); font-size: .56rem; font-weight: 700;
  display: flex; align-items: center; padding: 0 5px;
  overflow: hidden; white-space: nowrap;
}
```

- [ ] **Step 2: 旧トークンが1つも残っていないことをテストで確認する**

Run: `./.venv/bin/python -m pytest tests/test_design_tokens.py -v`
Expected: **全 PASS**。`TestOldTokensRemoved` を含めてすべて通ること。落ちた場合は、テストが指す残存トークンを潰してから次へ進む。

- [ ] **Step 3: コミット**

```bash
git add public/style.css
git commit -m "feat(design): カレンダー・KPI・その他セクションを新トークンへ追従

旧インディゴ系トークンの参照が style.css から一掃された。"
```

---

## Task 9: Chart.js の色をトークン化

**Files:**
- Modify: `public/app.js:1622, 1631, 2717, 2727`（Chart の色指定）
- Modify: `public/app.js`（テーマ切替時のチャート再描画）

**Interfaces:**
- Consumes: Task 1 のトークン
- Produces: `cssVar(name)` — CSS 変数を読む関数。引数は `'--ink-3'` のような変数名、戻り値は文字列

**背景:** 現在 `rgba(99,102,241,.6)` `#6366F1` `#64748B` `rgba(148,163,184,.1)` がハードコードされている。テーマを切り替えても追従しない。

- [ ] **Step 1: CSS 変数を読むヘルパを追加する**

`public/app.js` の `roleClass` の近くに置く。

```javascript
/** CSS 変数の現在値を読む。テーマ切替後は値が変わるので、描画のたびに呼ぶこと。 */
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** 不透明度を落とした色を作る。Chart.js の塗りに使う。
 *  Chart.js は canvas に描画するため CSS 関数（color-mix 等）を解釈できない。
 *  トークンの hex を読んで rgba() の文字列に変換する。 */
function cssVarAlpha(name, alpha) {
  const hex = cssVar(name).replace('#', '');
  if (hex.length !== 6) return cssVar(name);   // 想定外の形式ならそのまま返す
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}
```

**注意:** `color-mix()` を Chart.js に渡してはいけない。CSS 上では有効だが canvas の `fillStyle` は解釈できず、色が既定値（黒）に落ちる。CSS 側で `color-mix()` を使うのは問題ないが、JS から Chart.js に渡す色は必ず `rgba()` か `#rrggbb` の形式にすること。

- [ ] **Step 2: 今日の人員チャート（1622行付近）を差し替える**

```javascript
    if (todayCanvas) chartInstances.today = new Chart(todayCanvas, {
      type: 'bar',
      data: { labels: hours.length ? hours : ['データなし'], datasets: [{ label: '人数', data: counts.length ? counts : [0], backgroundColor: cssVarAlpha('--role-employee', .9), borderRadius: 6 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { color: cssVar('--ink-3') }, grid: { color: cssVar('--rule') } }, x: { ticks: { color: cssVar('--ink-3') }, grid: { display: false } } } }
    });
```

- [ ] **Step 3: 人件費チャート（1631行付近）を差し替える**

```javascript
    if (costCanvas) chartInstances.cost = new Chart(costCanvas, {
      type: 'line',
      data: { labels: costData.map((c) => c.date.slice(5)), datasets: [{ label: '人件費(円)', data: costData.map((c) => c.cost), borderColor: cssVar('--ink'), backgroundColor: cssVarAlpha('--ink', .08), fill: true, tension: .3, pointRadius: 0 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { color: cssVar('--ink-3'), callback: (v) => '¥' + (v / 1000) + 'K' }, grid: { color: cssVar('--rule') } }, x: { ticks: { color: cssVar('--ink-3'), maxTicksLimit: 8 }, grid: { display: false } } } }
    });
```

- [ ] **Step 4: 人件費分析画面のチャート（2717・2727行付近）も同様に差し替える**

同じ方針で、`#6366F1` 系を `cssVar('--ink')` に、`#64748B` を `cssVar('--ink-3')` に、`rgba(148,163,184,.1)` を `cssVar('--rule')` に置き換える。複数系列を色分けしているグラフがある場合は、系列順に `--role-manager` → `--role-employee` → `--role-part-time` → `--role-student` を割り当てる。

- [ ] **Step 5: テーマ切替でチャートを描き直す**

テーマ切替ハンドラ（`themeToggleBtn` のクリック処理）の末尾に、開いている画面を再描画する処理を足す。

```javascript
    // チャートは生成時の色を保持するため、テーマが変わったら現在画面を描き直す
    Object.values(chartInstances).forEach((c) => { try { c.destroy(); } catch (e) {} });
    Object.keys(chartInstances).forEach((k) => delete chartInstances[k]);
    if (typeof navigateTo === 'function' && currentScreen) navigateTo(currentScreen);
```

- [ ] **Step 6: ハードコード色が残っていないことを確認する**

Run: `grep -nE "#6366F1|#818CF8|#4F46E5|#10B981|#64748B|rgba\(99,102,241|rgba\(148,163,184" public/app.js`
Expected: 0件。

- [ ] **Step 7: 目視で確認する**

Run: サーバを起動し、ダッシュボードと人件費分析を開き、テーマを切り替える。
Expected: 両テーマでグラフの軸・グリッド・線が読める。切り替え直後に色が追従する。

- [ ] **Step 8: コミット**

```bash
git add public/app.js
git commit -m "feat(design): Chart.jsの色をCSS変数から読むようにしテーマ追従させる"
```

---

## Task 10: ダッシュボードの構成変更

**Files:**
- Modify: `public/app.js`（店長ダッシュボードの描画。`screenDashboard` 相当の関数）
- Modify: `public/style.css:530-566`（16 KPI Cards / 17 Dashboard grid）

**Interfaces:**
- Consumes: Task 6 の配置帯、Task 9 のチャート、既存の `_computeHourlyGaps(shifts, dayStr, opts)`（`app.js:605`）と `_mergeHourlyGaps(gaps)`（`app.js:661`。戻り値は `[{start, end, gap}]`）
- Produces: `gapSummaryText(merged)` — 引数は `_mergeHourlyGaps` の戻り値、戻り値は不足を説明する日本語1文の文字列

**背景:** 設計では、店長が最初に見るのは「今日は回るのか」であり、集計値はその後。配置表を最上部に置き、数値を下に小さく敷く。

**制約:** 表示する指標は**既存の実装に準拠**する。新しい集計を追加しない。`/api/shop/dashboard` が返す `today_shifts` / `today_attendance` / `today_hourly` などを使う。

- [ ] **Step 1: 現在のダッシュボード描画を読む**

Run: `grep -n "dashboard" public/app.js | head -20` して描画関数を特定し、現在の要素の並び順を書き出す。

- [ ] **Step 2: 配置表を最上部へ移す**

KPI カード群より前に「今日の配置」ブロックを置く。既存のタイムライン描画関数を、`/api/shop/dashboard` の `today_shifts` ではなく当日の `/api/shop/shifts` の結果に対して呼ぶ。`staff_role` が必要なため、`today_shifts`（role は返るが `staff_role` キー名ではない）ではなく `/api/shop/shifts?start=今日&end=今日` を使う。

不足があるときは、見出しの直下に文章でも出す。色と模様だけに頼らないため。まず要約文を作る関数を `public/app.js` の `statusClass` の直後に追加する。

```javascript
/** 不足の時間帯を一文にまとめる。色と模様だけに頼らず言葉でも届けるため。
 *  引数は _mergeHourlyGaps() の戻り値 [{start, end, gap}]（start/end は拡張時間の整数。25=翌1時）。
 *  例: 「22:00–翌02:00 が 1名不足」 */
function gapSummaryText(merged) {
  if (!merged.length) return '';
  const t = (h) => (h >= 24 ? `翌${_extHourLabel(h)}:00` : `${_extHourLabel(h)}:00`);
  const head = merged.slice(0, 2)
    .map((g) => `${t(g.start)}–${t(g.end)} が ${g.gap}名不足`)
    .join('、');
  return merged.length > 2 ? `${head}（ほか${merged.length - 2}件）` : head;
}
```

ダッシュボードの描画側では、既存の不足計算関数（`_computeHourlyGaps` は `public/app.js:605`、`_mergeHourlyGaps` は `:661` に実在する）を使って要約を作る。

```javascript
  const todayGaps = _mergeHourlyGaps(_computeHourlyGaps(todayShifts, todayStr, { includeRequested: true }));
  const shortageNote = todayGaps.length
    ? `<div class="dash-shortage-note">${esc(gapSummaryText(todayGaps))}</div>`
    : '';
```

対応する CSS:
```css
.dash-shortage-note { color: var(--alert); font-weight: 700; font-size: .8rem; margin: 2px 0 10px; }
```

- [ ] **Step 3: KPI カードを罫線区切りの帯に変える**

大きな数字カードを並べる作りをやめ、上端の罫線で区切った1本の帯にする。

```css
/* ---------- 16. KPI（配置表の従。大きなカードにしない） ---------- */
.kpi-strip {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  border-top: 1px solid var(--rule); margin-top: 18px;
}
.kpi-cell { padding: 12px 14px; border-right: 1px solid var(--rule); }
.kpi-cell:last-child { border-right: none; }
.kpi-label { font-size: .68rem; color: var(--ink-3); }
.kpi-value { font-family: var(--font-num); font-size: 1.2rem; font-weight: 700; margin-top: 2px; color: var(--ink); }
.kpi-value small { font-size: .7rem; font-weight: 400; color: var(--ink-3); margin-left: 2px; }
.kpi-cell.attention .kpi-value { color: var(--alert); }
```

既存の KPI カードのクラス名を使っている箇所があれば、そのクラス名を変えずに上の見た目を当てる（e2e が参照している可能性があるため）。まず `grep -n "kpi" public/app.js e2e/*.js` で使用箇所を確認してから決める。

- [ ] **Step 4: e2e を実行する**

Run: `npx playwright test`
Expected: **6 failed / 49 passed**。この6件は刷新の着手前から失敗している既存不具合（`draft_preserves_requests` 2件・`requests_cards` 3件・`timeline_visual:114` 1件）で、今回の変更とは無関係。**7件以上に増えていたら、それが今回の変更による回帰。**その場合はセレクタを壊していないか確認する（DOM 構造を変えたのが原因なら、クラス名を元に戻して CSS 側で対応する）。

- [ ] **Step 5: コミット**

```bash
git add public/app.js public/style.css
git commit -m "feat(design): ダッシュボードの主役を配置表にしKPIを従に配置"
```

---

## Task 11: 印刷 / PDF ビュー

**Files:**
- Modify: `public/style.css:872-1108`（セクション33 Print / PDF）

**Interfaces:**
- Consumes: Task 1 のトークン、Task 5 のロールクラス

**背景:** 印刷して壁に貼る運用が実在する。ロール色は淡いため、モノクロ印刷ではロールバッジの文字で判別できる必要がある。

- [ ] **Step 1: 印刷時のロール色を差し替える**

`style.css:938-940` の `.print-page .tl-bar.morning/.noon/.evening` を削除し、次を書く。`print-color-adjust` は色を落とさせないために必要。

```css
  .print-page .tl-bar.role-manager   { background: #B9CBE2 !important; color: #22364F !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .print-page .tl-bar.role-employee  { background: #C9DFE0 !important; color: #1D3D42 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .print-page .tl-bar.role-part-time { background: #F3DFA4 !important; color: #4A3A12 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .print-page .tl-bar.role-student   { background: #F2DADD !important; color: #4A2A2E !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
```

**注意:** 印刷 CSS ではトークンではなく生の16進数を書く。ダークテーマで印刷したときに暗い色が出るのを防ぐため。値は設計書 4.2 節（ライト）から引く。

- [ ] **Step 2: 印刷時は常にライトの地にする**

```css
@media print {
  html, html[data-theme="dark"] {
    --paper: #FBFAF6; --surface: #FFFFFF; --zebra: #F5F3EC;
    --ink: #243240; --ink-2: #5A6472; --ink-3: #8A8272;
    --rule: #E4E1D6; --grid-1h: #EEEBE1; --grid-4h: #DCD6C4;
    --daybreak: #B9AF98; --alert: #B14A35;
  }
}
```

- [ ] **Step 3: 印刷でもロール名が読めるようにする**

配置帯のスタッフ名の横にロールバッジを出す。`buildPrintTimelineHtml` の `.tl-name` を次に変える。

```javascript
    return `<div class="tl-row"><div class="tl-name">${esc(st.name)}<span class="tl-role-badge ${roleClass(st.role)}">${roleLabel(st.role)}</span></div><div class="tl-track" style="${trackVars}">${bars}</div></div>`;
```

`staffMap` に role を持たせる必要がある。`public/app.js:801` を次に変える。

```javascript
      staffMap[s.staff_id] = { name: s.staff_name || ('#' + s.staff_id), role: s.staff_role, shifts: [] };
```

画面用（1134行）の `staffMap` 構築にも同じく `role: s.staff_role,` を足す。

対応する CSS:
```css
.tl-role-badge {
  display: inline-block; margin-left: 4px; padding: 0 3px;
  border-radius: var(--radius-xs);
  font-size: .52rem; font-weight: 700; vertical-align: 1px;
}
```

- [ ] **Step 4: 印刷プレビューで確認する**

Run: サーバを起動し、シフト画面から印刷ビューを開いてブラウザの印刷プレビューを表示する。ライトとダークの両方で試す。
Expected: どちらのテーマから印刷しても紙の地で出る。ロールバッジの文字が読める。グレースケール印刷でもロールが判別できる。

- [ ] **Step 5: コミット**

```bash
git add public/style.css public/app.js
git commit -m "feat(design): 印刷ビューをロール色とロールバッジに対応

ダークテーマから印刷しても紙の地で出るよう@media printでトークンを固定。"
```

---

## Task 12: 全体検証と仕上げ

**Files:**
- Modify: 検証で見つかった箇所

- [ ] **Step 1: 全テストを実行する**

Run: `./.venv/bin/python -m pytest tests/ -v`
Expected: 全 PASS。`tests/test_design_tokens.py` を含む。

Run: `npx playwright test`
Expected: **6 failed / 49 passed**（刷新着手前と同じ）。内訳は `draft_preserves_requests` 2件・`requests_cards` 3件・`timeline_visual:114`（印刷画面のドラフト表示）1件。`fast_navigation:45` は flaky でリトライで通る。**これらは刷新とは無関係の既存不具合であり、この計画では直さない。**7件以上に増えていたら回帰なので原因を特定すること。

- [ ] **Step 2: 旧配色が1つも残っていないことを確認する**

Run:
```bash
grep -rnE "#6366F1|#818CF8|#4F46E5|#10B981|#34D399|#0F172A|#111827|#1F2937|#F59E0B|Inter|Space Grotesk" public/ | grep -v node_modules
```
Expected: 0件。ヒットしたら潰す。

- [ ] **Step 3: 狭い画面を確認する**

Run: ブラウザの開発者ツールで幅 375px にしてシフト画面を開く。
Expected: 1時間グリッドが潰れて灰色の面にならないこと。潰れる場合は、次を追加して細線を間引き4時間ごとに落とす。

```css
@media (max-width: 575px) {
  .tl-track {
    background-image: linear-gradient(90deg, var(--grid-4h) 1px, transparent 1px);
    background-size: calc(400% / var(--tl-hours, 22)) 100%;
  }
}
```

- [ ] **Step 4: キーボード操作を確認する**

Run: ログイン画面で Tab キーを押していく。
Expected: すべての入力とボタンにフォーカスリングが見える。`outline: none` だけを書いた箇所が残っていないこと。

Run: `grep -n "outline: *none\|outline:none" public/style.css`
Expected: 0件、または必ず `:focus-visible` で代替のリングを与えている箇所のみ。

- [ ] **Step 5: モーションの設定を尊重する**

`style.css` のセクション29（Animations）の末尾に無ければ追加する。

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .01ms !important;
    scroll-behavior: auto !important;
  }
}
```

- [ ] **Step 6: 両テーマを目視で確認する**

Run: サーバを起動し、ライトとダークで次の画面を開く。ログイン／ダッシュボード／シフト（タイムライン）／スタッフ管理／希望表管理／人件費分析／設定。
Expected: 文字が読めない箇所、色が浮く箇所、旧配色の残りがないこと。

- [ ] **Step 7: コミット**

```bash
git add -A
git commit -m "chore(design): 全体検証と仕上げ

狭い画面でのグリッド間引き、フォーカスリング、reduced-motion対応。"
```

---

## Self-Review

**1. Spec coverage** — 設計書の各節と対応するタスク:

| 設計書 | タスク |
|---|---|
| 2. デザインの方向・シグネチャ要素 | Task 6, 7 |
| 3. 決定事項（ロール別配色） | Task 5 |
| 4.1 色トークン | Task 1 |
| 4.2 ロール色 | Task 1, 5 |
| 4.3 書体 | Task 1 |
| 4.4 形と質感 | Task 1, 3 |
| 4.5 状態の質感 | Task 7 |
| 5. ダークテーマ | Task 1（値）、Task 12（検証） |
| 6. 影響範囲 style.css | Task 1, 2, 3, 4, 8, 11 |
| 6. 影響範囲 index.html | Task 1, 4 |
| 6. 影響範囲 app.js / Chart.js | Task 9 |
| 6. 影響範囲 ダッシュボード構成 | Task 10 |
| 6. 影響範囲 印刷 | Task 11 |
| 8. 検証 | Task 12 |

**2. 未解決として残す点**

`tests/test_design_tokens.py` の `_tokens_in_scope` は `\n}` で終わるブロックを正規表現で拾う。`:root` ブロック内にネストした波括弧を書くと壊れるため、トークン定義ブロックには入れ子を書かないこと（Task 1 の CSS はこれを満たしている）。

**3. 型・名前の一貫性**

`roleClass(role)` / `roleLabel(role)` / `statusClass(status)` / `cssVar(name)` / `cssVarAlpha(name, alpha)` は Task 5・7・9 で定義し、Task 6・10・11 で参照する。CSS クラス名は `role-manager` / `role-employee` / `role-part-time` / `role-student`（`part_time` ではなくハイフン）で全タスク統一。CSS 変数は `--role-part-time` でこれに揃えている。
