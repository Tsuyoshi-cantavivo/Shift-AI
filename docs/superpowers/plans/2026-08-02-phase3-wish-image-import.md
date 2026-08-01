# Phase 3: 希望表の画像取込と名前のあいまいマッピング 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 希望表の画像を貼り付け・選択・撮影して取り込めるようにし、読み取った名前をスタッフに自動で突き合わせて確認画面で承認させる。

**Architecture:** 既存の希望テキスト取込（`POST /api/shop/wishes/parse` → 確認画面 → `POST /api/shop/wishes/bulk`）の**前段だけ**を足す。画像は OpenAI 互換の Chat Completions に vision 形式で送り、返ってきた結果を既存の `_sanitize_llm_wish_result` に通して同じ契約に合流させる。確認画面以降は一切変更しない。名前の突き合わせは依存を増やさない純関数モジュールとして切り出し、テキスト経路にも適用する。

**Tech Stack:** Python Flask / Vanilla JS / pytest / Playwright

## Global Constraints

- **新しい依存パッケージを追加しない。** `requirements.txt` は Flask, python-dotenv, requests, pytest, gunicorn のみ。Pillow も OCR ライブラリも入れない
- コード内コメントは日本語。「なぜ」を書く
- コミットメッセージは `feat:` / `fix:` / `test:` プレフィックス + 日本語サマリ、末尾に `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Python の実行は必ず `.venv/bin/python`
- `public/app.js` を編集したら必ず `node --check public/app.js` を通す
- **Playwright は必ずフォアグラウンドで実行する**（`timeout` を 600000 に）。バックグラウンド実行は通知が届かず作業が止まる
- **画像は保存しない。** メモリ上で処理して破棄する。ログにも base64 を出さない
- **基準値（Phase 3 開始時点）**: pytest `1190 passed, 1 skipped` / E2E `133 passed` / `tests/run_tests.py` 全 PASS
- 各タスクの最後に pytest 全件・`node --check`・`tests/run_tests.py` を確認する
- **テストは「実装のどの行を消せば落ちるか」を言える形で書く。** Phase 1・2 では「緑だが何も守っていないテスト」が繰り返し見つかった（旧実装でも通る値／分岐に到達しない／閾値に届かない／Playwright の自己修復／粗い壊し方で部分的な壊れを見逃す／機能の対称な半分が無検証）。各テストについて**守りたい実装の1行だけを消して**赤になることを確認し、実出力をレポートに貼ること

## 前提となる調査結果（コードで確認済み・行番号は 2026-08-02 時点）

- `POST /api/shop/wishes/parse`（`src/app.py:3313-3361`）は解析のみでDBに書かない。`year_month` / `staff_id` の検証を持つ
- 名前の突き合わせは `by_name = {s["name"]: s["id"] ...}` の**完全一致のみ**（`src/app.py:3347, 3361`）。正規化なし
- `staffs` の名前関連カラムは `name` の1つだけ（`schema.sql:27-42`）。kana / nickname は無い
- `_wish_raw_norm`（`src/app.py:3271`）は NFKC + casefold + 空白/引用符除去。`_wish_raw_verified`（`:3284`）が「AIが創作した文」を検出する安全弁で、**貼付テキストとの照合が前提**
- `_post_llm`（`src/ai.py:75`）は `messages` を無検査でJSONシリアライズする。`content` を配列にすれば **vision がそのまま通る**。`timeout=30` 固定・リトライなし
- `_call_llm_messages`（`src/ai.py:580`）が messages 配列を受ける入口
- `_sanitize_llm_wish_result`（`src/ai.py:1394`）が entries/unparsed の契約を守る。**検証で落ちた entry は捨てず必ず unparsed に積む**のが既存の原則
- `_extract_staff_hint`（`src/ai.py:1067`）は候補が2件以上ヒットしたら `None` を返す。「名簿順で勝手に決めない」不変量は `tests/test_wish_text_import.py::TestStaffHintAmbiguity` が固定
- `LLM_MODEL` 既定は `gpt-4o-mini`（`.env.example:39`）＝画像入力対応
- フロント: `_wtiRenderStep1`（`public/app.js:3259`）が貼付欄、`_wtiParse`（`:3284`）が API 呼び出し、`_wtiRenderUnassigned`（`:3679`）が未割り当ての `<select>`
- `api()`（`public/app.js:42`）は `Content-Type: application/json` 決め打ち。**FormData を送る経路が無い**ので base64 を JSON に載せる
- Flask に `MAX_CONTENT_LENGTH` は**未設定**

## スコープ外

- 画像の保存・再表示（ユーザー判断で「保存しない」に決定済み）
- スタッフのカナ・愛称カラムの追加（スキーマ変更が必要）
- 名前の自動確定（ユーザー判断で「確認画面で人が承認」に決定済み）

---

### Task 1: 名前のあいまいマッピング（純関数）

依存を増やさない純関数モジュールを作る。テキスト経路・画像経路の両方から使う。

**Files:**
- Create: `src/name_match.py`
- Create: `tests/test_name_match.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - `normalize_name(s: str) -> str`
  - `match_staff(hint: str, staffs: list[dict]) -> list[dict]` — `[{"staff_id": int, "name": str, "score": float, "reason": str}]` をスコア降順で返す。0.6 未満は含めない
  - `best_exact(hint: str, staffs: list[dict]) -> int | None` — 正規化完全一致が**ちょうど1件**のときだけ staff_id を返す。0件・2件以上は None

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_name_match.py`:

```python
"""tests/test_name_match.py — 希望表の名前をスタッフに突き合わせる純関数。

実行: ./.venv/bin/python -m pytest tests/test_name_match.py -v

背景: 従来は by_name の完全一致のみ（src/app.py:3347）だったため、
「田中さん」「タナカ」のような表記ゆれが全部未割り当てになっていた。
OCR は表記ゆれ・誤認識を起こしやすいので、候補を出す仕組みが要る。

不変量: 同点の最有力候補が2件以上あるときは自動確定しない。
既存の _extract_staff_hint（src/ai.py:1087）が守っている
「名簿順で勝手に決めない」原則を引き継ぐ。
"""
import pytest

from src.name_match import normalize_name, match_staff, best_exact


def _staffs(*names):
    return [{"id": i + 1, "name": n} for i, n in enumerate(names)]


class TestNormalizeName:
    def test_full_width_becomes_half_width(self):
        assert normalize_name("ﾀﾅｶ") == normalize_name("タナカ")

    def test_spaces_are_removed(self):
        assert normalize_name("田中 太郎") == normalize_name("田中　太郎") == normalize_name("田中太郎")

    def test_honorifics_are_removed(self):
        base = normalize_name("田中")
        for suffix in ("さん", "サン", "くん", "君", "ちゃん", "様", "氏"):
            assert normalize_name("田中" + suffix) == base, f"{suffix} が除去されていない"

    def test_hiragana_and_katakana_match(self):
        assert normalize_name("たなか") == normalize_name("タナカ")

    def test_case_is_folded(self):
        assert normalize_name("Tanaka") == normalize_name("TANAKA")

    def test_empty_and_none_do_not_crash(self):
        assert normalize_name("") == ""
        assert normalize_name(None) == ""


class TestBestExact:
    def test_normalized_exact_match_resolves(self):
        st = _staffs("田中太郎", "佐藤花子")
        assert best_exact("田中太郎さん", st) == 1
        assert best_exact("たなかたろう", st) is None  # 読みは完全一致ではない

    def test_two_identical_names_do_not_resolve(self):
        """同姓同名が2人いたら自動確定しない（誤配属を防ぐ）。"""
        st = _staffs("田中", "田中")
        assert best_exact("田中", st) is None

    def test_no_match_returns_none(self):
        assert best_exact("存在しない", _staffs("田中太郎")) is None

    def test_empty_hint_returns_none(self):
        assert best_exact("", _staffs("田中太郎")) is None
        assert best_exact(None, _staffs("田中太郎")) is None


class TestMatchStaff:
    def test_exact_match_scores_highest(self):
        r = match_staff("田中太郎", _staffs("田中太郎", "田中花子"))
        assert r[0]["staff_id"] == 1
        assert r[0]["score"] == 1.0

    def test_surname_only_is_a_candidate(self):
        r = match_staff("田中", _staffs("田中太郎", "佐藤花子"))
        assert r, "姓のみでも候補に挙がるべき"
        assert r[0]["staff_id"] == 1
        assert 0.6 <= r[0]["score"] < 1.0

    def test_given_name_only_is_a_candidate(self):
        r = match_staff("太郎", _staffs("田中太郎", "佐藤花子"))
        assert r
        assert r[0]["staff_id"] == 1

    def test_typo_within_edit_distance_is_a_candidate(self):
        r = match_staff("田中太朗", _staffs("田中太郎", "佐藤花子"))
        assert r
        assert r[0]["staff_id"] == 1

    def test_unrelated_name_is_not_a_candidate(self):
        r = match_staff("山田", _staffs("田中太郎", "佐藤花子"))
        assert not r, f"無関係な名前が候補に出た: {r}"

    def test_results_are_sorted_by_score_desc(self):
        r = match_staff("田中", _staffs("佐藤花子", "田中太郎", "田中"))
        scores = [c["score"] for c in r]
        assert scores == sorted(scores, reverse=True)

    def test_each_candidate_has_a_reason(self):
        r = match_staff("田中", _staffs("田中太郎"))
        assert r[0]["reason"], "候補に理由が付いていないと UI で説明できない"

    def test_empty_hint_returns_empty(self):
        assert match_staff("", _staffs("田中太郎")) == []
        assert match_staff(None, _staffs("田中太郎")) == []

    def test_resigned_staff_are_not_passed_in(self):
        """呼び出し側が在籍者だけを渡す契約であることを、空リストで確認する。"""
        assert match_staff("田中", []) == []
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_name_match.py -v
```
Expected: 全件 FAIL（`ModuleNotFoundError: No module named 'src.name_match'`）

`from src.name_match import ...` が通らない場合は、他のテストのインポート作法（`tests/test_wish_text_import.py` は `from src import ai`）に合わせてよい。

- [ ] **Step 3: 実装する**

Create `src/name_match.py`:

```python
"""name_match.py - 希望表に書かれた名前をスタッフに突き合わせる。

【なぜ必要か】
  従来は by_name の完全一致のみで、「田中さん」「タナカ」のような表記ゆれが
  すべて未割り当てになっていた。画像から読み取る場合は誤認識も加わるため、
  候補を出して人に選ばせる仕組みが要る。

【自動確定しない原則】
  best_exact は「正規化して完全一致がちょうど1件」のときだけ ID を返す。
  同姓同名が2人いる場合は None を返して人に選ばせる。
  既存の _extract_staff_hint（src/ai.py:1087）が守っている
  「候補が複数なら名簿順で決めない」原則を引き継ぐ。

外部依存なし（標準ライブラリのみ）。
"""
import unicodedata

# 敬称。長いものから順に剥がす（「ちゃん」を「ん」より先に消す）
_HONORIFICS = ("ちゃん", "さん", "サン", "くん", "クン", "君", "様", "さま", "氏")

# 除去する記号・空白。全角スペースは NFKC で半角になる
_STRIP_CHARS = " \t　・､、,。.／/\\-ー―‐_（）()「」『』【】[]{}〈〉"


def _kata(s):
    """ひらがなをカタカナに寄せる（読み表記のゆれを吸収する）。"""
    out = []
    for ch in s:
        code = ord(ch)
        # ひらがな（U+3041-U+3096）をカタカナ（U+30A1-U+30F6）へ
        if 0x3041 <= code <= 0x3096:
            out.append(chr(code + 0x60))
        else:
            out.append(ch)
    return "".join(out)


def normalize_name(s):
    """名前を比較用に正規化する。

    NFKC（全角→半角・互換文字の統一）→ 敬称除去 → 記号/空白除去
    → ひらがな→カタカナ → casefold の順。
    敬称は記号除去より先に剥がす（「田中 さん」のような分かち書きに対応するため
    空白除去の後にもう一度剥がす）。
    """
    if not s or not isinstance(s, str):
        return ""
    t = unicodedata.normalize("NFKC", s)
    t = _strip_honorifics(t)
    t = "".join(ch for ch in t if ch not in _STRIP_CHARS)
    t = _strip_honorifics(t)  # 「田中 さん」→空白除去後に再度剥がす
    t = _kata(t)
    return t.casefold()


def _strip_honorifics(t):
    changed = True
    while changed:
        changed = False
        for h in _HONORIFICS:
            if len(t) > len(h) and t.endswith(h):
                t = t[: -len(h)]
                changed = True
    return t


def _levenshtein(a, b):
    """編集距離。名前は短いので素朴な DP で十分（外部依存を増やさない）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _split_candidates(n):
    """姓・名の切り出し候補。区切りが無い日本語名は前半/後半で割る。

    「田中太郎」(4文字) → ["田中", "太郎"] のような分割を試す。
    厳密な姓名分解はできないので、部分一致の判定材料として使う。
    """
    out = set()
    if len(n) >= 2:
        for cut in range(1, len(n)):
            out.add(n[:cut])
            out.add(n[cut:])
    return out


def best_exact(hint, staffs):
    """正規化して完全一致がちょうど1件のときだけ staff_id を返す。

    0件・2件以上（同姓同名）は None。自動確定してよいのはこのケースだけ。
    """
    h = normalize_name(hint)
    if not h:
        return None
    hits = [s for s in (staffs or []) if normalize_name(s.get("name")) == h]
    return hits[0]["id"] if len(hits) == 1 else None


def match_staff(hint, staffs):
    """候補をスコア降順で返す。0.6 未満は含めない。

    戻り値: [{"staff_id", "name", "score", "reason"}]
    """
    h = normalize_name(hint)
    if not h:
        return []
    out = []
    for s in (staffs or []):
        n = normalize_name(s.get("name"))
        if not n:
            continue
        score, reason = _score(h, n)
        if score >= 0.6:
            out.append({"staff_id": s["id"], "name": s.get("name"),
                        "score": round(score, 3), "reason": reason})
    out.sort(key=lambda c: (-c["score"], c["staff_id"]))
    return out


def _score(h, n):
    """正規化済みの2つの名前からスコアと理由を返す。"""
    if h == n:
        return 1.0, "名前が一致"
    parts = _split_candidates(n)
    if h in parts:
        return 0.85, "姓または名が一致"
    if n.startswith(h) or n.endswith(h):
        return 0.75, "名前の一部が一致"
    if h in n:
        return 0.7, "名前に含まれる"
    dist = _levenshtein(h, n)
    longest = max(len(h), len(n))
    sim = 1.0 - (dist / longest) if longest else 0.0
    if sim >= 0.6:
        return sim, "よく似た名前"
    return 0.0, ""
```

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_name_match.py -v
```
Expected: 全件 PASS（20件）

FAIL する場合、**閾値やスコアを合わせるためにテストを緩めないこと。** テストが表現している要件（何が候補で何が候補でないか）が実装の意図とずれているなら、実装を直すか、テストの期待値が誤りである根拠を示してからテストを直すこと。

- [ ] **Step 5: テストが実際に守っていることを確認する**

次を1つずつ一時的に壊し、**対応するテストが赤になること**を確認する。確認したら戻し、実出力をレポートに貼る。

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `best_exact` の `len(hits) == 1` を `len(hits) >= 1` に | `test_two_identical_names_do_not_resolve` |
| `normalize_name` の `_strip_honorifics` 呼び出しを1つ消す | `test_honorifics_are_removed` |
| `_kata` を恒等関数にする | `test_hiragana_and_katakana_match` |
| `_score` の `if score >= 0.6` を `>= 0.0` に | `test_unrelated_name_is_not_a_candidate` |
| `out.sort(...)` を消す | `test_results_are_sorted_by_score_desc` |

- [ ] **Step 6: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/name_match.py tests/test_name_match.py
git commit -m "feat: 名前のあいまいマッピングを純関数として追加

従来は by_name の完全一致のみで、「田中さん」「タナカ」のような表記ゆれが
すべて未割り当てになっていた。画像から読み取る場合は誤認識も加わるため、
候補を出して人に選ばせる仕組みを用意する。

自動確定するのは「正規化して完全一致がちょうど1件」のときだけ。
同姓同名が2人いれば None を返して人に選ばせる（既存の _extract_staff_hint が
守っている「名簿順で決めない」原則を引き継ぐ）。外部依存なし。"
```

---

### Task 2: テキスト経路に名前マッピングを適用する

`POST /api/shop/wishes/parse` の完全一致を正規化完全一致に置き換え、候補を返す。**画像機能より先にテキスト経路で通すことで、既存テストが安全弁になる。**

**Files:**
- Modify: `src/app.py:3347, 3361`（`by_name` の解決部）
- Modify: `tests/test_wish_text_import.py`（`TestWishParseApi` に追加）

**Interfaces:**
- Consumes: Task 1 の `best_exact` / `match_staff`
- Produces: `POST /api/shop/wishes/parse` のレスポンスに `name_candidates` が加わる
  - 形: `{"<entry index>": [{"staff_id", "name", "score", "reason"}]}`
  - `staff_id` が解決できた entry には候補を付けない（付けても UI が使わない）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_wish_text_import.py` の `TestWishParseApi` の末尾に追記:

```python
    def test_normalized_exact_match_resolves_staff(self, client):
        """敬称や全角空白が付いていても staff_id が解決されること。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "PT1", "田中太郎")
        tok = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/wishes/parse",
                        json={"text": "田中太郎さん 8/3は休みたいです", "year_month": "2026-08"},
                        headers=auth(tok))
        assert r.status_code == 200
        d = r.get_json()
        assert d["entries"][0]["staff_id"] == sid, \
            "敬称付きの名前が解決されていない（完全一致のままになっている）"

    def test_ambiguous_name_is_not_auto_resolved(self, client):
        """同姓同名が2人いたら自動確定しないこと。"""
        shop_id = insert_shop()
        insert_staff(shop_id, "PT1", "田中")
        insert_staff(shop_id, "PT2", "田中")
        tok = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/wishes/parse",
                        json={"text": "田中 8/3は休みたいです", "year_month": "2026-08"},
                        headers=auth(tok))
        d = r.get_json()
        assert d["entries"][0]["staff_id"] is None, "同姓同名で自動確定してしまった"

    def test_candidates_are_returned_for_unresolved_entries(self, client):
        """解決できなかった entry に候補が返ること。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "PT1", "田中太郎")
        tok = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/wishes/parse",
                        json={"text": "田中 8/3は休みたいです", "year_month": "2026-08"},
                        headers=auth(tok))
        d = r.get_json()
        assert d["entries"][0]["staff_id"] is None
        cands = (d.get("name_candidates") or {}).get("0") or []
        assert cands, "未解決なのに候補が返っていない"
        assert cands[0]["staff_id"] == sid
        assert cands[0]["reason"]

    def test_candidates_only_include_own_shop_staff(self, client):
        """他店舗のスタッフが候補に混ざらないこと。"""
        shop_a = insert_shop(code="SHOPA")
        shop_b = insert_shop(code="SHOPB", name="別店舗")
        insert_staff(shop_b, "PT9", "田中太郎")
        tok = make_session("shop", shop_a, shop_a)

        r = client.post("/api/shop/wishes/parse",
                        json={"text": "田中 8/3は休みたいです", "year_month": "2026-08"},
                        headers=auth(tok))
        d = r.get_json()
        for cands in (d.get("name_candidates") or {}).values():
            assert not cands, f"他店舗のスタッフが候補に出た: {cands}"
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_wish_text_import.py -q -k "normalized_exact or ambiguous_name or candidates"
```
Expected: 4件とも FAIL

`insert_shop` の引数は `tests/helpers.py` で確認すること。

- [ ] **Step 3: 実装する**

`src/app.py` の `shop_wishes_parse` を書き換える。冒頭のインポートに `from name_match import best_exact, match_staff` を足す（`src/` は sys.path に入っている。既存の `from auth import ...` と同じ作法）。

`by_name = {...}` の行を削除し、entry ループを次にする:

```python
    entries = [e for e in (result.get("entries") or []) if isinstance(e, dict)]
    result["entries"] = entries
    text_norm = _wish_raw_norm(text)
    name_candidates = {}
    for i, e in enumerate(entries):
        # ★ 「元の文」が本当に入力に在るかを毎回検証して返す（UI が警告を出す）
        e["raw_verified"] = _wish_raw_verified(e.get("raw"), text_norm)
        if staff_id is not None:
            e["staff_id"] = staff_id  # 明示指定が最優先。staff_hint は無視する
            continue
        hint = e.get("staff_hint")
        hint = hint if isinstance(hint, str) else ""
        # 自動確定するのは「正規化して完全一致がちょうど1件」のときだけ。
        # 姓のみ一致・編集距離での類似は候補として返し、人に選ばせる
        # （誤配属は希望の取り違えに直結するため）。
        e["staff_id"] = best_exact(hint, staffs)
        if e["staff_id"] is None and hint:
            cands = match_staff(hint, staffs)
            if cands:
                name_candidates[str(i)] = cands
    result["name_candidates"] = name_candidates
    return jsonify(result)
```

`staffs` は `query_all("SELECT id, name FROM staffs WHERE shop_id=? AND is_resigned=0", ...)` の結果で、**自店舗の在籍者のみ**なので他店舗が混ざる経路は無い。

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_wish_text_import.py -q
.venv/bin/python -m pytest tests/ -q
```
Expected: すべて PASS。**既存の `TestWishParseApi` が落ちたら内容を必ずレポートに記録してから対処すること。** 完全一致だったものが正規化完全一致に広がるので、既存の期待値が変わる可能性がある。

- [ ] **Step 5: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `best_exact(hint, staffs)` を `by_name.get(hint)` 相当に戻す | `test_normalized_exact_match_resolves_staff` |
| `name_candidates[str(i)] = cands` の行を消す | `test_candidates_are_returned_for_unresolved_entries` |

実出力をレポートに貼ること。

- [ ] **Step 6: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/app.py tests/test_wish_text_import.py
git commit -m "feat(api): 希望取込の名前照合を正規化＋候補提示にする

従来は完全一致のみで、「田中さん」「タナカ」が全部未割り当てだった。
正規化して完全一致がちょうど1件のときだけ自動確定し、それ以外は
候補を name_candidates で返して人に選ばせる。同姓同名では確定しない。"
```

---

### Task 3: 画像を解析する LLM 経路

**Files:**
- Modify: `src/ai.py`（`_post_llm` に timeout 引数、`parse_wish_image` を新設）
- Modify: `.env.example`
- Create: `tests/test_wish_image_import.py`

**Interfaces:**
- Consumes: 既存の `_call_llm_messages` / `_sanitize_llm_wish_result` / `is_llm_available`
- Produces: `ai.parse_wish_image(images: list[str], year_month: str, staff_names: list[str]) -> dict`
  - 戻り値は `parse_wish_text` と同形 + `ocr_text`
  - `is_llm_available()` が False のとき `None` を返す（呼び出し側が 503 にする）

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_wish_image_import.py`:

```python
"""tests/test_wish_image_import.py — 希望表の画像取込。

実行: ./.venv/bin/python -m pytest tests/test_wish_image_import.py -v

外部APIは叩かない。_call_llm_messages を monkeypatch して検証ロジックだけを見る
（tests/test_wish_text_import.py の _use_llm と同じ作法）。
"""
import json

import pytest

from src import ai

_PNG_1PX = ("data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _use_vision(monkeypatch, response, capture=None):
    """LLM を有効にし、_call_llm_messages の戻り値を差し替える。"""
    monkeypatch.setattr(ai, "is_llm_available", lambda: True)

    def fake(messages, *a, **k):
        if capture is not None:
            capture.append(messages)
        return response

    monkeypatch.setattr(ai, "_call_llm_messages", fake)


class TestParseWishImage:
    def test_returns_none_when_llm_unavailable(self, monkeypatch):
        """AI未接続では画像を読めない。正規表現フォールバックは存在しない。"""
        monkeypatch.setattr(ai, "is_llm_available", lambda: False)
        assert ai.parse_wish_image([_PNG_1PX], "2026-08", []) is None

    def test_parses_valid_response(self, monkeypatch):
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "田中 8/3 休み",
            "entries": [{"staff_hint": "田中", "dates": ["2026-08-03"],
                         "availability": "rest", "start": None, "end": None,
                         "raw": "田中 8/3 休み"}],
            "unparsed": [],
        }))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", ["田中太郎"])
        assert r["entries"][0]["dates"] == ["2026-08-03"]
        assert r["entries"][0]["availability"] == "rest"
        assert r["ocr_text"] == "田中 8/3 休み"

    def test_image_is_sent_as_vision_content(self, monkeypatch):
        """messages の content が配列で、image_url を含むこと。"""
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert cap, "_call_llm_messages が呼ばれていない"
        user_msgs = [m for m in cap[0] if m.get("role") == "user"]
        assert user_msgs, "user メッセージが無い"
        content = user_msgs[-1]["content"]
        assert isinstance(content, list), "content が配列でない（vision 形式になっていない）"
        assert any(p.get("type") == "image_url" for p in content), "image_url が含まれていない"

    def test_multiple_images_are_all_sent(self, monkeypatch):
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX, _PNG_1PX], "2026-08", [])
        content = [m for m in cap[0] if m.get("role") == "user"][-1]["content"]
        assert sum(1 for p in content if p.get("type") == "image_url") == 2

    def test_non_json_response_does_not_crash(self, monkeypatch):
        _use_vision(monkeypatch, "すみません、読み取れませんでした")
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r is not None
        assert r["entries"] == []
        assert r["unparsed"], "読めなかった旨が unparsed に残っていない"

    def test_none_response_does_not_crash(self, monkeypatch):
        _use_vision(monkeypatch, None)
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r is not None
        assert r["entries"] == []

    def test_missing_ocr_text_defaults_to_empty(self, monkeypatch):
        _use_vision(monkeypatch, json.dumps({"entries": [], "unparsed": []}))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r["ocr_text"] == ""

    def test_invalid_entries_go_to_unparsed(self, monkeypatch):
        """既存の契約（検証で落ちた entry は捨てず unparsed に積む）を守ること。"""
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "x",
            "entries": [{"staff_hint": "田中", "dates": ["めちゃくちゃな日付"],
                         "availability": "rest", "raw": "田中 ??"}],
            "unparsed": [],
        }))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r["entries"] == []
        assert r["unparsed"], "落とした entry が unparsed に残っていない"

    def test_prompt_contains_injection_guard(self, monkeypatch):
        """画像内に書かれた指示に従わない旨がプロンプトに含まれること。"""
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        sys_msgs = [m for m in cap[0] if m.get("role") == "system"]
        assert sys_msgs
        text = json.dumps(sys_msgs, ensure_ascii=False)
        assert "指示" in text, "画像内の指示に従わない旨の記述が無い"


class TestPostLlmTimeout:
    def test_post_llm_accepts_timeout(self):
        """画像は推論が長いので timeout を延ばせること（署名の確認）。"""
        import inspect
        sig = inspect.signature(ai._post_llm)
        assert "timeout" in sig.parameters, "_post_llm に timeout 引数が無い"
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_wish_image_import.py -v
```
Expected: 全件 FAIL（`AttributeError: module 'src.ai' has no attribute 'parse_wish_image'` など）

- [ ] **Step 3: `_post_llm` に timeout 引数を足す**

`src/ai.py:75` の `_post_llm` の署名を `def _post_llm(messages, temperature, timeout=30):` にし、`requests.post(..., timeout=timeout)` に変える。`_call_llm_messages`（`:580`）にも `timeout=None` を通せるようにする（既定は従来どおり30秒）。

**既存の呼び出し元は引数を増やさないこと。** 既定値で従来と同じ挙動になる。

- [ ] **Step 4: `parse_wish_image` を実装する**

`src/ai.py` の `parse_wish_text`（`:1466`）の直後に追加する。プロンプトは `parse_wish_text` のものを流用し、**画像内の指示に従わない旨を追記**する。

要点:
- `messages` の user content を `[{"type": "text", "text": ...}, {"type": "image_url", "image_url": {"url": <data URL>}}, ...]` にする
- `_call_llm_messages(messages, timeout=90)` を使う（画像は推論が長い）
- LLM には `{"ocr_text": ..., "entries": [...], "unparsed": [...]}` を返させる
- 返答を `_sanitize_llm_wish_result(parsed, ocr_text)` に通す（**第2引数は画像経路では OCR 全文**。テキスト経路では貼付テキストが入る位置）
- `is_llm_available()` が False なら `None` を返す
- JSON でない・None のときは `entries: []` + 元の返答（または定型文）を `unparsed` に入れて返す。**None を返さないこと**（None は「AI未接続」専用）
- 環境変数 `LLM_VISION_MODEL` を追加。未設定なら `LLM_MODEL` を使う。`get_llm_config()` に足すか、`parse_wish_image` の中で `os.environ.get` するかは実装者の判断でよいが、**理由をレポートに書くこと**

- [ ] **Step 5: `.env.example` に追記する**

```
# 画像（希望表の写真）の読み取りに使うモデル。未設定なら LLM_MODEL を使う。
# 画像入力に対応したモデルを指定すること（例: gpt-4o-mini）。
LLM_VISION_MODEL=
```

- [ ] **Step 6: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_wish_image_import.py -v
.venv/bin/python -m pytest tests/ -q
```
Expected: すべて PASS

- [ ] **Step 7: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `is_llm_available()` の分岐を消す | `test_returns_none_when_llm_unavailable` |
| user content を文字列に戻す | `test_image_is_sent_as_vision_content` |
| 画像のループを1枚目だけにする | `test_multiple_images_are_all_sent` |
| `_sanitize_llm_wish_result` の呼び出しを消して parsed をそのまま返す | `test_invalid_entries_go_to_unparsed` |
| システムプロンプトから「指示」を含む一文を消す | `test_prompt_contains_injection_guard` |

実出力をレポートに貼ること。

- [ ] **Step 8: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/ai.py .env.example tests/test_wish_image_import.py
git commit -m "feat(ai): 希望表の画像を vision で読み取る経路を追加

_post_llm は messages を無検査で透過するため、content を配列にするだけで
vision リクエストが通る（送信基盤の改造は不要）。画像は推論が長いので
timeout を引数化し、画像経路では 90 秒を使う。

返答は既存の _sanitize_llm_wish_result に通して entries/unparsed の契約に
合流させる。照合元テキストには OCR 全文を使う（画像には貼付テキストが
無いため、raw_verified の安全弁がこれで機能する）。

AI未接続時は正規表現フォールバックが存在しないため None を返し、
呼び出し側が明示的なエラーにする。"
```

---

### Task 4: 画像解析エンドポイント

**Files:**
- Modify: `src/app.py`（`shop_wishes_parse` の直後に新規エンドポイント、`MAX_CONTENT_LENGTH` の設定）
- Modify: `tests/test_wish_image_import.py`

**Interfaces:**
- Consumes: Task 1 の `best_exact` / `match_staff`、Task 3 の `ai.parse_wish_image`
- Produces: `POST /api/shop/wishes/parse-image`
  - リクエスト: `{"images": ["data:image/png;base64,..."], "year_month": "2026-08", "staff_id": null}`
  - レスポンス: `parse` と同形 + `ocr_text`
  - AI未接続: 503

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_wish_image_import.py` に追記:

```python
from helpers import insert_shop, insert_staff, make_session, auth


class TestParseImageApi:
    def _tok(self, shop_id):
        return make_session("shop", shop_id, shop_id)

    def test_requires_shop_role(self, client):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "PT1", "田中太郎")
        tok = make_session("staff", sid, shop_id)
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX]}, headers=auth(tok))
        assert r.status_code in (401, 403)

    def test_returns_503_when_llm_unavailable(self, client, monkeypatch):
        monkeypatch.setattr(ai, "is_llm_available", lambda: False)
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX], "year_month": "2026-08"},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 503
        assert "テキスト" in (r.get_json() or {}).get("error", ""), \
            "テキスト貼り付けへの誘導が無い"

    def test_rejects_empty_images(self, client):
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": []}, headers=auth(self._tok(shop_id)))
        assert r.status_code == 400

    def test_rejects_non_image_data_url(self, client):
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": ["data:text/html;base64,PHNjcmlwdD4="]},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 400

    def test_rejects_too_many_images(self, client):
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX] * 4},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 400

    def test_rejects_oversized_image(self, client):
        shop_id = insert_shop()
        big = "data:image/png;base64," + ("A" * (5 * 1024 * 1024))
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [big]}, headers=auth(self._tok(shop_id)))
        assert r.status_code in (400, 413)

    def test_resolves_staff_and_returns_candidates(self, client, monkeypatch):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "PT1", "田中太郎")
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "田中 8/3 休み",
            "entries": [{"staff_hint": "田中", "dates": ["2026-08-03"],
                         "availability": "rest", "raw": "田中 8/3 休み"}],
            "unparsed": [],
        }))
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX], "year_month": "2026-08"},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 200
        d = r.get_json()
        assert d["entries"][0]["staff_id"] is None, "姓のみで自動確定してしまった"
        assert (d.get("name_candidates") or {}).get("0"), "候補が返っていない"
        assert d["ocr_text"] == "田中 8/3 休み"

    def test_raw_verified_uses_ocr_text(self, client, monkeypatch):
        """OCR全文に無い raw は raw_verified=False になること（創作の検出）。"""
        shop_id = insert_shop()
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "田中 8/3 休み",
            "entries": [{"staff_hint": "田中", "dates": ["2026-08-03"],
                         "availability": "rest", "raw": "画像に無い作り話"}],
            "unparsed": [],
        }))
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX], "year_month": "2026-08"},
                        headers=auth(self._tok(shop_id)))
        d = r.get_json()
        assert d["entries"][0]["raw_verified"] is False, \
            "OCR全文に無い文が検証を通ってしまった（安全弁が効いていない）"

    def test_does_not_save_anything(self, client, monkeypatch):
        """解析だけで DB に書かないこと。"""
        from db import query_all
        shop_id = insert_shop()
        insert_staff(shop_id, "PT1", "田中太郎")
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "田中太郎 8/3 休み",
            "entries": [{"staff_hint": "田中太郎", "dates": ["2026-08-03"],
                         "availability": "rest", "raw": "田中太郎 8/3 休み"}],
            "unparsed": [],
        }))
        client.post("/api/shop/wishes/parse-image",
                    json={"images": [_PNG_1PX], "year_month": "2026-08"},
                    headers=auth(self._tok(shop_id)))
        assert not query_all("SELECT id FROM wish_history"), "wish_history に書かれた"
        assert not query_all("SELECT id FROM shifts"), "shifts に書かれた"
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_wish_image_import.py -q -k "ParseImageApi"
```
Expected: 全件 FAIL（404）

- [ ] **Step 3: `MAX_CONTENT_LENGTH` を設定する**

`src/app.py` の Flask 設定（`JSON_AS_ASCII` の近く）に追加:

```python
# 画像取込（/api/shop/wishes/parse-image）が base64 を JSON に載せるため、
# 未設定だと巨大なボディをそのままメモリに読み込んでしまう。
# 画像3枚 × 4MB + 余裕。
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
```

- [ ] **Step 4: エンドポイントを実装する**

`shop_wishes_parse` の直後に追加する。実装の要点:

- `_shop_ctx()` で認可（店長のみ）
- `year_month` / `staff_id` の検証は `shop_wishes_parse` と**同じロジック**。共通化してもよいが、その場合は既存テストが通ることを確認すること
- `images` が list で 1〜3 件、各要素が `data:image/(jpeg|png|webp);base64,` で始まること
- **base64 をデコードしてマジックナンバーも確認する**（`\x89PNG`, `\xff\xd8\xff`, `RIFF....WEBP`）。data URL のヘッダだけを信じない
- デコード後のサイズが1枚 4MB 以下
- `ai.is_llm_available()` が False なら 503 と「AI未接続のため画像を読み取れません。テキストを貼り付けてください」
- `ai.parse_wish_image(images, year_month, [s["name"] for s in staffs])` を呼ぶ
- 返り値が `None`（AI未接続）なら 503
- `ocr_text` を `_wish_raw_norm` に通したものを照合元にして `_wish_raw_verified` を回す
- 名前解決は Task 2 と同じ（`best_exact` / `match_staff`）
- **例外時にリクエストボディをログに出さない**（base64 が流出する）

- [ ] **Step 5: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_wish_image_import.py -v
.venv/bin/python -m pytest tests/ -q
```
Expected: すべて PASS

- [ ] **Step 6: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| マジックナンバー検証を消す | `test_rejects_non_image_data_url` |
| 枚数上限のチェックを消す | `test_rejects_too_many_images` |
| サイズ上限のチェックを消す | `test_rejects_oversized_image` |
| `_wish_raw_verified` の照合元を `ocr_text` から空文字に変える | `test_raw_verified_uses_ocr_text` |
| `is_llm_available` の 503 分岐を消す | `test_returns_503_when_llm_unavailable` |

実出力をレポートに貼ること。

- [ ] **Step 7: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/app.py tests/test_wish_image_import.py
git commit -m "feat(api): 希望表の画像解析エンドポイントを追加

POST /api/shop/wishes/parse-image。解析のみでDBに書かない（既存の
/wishes/parse と同じ）。確認画面以降は一切変更しない。

画像は保存せずメモリ上で処理して破棄する。data URL のヘッダだけを
信じず、デコード後のマジックナンバーも確認する。MAX_CONTENT_LENGTH も
併せて設定した（未設定だった）。

raw_verified の照合元には OCR 全文を使う。画像には貼付テキストが
無いため、これが無いと「AIが創作した文」の検出が機能しない。"
```

---

### Task 5: 画像入力UI（貼り付け・ファイル/D&D・撮影）

**Files:**
- Modify: `public/app.js`（`_wtiRenderStep1` at `:3259`、`_wtiParse` at `:3284`）
- Modify: `public/style.css`
- Create: `e2e/wish_image_import.spec.js`

**Interfaces:**
- Consumes: Task 4 の `POST /api/shop/wishes/parse-image`
- Produces:
  - `state.images` — data URL の配列（最大3枚）
  - `reqImageResize(dataUrl, maxEdge) -> Promise<string>` — canvas で長辺を縮めて JPEG 化する純粋関数に近いヘルパ
  - DOM: `#wtiImageDrop` / `#wtiImageInput` / `#wtiCameraInput` / `.wti-image-thumb[data-idx]`

- [ ] **Step 1: E2E の失敗テストを書く**

Create `e2e/wish_image_import.spec.js`。既存の `e2e/wish_text_import.spec.js` の作法（`page.route` で `/wishes/parse` をスタブし、`/wishes/bulk` のボディを傍受）に揃える。**実画像を LLM に投げない。**

最低限のケース:
1. 画像ゾーンが表示され、ファイルを選ぶとサムネイルが出る
2. 画像を選んだ状態で「解析する」を押すと `/wishes/parse-image` が呼ばれる（テキストのみなら `/wishes/parse`）
3. スタブしたレスポンスでステップ2（確認画面）へ進む
4. サムネイルの削除ボタンで取り消せる
5. 4枚目を追加しようとすると弾かれる

ファイル選択は `page.setInputFiles` で行う。テスト用の小さな PNG を `e2e/fixtures/` に置くか、`Buffer` から生成すること。

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
npx playwright test e2e/wish_image_import.spec.js
```
Expected: 全件 FAIL（`#wtiImageDrop` が見つからない）

- [ ] **Step 3: 画像ゾーンを描画する**

`_wtiRenderStep1` の `<label for="wtiText">` の**前**に挿入する。

```
┌─────────────────────────────────────┐
│   📷 希望表の画像をここに貼り付け      │
│                                     │
│   [ファイルを選ぶ]  [写真を撮る]      │
│   ドラッグ＆ドロップも可              │
└─────────────────────────────────────┘
      ↓ 選択後はサムネイル一覧＋削除ボタン

      ─── または ───

  [ テキストを貼り付ける textarea ]
```

- ファイル選択: `<input type="file" accept="image/jpeg,image/png,image/webp" multiple hidden id="wtiImageInput">`
- 撮影: `<input type="file" accept="image/*" capture="environment" hidden id="wtiCameraInput">`。**PC では撮影ボタンを出さない**（`isPC()` で判定）
- 貼り付け: モーダルに `paste` リスナ。`e.clipboardData.items` から `type.startsWith('image/')` を拾う
- D&D: `#wtiImageDrop` に `dragover` / `drop`

- [ ] **Step 4: 送信前のリサイズを実装する**

canvas で長辺を 1600px に縮め、JPEG 品質 0.85 で再エンコードする。通信量・LLM トークン・タイムアウトの3つに効く。

**この変換関数は純関数に近い形で切り出し、`tests/helpers.py` の `run_js` でテストできるようにすること**（サイズ計算部分だけでもよい）。Phase 2 で座標計算を純関数化したのと同じ方針。

- [ ] **Step 5: `_wtiParse` を分岐させる**

`state.images` が空でなければ `/shop/wishes/parse-image` に、空なら従来どおり `/shop/wishes/parse` に送る。**ステップ2以降（`_wtiFlatten` / カレンダー確認 / `_wtiSubmit`）は一切変更しない。**

画像経路のときは `state.ocrText = r.ocr_text` を保持し、ステップ2の冒頭に「画像から読み取った文章」を折りたたみで表示する（既存設計書 §6「元の文を必ず見せる」の原則）。

- [ ] **Step 6: CSS を追加する**

`@media print` ブロックより**前**に追加すること。`tests/test_design_tokens.py` のコントラスト検査に引っかかったら、**テストを緩めずに色を直す**。

- [ ] **Step 7: テストが通ることを確認する**

Run:
```bash
node --check public/app.js
.venv/bin/python -m pytest tests/ -q
npx playwright test e2e/wish_image_import.spec.js
npx playwright test
```

- [ ] **Step 8: テストが実際に守っていることを確認する**

各テストについて、**守りたい実装の1行だけを消して**赤になることを確認し、実出力をレポートに貼る。特に:
- `_wtiParse` の分岐（`state.images.length ? '/shop/wishes/parse-image' : '/shop/wishes/parse'`）
- 枚数上限のチェック
- サムネイルの削除

**Playwright の Locator API はフォーカスやクリックの競合を自己修復します。** Phase 2 では壊れたコードでも緑になるテストが3度書かれました。ファイル選択は `setInputFiles` で問題ありませんが、貼り付けや D&D を検証する場合は生のイベントを使うこと。

- [ ] **Step 9: コミット**

```bash
git add public/app.js public/style.css e2e/wish_image_import.spec.js
git commit -m "feat(ui): 希望表の画像を貼り付け・選択・撮影で取り込めるようにする

店長から「希望表の画像を貼るだけでテキストに変換して読み込みたい」との
要望を受けての対応。貼り付け・ファイル選択/D&D・スマホ撮影の3方式に対応。

送信前に canvas で長辺1600pxに縮小する（通信量・LLMトークン・タイムアウトの
3つに効く）。画像は保存せず、確認画面には OCR 全文を折りたたみで表示して
原文と照合できるようにした。

ステップ2以降（確認画面・カレンダー・bulk確定）は一切変更していない。"
```

---

### Task 6: 名前候補の確認UI

**Files:**
- Modify: `public/app.js`（`_wtiRenderUnassigned` at `:3679`）
- Modify: `e2e/wish_image_import.spec.js`

**Interfaces:**
- Consumes: Task 2/4 の `name_candidates`
- Produces: なし

- [ ] **Step 1: E2E の失敗テストを書く**

現状の未割り当て欄は素の `<select>` にスタッフ全員が並ぶだけ。候補を確度順に見せる形にする。

```
「田中」さんの希望が未割り当てです
  ● 田中太郎   よく似ています
  ○ 田中花子   よく似ています
  ○ その他から選ぶ ▼
```

検証すること:
1. `name_candidates` がある entry で候補がラジオとして並ぶ
2. スコアに応じたラベル（0.9以上「ほぼ一致」/ 0.7〜0.9「よく似ています」/ 0.6〜0.7「似ているかもしれません」）が出る
3. **同点の候補が複数あるときは先頭が選択済みにならない**（店長に必ず選ばせる）
4. 候補が無い entry では従来の `<select>` にフォールバックする
5. 候補を選ぶと `state.items` の `staffId` が更新され、`bulk` のペイロードに載る

- [ ] **Step 2: テストが失敗することを確認する**

- [ ] **Step 3: 実装する**

既存の `<select>` は残す（フォールバックと「その他から選ぶ」用）。候補があるときだけラジオを上に足す。

**`_wtiEnsureExistingLoaded` の呼び出しを落とさないこと**（`public/app.js:3714` 付近）。未割り当てからスタッフを決めたときに、そのスタッフの既存希望を取得している。

- [ ] **Step 4〜6: 通ることを確認 → 壊して赤を確認 → コミット**

Task 5 と同じ手順。特に3（同点で先頭を選択済みにしない）は**誤配属に直結する**ので、実装の1行を消して赤になることを必ず確認すること。

---

## Self-Review

**設計書（Phase 3 該当部分）のカバレッジ**

| 設計書の項目 | 対応タスク |
|---|---|
| 3-1. サーバ: 画像解析API | Task 4 |
| 3-2. `ai.parse_wish_image()` | Task 3 |
| 3-3. `raw_verified` を OCR 全文で | Task 4 |
| 3-4. 名前のあいまいマッピング | Task 1（純関数）、Task 2（テキスト経路）、Task 4（画像経路） |
| 3-5. フロント: 画像入力UI（3方式） | Task 5 |
| 3-6. フロント: 名前候補の確認UI | Task 6 |
| 3-7. テスト | 各タスク |

**設計書から変えた点**

設計書では名前マッピングを画像機能と同時に入れる想定だったが、**Task 1・2 で先にテキスト経路へ入れる**順序にした。既存の `tests/test_wish_text_import.py`（103件）が安全弁になり、名前照合の変更が既存の取込を壊していないかを画像機能より先に確認できる。

**依存関係**

Task 1 → Task 2 → Task 4
Task 3 → Task 4 → Task 5 → Task 6

**Phase 1・2 の教訓の反映**

各タスクに「守りたい実装の1行だけを消して赤を確認する」ステップを、**壊す箇所と落ちるべきテスト名の対応表つき**で置いた。Phase 2 では「ハンドラを丸ごと空にする」粗い壊し方で部分的な壊れを見逃した例があったため。

また Phase 2 の最終レビューで「機能の対称な半分が無検証」（開始ハンドルのドラッグ）が見つかったので、Task 5・6 では**テキスト経路と画像経路の両方**、**候補あり／候補なし**の両方を検証するよう明示した。

**残る不確実性**

Task 5 の画像リサイズは canvas に依存するため、`run_js`（Node）でテストできるのはサイズ計算部分だけ。実際の縮小は E2E で確認するしかない。Task 5 Step 4 でその旨を明示している。

Task 3 の `LLM_VISION_MODEL` の実装方法（`get_llm_config()` に足すか関数内で読むか）は実装者判断とした。既存の `get_llm_config()` の形を見てから決めるのが妥当なため。
