# Phase 0+1: 土台と印刷の修正 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 印刷が白紙になるバグを直し、アプリ内に用紙の縦／横切替を新設する。あわせて CI と印刷CSSの回帰テスト土台を作る。

**Architecture:** `#printView` は印刷ボタン押下時にしか組み立てられず `afterprint` で破棄されるため、印刷プレビューの再レンダリング（向き変更・用紙変更・倍率変更）・Ctrl+P・システムダイアログ・2回目の印刷がすべて白紙になる。印刷内容を `appState.printPayload` に保持し、`afterprint` で DOM を破棄しない方式に変える。向きは `<style id="printPageRule">` に `@page { size }` を注入して切り替え、`#printView[data-orientation]` で縦向き専用のレイアウト調整を行う。

**Tech Stack:** Python Flask / Vanilla JS / Bootstrap 5 / pytest / Playwright / GitHub Actions

## Global Constraints

- 新しい依存パッケージを追加しない（`requirements.txt` は Flask, python-dotenv, requests, pytest, gunicorn のみ）
- コード内コメントは日本語。「なぜ」を書く（What はコードを見れば分かる）
- コミットメッセージは `fix:` / `feat:` / `refactor:` / `test:` / `ci:` プレフィックス + 日本語サマリ
- Python の実行は必ず `.venv/bin/python`
- フロントエンドはモジュール化なし・グローバル関数。画面は `SCREENS.<name>`
- 既存の `@page { size: A4 landscape; margin: 10mm; }`（`public/style.css:1203`）は既定値として残す。JS で注入するルールが後勝ちで上書きする
- `public/app.js` を編集したら必ず `node --check public/app.js` を通す
- 各タスクの最後に `.venv/bin/python -m pytest tests/ -q` が全緑であることを確認する（基準: 1109 passed, 1 skipped）

---

### Task 1: CI を新設する

pytest が 13 秒で 1109 件全緑なのに CI が存在せず、手動実行に頼っている。以降のタスクで印刷CSSと `public/app.js` を触るため、先に自動実行の土台を作る。

**Files:**
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: なし
- Produces: `push` / `pull_request` で pytest と `node --check` が回る CI

- [ ] **Step 1: 現状のテストが全緑であることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/ -q
```
Expected: `1109 passed, 1 skipped`

- [ ] **Step 2: 構文チェックが通ることを確認する**

Run:
```bash
node --check public/app.js && node --check public/admin.js && echo OK
```
Expected: `OK`

- [ ] **Step 3: ワークフローを作成する**

Create `.github/workflows/test.yml`:

```yaml
# pytest は 1109 件が約 13 秒で終わるため、push のたびに全件回しても十分安い。
# E2E (Playwright) は約 1.5 分かかるうえサーバ起動を伴うので、ここには含めない。
name: test

on:
  push:
  pull_request:

jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - name: 依存パッケージをインストール
        run: pip install -r requirements.txt
      - name: ユニットテスト
        run: python -m pytest tests/ -q
      - name: シフトエンジン不変量テスト
        run: python tests/run_tests.py

  jscheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - name: フロントエンドの構文チェック
        run: |
          node --check public/app.js
          node --check public/admin.js
```

- [ ] **Step 4: `tests/run_tests.py` が CI で通ることをローカルで確認する**

Run:
```bash
.venv/bin/python tests/run_tests.py
```
Expected: 不変量テストが全て通る。失敗する場合は CI に含めず、ワークフローから「シフトエンジン不変量テスト」ステップを削除して理由を Step 5 のコミットメッセージに書く。

- [ ] **Step 5: コミット**

```bash
git add .github/workflows/test.yml
git commit -m "ci: pytest と構文チェックを GitHub Actions で回す

pytest は 1109 件が 13 秒で終わるのに CI が無く手動実行だった。
印刷CSSとフロントエンドに手を入れる前に自動実行の土台を作る。"
```

---

### Task 2: 印刷CSSをテスト対象に入れる

`tests/test_design_tokens.py:272` の `css.split("@media print")[0]` は、`@media print` が 2 箇所（`public/style.css:1015` と `1202`）あるため **1016〜1201 行目の画面用CSSまで検査対象から落としている**。波括弧の対応を数えて正確に分離するヘルパに置き換え、そのうえで印刷CSSの構造を守るテストを新設する。

**Files:**
- Modify: `tests/test_design_tokens.py:265-279`
- Create: `tests/test_print_view.py`

**Interfaces:**
- Consumes: なし
- Produces: `tests/test_print_view.py` の `_read_css()` / `_read_appjs()` / `_print_css()`（Task 4, 5 が使う）

- [ ] **Step 1: 現状の欠落を示す失敗テストを書く**

`tests/test_design_tokens.py` の末尾に追記:

```python
class TestMediaPrintSplit:
    """@media print の切り出しが正しいことを保証する。

    style.css には @media print が 2 箇所（アニメーション停止用と印刷レイアウト用）
    ある。単純な split("@media print")[0] だと 1 つ目以降の画面用CSSが丸ごと
    検査対象から落ちるため、波括弧の対応を数えて分離する。
    """

    def test_screen_css_keeps_rules_after_first_media_print(self):
        css = _read_css()
        screen, _printed = _split_media_print(css)
        # .matrix-input / .shortage-chip は 1 つ目の @media print（アニメーション停止、
        # style.css:1015）より後にある画面用CSS。単純 split ではここが丸ごと落ちる。
        # 落ちていたことを実証するため、旧実装との差も同時に確認する。
        assert ".matrix-input" in screen
        assert ".shortage-chip" in screen
        naive = css.split("@media print")[0]
        assert ".matrix-input" not in naive, \
            "旧実装でも拾えるセレクタでは、この回帰テストは何も守っていない"

    def test_print_css_contains_page_rule(self):
        css = _read_css()
        _screen, printed = _split_media_print(css)
        assert "@page" in printed

    def test_screen_css_excludes_print_only_rules(self):
        css = _read_css()
        screen, _printed = _split_media_print(css)
        # 印刷ブロック内にしか存在しないセレクタが画面側に混ざっていないこと
        assert ".print-page-header" not in screen
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_design_tokens.py::TestMediaPrintSplit -v
```
Expected: FAIL with `NameError: name '_split_media_print' is not defined`

- [ ] **Step 3: 分離ヘルパを実装する**

`tests/test_design_tokens.py` の既存ヘルパ群（`_read_css` の隣）に追加:

```python
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _split_media_print(css):
    """CSS を (画面用, 印刷用) に分ける。

    @media print は複数箇所にあるため split では足りない。開き波括弧から
    対応する閉じ波括弧までを数え、ブロックごとに正確に切り出す。

    先にコメントを落とすのが要。style.css:998 のようにコメント文中へ
    "@media print" と書かれている箇所があり、落とさないとそこをブロック開始と
    誤認して、直後の画面用ルールを印刷ブロックとして取り込んでしまう
    （＝その範囲がコントラスト検査から漏れる）。

    戻り値の印刷用は @media print の中身のみ（外側の波括弧を含まない）。
    """
    css = _CSS_COMMENT_RE.sub("", css)
    screen_parts, print_parts = [], []
    i = 0
    while True:
        j = css.find("@media print", i)
        if j < 0:
            screen_parts.append(css[i:])
            break
        screen_parts.append(css[i:j])
        open_at = css.find("{", j)
        if open_at < 0:                      # 壊れたCSS。残り全部を画面側として扱う
            screen_parts.append(css[j:])
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
        print_parts.append(css[open_at + 1:m])
        i = m + 1
    return "".join(screen_parts), "\n".join(print_parts)
```

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_design_tokens.py::TestMediaPrintSplit -v
```
Expected: PASS（3件）

- [ ] **Step 5: 既存テストを新ヘルパに切り替える**

`tests/test_design_tokens.py` の `test_no_white_literal_on_token_fill` 内を書き換える:

```python
        # @media print は常にライトのトークン値で固定されるため対象外。
        # ただし @media print は複数箇所にあるので、波括弧対応で正確に除く。
        screen_css, _printed = _split_media_print(css)
```

（`screen_css = css.split("@media print")[0]` の行を上記に置き換える）

- [ ] **Step 6: 既存テストが引き続き通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_design_tokens.py -q
```
Expected: 全件 PASS。もし `test_no_white_literal_on_token_fill` が新たに失敗するなら、それは**これまで検査から漏れていた本物の違反**。違反行を修正してから進む。

- [ ] **Step 7: 印刷ビューの構造テストを新設する**

Create `tests/test_print_view.py`:

```python
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
```

- [ ] **Step 8: 新テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_print_view.py -v
```
Expected: PASS（3件）

- [ ] **Step 9: 全体が緑であることを確認してコミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add tests/test_design_tokens.py tests/test_print_view.py
git commit -m "test: 印刷CSSを検査対象に入れる

@media print は style.css に 2 箇所あるため split(\"@media print\")[0] では
1016〜1201行目の画面用CSSまで検査から落ちていた。波括弧の対応を数えて
正確に分離するヘルパに置き換え、印刷ビューの構造テストを新設する。"
```

---

### Task 3: 印刷が白紙になるバグを直す

**再現条件**: 印刷ボタン → プレビューで向き/用紙/倍率を変更、または Ctrl+P、または「システムダイアログを使用して印刷」、または 2 回目の印刷。いずれも `#printView` が空のまま印刷され、`@media print` が `#appView` を消しているため紙面は完全な白紙になる。

**原因**: `public/app.js:975-978` の `afterprint` が印刷用DOMを破棄し、`beforeprint` による再構築が存在しない。

**方針**: 印刷内容を `appState.printPayload` に保持し、`afterprint` で DOM を破棄しない。加えて `beforeprint` でも空なら payload から組み立て直す。ブラウザが `beforeprint` を発火せずにプレビューを再描画する場合にも耐えるため、**DOM を残す方を主、`beforeprint` を保険**とする。

**Files:**
- Modify: `public/app.js:975-978`（`afterprint` リスナ）
- Modify: `public/app.js:1144-1148`（`openPrintView` の DOM 反映部）
- Create: `e2e/print_view.spec.js`
- Modify: `tests/test_print_view.py`（構造テストを追加）

**Interfaces:**
- Consumes: `tests/test_print_view.py` の `_read_appjs()` / `_print_css()`（Task 2 で作成）
- Produces: `appState.printPayload = { start, end, html }`（Task 5 が向き切替時に参照する）

- [ ] **Step 1: E2E の再現テストを書く**

Create `e2e/print_view.spec.js`:

```js
/**
 * e2e/print_view.spec.js — 印刷ビューの回帰テスト。
 *
 * 既存の timeline_visual.spec.js は window.print を潰しているため
 * afterprint が一切発火せず、「2回目の印刷が白紙になる」バグを構造的に
 * 検出できなかった。ここでは print イベントを自前で dispatch して
 * 印刷用DOMの生存を直接検証する。
 *
 * 実行: npx playwright test e2e/print_view.spec.js
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager } = require('./helpers');

const SHOP = {
  shopCode: 'PRINT1',
  shopName: '印刷テスト店',
  managerCode: 'mgr1',
  managerPassword: 'mgr1pass',
  managerName: '印刷店長',
};

test.beforeEach(async ({ page, request }) => {
  await ensureShop(request, SHOP);
  // window.print はダイアログを開いてテストを止めるので無害化する。
  // ただし beforeprint / afterprint は自前で dispatch して挙動を見る。
  await page.addInitScript(() => { window.print = () => {}; });
  await loginAsManager(page, {
    shopCode: SHOP.shopCode,
    managerCode: SHOP.managerCode,
    password: SHOP.managerPassword,
  });
  await page.click('.side-item[data-screen="shifts"]');
  await page.waitForSelector('#printBtn');
});

async function openPrint(page) {
  // 期間を1日に絞る（シフトが無くても print-page は日数分生成される）
  await page.fill('#sStart', '2026-08-03');
  await page.fill('#sEnd', '2026-08-03');
  await page.click('#printBtn');
  await page.waitForFunction(() => {
    const pv = document.getElementById('printView');
    return pv && pv.querySelectorAll('.print-page').length > 0;
  }, { timeout: 10000 });
}

test('印刷ボタンで印刷用DOMが組み立てられる', async ({ page }) => {
  await openPrint(page);
  await expect(page.locator('#printView .print-page')).toHaveCount(1);
});

test('afterprint の後もプレビュー再描画で内容が残る', async ({ page }) => {
  await openPrint(page);

  // ブラウザの印刷プレビューは向き・用紙・倍率を変えるたびにライブDOMから
  // 再レンダリングする。afterprint で消してしまうとその瞬間に白紙になる。
  await page.evaluate(() => window.dispatchEvent(new Event('afterprint')));
  await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));

  const count = await page.locator('#printView .print-page').count();
  expect(count).toBe(1);
});

test('beforeprint が発火しない再描画でも内容が残る', async ({ page }) => {
  await openPrint(page);

  // beforeprint を伴わない再描画（Chrome のプレビュー再生成など）を模す
  await page.evaluate(() => window.dispatchEvent(new Event('afterprint')));

  const count = await page.locator('#printView .print-page').count();
  expect(count).toBe(1);
});

test('印刷メディアで印刷ビューが表示され画面アプリが隠れる', async ({ page }) => {
  await openPrint(page);
  await page.emulateMedia({ media: 'print' });

  await expect(page.locator('#printView')).toBeVisible();
  await expect(page.locator('#appView')).toBeHidden();

  await page.emulateMedia({ media: 'screen' });
});
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
npx playwright test e2e/print_view.spec.js
```
Expected: 「afterprint の後も…」と「beforeprint が発火しない…」の 2 件が FAIL（`expect(count).toBe(1)` に対し `0`）。残り 2 件は PASS。

失敗しない場合は、テストが実装に届いていない（ログイン失敗・画面遷移失敗）ことを疑う。`page.content()` をダンプして確認すること。ナビは `renderNav()`（`public/app.js:615-643`）が `.side-item[data-screen="<key>"]` を生成する。E2E のビューポートは 1280x800（PC）なのでサイドバーが見えている。

- [ ] **Step 3: `openPrintView` が payload を保持するようにする**

`public/app.js:1144-1148` を置き換える:

```js
    const pv = document.getElementById('printView');
    // 印刷用DOMは印刷が終わっても消さない（appState にも保持する）。
    // ブラウザの印刷プレビューは向き・用紙・倍率を変えるたびにライブDOMから
    // 再レンダリングするため、afterprint で消すと2回目以降が白紙になる。
    // @media print が #appView を display:none にしているので代替表示も無い。
    appState.printPayload = { start, end, html: pagesHtml };
    pv.innerHTML = pagesHtml;
    setLoading(false);
    // レンダリングを1フレーム待ってから印刷ダイアログを開く
    requestAnimationFrame(() => requestAnimationFrame(() => window.print()));
```

- [ ] **Step 4: `afterprint` を `beforeprint` に置き換える**

`public/app.js:975-978` を置き換える:

```js
// 印刷用DOMは afterprint で破棄しない。
// ブラウザの印刷プレビューは向き・用紙・倍率・余白を変更するたびに
// ライブDOMから再レンダリングする。破棄してしまうと、向きを変えた瞬間・
// Ctrl+P・「システムダイアログを使用して印刷」・2回目の印刷がすべて白紙になる。
// beforeprint は「何らかの理由で空になっていた場合」の保険として置く。
window?.addEventListener('beforeprint', () => {
  const pv = document.getElementById('printView');
  if (!pv || pv.innerHTML.trim()) return;
  const payload = appState.printPayload;
  if (payload && payload.html) pv.innerHTML = payload.html;
});
```

- [ ] **Step 5: 構文チェック**

Run:
```bash
node --check public/app.js && echo OK
```
Expected: `OK`

- [ ] **Step 6: E2E が通ることを確認する**

Run:
```bash
npx playwright test e2e/print_view.spec.js
```
Expected: 4 件すべて PASS

- [ ] **Step 7: 構造的な回帰防止テストを追加する**

`tests/test_print_view.py` に追記:

```python
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
        assert "printPayload" in _read_appjs()
```

- [ ] **Step 8: 新テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_print_view.py -v
```
Expected: PASS（6件）

- [ ] **Step 9: 既存E2Eが壊れていないことを確認する**

Run:
```bash
npx playwright test e2e/timeline_visual.spec.js
```
Expected: 全件 PASS

- [ ] **Step 10: 全体が緑であることを確認してコミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add public/app.js tests/test_print_view.py e2e/print_view.spec.js
git commit -m "fix(print): 印刷が白紙になる問題を修正

#printView は印刷ボタン押下時にしか組み立てられず afterprint で破棄されて
いた。ブラウザの印刷プレビューは向き・用紙・倍率を変えるたびにライブDOMから
再レンダリングするため、向きを変えた瞬間に白紙になっていた。同じ理由で
Ctrl+P・システムダイアログからの印刷・2回目の印刷もすべて白紙だった。

印刷内容を appState.printPayload に保持し、afterprint では破棄しないように
する。beforeprint は空だった場合の保険として置く。

既存E2Eは window.print を潰していて afterprint が発火せず本バグを検出
できなかったため、print イベントを自前で dispatch する e2e を新設した。"
```

---

### Task 4: 印刷時のクリップを解消する

画面用の `.tl-wrap { overflow-x: auto }`（`public/style.css:859`）と `.tl-axis-row / .tl-row { min-width: 480px }`（`public/style.css:860, 892`）は印刷ブロックで上書きされておらず、印刷レンダリング時も生きている。印刷ビューポート幅が 480px を下回ると（縮小率・小型用紙・縦向き）、あふれた分は横方向にページ分割されず**切り捨てられる**。

**Files:**
- Modify: `public/style.css:1250-1251, 1268`（印刷ブロック内のタイムライン上書き）
- Modify: `tests/test_print_view.py`

**Interfaces:**
- Consumes: `tests/test_print_view.py` の `_print_css()`
- Produces: なし

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_print_view.py` に追記:

```python
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
        css = _print_css()
        assert re.search(r"\.print-page\s+\.tl-row[^{]*\{[^}]*min-width:\s*0", css) \
            or re.search(r"\.print-page\s+\.tl-axis-row[^{]*\{[^}]*min-width:\s*0", css), \
            "印刷で .tl-row / .tl-axis-row の min-width を解除していない"
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_print_view.py::TestPrintTimelineNotClipped -v
```
Expected: FAIL（2件とも）

- [ ] **Step 3: 印刷CSSを修正する**

`public/style.css` の印刷ブロック内、`.print-page .tl-wrap { padding: 0; }` の行を次に置き換える:

```css
  /* 画面用の overflow-x:auto と min-width:480px は印刷でも生きており、
     用紙幅や縮小率で印刷ビューポートが 480px を割ると帯の右側が
     ページ分割されずに切り捨てられる。印刷では両方とも解除する。 */
  .print-page .tl-wrap { padding: 0; overflow-x: visible; }
  .print-page .tl-axis-row { margin-bottom: 6px; min-width: 0; }
```

そして既存の `.print-page .tl-axis-row { margin-bottom: 6px; }` の行を削除し、`.print-page .tl-row { margin-bottom: 8px; }` を次に置き換える:

```css
  .print-page .tl-row { margin-bottom: 8px; min-width: 0; }
```

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_print_view.py -v
```
Expected: PASS（8件）

- [ ] **Step 5: 印刷メディアで実際にクリップされないことを確認する**

`e2e/print_view.spec.js` に追記:

```js
test('印刷メディアでタイムラインが横にはみ出さない', async ({ page }) => {
  await openPrint(page);
  await page.emulateMedia({ media: 'print' });

  const overflow = await page.evaluate(() => {
    const wrap = document.querySelector('#printView .tl-wrap');
    if (!wrap) return null;
    return { scrollWidth: wrap.scrollWidth, clientWidth: wrap.clientWidth };
  });
  expect(overflow).not.toBeNull();
  // 中身が枠に収まっていること（切り捨てが起きていない）
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);

  await page.emulateMedia({ media: 'screen' });
});
```

Run:
```bash
npx playwright test e2e/print_view.spec.js
```
Expected: 5 件すべて PASS

- [ ] **Step 6: 全体が緑であることを確認してコミット**

```bash
.venv/bin/python -m pytest tests/ -q
node --check public/app.js
git add public/style.css tests/test_print_view.py e2e/print_view.spec.js
git commit -m "fix(print): 用紙幅によってタイムラインが切り捨てられる問題を修正

画面用の .tl-wrap{overflow-x:auto} と .tl-row{min-width:480px} が印刷でも
生きており、印刷ビューポートが 480px を割ると帯の右側がページ分割されずに
消えていた。印刷ブロックで両方を解除する。"
```

---

### Task 5: 用紙の縦／横切替を新設する

アプリに向きの切替UIは存在せず、`@page` は `A4 landscape` 固定（`public/style.css:1203`）。店長はブラウザ側のドロップダウンを探して操作し、その結果 Task 3 のバグを踏んだ。アプリ内に切替を用意する。

**Files:**
- Modify: `public/app.js`（`openPrintView` の直前にヘルパ群を追加、`SCREENS.shifts` のツールバー、`printBtn` のバインド部）
- Modify: `public/style.css`（印刷ブロックに縦向き用の調整を追加）
- Modify: `e2e/print_view.spec.js`
- Modify: `tests/test_print_view.py`

**Interfaces:**
- Consumes: `appState.printPayload`（Task 3 で導入）
- Produces:
  - `getPrintOrientation() -> 'landscape' | 'portrait'`
  - `setPrintOrientation(value: string) -> void`
  - `applyPrintOrientation() -> void`
  - DOM: `<style id="printPageRule">`、`#printView[data-orientation]`、`#printOrientBtn`、`#printOrientLabel`

- [ ] **Step 1: E2E の失敗テストを書く**

`e2e/print_view.spec.js` に追記:

```js
test('用紙の向きを切り替えられる', async ({ page }) => {
  // 既定は横
  await expect(page.locator('#printOrientLabel')).toHaveText('横');

  await page.click('#printOrientBtn');
  await expect(page.locator('#printOrientLabel')).toHaveText('縦');

  const rule = await page.evaluate(() => {
    const st = document.getElementById('printPageRule');
    return st ? st.textContent : null;
  });
  expect(rule).toContain('A4 portrait');

  const orient = await page.getAttribute('#printView', 'data-orientation');
  expect(orient).toBe('portrait');
});

test('選んだ向きは再読み込み後も保たれる', async ({ page }) => {
  await page.click('#printOrientBtn');
  await expect(page.locator('#printOrientLabel')).toHaveText('縦');

  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="shifts"]');
  await page.waitForSelector('#printOrientBtn');

  await expect(page.locator('#printOrientLabel')).toHaveText('縦');
});
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
npx playwright test e2e/print_view.spec.js
```
Expected: 新規 2 件が FAIL（`#printOrientLabel` が見つからない）

- [ ] **Step 3: 向き切替のヘルパを実装する**

`public/app.js` の `openPrintView` 定義の直前（`function isAiDraftShift` より前、印刷セクション内）に追加:

```js
const PRINT_ORIENTATION_KEY = 'shift_print_orientation';

/** 保存されている用紙の向きを返す。既定は横（従来の @page と同じ）。 */
function getPrintOrientation() {
  try {
    return localStorage.getItem(PRINT_ORIENTATION_KEY) === 'portrait' ? 'portrait' : 'landscape';
  } catch (e) {
    return 'landscape';  // プライベートモード等で localStorage が使えない場合
  }
}

/** 用紙の向きを保存して即座に反映する。 */
function setPrintOrientation(value) {
  const o = value === 'portrait' ? 'portrait' : 'landscape';
  try { localStorage.setItem(PRINT_ORIENTATION_KEY, o); } catch (e) { /* 保存できなくても表示は切り替える */ }
  applyPrintOrientation();
}

/** @page の size を差し替え、印刷ビューに向きを伝える。
 *  style.css の @page（A4 landscape）より後に挿入されるため後勝ちで上書きされる。 */
function applyPrintOrientation() {
  const o = getPrintOrientation();
  let st = document.getElementById('printPageRule');
  if (!st) {
    st = document.createElement('style');
    st.id = 'printPageRule';
    document.head.appendChild(st);
  }
  st.textContent = `@media print { @page { size: A4 ${o}; margin: 10mm; } }`;
  const pv = document.getElementById('printView');
  if (pv) pv.dataset.orientation = o;
  const label = document.getElementById('printOrientLabel');
  if (label) label.textContent = (o === 'portrait' ? '縦' : '横');
}
```

- [ ] **Step 4: ツールバーにボタンを追加する**

`public/app.js` の `SCREENS.shifts` 内、印刷ボタンの行を置き換える:

```js
        <button class="btn btn-light" id="printBtn" title="印刷"><i class="bi bi-printer"></i> 印刷</button>
        <button class="btn btn-light" id="printOrientBtn" title="用紙の向きを切り替える"><i class="bi bi-arrow-repeat"></i> <span id="printOrientLabel">横</span></button>
```

（元の `<button class="btn btn-light" id="printBtn"><i class="bi bi-printer"></i></button>` を上記 2 行に差し替える。元のボタンはアイコンのみでラベルが無く、何のボタンか分からなかったため「印刷」の文字も足す）

- [ ] **Step 5: バインドを追加する**

`public/app.js` の `printBtn` のクリックハンドラの直後に追加:

```js
  document.getElementById('printOrientBtn')?.addEventListener('click', () => {
    setPrintOrientation(getPrintOrientation() === 'portrait' ? 'landscape' : 'portrait');
    toast(`用紙を${getPrintOrientation() === 'portrait' ? '縦' : '横'}向きにしました`);
  });
  // 画面を開いた時点で保存済みの向きをボタンラベルと @page に反映する
  applyPrintOrientation();
```

- [ ] **Step 6: 縦向き用のCSSを追加する**

`public/style.css` の印刷ブロック内、`.print-page .tl-name { ... }` の直後に追加:

```css
  /* 縦向きは印刷可能幅が狭い（A4縦 210mm - 余白20mm = 190mm ≒ 718px）ため、
     名前欄と時間軸を詰めて帯の描画幅を確保する。 */
  .print-view[data-orientation="portrait"] .tl-name { width: 56px; flex: 0 0 56px; font-size: 10px; }
  .print-view[data-orientation="portrait"] .tl-hour { font-size: 8px; padding-left: 1px; }
  .print-view[data-orientation="portrait"] .print-page-header h2 { font-size: 16px; }
  .print-view[data-orientation="portrait"] .print-page-header .print-shop { font-size: 12px; }
```

- [ ] **Step 7: 構文チェック**

Run:
```bash
node --check public/app.js && echo OK
```
Expected: `OK`

- [ ] **Step 8: E2E が通ることを確認する**

Run:
```bash
npx playwright test e2e/print_view.spec.js
```
Expected: 7 件すべて PASS

- [ ] **Step 9: 構造テストを追加する**

`tests/test_print_view.py` に追記:

```python
class TestPrintOrientation:
    """用紙の向き切替が実装されていること。"""

    def test_orientation_helpers_exist(self):
        js = _read_appjs()
        for name in ("getPrintOrientation", "setPrintOrientation", "applyPrintOrientation"):
            assert name in js, f"{name} が実装されていない"

    def test_orientation_toggle_button_exists(self):
        assert "printOrientBtn" in _read_appjs()

    def test_portrait_layout_rules_exist(self):
        assert 'data-orientation="portrait"' in _print_css(), \
            "縦向き用のレイアウト調整が印刷CSSに無い"
```

- [ ] **Step 10: 新テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_print_view.py -v
```
Expected: PASS（11件）

- [ ] **Step 11: 縦横それぞれの印刷結果を目視確認する**

Run:
```bash
PORT=5555 FLASK_DEBUG=1 .venv/bin/python src/app.py
```

ブラウザで `http://localhost:5555` を開き、店長でログイン → シフト管理 → 期間を1週間に設定 → 「横」のまま印刷 → プレビューで内容が出ることを確認 → **プレビューを開いたまま向きを縦に変更して、内容が消えないことを確認** → 一度閉じてから「縦」ボタンを押して印刷し、名前欄が詰まって帯が収まっていることを確認。

スクリーンショットを `screenshots/print-landscape.png` と `screenshots/print-portrait.png` に保存する（`screenshots/` は `.gitignore` 済み）。

- [ ] **Step 12: 全体が緑であることを確認してコミット**

```bash
.venv/bin/python -m pytest tests/ -q
node --check public/app.js
git add public/app.js public/style.css tests/test_print_view.py e2e/print_view.spec.js
git commit -m "feat(print): 用紙の縦／横切替をアプリ内に新設

これまで @page は A4 landscape 固定で、アプリ内に向きの切替UIが無かった。
店長はブラウザ側のドロップダウンを探して操作し、その結果 #printView が
破棄されて白紙になるバグを踏んでいた。

印刷ボタンの隣にトグルを置き、@page の size を差し替える。選択は
localStorage に保持する。縦向きは印刷可能幅が狭いため名前欄と時間軸を
詰めるレイアウト調整を入れた。あわせて、アイコンのみで何のボタンか
分からなかった印刷ボタンに「印刷」のラベルを追加した。"
```

---

## Self-Review

**仕様カバレッジ（設計書 Phase 0 と Phase 1）**

| 設計書の項目 | 対応タスク |
|---|---|
| 0-1. CI | Task 1 |
| 0-2. 印刷CSSをテスト対象に入れる | Task 2 |
| 1-1. 根本原因 | Task 3（原因の記述） |
| 1-2. `beforeprint` で再構築 | Task 3 Step 3-4 |
| 1-3. 縦／横切替の新設 | Task 5 |
| 1-4. クリップの解消 | Task 4 |
| 1-5. テスト | Task 2（静的）、Task 3-5（E2E + 静的） |

**設計書からの変更点（実装時に判断を変えた箇所）**

設計書 1-2 では「`beforeprint` で再構築する」を主としていたが、Chrome が `beforeprint` を伴わずにプレビューを再描画する可能性があるため、**「`afterprint` で破棄しない」を主、`beforeprint` を保険**に変えた。イベントの発火順に依存しないぶん堅い。Task 3 の E2E には「`beforeprint` が発火しない再描画でも内容が残る」ケースを含めてある。

**型・名前の整合**

- `appState.printPayload`（Task 3 Step 3 で導入）を Task 5 は参照しない（向き切替は `@page` と `data-orientation` のみを触る）。読み取り専用の依存もないため衝突しない。
- `getPrintOrientation` / `setPrintOrientation` / `applyPrintOrientation` は Task 5 内で完結。
- `_read_css()` / `_read_appjs()` / `_print_css()` は `tests/test_print_view.py` に定義（Task 2 Step 7）。Task 3, 4, 5 が同ファイル内で使うため import 不要。
- `_split_media_print()` は `tests/test_design_tokens.py` に定義（Task 2 Step 3）。`tests/test_print_view.py` の `_print_css()` とはロジックが重複するが、テストファイル間の import 依存を作らないほうが壊れにくいため意図的に分けている。

**残る不確実性**

Task 3 Step 2 で「2 件が FAIL する」と書いたが、`window.dispatchEvent(new Event('afterprint'))` が実装のリスナを起動することは確実である一方、`beforeprint` を発火させないケース（3 件目のテスト）は修正前でも修正後でも DOM の状態だけで決まる。修正前は `afterprint` で消えるので FAIL、修正後は消えないので PASS。想定どおり。

もし Step 2 で FAIL しない場合は、テストが実装に届いていない（セレクタ違い・ログイン失敗）ことを疑い、`page.content()` をダンプして確認すること。**FAIL を確認せずに Step 3 へ進んではいけない。**
