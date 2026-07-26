# 希望テキスト取り込み 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 店長が LINE 等のテキストを貼り付けるだけで、スタッフ全員分のシフト希望を登録できるようにする。

**Architecture:** `src/ai.py` に日付ベースの解析関数を新設（既存の LLM 基盤とフォールバック機構を再利用）。`src/app.py` に解析用と一括登録用の API を2本追加。`public/app.js` の希望表管理画面にモーダルを追加し、解析結果を月間カレンダーに戻して確認させてから登録する。既存の希望提出フロー（スタッフ本人・店長自身）には手を入れない。

**Tech Stack:** Flask / SQLite（本番は Cloudflare D1）/ Vanilla JS / Bootstrap 5（外観は自前CSSで上書き済み）/ OpenAI 互換 LLM API（既定 `gpt-4o-mini`）/ pytest / Playwright

**設計の根拠:** `docs/superpowers/specs/2026-07-26-wish-text-import-design.md`

## Global Constraints

- **既存の希望提出 API（`/api/staff/requests`、`/api/shop/my-requests`）を変更しない**
- **希望は `shifts`（`status='requested'`）と `wish_history` の両方に INSERT する。** 片方だけでは機能しない（前者は AI 生成の入力、後者は希望表管理画面が読む永久履歴）
- **`availability` の語彙は既存のまま**（`rest` / `any` / `morning` / `evening` / `time`）。データモデルは変更しない
- **時刻の決め方は既存実装に合わせる**（設計書 §3 の表）。`any`/`morning`/`evening` は `09:00` 開始・`_get_shop_shift_end_time()` 終了、`rest` は `00:00:00`〜`23:59:59`
- **新しい色を作らない。** `tests/test_design_tokens.py`（114件）が旧配色の不在とコントラストを守っている。既存トークンのみ使う
- **`--zebra` は行が横に長い表の交互行にのみ使う**（カード・フォーム・カレンダーには使わない）
- **既存の DOM 構造・クラス名・`data-*` 属性・要素 ID を壊さない**（e2e がセレクタに使用）
- **`public/app.js` の `roleLabel()` を変更しない**（13箇所で使用、差し戻し歴あり）
- **テストの基準線:** pytest **700 passed, 1 skipped, 0 failed** / e2e **56 passed, 0 failed** / `test_design_tokens.py` **114 passed**
- **テスト実行:** Python は `.venv/bin/python -m pytest`、e2e は `npx playwright test`（**直列実行のため約1.5分かかる**）

---

## File Structure

| ファイル | 責務 | 変更種別 |
|---|---|---|
| `src/ai.py` | `parse_wish_text()` と正規表現フォールバックを追加 | 追記（既存関数は触らない） |
| `src/app.py` | `/api/shop/wishes/parse` と `/api/shop/wishes/bulk` を追加 | 追記 |
| `public/app.js` | 取り込みモーダル（貼付・解析・カレンダー確認・登録） | 追記 |
| `public/style.css` | 取り込みモーダル用のスタイル | 追記（セクション末尾） |
| `tests/test_wish_text_import.py` | 解析と API のテスト | 新規 |
| `e2e/wish_text_import.spec.js` | 取り込みの一連の流れ | 新規 |

---

## Task 1: 解析関数とフォールバック

**Files:**
- Modify: `src/ai.py`（末尾に追記）
- Create: `tests/test_wish_text_import.py`

**Interfaces:**
- Produces: `parse_wish_text(text, year_month, staff_names=None)` → `{"entries": [...], "unparsed": [...], "source": "llm"|"fallback"}`
- Produces: `_parse_wish_fallback(text, year_month, staff_names=None)` — 同じ形を返す正規表現版

- [ ] **Step 1: フォールバックのテストを書く（先に落とす）**

`tests/test_wish_text_import.py` を新規作成。**LLM を呼ばずフォールバック経路だけを検証する**（外部 API に依存させないため）。

```python
"""tests/test_wish_text_import.py — 希望テキスト取り込みのテスト。

実行: ./.venv/bin/python -m pytest tests/test_wish_text_import.py -v

解析は LLM を使わないフォールバック経路のみを検証する（外部APIに依存させない）。
LLM 経路は本番でのみ動き、失敗時は自動でフォールバックに落ちる設計。
"""
import pytest
from src import ai


class TestParseWishFallback:
    """正規表現ベースの解析。LLM 未設定でも機能が死なないことを保証する。"""

    def test_single_date_rest(self):
        r = ai._parse_wish_fallback("8/3は休みたいです", "2026-08")
        assert len(r["entries"]) == 1
        e = r["entries"][0]
        assert e["dates"] == ["2026-08-03"]
        assert e["availability"] == "rest"
        assert "8/3" in e["raw"]

    def test_multiple_dates_same_content(self):
        r = ai._parse_wish_fallback("8/3、8/5、8/7 は17時から22時まで入れます", "2026-08")
        e = r["entries"][0]
        assert e["dates"] == ["2026-08-03", "2026-08-05", "2026-08-07"]
        assert e["availability"] == "time"
        assert e["start"] == "17:00"
        assert e["end"] == "22:00"

    def test_different_content_splits_entries(self):
        r = ai._parse_wish_fallback("8/1 9-17\n8/3 13-22", "2026-08")
        assert len(r["entries"]) == 2
        assert r["entries"][0]["start"] == "09:00"
        assert r["entries"][1]["start"] == "13:00"

    def test_date_range(self):
        r = ai._parse_wish_fallback("8/10〜8/12 は休みです", "2026-08")
        assert r["entries"][0]["dates"] == ["2026-08-10", "2026-08-11", "2026-08-12"]
        assert r["entries"][0]["availability"] == "rest"

    def test_any_availability(self):
        r = ai._parse_wish_fallback("8/15 終日OK", "2026-08")
        assert r["entries"][0]["availability"] == "any"

    def test_unparsed_line_is_kept(self):
        r = ai._parse_wish_fallback("よろしくお願いします", "2026-08")
        assert r["entries"] == []
        assert "よろしくお願いします" in r["unparsed"]

    def test_empty_text_does_not_crash(self):
        r = ai._parse_wish_fallback("", "2026-08")
        assert r["entries"] == []

    def test_staff_hint_extracted(self):
        r = ai._parse_wish_fallback("小久保: 8/3休み", "2026-08", ["小久保", "佐藤"])
        assert r["entries"][0]["staff_hint"] == "小久保"

    def test_source_is_fallback(self):
        r = ai._parse_wish_fallback("8/3は休み", "2026-08")
        assert r["source"] == "fallback"


class TestParseWishText:
    """LLM が使えない環境では自動でフォールバックに落ちること。"""

    def test_falls_back_when_llm_unavailable(self, monkeypatch):
        monkeypatch.setattr(ai, "is_llm_available", lambda: False)
        r = ai.parse_wish_text("8/3は休み", "2026-08")
        assert r["source"] == "fallback"
        assert r["entries"][0]["availability"] == "rest"
```

- [ ] **Step 2: テストを実行して落ちることを確認**

Run: `.venv/bin/python -m pytest tests/test_wish_text_import.py -v`
Expected: FAIL（`AttributeError: module 'src.ai' has no attribute '_parse_wish_fallback'`）

- [ ] **Step 3: フォールバックを実装**

`src/ai.py` の末尾に追記する。**既存の関数は一切変更しない。**

方針:
- テキストを行で分割し、行ごとに解析する
- 日付の抽出: `8/3`、`8月3日`、`08/03`、範囲 `8/3〜8/5`、列挙 `8/3、8/5`
- 年は `year_month` から補う（`"2026-08"` → 8月なら 2026年）。月をまたぐ表記（`9/1`）は `year_month` の年を使う
- 時刻の抽出: 既存の `_parse_explicit_time_range(text)` を使う。返り値は `(start, end)` でどちらかが `None` のことがある
- `availability` の判定順:
  1. 「休」「不可」「NG」「無理」「×」を含む → `rest`
  2. 時刻が取れた → `time`
  3. 「終日」「いつでも」「フル」「終日OK」 → `any`
  4. 「早番」「午前」「朝」 → `morning`
  5. 「遅番」「午後」「夕方」「夜」 → `evening`
  6. どれでもない → その行は `unparsed` に入れる
- `staff_hint`: 行頭の `名前:` `名前：` `【名前】` パターン、または `staff_names` に含まれる名前が行内にあれば拾う
- **日付が1つも取れない行は `unparsed` に入れる。** 捨てない
- 同じ `(availability, start, end, staff_hint)` の行は `dates` をまとめる

```python
# ---------- 機能5: 希望テキストの取り込み（日付ベース） ----------
# parse_shift_request() とは別物。あちらは「月8万円稼ぎたい」から必要日数を
# 逆算する収入目標ベースで、特定日付を表現できない。こちらは「8/3は休み」の
# ような日付ごとの希望を抽出する。

_WISH_REST_WORDS = ("休", "不可", "NG", "ng", "無理", "×", "ムリ")
_WISH_ANY_WORDS = ("終日", "いつでも", "フル", "どこでも")
_WISH_MORNING_WORDS = ("早番", "午前", "朝")
_WISH_EVENING_WORDS = ("遅番", "午後", "夕方", "夜")


def _extract_dates(line, year_month):
    """行から日付を抽出して ["YYYY-MM-DD", ...] を返す。取れなければ []。"""
    # 実装: 範囲（8/3〜8/5）→ 列挙（8/3、8/5）→ 単独（8/3）の順に試す
    ...


def _parse_wish_fallback(text, year_month, staff_names=None):
    """LLM を使わない正規表現ベースの解析。LLM 未設定・失敗時の受け皿。"""
    ...
    return {"entries": entries, "unparsed": unparsed, "source": "fallback"}
```

**`_extract_dates` と `_parse_wish_fallback` の中身は、上の方針とテストを満たすように書いてください。** 正規表現の細部は実装者の裁量とします（テストが仕様です）。

- [ ] **Step 4: テストが通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_wish_text_import.py::TestParseWishFallback -v`
Expected: 9件すべて PASS

- [ ] **Step 5: LLM 経路を実装**

```python
def parse_wish_text(text, year_month, staff_names=None):
    """テキストから日付ごとの希望を抽出する。LLM が使えなければフォールバックする。"""
    if not is_llm_available():
        return _parse_wish_fallback(text, year_month, staff_names)
    names_hint = "、".join(staff_names or []) or "（不明）"
    system_prompt = (
        "あなたはシフト希望の解析アシスタントです。入力テキストから次のJSONを厳密に出力してください（他の文章不可）。"
        'スキーマ: {"entries":[{"staff_hint":"名前またはnull","dates":["YYYY-MM-DD"],'
        '"availability":"rest"|"any"|"morning"|"evening"|"time","start":"HH:MM"|null,'
        '"end":"HH:MM"|null,"raw":"根拠となった元の文"}],"unparsed":["読み取れなかった文"]}。'
        "availability の意味: rest=休み希望, any=いつでも可, morning=早番, evening=遅番, time=時間指定。"
        "同じ内容の日は dates にまとめ、内容が違えば entries を分けること。"
        "raw には必ずその判断の根拠になった入力文をそのまま入れること。"
        "日付が読み取れない文、挨拶や雑談は unparsed に入れ、推測で日付を作らないこと。"
    )
    user_prompt = (
        f'入力テキスト:\n"""\n{text}\n"""\n'
        f"対象月: {year_month}（日付はこの月として解釈。月をまたぐ記述があればその月で）\n"
        f"この店舗のスタッフ名: {names_hint}\n"
        "上記スキーマのJSONのみを出力してください。"
    )
    result = call_llm(system_prompt, user_prompt, temperature=0.1)
    if result:
        try:
            parsed = json.loads(re.sub(r"```json|```", "", result).strip())
            entries = parsed.get("entries") or []
            # LLM の取りこぼし・逸脱を補正する
            for e in entries:
                e.setdefault("staff_hint", None)
                e.setdefault("start", None)
                e.setdefault("end", None)
                e.setdefault("raw", "")
                if e.get("availability") not in ("rest", "any", "morning", "evening", "time"):
                    e["availability"] = "any"
                e["dates"] = [d for d in (e.get("dates") or []) if _is_iso_date(d)]
            entries = [e for e in entries if e["dates"]]
            return {"entries": entries, "unparsed": parsed.get("unparsed") or [], "source": "llm"}
        except Exception:
            pass
    return _parse_wish_fallback(text, year_month, staff_names)
```

`_is_iso_date(s)` も併せて実装してください（`YYYY-MM-DD` 形式かの判定）。

- [ ] **Step 6: 全テストを実行**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: **709 passed, 1 skipped, 0 failed**（既存700 + 新規9）

- [ ] **Step 7: コミット**

```bash
git add src/ai.py tests/test_wish_text_import.py
git commit -m "feat(ai): 希望テキストの日付ベース解析を追加

既存の parse_shift_request は収入目標から逆算する設計で「8/3は休み」の
ような日付指定を表現できない。日付ごとの希望を抽出する関数を新設した。
LLM 未設定・失敗時は正規表現フォールバックに落ちる。"
```

---

## Task 2: 解析 API

**Files:**
- Modify: `src/app.py`
- Modify: `tests/test_wish_text_import.py`

**Interfaces:**
- Consumes: `ai.parse_wish_text()`
- Produces: `POST /api/shop/wishes/parse`

- [ ] **Step 1: API のテストを書く**

`tests/test_wish_text_import.py` に追記。既存のテストの流儀（`conftest.py` の `client` フィクスチャ、`auth()` ヘルパ）に合わせてください。他のテストファイル（例: `tests/test_app.py`）を読んで同じ書き方にすること。

```python
class TestWishParseApi:
    def test_parse_returns_entries_without_saving(self, client):
        """解析しても DB には保存されないこと。"""
        # shop でログイン → POST /api/shop/wishes/parse
        # → entries が返る／wish_history と shifts の件数が増えていない

    def test_parse_requires_shop_role(self, client):
        """staff ロールでは 403。"""

    def test_parse_with_staff_id_assigns_all_entries(self, client):
        """staff_id を指定すると staff_hint を無視して全件その人になる。"""

    def test_parse_empty_text_returns_400(self, client):
        """text が空なら 400。"""
```

- [ ] **Step 2: テストを実行して落ちることを確認**

Run: `.venv/bin/python -m pytest tests/test_wish_text_import.py::TestWishParseApi -v`
Expected: FAIL（404 が返る）

- [ ] **Step 3: API を実装**

`src/app.py` に追記。既存の `/api/shop/wishes`（GET、3439行付近）の近くに置くこと。

```python
@app.post("/api/shop/wishes/parse")
def shop_wishes_parse():
    """希望テキストを解析する。保存はしない（何度でも試せる）。"""
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        abort(400, description="text が必要です")
    year_month = body.get("year_month") or jst_today().strftime("%Y-%m")
    staff_id = body.get("staff_id")
    staffs = query_all("SELECT id, name FROM staffs WHERE shop_id=? AND is_resigned=0", (shop_id,))
    result = ai.parse_wish_text(text, year_month, [s["name"] for s in staffs])
    # staff_hint をスタッフIDに解決する
    by_name = {s["name"]: s["id"] for s in staffs}
    for e in result.get("entries", []):
        if staff_id:
            e["staff_id"] = staff_id           # 明示指定が最優先
        else:
            hint = e.get("staff_hint")
            e["staff_id"] = by_name.get(hint) if hint else None
    return jsonify(result)
```

**`_shop_ctx()` が `shop` ロールを要求することを確認してください**（既存の使い方に倣う）。

- [ ] **Step 4: テストが通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_wish_text_import.py -v`
Expected: 全 PASS

- [ ] **Step 5: コミット**

```bash
git add src/app.py tests/test_wish_text_import.py
git commit -m "feat(api): 希望テキストの解析APIを追加（保存は伴わない）"
```

---

## Task 3: 一括登録 API

**Files:**
- Modify: `src/app.py`
- Modify: `tests/test_wish_text_import.py`

**Interfaces:**
- Produces: `POST /api/shop/wishes/bulk`

- [ ] **Step 1: テストを書く**

**このタスクで最も重要なのは「shifts と wish_history の両方に入る」ことの検証です。** 片方だけだと機能しません（前者は AI 生成の入力、後者は希望表管理画面が読む）。

```python
class TestWishBulkApi:
    def test_creates_in_both_tables(self, client):
        """shifts(status=requested) と wish_history の両方に入ること。"""
        # POST /api/shop/wishes/bulk で3件登録
        # → SELECT COUNT(*) FROM shifts WHERE status='requested' が 3
        # → SELECT COUNT(*) FROM wish_history が 3

    def test_rest_uses_full_day(self, client):
        """availability=rest は 00:00:00〜23:59:59 で入ること。"""

    def test_availability_uses_shop_end_time(self, client):
        """any/morning/evening は 09:00 開始・店舗の終了時刻で入ること。"""

    def test_duplicate_is_skipped(self, client):
        """同じ (staff_id, date) を2回登録したら2回目はスキップされること。"""

    def test_overwrite_replaces_existing(self, client):
        """overwrite=true なら既存を消して入れ直すこと。"""

    def test_ignores_deadline(self, client):
        """締切を過ぎていても店長は登録できること（スタッフの提出とは違う）。"""

    def test_rejects_other_shop_staff(self, client):
        """他店舗の staff_id は拒否されること。"""

    def test_requires_shop_role(self, client):
        """staff ロールでは 403。"""
```

- [ ] **Step 2: テストを実行して落ちることを確認**

Expected: FAIL（404）

- [ ] **Step 3: API を実装**

```python
@app.post("/api/shop/wishes/bulk")
def shop_wishes_bulk():
    """プレビューで確定した希望を一括登録する。

    店長の代理入力なので、スタッフ提出時の募集期間・締切の検証は行わない
    （締切はスタッフに対する期限であり店長を縛らない）。
    """
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    wishes = body.get("wishes") or []
    overwrite = bool(body.get("overwrite"))
    if not wishes:
        abort(400, description="wishes が必要です")
    shop_end = _get_shop_shift_end_time(shop_id)
    created = skipped = 0
    for w in wishes:
        staff_id = w.get("staff_id")
        date = w.get("date")
        avail = w.get("availability")
        # 他店舗のスタッフを弾く
        staff = query_one("SELECT id FROM staffs WHERE id=? AND shop_id=?", (staff_id, shop_id))
        if not staff or not date or not avail:
            skipped += 1
            continue
        start_dt, end_dt = _wish_times(date, avail, w.get("start"), w.get("end"), shop_end)
        if overwrite:
            execute("DELETE FROM wish_history WHERE staff_id=? AND start_datetime LIKE ?", (staff_id, date + "%"))
            execute("DELETE FROM shifts WHERE staff_id=? AND status='requested' AND start_datetime LIKE ?", (staff_id, date + "%"))
        else:
            overlap, _c = _check_staff_overlap(shop_id, staff_id, start_dt, end_dt, include_requested=True)
            if overlap:
                skipped += 1
                continue
        note = "店長が取り込み"
        raw = (w.get("raw") or "").strip()
        if raw:
            note += f": {raw[:200]}"
        if avail == "time":
            work = minutes_between(start_dt, end_dt)
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, staff_id, start_dt, end_dt, compute_break_minutes(work), "requested", note))
        else:
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason, availability) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, staff_id, start_dt, end_dt, "requested", note, avail))
        execute("INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, availability, note) VALUES (?,?,?,?,?,?)",
                (shop_id, staff_id, start_dt, end_dt, avail, note))
        created += 1
    msg = f"{created}件の希望を登録しました"
    if skipped:
        msg += f"（{skipped}件は重複または不正のためスキップ）"
    return jsonify({"ok": True, "created": created, "skipped": skipped, "message": msg})
```

`_wish_times(date, availability, start, end, shop_end)` を併せて実装してください。設計書 §3 の表のとおりに時刻を決めるヘルパです。`time` で `end <= start` の場合は翌日扱いにすること（既存の `_calcOvernightEndDay` と同じ考え方）。

- [ ] **Step 4: テストが通ることを確認**

Run: `.venv/bin/python -m pytest tests/test_wish_text_import.py -v`

- [ ] **Step 5: 全テストで回帰がないことを確認**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 既存700件が通ったまま、新規分が加わる

- [ ] **Step 6: コミット**

```bash
git add src/app.py tests/test_wish_text_import.py
git commit -m "feat(api): 希望の一括登録APIを追加

shifts(status=requested) と wish_history の両方に登録する。前者はAI生成の
入力、後者は希望表管理画面が読む永久履歴で、片方だけでは機能しない。
店長の代理入力なので締切・募集期間の検証は行わない。"
```

---

## Task 4: 取り込みモーダル（貼り付けと解析）

**Files:**
- Modify: `public/app.js`
- Modify: `public/style.css`

**Interfaces:**
- Consumes: `POST /api/shop/wishes/parse`
- Produces: モーダルの骨格と解析結果を保持する状態

- [ ] **Step 1: 希望表管理画面にボタンを追加**

`SCREENS.requests`（`public/app.js:2616` 付近）のヘッダ部分に「テキストから取り込む」ボタンを追加する。既存の `#reqLoadBtn` の並びに置くこと。

- [ ] **Step 2: モーダルを実装**

既存の `openModal()` を使う。設計書 §6 のステップ1（貼り付け）を実装する。

- 対象月のセレクト（既定は当月）
- スタッフのセレクト（既定は「自動判定」）
- テキストエリア
- 「解析する」ボタン

**新しい CSS クラスを作る場合は既存トークンのみを使うこと。** 色を発明しない。

- [ ] **Step 3: 解析を呼ぶ**

「解析する」で `POST /api/shop/wishes/parse` を叩き、結果を状態に保持する。`source` が `fallback` なら「簡易解析で読み取りました。内容をよく確認してください」と画面に出す。

- [ ] **Step 4: 手で確認**

サーバを起動し、店長でログイン → 希望表管理 → 取り込みモーダルを開く → テキストを貼って解析 → 結果が返ることを確認（この時点では生の JSON を出すだけでよい）。

- [ ] **Step 5: コミット**

---

## Task 5: カレンダープレビューと登録

**Files:**
- Modify: `public/app.js`
- Modify: `public/style.css`

**Interfaces:**
- Consumes: Task 4 の解析結果、`POST /api/shop/wishes/bulk`

- [ ] **Step 1: カレンダーを描く**

設計書 §6 のステップ2を実装する。

- スタッフごとに月間カレンダーを描く（スタッフ切り替えのセレクト）
- 日付マスに読み取り結果を出す（`rest`→「休」、`time`→「17-22」、`any`→「終日」、`morning`/`evening`→「早」「遅」）
- 既存の希望がある日には印を付ける（`/api/shop/wishes` で取得）

**既存のカレンダー実装（`SCREENS.request` のスタッフ希望カレンダー、`app.js:3448` 付近）を参考にすること。** 同じ見た目にできれば学習コストが下がる。

- [ ] **Step 2: 日付クリックで詳細を出す**

マスをクリックすると、その日の解釈と**元の文（`raw`）**を表示する。ここで `availability` を変更でき、削除もできる。

**元の文の表示は必須です。** これが無いと店長は AI の解釈が正しいか判断できません。

- [ ] **Step 3: 未割り当ての扱い**

`staff_id` が `null` のエントリを「未割り当て」として一覧表示し、スタッフを選んで振り分けられるようにする。振り分けなければ登録されない。

- [ ] **Step 4: 期間外の警告**

対象月の募集期間（`/api/shop/periods`）を取得し、期間外の日付が含まれる場合は警告を出す。**登録は妨げない。**

- [ ] **Step 5: 登録**

「登録する」で `POST /api/shop/wishes/bulk` を叩く。`dates` の配列は**クライアント側で日付ごとに展開**してから送る。既存の希望がある日は既定でスキップし、店長が「上書き」を選んだ場合のみ `overwrite: true` を送る。

登録後、モーダルを閉じて希望表管理を再読み込みし、結果を toast で出す。

- [ ] **Step 6: 手で確認（ライト・ダーク両テーマ）**

実際にテキストを貼って登録し、希望表管理にカードが出ることを確認する。**375px 幅でも崩れないことを確認すること。**

- [ ] **Step 7: コミット**

---

## Task 6: e2e と全体検証

**Files:**
- Create: `e2e/wish_text_import.spec.js`

- [ ] **Step 1: e2e を書く**

既存の e2e の流儀に合わせること（`e2e/helpers.js` の `ensureShop` / `loginAsManager` を使う）。

- 店長でログイン → 希望表管理 → 「テキストから取り込む」
- テキストを貼って解析 → カレンダーに反映される
- 登録 → 希望表管理にカードが出る

**LLM に依存させないこと。** テスト環境では `LLM_API_KEY` が未設定でフォールバックが動くはずですが、念のため解析結果に依存しない書き方（「カレンダーに何かが表示される」ではなく「登録後にカードが増える」で検証）にしてください。

- [ ] **Step 2: 全テストを実行**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: **0 failed**

Run: `npx playwright test`
Expected: **0 failed**（直列実行のため約1.5分）

- [ ] **Step 3: デザインの回帰がないことを確認**

Run: `.venv/bin/python -m pytest tests/test_design_tokens.py -q`
Expected: **114 passed**

Run: `grep -rnE "#6366F1|#818CF8|#4F46E5|#10B981|#34D399|#0F172A|#111827|#1F2937|#EF4444|#D97706|#475569|#F59E0B|#64748B|#94A3B8|#334155" public/ | grep -v node_modules`
Expected: 0件

- [ ] **Step 4: コミット**

---

## Self-Review

**1. Spec coverage**

| 設計書 | タスク |
|---|---|
| §3 データの保存先（shifts + wish_history） | Task 3 |
| §3 availability と時刻の決め方 | Task 3（`_wish_times`） |
| §3 募集期間・締切を適用しない | Task 3 |
| §4 解析（LLM + フォールバック） | Task 1 |
| §5 API 2本 | Task 2, 3 |
| §6 画面（貼付・カレンダー・未割り当て） | Task 4, 5 |
| §7 安全策 | Task 3（上書き既定オフ）、Task 5（元の文の表示、期間外の警告） |
| §8 テスト | Task 1, 2, 3, 6 |

**2. 未確定として残す点**

Task 1 Step 3 の `_extract_dates` と `_parse_wish_fallback` は、方針とテストのみを示し実装コードを書いていない。正規表現の細部は実装者の裁量とし、**テストを仕様とする**。ここを完全に書き下すと、かえって実装者が別の（より良い）書き方を選べなくなるため。

**3. 型・名前の一貫性**

`parse_wish_text` / `_parse_wish_fallback` / `_extract_dates` / `_is_iso_date`（Task 1）、`_wish_times`（Task 3）は各タスクで定義し、後続で参照する。API のパスは `/api/shop/wishes/parse` と `/api/shop/wishes/bulk` で統一。エントリのキーは `staff_hint` / `staff_id` / `dates` / `availability` / `start` / `end` / `raw` で全タスク共通。
