# Phase 2: 必要人数のバーUI 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 必要人数の設定画面を、シフト表と同じ時間軸のバーで操作できるようにする。あわせて「必要人数0」の意味が実装と説明で正反対になっている既存バグを塞ぐ。

**Architecture:** 設定→「シフト設定」タブを、日〜土＋「基本」の8タブ構成に作り直す。各タブは1日分のタイムラインを大きく表示し、時間帯パターンをバーとして描く。バーの高さが必要人数を表し、上端の上下ドラッグと数値欄の双方向で増減する。左右端のドラッグで時間帯を15分単位で伸縮する（時間帯は全曜日共通なので、変更時にその旨を明示する）。座標計算は既存タイムライン（`buildStaticTimelineHtml` / `installDraftTimelineDrag`）の資産を流用し、保存は新設の一括APIで行う。

**Tech Stack:** Python Flask / SQLite / Vanilla JS / Bootstrap 5 / pytest / Playwright

## Global Constraints

- 新しい依存パッケージを追加しない（`requirements.txt` は Flask, python-dotenv, requests, pytest, gunicorn のみ）
- コード内コメントは日本語。「なぜ」を書く（What はコードを見れば分かる）
- コミットメッセージは `fix:` / `feat:` / `refactor:` / `test:` プレフィックス + 日本語サマリ、末尾に `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Python の実行は必ず `.venv/bin/python`
- `public/app.js` を編集したら必ず `node --check public/app.js` を通す
- フロントエンドはモジュール化なし・グローバル関数。画面は `SCREENS.<name>`
- DBアクセスは `query_all` / `query_one` / `execute`（local/D1 自動切替）
- 時刻は `"HH:MM"` ゼロ埋め。日またぎは `end_time <= start_time` で翌日扱い（拡張スロット +1440）
- **基準値（Phase 2 開始時点）**: pytest `1124 passed, 1 skipped` / E2E `94 passed` / `tests/run_tests.py` 全 PASS
- 各タスクの最後に pytest 全件・`node --check`・`.venv/bin/python tests/run_tests.py` を確認する
- **テストは「対応する実装のどの行を消せば落ちるか」を言える形で書く。** 言えないテストは何も守っていない。Phase 1 ではこの形になっていないテストが5件見つかった

## 前提となる調査結果（コードで確認済み）

- `shift_patterns`（`schema.sql:45-53`）が時間帯と基本必要人数、`shift_pattern_weekday_required`（`schema.sql:56-71`）が曜日別の上書き。**時間帯（start/end）は全曜日共通**で、曜日ごとに変わるのは人数のみ
- `required_staff` は「必要人数（下限）」と「配置上限（cap）」を兼ねる。専用の列はない
- `_day_requirements()`（`src/shift_engine.py:60-93`）は `needed <= 0` のときスロットにキーを書かない（`:83-84`）
- `cap_ok()`（`src/shift_engine.py:337-347`）は `req_map.get(sl, 0)` が `<= 0` のスロットを「パターン外＝上限なし」として `continue` する（`:342-343`）
- 結果として **「明示的に0人」と「パターン圏外」が区別できず、どちらも上限なしになる**
- 一方 UI の注記は「**0**を入れるとその曜日は募集しません」（`public/app.js:4212`）。**意味が正反対**
- `POST` / `PUT /api/shop/patterns`（`src/app.py:1901, 1914`）は `req or 1` なので基本必要人数に 0 を保存できない。曜日別（`src/app.py:1944-1947`）は 0 を保存できる
- 現行 UI の保存は画面の文字列 `"09:00 - 22:00"` を `' - '` で split して復元する（`public/app.js:4266-4268`）
- 保存はパターンごとに直列2リクエスト（`public/app.js:4263-4275`）
- `_validate_pattern_hours()`（`src/app.py:1858-1888`）は 9h/13h 超で `warning` を返すが、フロントは読み捨てている（`public/app.js:4292-4294`）
- タイムラインの座標計算は `buildStaticTimelineHtml`（`public/app.js:1089` 付近）、ドラッグは `installDraftTimelineDrag`（`public/app.js:1247` 付近）。`.tl-hour:last-child` の絶対配置トリック（`public/style.css:874-890`）を移植しないと目盛りが1時間ズレる
- `appState.businessHours` は `ensureBusinessHours()`（`public/app.js:756` 付近）が `GET /shop/patterns` の最小 start / 最大 end から算出する

---

### Task 1: 「必要人数0」の意味を実装と説明で一致させる

**これはシフト配置ロジックの中核に触る。最も慎重に進めること。**

現状「0人」と設定すると、募集しないどころか**その時間帯に何人でも配置できる**。UI の注記と正反対。バーUIで0人を直感的に設定できるようにする前に、ここを塞ぐ。

方針: `req_map` の**キーの有無**で「パターン圏外」を表し、**値0**で「配置禁止」を表す。

- スロットにキーが無い → パターンが1つも被っていない → 上限なし（現状維持）
- `req_map[slot] == 0` → 明示的に0人 → 配置禁止
- `req_map[slot] > 0` → その人数が上限かつ必要人数（現状維持）

**Files:**
- Create: `tests/test_required_zero.py`
- Modify: `src/shift_engine.py:75-92`（`_day_requirements`）
- Modify: `src/shift_engine.py:337-347`（`cap_ok`）
- Modify: `src/app.py`（`_check_slot_cap` の該当箇所）
- Modify: `src/app.py:1901, 1914`（`req or 1`）

**Interfaces:**
- Consumes: なし
- Produces: `_day_requirements` の戻り値に「値0のキー」が現れうるようになる。`cap_ok` は `required is None` で圏外判定する

- [ ] **Step 1: 現状の挙動を固定する特性テストを書く**

現状を「正しい」として固定するのではなく、**現状がどうなっているかを記録する**ためのテスト。修正で何が変わるかを差分として見えるようにする。

Create `tests/test_required_zero.py`:

```python
"""tests/test_required_zero.py — 必要人数0の意味を実装と説明で一致させる。

実行: ./.venv/bin/python -m pytest tests/test_required_zero.py -v

背景: UI は「0 を入れるとその曜日は募集しません」と説明しているが、
実装では required <= 0 のスロットが「上限なし」として扱われ、
何人でも配置できてしまっていた（意味が正反対）。

このファイルは req_map のキーの有無と値0を区別する契約を固定する。
  - キーが無い    = パターン圏外 = 上限なし
  - 値が 0        = 明示的に0人 = 配置禁止
  - 値が 1 以上   = その人数が上限かつ必要人数
"""
import pytest

import shift_engine
from helpers import insert_shop, insert_staff, insert_pattern


class TestDayRequirementsZero:
    """_day_requirements が「0」と「圏外」を区別すること。"""

    def test_zero_pattern_writes_key_with_value_zero(self):
        pats = [{"id": 1, "start_time": "09:00", "end_time": "12:00", "required_staff": 0}]
        req = shift_engine._day_requirements(pats)
        # 09:00-12:00 のスロットにキーがあり、値が 0 であること
        assert req.get(540) == 0, "明示的に0人のスロットにキーが無い（圏外と区別できない）"
        assert req.get(690) == 0

    def test_out_of_pattern_slot_has_no_key(self):
        pats = [{"id": 1, "start_time": "09:00", "end_time": "12:00", "required_staff": 2}]
        req = shift_engine._day_requirements(pats)
        # 08:30 はパターン圏外なのでキーが無いこと
        assert 510 not in req

    def test_overlapping_zero_does_not_lower_positive(self):
        """0のパターンが重なっても、正の要件を0に引き下げないこと。"""
        pats = [
            {"id": 1, "start_time": "09:00", "end_time": "12:00", "required_staff": 3},
            {"id": 2, "start_time": "10:00", "end_time": "11:00", "required_staff": 0},
        ]
        req = shift_engine._day_requirements(pats)
        assert req.get(600) == 3, "重なりの集約は max のはず"

    def test_weekday_override_zero_is_respected(self):
        pats = [{"id": 1, "start_time": "09:00", "end_time": "12:00", "required_staff": 3}]
        # 土曜(6)だけ 0 人
        req = shift_engine._day_requirements(pats, weekday=6, weekday_overrides={(1, 6): 0})
        assert req.get(540) == 0, "曜日別の0が反映されていない"

    def test_overnight_zero_extends_to_next_day_slots(self):
        """日またぎパターンの0も拡張スロットに書かれること。"""
        pats = [{"id": 1, "start_time": "22:00", "end_time": "02:00", "required_staff": 0}]
        req = shift_engine._day_requirements(pats)
        assert req.get(1320) == 0        # 22:00
        assert req.get(1500) == 0        # 翌 01:00 = 1440 + 60
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_required_zero.py -v
```
Expected: `test_zero_pattern_writes_key_with_value_zero` / `test_weekday_override_zero_is_respected` / `test_overnight_zero_extends_to_next_day_slots` の3件が FAIL（`assert None == 0`）。`test_out_of_pattern_slot_has_no_key` と `test_overlapping_zero_does_not_lower_positive` は PASS。

この落ち方と違う場合は、`_day_requirements` の引数の渡し方が実装と合っていない。`src/shift_engine.py:60` のシグネチャを読んで合わせること。

- [ ] **Step 3: `_day_requirements` を修正する**

`src/shift_engine.py` の `_day_requirements` 内、`needed <= 0` の分岐を書き換える:

```python
        needed = pat.get("required_staff") or 0
        if weekday_overrides and weekday is not None:
            ov = weekday_overrides.get((pat.get("id"), weekday))
            if ov is not None:
                needed = ov
        if needed < 0:
            needed = 0
        if pe <= ps:
            pe += 1440  # overnight: 当日ベースで +1440 した拡張スロットに
        s = (ps // gran) * gran
        while s < pe:
            # キーの有無で「パターン圏外」を、値0で「明示的に0人（配置禁止）」を表す。
            # 以前は needed<=0 のとき continue でキーを書かず、cap_ok から見て
            # 圏外と区別できなかった（＝0人と設定すると上限なしになっていた）。
            if s not in req or needed > req[s]:
                req[s] = needed
            s += gran
```

（`if needed <= 0: continue` の行を削除する）

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_required_zero.py -v
```
Expected: 5件すべて PASS

- [ ] **Step 5: 既存テストへの影響を確認する**

Run:
```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tests/run_tests.py
```
Expected: pytest `1129 passed, 1 skipped`（1124 + 新規5）、`run_tests.py` 全 PASS。

**失敗するテストがあれば、その内容を必ずレポートに記録してから対処すること。** `_day_requirements` の戻り値に値0のキーが増えたことで、`_day_shortage_segments` や `compute_shortage*` の挙動が変わった可能性がある。不足判定は `required - coverage` なので 0 - 0 = 0 で不足にならないはずだが、**実際にそうなっているかをテストの失敗内容で確認すること。** 期待と違うなら `src/shift_engine.py:170-209` の `_day_shortage_segments` を読んで原因を特定する。

- [ ] **Step 6: `cap_ok` の配置禁止テストを書く**

`tests/test_required_zero.py` に追記:

```python
class TestCapOkZero:
    """必要人数0のスロットに配置できないこと（cap_ok の契約）。

    cap_ok は auto_generate 内のクロージャなので直接は呼べない。
    ここでは実際にシフト生成を走らせ、0人の時間帯に誰も置かれないことで確認する。
    """

    def test_zero_weekday_blocks_placement(self):
        shop_id = insert_shop()
        insert_staff(shop_id, "E1", "社員A", role="employee")
        pid = insert_pattern(shop_id, "通し", "09:00", "22:00", 2)
        # 2026-08-01 は土曜。土曜(6)だけ 0 人にする
        from db import execute
        execute("INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
                (pid, shop_id, 6, 0))

        settings = {}
        result = shift_engine.auto_generate(shop_id, settings, "2026-08-01", "2026-08-01")
        shifts = result.get("shifts", [])
        assert not shifts, f"0人設定の土曜に {len(shifts)} 件配置された（上限なし扱いのまま）"
```

- [ ] **Step 7: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_required_zero.py::TestCapOkZero -v
```
Expected: PASS。

FAIL する場合、`cap_ok` の `if required <= 0: continue`（`src/shift_engine.py:342-343`）がまだ 0 を「上限なし」と扱っている。次の Step で直す。

`auto_generate` のシグネチャや `settings` の必須キーが違う場合は `src/shift_engine.py` の該当箇所と `tests/run_tests.py` の呼び出し例を読んで合わせること。`insert_pattern` のシグネチャは `tests/helpers.py` を参照。

- [ ] **Step 8: `cap_ok` を修正する**

`src/shift_engine.py` の `cap_ok` 内を書き換える:

```python
        for sl in _shift_slots(start_iso, end_iso, GRAN):
            required = req_map.get(sl)
            if required is None:
                continue  # パターンが1つも被っていないスロットは上限なし
            # required == 0 は「その時間帯は募集しない」。1人でも置けないので下の比較で弾かれる。
            if cov.get(sl, 0) + 1 > required:
                return False
        return True
```

- [ ] **Step 9: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_required_zero.py -v
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tests/run_tests.py
```
Expected: すべて PASS

- [ ] **Step 10: 手動追加経路（`_check_slot_cap`）も同じ意味にする**

`src/app.py` の `_check_slot_cap` を読み、`required <= 0` を「上限なし」として扱っている箇所があれば `cap_ok` と同じ形（キーの有無で判定）に直す。

**まず該当箇所を特定してからレポートに書くこと。** `grep -n "required" src/app.py` で `_check_slot_cap` の本体を読み、実際にどう判定しているかを確認する。`cap_ok` と同じ形になっていなければ直す。既に `req_map.get(sl, 0)` のような形なら `req_map.get(sl)` + `is None` に変える。

修正したら、手動追加でも0人の時間帯に置けないことのAPIテストを `tests/test_required_zero.py` に追加する:

```python
class TestManualAddZero:
    """手動追加でも0人の時間帯には置けないこと。"""

    def test_manual_add_blocked_on_zero_weekday(self, client):
        from helpers import make_session, auth
        from db import execute
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "社員A", role="employee")
        pid = insert_pattern(shop_id, "通し", "09:00", "22:00", 2)
        execute("INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
                (pid, shop_id, 6, 0))
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/shifts", json={
            "staff_id": staff_id,
            "start_datetime": "2026-08-01T09:00:00",
            "end_datetime": "2026-08-01T17:00:00",
        }, headers=auth(token))
        assert r.status_code in (400, 409), \
            f"0人設定の土曜に手動追加できてしまった（status={r.status_code}）"
```

期待するステータスコードが実装と違う場合は、実装が返す値に合わせてよい。ただし**200 で通ってしまう場合は実装のバグ**なので直すこと。

- [ ] **Step 11: 基本必要人数に 0 を保存できるようにする**

`src/app.py:1901` と `:1914` の `req or 1` を直す。`validate_numeric_field` が `None` を返しうるので、`None` と `0` を区別する。

```python
    # required_staff は必要人数マトリクスで無エスケープの value 属性として描画される。
    # 数値以外を保存させない（保存型XSS の入口封じ）。
    req = validate_numeric_field(body.get("required_staff"), "必要人数")
    # 0 は「その時間帯は募集しない」という意味を持つため、1 に丸めてはいけない。
    # 未指定（None）のときだけ既定値 1 を使う。
    if req is None:
        req = 1
```

そのうえで `INSERT` / `UPDATE` の `req or 1` を `req` に変える。

`validate_numeric_field`（`src/utils.py:447` 付近）を読んで、0 を渡したときに何を返すかを**実際に確認してから**書くこと。0 を `None` に落とす実装なら、そこも直す必要がある。

- [ ] **Step 12: 0 の保存と往復のテストを書く**

`tests/test_required_zero.py` に追記:

```python
class TestPatternZeroPersistence:
    """基本必要人数 0 が保存され、読み出せること。"""

    def test_post_pattern_with_zero(self, client):
        from helpers import make_session, auth
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/patterns", json={
            "pattern_name": "休止中", "start_time": "09:00", "end_time": "12:00",
            "required_staff": 0,
        }, headers=auth(token))
        assert r.status_code == 200

        d = client.get("/api/shop/patterns", headers=auth(token)).get_json()
        pat = [p for p in d["patterns"] if p["pattern_name"] == "休止中"][0]
        assert pat["required_staff"] == 0, "0 が 1 に丸められている"

    def test_put_pattern_to_zero(self, client):
        from helpers import make_session, auth
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        token = make_session("shop", shop_id, shop_id)

        r = client.put(f"/api/shop/patterns/{pid}", json={
            "pattern_name": "夜", "start_time": "17:00", "end_time": "22:00",
            "required_staff": 0,
        }, headers=auth(token))
        assert r.status_code == 200

        d = client.get("/api/shop/patterns", headers=auth(token)).get_json()
        assert d["patterns"][0]["required_staff"] == 0
```

- [ ] **Step 13: 全体が緑であることを確認してコミット**

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tests/run_tests.py
git add tests/test_required_zero.py src/shift_engine.py src/app.py
git commit -m "fix(engine): 必要人数0が「上限なし」になっていたのを「配置禁止」に直す

UI は「0 を入れるとその曜日は募集しません」と説明していたが、
_day_requirements が needed<=0 のときスロットにキーを書かず、cap_ok から
見て「パターン圏外＝上限なし」と区別できなかった。結果、0 人と設定した
時間帯に何人でも配置できていた（意味が正反対）。

req_map のキーの有無で圏外を、値0で配置禁止を表すように変える。
あわせて基本必要人数に 0 を保存できるようにした（req or 1 で 1 に
丸められていた。曜日別は 0 を保存できるのに基本だけできない非対称だった）。"
```

---

### Task 2: 必要人数の一括保存APIを追加する

現行の保存は画面の文字列 `"09:00 - 22:00"` を `' - '` で split して復元しており（`public/app.js:4266-4268`）、表示形式を変えた瞬間に壊れる。バーUIでは表示が根本的に変わるため、state から送る一括APIに置き換える。あわせてパターンごと直列2リクエスト（N件で2N回）も解消する。

**Files:**
- Modify: `src/app.py`（`shop_pattern_weekday_required` の直後に新規エンドポイント）
- Create: `tests/test_patterns_bulk.py`

**Interfaces:**
- Consumes: Task 1 の「0 を保存できる」挙動
- Produces: `PUT /api/shop/patterns/bulk`
  - リクエスト: `{"patterns": [{"id": int, "pattern_name": str, "start_time": "HH:MM", "end_time": "HH:MM", "required_staff": int, "weekday_required": {"0": int, ...}}]}`
  - レスポンス: `{"ok": true, "warnings": [{"id": int, "pattern_name": str, "warning": str}]}`
  - 検証失敗時: 400 と `{"error": "...", "failed": {"id": int, "pattern_name": str, "reason": str}}`

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_patterns_bulk.py`:

```python
"""tests/test_patterns_bulk.py — 必要人数の一括保存API。

実行: ./.venv/bin/python -m pytest tests/test_patterns_bulk.py -v

従来はパターンごとに PUT を2発（本体 + 曜日別）直列で投げており、
しかもフロントが画面の文字列 "09:00 - 22:00" を再パースして送っていた。
表示を変えると保存が壊れる構造だったため、state から一括で送る形に変える。
"""
import pytest

from helpers import insert_shop, insert_pattern, make_session, auth


def _token(shop_id):
    return make_session("shop", shop_id, shop_id)


class TestBulkSave:
    def test_saves_base_and_weekday(self, client):
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": "夜番", "start_time": "18:00", "end_time": "23:00",
            "required_staff": 3, "weekday_required": {"0": 4, "6": 5},
        }]}, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        p = d["patterns"][0]
        assert p["pattern_name"] == "夜番"
        assert p["start_time"] == "18:00"
        assert p["end_time"] == "23:00"
        assert p["required_staff"] == 3
        assert p["weekday_required"] == {"0": 4, "6": 5}

    def test_weekday_map_is_replaced_not_merged(self, client):
        """曜日別は置換方式（送らなかった曜日は削除される）。"""
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)
        base = {"id": pid, "pattern_name": "夜", "start_time": "17:00",
                "end_time": "22:00", "required_staff": 2}

        client.put("/api/shop/patterns/bulk",
                   json={"patterns": [dict(base, weekday_required={"0": 4, "6": 5})]},
                   headers=auth(tok))
        client.put("/api/shop/patterns/bulk",
                   json={"patterns": [dict(base, weekday_required={"6": 5})]},
                   headers=auth(tok))

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        assert d["patterns"][0]["weekday_required"] == {"6": 5}

    def test_zero_is_saved(self, client):
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "昼", "09:00", "17:00", 2)
        tok = _token(shop_id)

        client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": "昼", "start_time": "09:00", "end_time": "17:00",
            "required_staff": 0, "weekday_required": {"0": 0},
        }]}, headers=auth(tok))

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        assert d["patterns"][0]["required_staff"] == 0
        assert d["patterns"][0]["weekday_required"] == {"0": 0}


class TestBulkValidation:
    def test_invalid_hours_rolls_back_everything(self, client):
        """1件でも検証に失敗したら全体をロールバックする。"""
        shop_id = insert_shop()
        p1 = insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        p2 = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [
            {"id": p1, "pattern_name": "朝", "start_time": "09:00", "end_time": "14:00",
             "required_staff": 9, "weekday_required": {}},
            # 16時間 → _validate_pattern_hours が 400 で弾く
            {"id": p2, "pattern_name": "長すぎ", "start_time": "06:00", "end_time": "22:00",
             "required_staff": 2, "weekday_required": {}},
        ]}, headers=auth(tok))
        assert r.status_code == 400

        d = client.get("/api/shop/patterns", headers=auth(tok)).get_json()
        pats = {p["pattern_name"]: p for p in d["patterns"]}
        assert "朝" in pats
        assert pats["朝"]["end_time"] == "13:00", "1件目が保存されたまま残っている（ロールバックされていない）"
        assert pats["朝"]["required_staff"] == 2

    def test_failure_reports_which_pattern(self, client):
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": "長すぎ", "start_time": "06:00", "end_time": "22:00",
            "required_staff": 2, "weekday_required": {},
        }]}, headers=auth(tok))
        assert r.status_code == 400
        body = r.get_json()
        assert "長すぎ" in str(body), f"どのパターンが原因か分からない: {body}"

    def test_warning_is_returned_per_pattern(self, client):
        """9h/13h 超の警告がパターンごとに返ること（拒否はしない）。"""
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "通し", "09:00", "17:00", 2)
        tok = _token(shop_id)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid, "pattern_name": "通し", "start_time": "09:00", "end_time": "21:00",
            "required_staff": 2, "weekday_required": {},
        }]}, headers=auth(tok))
        assert r.status_code == 200
        warnings = r.get_json().get("warnings") or []
        assert warnings, "12時間のパターンに警告が返っていない"
        assert warnings[0]["pattern_name"] == "通し"

    def test_other_shop_pattern_is_rejected(self, client):
        """他店舗のパターンIDを混ぜても更新されないこと（IDOR対策）。"""
        shop_a = insert_shop(code="SHOPA")
        shop_b = insert_shop(code="SHOPB", name="別店舗")
        pid_b = insert_pattern(shop_b, "他店", "10:00", "15:00", 9)
        tok_a = _token(shop_a)

        r = client.put("/api/shop/patterns/bulk", json={"patterns": [{
            "id": pid_b, "pattern_name": "乗っ取り", "start_time": "10:00", "end_time": "15:00",
            "required_staff": 1, "weekday_required": {},
        }]}, headers=auth(tok_a))
        assert r.status_code in (400, 404)

        tok_b = _token(shop_b)
        d = client.get("/api/shop/patterns", headers=auth(tok_b)).get_json()
        assert d["patterns"][0]["pattern_name"] == "他店", "他店舗のパターンを書き換えられた"
        assert d["patterns"][0]["required_staff"] == 9
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_patterns_bulk.py -v
```
Expected: 全件 FAIL（404 Not Found。エンドポイントが存在しない）

`insert_shop` が `code` / `name` 引数を取るかは `tests/helpers.py` で確認すること。取らない場合はシグネチャに合わせて書き換えてよい。

- [ ] **Step 3: エンドポイントを実装する**

`src/app.py` の `shop_pattern_weekday_required` の直後に追加:

```python
@app.put("/api/shop/patterns/bulk")
def shop_patterns_bulk():
    """必要人数を一括保存する。

    従来はパターンごとに PUT を2発（本体 + 曜日別）直列で投げていたため、
    N 件で 2N 回の往復が発生し、途中で失敗すると一部だけ保存された状態が残った。
    ここでは全件を検証してから書き込み、1件でも失敗したら何も書かない。
    """
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    items = body.get("patterns")
    if not isinstance(items, list):
        abort(400, description="patterns は配列で指定してください")

    # 自店舗のパターンIDを先に引いておく（他店舗のIDを混ぜられても書き換えさせない）
    own = {r["id"] for r in query_all(
        "SELECT id FROM shift_patterns WHERE shop_id=?", (shop_id,))}

    # --- 検証フェーズ: 1件でも落ちたら何も書かない ---
    validated = []
    warnings = []
    for it in items:
        if not isinstance(it, dict):
            abort(400, description="patterns の要素はオブジェクトで指定してください")
        pid = it.get("id")
        name = it.get("pattern_name")
        if pid not in own:
            abort(400, description=f"この店舗のパターンではありません（id={pid}）")
        ok, warning = _validate_pattern_hours(it.get("start_time"), it.get("end_time"))
        if not ok:
            abort(400, description=f"{name}: {warning}")
        req = validate_numeric_field(it.get("required_staff"), "必要人数")
        if req is None:
            req = 1
        wr = it.get("weekday_required") or {}
        if not isinstance(wr, dict):
            abort(400, description=f"{name}: weekday_required は {{曜日: 人数}} 形式で指定してください")
        wr_clean = {}
        for k, v in wr.items():
            try:
                wd, cnt = int(k), int(v)
            except (ValueError, TypeError):
                abort(400, description=f"{name}: 曜日別必要人数に数値以外が含まれています")
            if not (0 <= wd <= 6) or cnt < 0:
                abort(400, description=f"{name}: 曜日別必要人数の値が不正です")
            wr_clean[wd] = cnt
        validated.append((pid, name, it.get("start_time"), it.get("end_time"), req, wr_clean))
        if warning:
            warnings.append({"id": pid, "pattern_name": name, "warning": warning})

    # --- 書き込みフェーズ ---
    for pid, name, st, et, req, wr_clean in validated:
        execute("UPDATE shift_patterns SET pattern_name=?, start_time=?, end_time=?, required_staff=? WHERE id=? AND shop_id=?",
                (name, st, et, req, pid, shop_id))
        # 曜日別は置換方式。送られなかった曜日は「基本に戻す」を意味する。
        execute("DELETE FROM shift_pattern_weekday_required WHERE pattern_id=? AND shop_id=?",
                (pid, shop_id))
        for wd, cnt in wr_clean.items():
            execute("INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
                    (pid, shop_id, wd, cnt))

    return jsonify({"ok": True, "warnings": warnings})
```

**注意**: `abort(400, ...)` は例外を投げて処理を中断するため、検証フェーズで落ちれば書き込みフェーズには到達しない。これが「ロールバック」の実体。DB トランザクションではないので、**書き込みフェーズの途中で例外が出た場合は一部だけ書かれる**。その可能性を減らすため、検証は必ず全件を先に済ませること。

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_patterns_bulk.py -v
```
Expected: 全件 PASS

`_shop_ctx` / `validate_numeric_field` / `_validate_pattern_hours` のインポートや位置が違う場合は、同ファイル内の既存エンドポイント（`shop_patterns_put`）の書き方に合わせること。

- [ ] **Step 5: 既存エンドポイントを残したまま全体が緑か確認してコミット**

既存の個別エンドポイント（`POST` / `PUT` / `DELETE` / `weekday-required`）は削除しない。`tests/test_app.py:861-918` が使っている。

```bash
.venv/bin/python -m pytest tests/ -q
git add src/app.py tests/test_patterns_bulk.py
git commit -m "feat(api): 必要人数の一括保存APIを追加

従来はパターンごとに PUT を2発直列で投げており、N件で2N回の往復が
発生していた。さらにフロントが画面の文字列 \"09:00 - 22:00\" を
再パースして送る構造だったため、表示形式を変えると保存が壊れた。

全件を検証してから書き込む一括APIを新設する。1件でも検証に失敗したら
何も書かない。他店舗のパターンIDを混ぜても書き換えられないことを
テストで固定した。"
```

---

### Task 3: バーの座標計算を純関数として切り出す

バーUIの位置計算をDOM操作から切り離し、Node で直接テストできるようにする。Phase 1 で「実装を壊しても緑のままのテスト」が5件見つかったため、**計算部分は純関数にして確実に守る**。

**Files:**
- Modify: `public/app.js`（`renderShiftMatrixTab` の直前に純関数群を追加）
- Create: `tests/test_required_bar_geometry.py`

**Interfaces:**
- Consumes: なし
- Produces（すべて `public/app.js` のグローバル関数）:
  - `reqBarRange(patterns) -> {minH, maxH, rangeMin, rangeLen}` — 時間軸の範囲を返す
  - `reqBarPosition(startTime, endTime, range) -> {left, width}` — バーの左端と幅を % で返す
  - `reqBarHeightPx(count) -> number` — 必要人数からバーの高さ(px)を返す
  - `reqBarCountFromPx(px) -> number` — バーの高さ(px)から必要人数を返す
  - `reqBarEffective(pattern, weekday) -> {count, isOverride}` — 曜日を考慮した実効人数

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_required_bar_geometry.py`:

```python
"""tests/test_required_bar_geometry.py — 必要人数バーの座標計算（純関数）。

実行: ./.venv/bin/python -m pytest tests/test_required_bar_geometry.py -v

public/app.js の関数を Node で直接実行して検証する（helpers.run_js）。
DOM に触らない純関数として切り出しているので、描画を変えてもここは壊れない。
"""
import json

import pytest

from helpers import extract_js_function, run_js

SRC = None


def _fns(*names):
    global SRC
    if SRC is None:
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "public/app.js"), encoding="utf-8") as f:
            SRC = f.read()
    return [extract_js_function(SRC, n) for n in names]


class TestReqBarRange:
    def test_range_covers_all_patterns(self):
        pats = [{"start_time": "09:00", "end_time": "17:00"},
                {"start_time": "17:00", "end_time": "22:00"}]
        out = run_js(_fns("reqBarRange"),
                     f"JSON.stringify(reqBarRange({json.dumps(pats)}))")
        r = json.loads(out)
        assert r["minH"] == 9
        assert r["maxH"] == 22
        assert r["rangeMin"] == 540
        assert r["rangeLen"] == 780

    def test_overnight_extends_past_24(self):
        pats = [{"start_time": "22:00", "end_time": "02:00"}]
        out = run_js(_fns("reqBarRange"),
                     f"JSON.stringify(reqBarRange({json.dumps(pats)}))")
        r = json.loads(out)
        assert r["maxH"] == 26, "日またぎが翌日側に伸びていない"

    def test_empty_patterns_fall_back(self):
        out = run_js(_fns("reqBarRange"), "JSON.stringify(reqBarRange([]))")
        r = json.loads(out)
        assert r["rangeLen"] > 0, "パターン0件で幅0になると全バーが消える"


class TestReqBarPosition:
    def test_full_range_is_zero_to_hundred(self):
        rng = {"minH": 9, "maxH": 22, "rangeMin": 540, "rangeLen": 780}
        out = run_js(_fns("reqBarPosition"),
                     f"JSON.stringify(reqBarPosition('09:00','22:00',{json.dumps(rng)}))")
        p = json.loads(out)
        assert abs(p["left"]) < 0.01
        assert abs(p["width"] - 100) < 0.01

    def test_half_range(self):
        rng = {"minH": 0, "maxH": 24, "rangeMin": 0, "rangeLen": 1440}
        out = run_js(_fns("reqBarPosition"),
                     f"JSON.stringify(reqBarPosition('06:00','18:00',{json.dumps(rng)}))")
        p = json.loads(out)
        assert abs(p["left"] - 25) < 0.01
        assert abs(p["width"] - 50) < 0.01

    def test_overnight_wraps_to_extended_slot(self):
        rng = {"minH": 9, "maxH": 26, "rangeMin": 540, "rangeLen": 1020}
        out = run_js(_fns("reqBarPosition"),
                     f"JSON.stringify(reqBarPosition('22:00','02:00',{json.dumps(rng)}))")
        p = json.loads(out)
        # 22:00 = 1320分 → (1320-540)/1020 = 76.47%
        assert abs(p["left"] - 76.47) < 0.1
        # 翌02:00 = 1560分 → 幅 240/1020 = 23.53%
        assert abs(p["width"] - 23.53) < 0.1


class TestReqBarHeight:
    def test_height_is_proportional_to_count(self):
        out = run_js(_fns("reqBarHeightPx"),
                     "JSON.stringify([1,2,3].map(reqBarHeightPx))")
        h = json.loads(out)
        assert h[1] - h[0] == h[2] - h[1], "1人あたりの高さが一定でない"
        assert h[0] > 0

    def test_zero_still_has_visible_height(self):
        out = run_js(_fns("reqBarHeightPx"), "String(reqBarHeightPx(0))")
        assert float(out.strip()) > 0, "0人のバーが高さ0だと画面から消えて操作できない"

    def test_round_trip(self):
        """高さ → 人数 → 高さ で元に戻ること。"""
        out = run_js(_fns("reqBarHeightPx", "reqBarCountFromPx"),
                     "JSON.stringify([0,1,2,5,10].map((n) => reqBarCountFromPx(reqBarHeightPx(n))))")
        assert json.loads(out) == [0, 1, 2, 5, 10]

    def test_negative_px_clamps_to_zero(self):
        out = run_js(_fns("reqBarCountFromPx"), "String(reqBarCountFromPx(-50))")
        assert int(out.strip()) == 0, "マイナスにドラッグしたときに負の人数になる"


class TestReqBarEffective:
    def test_override_wins(self):
        pat = {"required_staff": 2, "weekday_required": {"6": 5}}
        out = run_js(_fns("reqBarEffective"),
                     f"JSON.stringify(reqBarEffective({json.dumps(pat)}, 6))")
        e = json.loads(out)
        assert e["count"] == 5
        assert e["isOverride"] is True

    def test_falls_back_to_base(self):
        pat = {"required_staff": 2, "weekday_required": {"6": 5}}
        out = run_js(_fns("reqBarEffective"),
                     f"JSON.stringify(reqBarEffective({json.dumps(pat)}, 1))")
        e = json.loads(out)
        assert e["count"] == 2
        assert e["isOverride"] is False

    def test_override_zero_is_not_treated_as_absent(self):
        """0 の上書きが「未設定」と誤判定されないこと（0 は falsy）。"""
        pat = {"required_staff": 3, "weekday_required": {"0": 0}}
        out = run_js(_fns("reqBarEffective"),
                     f"JSON.stringify(reqBarEffective({json.dumps(pat)}, 0))")
        e = json.loads(out)
        assert e["count"] == 0, "0 の上書きが基本値にフォールバックしている"
        assert e["isOverride"] is True
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_required_bar_geometry.py -v
```
Expected: 全件 FAIL（`extract_js_function` が関数を見つけられない）

`extract_js_function` / `run_js` のシグネチャは `tests/helpers.py` で確認すること。使い方の実例は `tests/test_settings_xss.py` にある。

- [ ] **Step 3: 純関数を実装する**

`public/app.js` の `renderShiftMatrixTab` の直前に追加:

```js
/* ============================================================
   必要人数バー（設定→シフト設定）の座標計算
   DOM に触らない純関数。tests/test_required_bar_geometry.py が Node で直接検証する。
   ============================================================ */

/** バー1人あたりの高さ(px)。人数が視覚的に比較できる最小単位。 */
const REQ_BAR_UNIT_PX = 14;
/** 0人のときも掴めるように残す高さ(px)。0だと画面から消えて操作不能になる。 */
const REQ_BAR_MIN_PX = 6;

/** 時間帯パターン群から時間軸の範囲を返す。
 *  end <= start は日またぎとみなし、翌日側（+24h）まで軸を伸ばす。 */
function reqBarRange(patterns) {
  const list = patterns || [];
  let minH = 24, maxH = 0;
  list.forEach((p) => {
    const s = _parseTimeParts(p.start_time);
    const e = _parseTimeParts(p.end_time);
    if (!s || !e) return;
    const sMin = s.h * 60 + s.m;
    let eMin = e.h * 60 + e.m;
    if (eMin <= sMin) eMin += 1440;   // 日またぎ
    minH = Math.min(minH, Math.floor(sMin / 60));
    maxH = Math.max(maxH, Math.ceil(eMin / 60));
  });
  if (maxH <= minH) { minH = 9; maxH = 22; }   // パターン0件など。幅0だと全バーが消える
  minH = Math.max(0, minH);
  maxH = Math.min(48, maxH);
  return { minH, maxH, rangeMin: minH * 60, rangeLen: (maxH - minH) * 60 };
}

/** 時間帯の左端と幅を軸に対する % で返す。 */
function reqBarPosition(startTime, endTime, range) {
  const s = _parseTimeParts(startTime);
  const e = _parseTimeParts(endTime);
  if (!s || !e) return { left: 0, width: 0 };
  const sMin = s.h * 60 + s.m;
  let eMin = e.h * 60 + e.m;
  if (eMin <= sMin) eMin += 1440;
  const rawLeft = ((sMin - range.rangeMin) / range.rangeLen) * 100;
  const rawRight = ((eMin - range.rangeMin) / range.rangeLen) * 100;
  const left = Math.max(0, rawLeft);
  const width = Math.max(1, Math.min(100, rawRight) - left);
  return { left, width };
}

/** 必要人数からバーの高さ(px)。 */
function reqBarHeightPx(count) {
  const n = Math.max(0, Math.round(count || 0));
  return REQ_BAR_MIN_PX + n * REQ_BAR_UNIT_PX;
}

/** バーの高さ(px)から必要人数。上下ドラッグの逆変換。 */
function reqBarCountFromPx(px) {
  return Math.max(0, Math.round(((px || 0) - REQ_BAR_MIN_PX) / REQ_BAR_UNIT_PX));
}

/** 曜日を考慮した実効必要人数。weekday が null なら基本値。 */
function reqBarEffective(pattern, weekday) {
  const base = Math.max(0, Math.round(pattern.required_staff || 0));
  if (weekday === null || weekday === undefined) return { count: base, isOverride: false };
  const wr = pattern.weekday_required || {};
  const v = wr[String(weekday)];
  // 0 は falsy なので `v || base` としてはいけない（0人設定が基本値に化ける）
  if (v === undefined || v === null) return { count: base, isOverride: false };
  return { count: Math.max(0, Math.round(v)), isOverride: true };
}
```

`_parseTimeParts`（`public/app.js:747` 付近）が `{h, m}` を返すことを確認してから使うこと。返り値の形が違う場合は実装に合わせる。

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_required_bar_geometry.py -v
node --check public/app.js
```
Expected: 全件 PASS

- [ ] **Step 5: テストが実際に守っていることを確認する**

**Phase 1 で「緑だが何も守っていないテスト」が5件見つかったため、この確認を必須とする。**

`reqBarEffective` の `if (v === undefined || v === null)` を `if (!v)` に一時的に書き換え、`test_override_zero_is_not_treated_as_absent` が**実際に落ちること**を確認する。確認したら元に戻す。実出力をレポートに貼ること。

- [ ] **Step 6: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
node --check public/app.js
git add public/app.js tests/test_required_bar_geometry.py
git commit -m "feat(ui): 必要人数バーの座標計算を純関数として切り出す

バーUIの描画に入る前に、位置・高さ・実効人数の計算を DOM から切り離し、
Node で直接テストできるようにする。特に曜日別の 0 が falsy として
基本値にフォールバックする事故を、テストで固定した。"
```

---

### Task 4: バーUIを描画する

現行のマトリクス表を、曜日タブ + 1日分のタイムライン表示に置き換える。この Task では**描画のみ**。ドラッグ編集は Task 5・6 で足す。

**Files:**
- Modify: `public/app.js:4209-4280`（`renderShiftMatrixTab` / `loadMatrix`）
- Modify: `public/style.css`（新規セクション）
- Create: `e2e/required_staff_bar.spec.js`

**Interfaces:**
- Consumes: Task 3 の `reqBarRange` / `reqBarPosition` / `reqBarHeightPx` / `reqBarEffective`、Task 2 の `PUT /api/shop/patterns/bulk`
- Produces:
  - `reqBarState` — `{ patterns: [...], weekday: null|0..6, dirty: bool }` のモジュールスコープ変数
  - DOM: `#reqBarTabs` / `#reqBarTrack` / `#reqBarSave` / `.rq-bar[data-pid]` / `.rq-count[data-pid]`

- [ ] **Step 1: E2E の失敗テストを書く**

Create `e2e/required_staff_bar.spec.js`:

```js
/**
 * e2e/required_staff_bar.spec.js — 必要人数のバーUI。
 *
 * 実行: npx playwright test e2e/required_staff_bar.spec.js
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager } = require('./helpers');

const SHOP = {
  shopCode: 'REQ1',
  shopName: '必要人数テスト店',
  managerCode: 'mgr1',
  managerPassword: 'mgr1pass',
  managerName: '必要人数店長',
};

/** 時間帯パターンを2件仕込む。バーが2本描かれる状態を作る。 */
async function seedPatterns(request, token) {
  for (const p of [
    { pattern_name: '早番', start_time: '09:00', end_time: '17:00', required_staff: 2 },
    { pattern_name: '夜番', start_time: '17:00', end_time: '22:00', required_staff: 3 },
  ]) {
    await request.post('/api/shop/patterns', {
      headers: { Authorization: `Bearer ${token}` },
      data: p,
    });
  }
}

test.beforeEach(async ({ page, request }) => {
  await ensureShop(request, SHOP);
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await seedPatterns(request, token);

  await loginAsManager(page, {
    shopCode: SHOP.shopCode,
    managerCode: SHOP.managerCode,
    password: SHOP.managerPassword,
  });
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');
});

test('時間帯がバーとして描画される', async ({ page }) => {
  await expect(page.locator('.rq-bar')).toHaveCount(2);
  await expect(page.locator('.rq-bar[data-name="早番"]')).toBeVisible();
});

test('バーの高さが必要人数に比例する', async ({ page }) => {
  const heights = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).getBoundingClientRect().height;
    return { asa: get('早番'), yoru: get('夜番') };
  });
  // 早番2人 / 夜番3人 → 夜番のほうが高い
  expect(heights.yoru).toBeGreaterThan(heights.asa);
});

test('曜日タブを切り替えられる', async ({ page }) => {
  await expect(page.locator('#reqBarTabs .rq-tab')).toHaveCount(8);  // 基本 + 日〜土
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  await expect(page.locator('#reqBarTabs .rq-tab[data-wd="6"]')).toHaveClass(/active/);
});

test('曜日別の人数が未設定なら基本値が表示される', async ({ page }) => {
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  const v = await page.inputValue('.rq-count[data-name="早番"]');
  expect(v).toBe('2');
  // 上書きではないことがクラスで分かる
  await expect(page.locator('.rq-bar[data-name="早番"]')).not.toHaveClass(/rq-override/);
});

test('時間帯が0件のとき案内が出る', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  const d = await (await request.get('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
  })).json();
  for (const p of d.patterns) {
    await request.delete(`/api/shop/patterns/${p.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await expect(page.locator('#reqBarEmpty')).toBeVisible();
});
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
npx playwright test e2e/required_staff_bar.spec.js
```
Expected: 全件 FAIL（`#reqBarTrack` が見つからずタイムアウト）

設定画面のナビキーが `settings` であることは `public/app.js:599` 付近の `NAV_DEFS` で確認すること。設定画面は開いた直後に「シフト設定」タブが選択済み（`public/app.js:3937` の `settingsTab = 'shift'`）。

- [ ] **Step 3: 描画を実装する**

`public/app.js` の `renderShiftMatrixTab` と `loadMatrix` を置き換える。

```js
/** 必要人数バーUIの状態。曜日タブの選択と、編集中のパターン群を持つ。
 *  weekday: null = 「基本」タブ（shift_patterns.required_staff を編集）
 *           0..6 = 曜日タブ（shift_pattern_weekday_required を編集） */
let reqBarState = { patterns: [], weekday: null, dirty: false };

const REQ_BAR_WD_LABELS = ['日', '月', '火', '水', '木', '金', '土'];

function renderShiftMatrixTab(body) {
  body.innerHTML = card(
    sectionTitle('bi-grid-3x3-gap', 'シフト設定', '<span class="small text-secondary">— 時間帯ごとの必要人数</span>') +
    `<div id="reqBarTabs" class="rq-tabs"></div>
     <div id="reqBarBody"></div>
     <div class="flex gap-2 mt-3">
       <button class="btn btn-light" id="reqBarAdd"><i class="bi bi-plus-lg"></i> 時間帯を追加</button>
       <button class="btn btn-primary" id="reqBarSave"><i class="bi bi-check-lg"></i> 保存</button>
       <span class="small text-secondary flex items-center" id="reqBarMsg"></span>
     </div>`);
  loadReqBar(body);
  body.querySelector('#reqBarAdd')?.addEventListener('click', () => openPatternModal(null, () => loadReqBar(body)));
}

async function loadReqBar(body) {
  const host = body.querySelector('#reqBarBody');
  try {
    const d = await api('/shop/patterns');
    reqBarState = { patterns: d.patterns || [], weekday: reqBarState.weekday, dirty: false };
    renderReqBarTabs(body);
    renderReqBarTrack(body);
  } catch (e) {
    safeSetHTML(host, `<div class="text-danger">${esc(e.message)}</div>`);
  }
}

function renderReqBarTabs(body) {
  const tabs = body.querySelector('#reqBarTabs');
  if (!tabs) return;
  const cur = reqBarState.weekday;
  const items = [{ wd: '', label: '基本' }]
    .concat(REQ_BAR_WD_LABELS.map((l, i) => ({ wd: String(i), label: l })));
  tabs.innerHTML = items.map((it) => {
    const isActive = (it.wd === '' && cur === null) || (it.wd !== '' && String(cur) === it.wd);
    const wdClass = it.wd === '0' ? ' rq-sun' : (it.wd === '6' ? ' rq-sat' : '');
    return `<button class="rq-tab${isActive ? ' active' : ''}${wdClass}" data-wd="${it.wd}">${esc(it.label)}</button>`;
  }).join('');
  tabs.querySelectorAll('.rq-tab').forEach((b) => b.addEventListener('click', () => {
    if (reqBarState.dirty && !confirm('保存していない変更があります。タブを切り替えると失われます。よろしいですか？')) return;
    reqBarState.weekday = b.dataset.wd === '' ? null : parseInt(b.dataset.wd, 10);
    reqBarState.dirty = false;
    renderReqBarTabs(body);
    renderReqBarTrack(body);
  }));
}

function renderReqBarTrack(body) {
  const host = body.querySelector('#reqBarBody');
  if (!host) return;
  const pats = reqBarState.patterns;
  if (!pats.length) {
    safeSetHTML(host, `<div id="reqBarEmpty">${emptyState('bi-grid-3x3-gap', '時間帯がありません。「時間帯を追加」で作成してください')}</div>`);
    return;
  }
  const wd = reqBarState.weekday;
  const range = reqBarRange(pats);
  const hours = [];
  for (let h = range.minH; h <= range.maxH; h++) {
    hours.push(`<div class="tl-hour${h >= 24 ? ' tl-hour-next' : ''}">${esc(_extHourLabel(h))}</div>`);
  }
  // --tl-hours は CSS が背景グラデーションでグリッドを描くのに使う。
  // 目盛りは N+1 個生成し、最後の1個だけ CSS 側で絶対配置する（.tl-hour:last-child）。
  // これを省くと目盛りが1時間分ずれる。
  const trackVars = `--tl-hours:${range.maxH - range.minH}`;
  const maxCount = Math.max(1, ...pats.map((p) => reqBarEffective(p, wd).count));

  const bars = pats.map((p) => {
    const eff = reqBarEffective(p, wd);
    const pos = reqBarPosition(p.start_time, p.end_time, range);
    const h = reqBarHeightPx(eff.count);
    return `<div class="rq-bar${eff.isOverride ? ' rq-override' : ''}${eff.count === 0 ? ' rq-zero' : ''}"
      data-pid="${esc(p.id)}" data-name="${esc(p.pattern_name)}"
      style="left:${pos.left.toFixed(2)}%;width:${pos.width.toFixed(2)}%;height:${h}px"
      title="${esc(p.pattern_name)} ${esc(p.start_time)}〜${esc(p.end_time)} / ${eff.count}人">
      <span class="rq-bar-label">${esc(p.pattern_name)} ${eff.count}人</span>
    </div>`;
  }).join('');

  const rows = pats.map((p) => {
    const eff = reqBarEffective(p, wd);
    return `<div class="rq-row" data-pid="${esc(p.id)}">
      <span class="rq-row-name">${esc(p.pattern_name)}</span>
      <span class="rq-row-time">${esc(p.start_time)} 〜 ${esc(p.end_time)}</span>
      <button class="rq-step" data-step="-1" data-pid="${esc(p.id)}" title="1人減らす">−</button>
      <input type="number" class="rq-count" min="0" data-pid="${esc(p.id)}" data-name="${esc(p.pattern_name)}" value="${esc(eff.count)}">
      <span class="rq-unit">人</span>
      <button class="rq-step" data-step="1" data-pid="${esc(p.id)}" title="1人増やす">＋</button>
      ${wd !== null && eff.isOverride ? `<button class="rq-reset" data-pid="${esc(p.id)}" title="基本に戻す"><i class="bi bi-arrow-counterclockwise"></i></button>` : ''}
      <button class="rq-edit" data-pid="${esc(p.id)}" title="時間帯を編集"><i class="bi bi-pencil"></i></button>
      <button class="rq-del" data-pid="${esc(p.id)}" title="削除"><i class="bi bi-trash"></i></button>
    </div>`;
  }).join('');

  const note = wd === null
    ? '<div class="small text-secondary mb-2">曜日ごとに人数を変えたいときは、上のタブで曜日を選んでください。</div>'
    : `<div class="small text-secondary mb-2">${esc(REQ_BAR_WD_LABELS[wd])}曜日の人数を設定しています。<strong>時間帯そのものを変えると全曜日に反映されます。</strong></div>`;

  safeSetHTML(host, `${note}
    <div class="rq-wrap">
      <div class="tl-axis-row"><div class="tl-name"></div><div class="tl-axis">${hours.join('')}</div></div>
      <div class="rq-track-row">
        <div class="tl-name"></div>
        <div class="tl-track rq-track" id="reqBarTrack" style="${trackVars};--rq-max:${maxCount}">${bars}</div>
      </div>
    </div>
    <div class="rq-rows">${rows}</div>`);

  bindReqBarRows(body);
}

/** 数値欄と ± ボタン、編集・削除・基本に戻すのバインド。 */
function bindReqBarRows(body) {
  const host = body.querySelector('#reqBarBody');
  if (!host) return;

  host.querySelectorAll('.rq-count').forEach((inp) => inp.addEventListener('input', () => {
    setReqBarCount(body, inp.dataset.pid, parseInt(inp.value, 10));
  }));
  host.querySelectorAll('.rq-step').forEach((b) => b.addEventListener('click', () => {
    const cur = reqBarEffective(findReqPattern(b.dataset.pid), reqBarState.weekday).count;
    setReqBarCount(body, b.dataset.pid, cur + parseInt(b.dataset.step, 10));
  }));
  host.querySelectorAll('.rq-reset').forEach((b) => b.addEventListener('click', () => {
    const p = findReqPattern(b.dataset.pid);
    if (!p || reqBarState.weekday === null) return;
    delete (p.weekday_required || {})[String(reqBarState.weekday)];
    reqBarState.dirty = true;
    renderReqBarTrack(body);
  }));
  host.querySelectorAll('.rq-edit').forEach((b) => b.addEventListener('click', () => {
    const p = findReqPattern(b.dataset.pid);
    if (!p) return;
    openPatternModal({ edit: p.id, n: p.pattern_name, st: p.start_time, et: p.end_time, req: p.required_staff },
      () => loadReqBar(body));
  }));
  host.querySelectorAll('.rq-del').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('この時間帯を削除しますか？曜日別の設定も削除されます。')) return;
    try {
      await api(`/shop/patterns/${b.dataset.pid}`, { method: 'DELETE' });
      toast('削除しました', 'success');
      loadReqBar(body);
    } catch (e) { toast(e.message, 'error'); }
  }));
}

function findReqPattern(pid) {
  return reqBarState.patterns.find((p) => String(p.id) === String(pid));
}

/** 人数を設定して再描画する。バーの高さと数値欄はここを通じて常に同期する。 */
function setReqBarCount(body, pid, count) {
  const p = findReqPattern(pid);
  if (!p) return;
  const n = Math.max(0, Math.round(isNaN(count) ? 0 : count));
  if (reqBarState.weekday === null) {
    p.required_staff = n;
  } else {
    p.weekday_required = p.weekday_required || {};
    p.weekday_required[String(reqBarState.weekday)] = n;
  }
  reqBarState.dirty = true;
  renderReqBarTrack(body);
}
```

`safeSetHTML` / `emptyState` / `_extHourLabel` / `openPatternModal` は既存関数。存在と引数を確認してから使うこと。

- [ ] **Step 4: CSS を追加する**

`public/style.css` の末尾（`@media print` ブロックより**前**）に新規セクションを追加する。**印刷ブロックの中に入れないこと。**

```css
/* ---------- 34. Required Staff Bar (必要人数バー) ---------- */
.rq-tabs { display: flex; gap: 2px; flex-wrap: wrap; margin-bottom: 12px; }
.rq-tab {
  border: 1px solid var(--rule); background: var(--surface); color: var(--ink-2);
  border-radius: var(--radius-sm); padding: 6px 14px; min-height: 40px;
  font-size: .88rem; cursor: pointer;
}
.rq-tab.active { background: var(--info); color: var(--paper); border-color: var(--info); font-weight: 700; }
.rq-tab.rq-sun { color: var(--danger); }
.rq-tab.rq-sat { color: var(--info); }
.rq-tab.rq-sun.active, .rq-tab.rq-sat.active { color: var(--paper); }

.rq-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.rq-track-row { display: flex; align-items: flex-end; min-width: 480px; }
/* バーは下端揃え。高さが人数を表すので、上に伸びる形にする。 */
.rq-track {
  position: relative; display: flex; align-items: flex-end;
  height: calc(var(--rq-max, 4) * 14px + 24px); min-height: 60px;
}
.rq-bar {
  position: absolute; bottom: 0;
  background: var(--role-part-time); color: var(--role-part-time-ink);
  border: 1px solid var(--grid-4h); border-radius: var(--radius-xs);
  display: flex; align-items: center; justify-content: center;
  font-size: .74rem; font-weight: 700; overflow: hidden; white-space: nowrap;
}
.rq-bar.rq-override { background: var(--role-manager); color: var(--role-manager-ink); }
/* 0人は「募集しない」。塗りを外して斜線で区別する。 */
.rq-bar.rq-zero {
  background: repeating-linear-gradient(45deg, var(--zebra), var(--zebra) 4px, transparent 4px, transparent 8px);
  color: var(--ink-3);
}
.rq-bar-label { padding: 0 4px; }

.rq-rows { margin-top: 14px; display: flex; flex-direction: column; gap: 6px; }
.rq-row {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 10px; border: 1px solid var(--rule); border-radius: var(--radius-sm);
  background: var(--surface);
}
.rq-row-name { font-weight: 700; min-width: 4em; }
.rq-row-time { font-family: var(--font-num); color: var(--ink-2); font-size: .82rem; min-width: 9em; }
.rq-step {
  width: 40px; min-height: 40px; border: 1px solid var(--rule);
  background: var(--surface); color: var(--ink); border-radius: var(--radius-sm);
  font-size: 1.1rem; line-height: 1; cursor: pointer;
}
.rq-count {
  width: 64px; min-height: 40px; text-align: center;
  font-family: var(--font-num); font-size: 1rem;
  border: 1px solid var(--rule); border-radius: var(--radius-sm);
  background: var(--surface); color: var(--ink);
}
.rq-unit { color: var(--ink-2); font-size: .82rem; }
.rq-reset, .rq-edit, .rq-del {
  width: 40px; min-height: 40px; border: none; background: transparent;
  color: var(--ink-2); cursor: pointer; border-radius: var(--radius-sm);
}
.rq-del { color: var(--danger); }
```

- [ ] **Step 5: E2E が通ることを確認する**

Run:
```bash
node --check public/app.js
npx playwright test e2e/required_staff_bar.spec.js
```
Expected: 5件すべて PASS

- [ ] **Step 6: デザイントークンのテストが通ることを確認する**

`tests/test_design_tokens.py` はコントラスト比を検査する。新しい CSS がこれに引っかかる可能性がある。

Run:
```bash
.venv/bin/python -m pytest tests/test_design_tokens.py -v
```
Expected: 全件 PASS。**FAIL したら CSS の色を直すこと。テストを緩めてはいけない。**

- [ ] **Step 7: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
node --check public/app.js
git add public/app.js public/style.css e2e/required_staff_bar.spec.js
git commit -m "feat(ui): 必要人数の設定をバー表示にする

店長から「必要人数の設定画面が使いにくく表示も分かりにくい。シフト表と
同じようなバーで設定したい」との要望を受けての対応。

8列×行数の数値マトリクスをやめ、曜日タブ + 1日分のタイムライン表示に
置き換える。バーの高さが人数を表し、数値欄と ± ボタンも併置する。
0人は斜線で「募集しない」と分かるようにした。

保存時に画面の文字列 \"09:00 - 22:00\" を再パースしていた構造も廃し、
state から一括APIに送る形にした。"
```

---

### Task 5: 保存とドラッグによる人数変更

Task 4 の描画に、一括保存と「バーの上端を上下にドラッグして人数を変える」操作を足す。

**Files:**
- Modify: `public/app.js`（`renderReqBarTrack` にドラッグハンドル、`bindReqBarRows` の隣に `installReqBarDrag`、`#reqBarSave` のバインド）
- Modify: `public/style.css`（ドラッグハンドル）
- Modify: `e2e/required_staff_bar.spec.js`

**Interfaces:**
- Consumes: Task 4 の `reqBarState` / `setReqBarCount` / `renderReqBarTrack`、Task 2 の `PUT /api/shop/patterns/bulk`
- Produces: `installReqBarDrag(host, body)`、`saveReqBar(body)`

- [ ] **Step 1: E2E の失敗テストを書く**

`e2e/required_staff_bar.spec.js` に追記:

```js
test('数値欄を変えて保存できる', async ({ page }) => {
  let sent = null;
  await page.route('**/api/shop/patterns/bulk', async (route) => {
    sent = JSON.parse(route.request().postData());
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, warnings: [] }) });
  });

  await page.fill('.rq-count[data-name="早番"]', '5');
  await page.click('#reqBarSave');
  await expect.poll(() => sent).not.toBeNull();

  const asa = sent.patterns.find((p) => p.pattern_name === '早番');
  expect(asa.required_staff).toBe(5);
});

test('曜日タブで変えた人数は曜日別として送られる', async ({ page }) => {
  let sent = null;
  await page.route('**/api/shop/patterns/bulk', async (route) => {
    sent = JSON.parse(route.request().postData());
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, warnings: [] }) });
  });

  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  await page.fill('.rq-count[data-name="早番"]', '4');
  await page.click('#reqBarSave');
  await expect.poll(() => sent).not.toBeNull();

  const asa = sent.patterns.find((p) => p.pattern_name === '早番');
  expect(asa.required_staff).toBe(2);          // 基本値は変わらない
  expect(asa.weekday_required['6']).toBe(4);   // 土曜だけ4人
});

test('＋ボタンでバーの高さが伸びる', async ({ page }) => {
  const before = await page.evaluate(() =>
    document.querySelector('.rq-bar[data-name="早番"]').getBoundingClientRect().height);
  await page.click('.rq-step[data-step="1"][data-pid]:left-of(.rq-count[data-name="早番"])').catch(async () => {
    // :left-of が使えない場合は data-pid で引く
    const pid = await page.getAttribute('.rq-count[data-name="早番"]', 'data-pid');
    await page.click(`.rq-step[data-step="1"][data-pid="${pid}"]`);
  });
  const after = await page.evaluate(() =>
    document.querySelector('.rq-bar[data-name="早番"]').getBoundingClientRect().height);
  expect(after).toBeGreaterThan(before);
});

test('バーを上にドラッグすると人数が増える', async ({ page }) => {
  const box = await page.locator('.rq-bar[data-name="早番"] .rq-drag-top').boundingBox();
  expect(box).not.toBeNull();

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y - 28, { steps: 6 });
  await page.mouse.up();

  const v = await page.inputValue('.rq-count[data-name="早番"]');
  expect(parseInt(v, 10)).toBeGreaterThan(2);
});

test('0人未満にはならない', async ({ page }) => {
  const pid = await page.getAttribute('.rq-count[data-name="早番"]', 'data-pid');
  for (let i = 0; i < 5; i++) await page.click(`.rq-step[data-step="-1"][data-pid="${pid}"]`);
  const v = await page.inputValue('.rq-count[data-name="早番"]');
  expect(parseInt(v, 10)).toBe(0);
});
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
npx playwright test e2e/required_staff_bar.spec.js
```
Expected: 新規5件が FAIL（`#reqBarSave` を押してもリクエストが飛ばない、`.rq-drag-top` が無い）。既存5件は PASS。

`:left-of()` は Playwright の実験的セレクタ。使えない場合は `data-pid` で引く方に統一してよい。

- [ ] **Step 3: 保存を実装する**

`public/app.js` の `renderShiftMatrixTab` 内、`#reqBarAdd` のバインドの直後に追加:

```js
  body.querySelector('#reqBarSave')?.addEventListener('click', () => saveReqBar(body));
```

そして `setReqBarCount` の後に追加:

```js
/** 編集中の state を一括APIで保存する。
 *  従来は画面の文字列 "09:00 - 22:00" を再パースして行ごとに2リクエスト送っていた。
 *  表示を変えると壊れるうえ N 件で 2N 回の往復が発生していたため、state から一括で送る。 */
async function saveReqBar(body) {
  const msg = body.querySelector('#reqBarMsg');
  const payload = {
    patterns: reqBarState.patterns.map((p) => ({
      id: p.id,
      pattern_name: p.pattern_name,
      start_time: p.start_time,
      end_time: p.end_time,
      required_staff: Math.max(0, Math.round(p.required_staff || 0)),
      weekday_required: p.weekday_required || {},
    })),
  };
  try {
    const r = await api('/shop/patterns/bulk', { method: 'PUT', body: JSON.stringify(payload) });
    reqBarState.dirty = false;
    toast('保存しました', 'success');
    // サーバが返す労働時間の警告（9h/13h 超）は従来フロントで捨てていた。必ず見せる。
    (r.warnings || []).forEach((w) => toast(`${w.pattern_name}: ${w.warning}`, 'warning'));
    if (msg) msg.textContent = '';
    loadReqBar(body);
  } catch (e) {
    if (msg) safeSetHTML(msg, `<span class="text-danger">${esc(e.message)}</span>`);
    toast(e.message, 'error');
  }
}
```

- [ ] **Step 4: ドラッグハンドルを描画に足す**

`renderReqBarTrack` のバー生成部、`<span class="rq-bar-label">` の**前**に追加:

```js
      <span class="rq-drag-top" title="上下にドラッグして人数を変える"></span>
```

- [ ] **Step 5: 上下ドラッグを実装する**

`bindReqBarRows` の末尾に `installReqBarDrag(host, body);` を追加し、その下に実装する。

```js
/** バー上端の上下ドラッグで人数を変える。
 *  シフト表の installDraftTimelineDrag と同じ作法（Pointer Events + setPointerCapture、
 *  タッチはロングプレスで開始してスクロールを妨げない）を踏襲する。 */
function installReqBarDrag(host, body) {
  let drag = null;

  const onMove = (ev) => {
    if (!drag) return;
    // 上に動かすほど人数が増える。1人あたり REQ_BAR_UNIT_PX。
    const dy = drag.startY - ev.clientY;
    const next = reqBarCountFromPx(reqBarHeightPx(drag.startCount) + dy);
    if (next !== drag.lastCount) {
      drag.lastCount = next;
      setReqBarCount(body, drag.pid, next);
      // 再描画でバー要素が作り直されるため、参照を取り直す
      drag.bar = host.querySelector(`.rq-bar[data-pid="${drag.pid}"]`);
    }
    ev.preventDefault();
  };

  const onUp = () => {
    if (!drag) return;
    drag.bar?.classList.remove('rq-dragging');
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    window.removeEventListener('pointercancel', onUp);
    drag = null;
  };

  host.querySelectorAll('.rq-drag-top').forEach((handle) => {
    handle.addEventListener('pointerdown', (ev) => {
      const bar = ev.target.closest('.rq-bar');
      if (!bar) return;
      const pid = bar.dataset.pid;
      const p = findReqPattern(pid);
      if (!p) return;
      drag = {
        pid, bar,
        startY: ev.clientY,
        startCount: reqBarEffective(p, reqBarState.weekday).count,
        lastCount: null,
      };
      bar.classList.add('rq-dragging');
      try { ev.target.setPointerCapture(ev.pointerId); } catch (e) { /* 未対応でも動く */ }
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('pointercancel', onUp);
      ev.preventDefault();
    });
  });
}
```

**注意**: `setReqBarCount` は `renderReqBarTrack` を呼んで DOM を作り直す。そのためドラッグ中にハンドル要素が消える。`window` にリスナを付けているので移動自体は追えるが、`setPointerCapture` の対象が消える点に注意すること。E2E が通らない場合は、ドラッグ中は再描画せず高さだけ直接書き換え、`pointerup` で確定する方式に変えてよい。**方式を変えた場合はレポートに理由を書くこと。**

- [ ] **Step 6: ドラッグハンドルの CSS を足す**

`public/style.css` の `.rq-bar-label` の後に追加:

```css
/* 上端の掴みしろ。バーが低いときも掴めるよう、バーの外側にもはみ出させる。 */
.rq-drag-top {
  position: absolute; left: 0; right: 0; top: -6px; height: 14px;
  cursor: ns-resize; touch-action: none;
}
.rq-drag-top::after {
  content: ''; position: absolute; left: 50%; top: 6px;
  width: 24px; height: 3px; margin-left: -12px; border-radius: 2px;
  background: var(--ink-3); opacity: .5;
}
.rq-bar.rq-dragging { outline: 2px solid var(--info); outline-offset: 1px; }
```

- [ ] **Step 7: E2E が通ることを確認する**

Run:
```bash
node --check public/app.js
npx playwright test e2e/required_staff_bar.spec.js
```
Expected: 10件すべて PASS

- [ ] **Step 8: テストが実際に守っていることを確認する**

次を一時的に壊し、**対応するテストが実際に落ちること**を確認する。確認したら戻す。実出力をレポートに貼ること。

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `saveReqBar` の `api(...)` 呼び出しを消す | 「数値欄を変えて保存できる」 |
| `setReqBarCount` の曜日分岐を `p.required_staff = n` 固定にする | 「曜日タブで変えた人数は曜日別として送られる」 |
| `installReqBarDrag` の `onMove` を空にする | 「バーを上にドラッグすると人数が増える」 |
| `reqBarCountFromPx` の `Math.max(0, ...)` を外す | 「0人未満にはならない」 |

**落ちないテストがあれば、そのテストは何も守っていない。** 検出できる形に書き直すこと。

- [ ] **Step 9: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
node --check public/app.js
git add public/app.js public/style.css e2e/required_staff_bar.spec.js
git commit -m "feat(ui): 必要人数をバーのドラッグと数値欄の両方で変えられるようにする

バー上端の上下ドラッグ、± ボタン、数値入力の3つが同じ state を通るため
常に同期する。保存は一括APIに1回だけ送る。

従来フロントが読み捨てていたサーバ側の労働時間警告（9h/13h超）も
トーストで表示するようにした。"
```

---

### Task 6: 時間帯のドラッグ伸縮

バーの左右端をドラッグして時間帯そのものを15分単位で伸縮できるようにする。**時間帯は全曜日共通**なので、変更が全曜日に影響することを操作前と操作後の両方で示す。

**Files:**
- Modify: `public/app.js`（`renderReqBarTrack` に左右ハンドル、`installReqBarDrag` に横方向モードを追加）
- Modify: `public/style.css`
- Modify: `e2e/required_staff_bar.spec.js`

**Interfaces:**
- Consumes: Task 5 の `installReqBarDrag`、Task 3 の `reqBarRange`
- Produces: なし（既存関数の拡張）

- [ ] **Step 1: E2E の失敗テストを書く**

`e2e/required_staff_bar.spec.js` に追記:

```js
test('バーの右端をドラッグして時間帯を伸ばせる', async ({ page }) => {
  const before = await page.textContent('.rq-row[data-pid] .rq-row-time');
  const box = await page.locator('.rq-bar[data-name="早番"] .rq-drag-end').boundingBox();
  expect(box).not.toBeNull();

  const trackBox = await page.locator('#reqBarTrack').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  // 右へ 1時間ぶん動かす
  await page.mouse.move(box.x + trackBox.width / 13, box.y + box.height / 2, { steps: 8 });
  await page.mouse.up();

  const after = await page.textContent('.rq-row[data-pid] .rq-row-time');
  expect(after).not.toBe(before);
});

test('時間帯を変えると全曜日に影響する旨が出る', async ({ page }) => {
  await expect(page.locator('.rq-drag-end').first()).toBeVisible();
  const title = await page.getAttribute('.rq-bar[data-name="早番"] .rq-drag-end', 'title');
  expect(title).toContain('全曜日');
});

test('時間帯は15分単位にスナップする', async ({ page }) => {
  const box = await page.locator('.rq-bar[data-name="早番"] .rq-drag-end').boundingBox();
  const trackBox = await page.locator('#reqBarTrack').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + trackBox.width / 40, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();

  const t = await page.textContent('.rq-row[data-pid] .rq-row-time');
  const m = t.match(/(\d{2}):(\d{2})\s*〜\s*(\d{2}):(\d{2})/);
  expect(m).not.toBeNull();
  expect(parseInt(m[4], 10) % 15).toBe(0);
});
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
npx playwright test e2e/required_staff_bar.spec.js
```
Expected: 新規3件が FAIL（`.rq-drag-end` が無い）。既存10件は PASS。

- [ ] **Step 3: 左右ハンドルを描画に足す**

`renderReqBarTrack` のバー生成部、`rq-drag-top` の隣に追加:

```js
      <span class="rq-drag-start" title="時間帯の開始を変える（全曜日に反映されます）"></span>
      <span class="rq-drag-end" title="時間帯の終了を変える（全曜日に反映されます）"></span>
```

- [ ] **Step 4: 時刻変換のヘルパを追加する**

Task 3 の純関数群の末尾に追加し、`tests/test_required_bar_geometry.py` にもテストを足す。

```js
/** 軸上の X 位置（0..1 の比率）を "HH:MM" に変換する。15分単位に丸める。
 *  拡張時間（24時以降）は翌日の時刻として返す。 */
function reqBarTimeFromRatio(ratio, range) {
  const r = Math.max(0, Math.min(1, ratio));
  const min = Math.round((range.rangeMin + r * range.rangeLen) / 15) * 15;
  const h = Math.floor(min / 60) % 24;
  const m = min % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}
```

`tests/test_required_bar_geometry.py` に追記:

```python
class TestReqBarTimeFromRatio:
    def test_snaps_to_15min(self):
        rng = {"minH": 9, "maxH": 22, "rangeMin": 540, "rangeLen": 780}
        out = run_js(_fns("reqBarTimeFromRatio"),
                     f"JSON.stringify([0, 0.01, 0.5, 1].map((r) => reqBarTimeFromRatio(r, {json.dumps(rng)})))")
        vals = json.loads(out)
        for v in vals:
            assert int(v.split(":")[1]) % 15 == 0, f"{v} が15分単位でない"

    def test_start_and_end_of_range(self):
        rng = {"minH": 9, "maxH": 22, "rangeMin": 540, "rangeLen": 780}
        out = run_js(_fns("reqBarTimeFromRatio"),
                     f"JSON.stringify([reqBarTimeFromRatio(0, {json.dumps(rng)}), reqBarTimeFromRatio(1, {json.dumps(rng)})])")
        assert json.loads(out) == ["09:00", "22:00"]

    def test_past_midnight_wraps(self):
        rng = {"minH": 9, "maxH": 26, "rangeMin": 540, "rangeLen": 1020}
        out = run_js(_fns("reqBarTimeFromRatio"),
                     f"String(reqBarTimeFromRatio(1, {json.dumps(rng)}))")
        assert out.strip() == "02:00", "翌日の時刻が 26:00 のまま返っている"
```

- [ ] **Step 5: 横方向ドラッグを実装する**

`installReqBarDrag` を拡張し、`.rq-drag-start` / `.rq-drag-end` にも `pointerdown` を付ける。縦方向と分けて `mode` で扱う。

実装の要点:

- `mode: 'count' | 'start' | 'end'` を drag オブジェクトに持つ
- 横方向は `#reqBarTrack` の `getBoundingClientRect()` を基準に `ratio = (ev.clientX - rect.left) / rect.width` を出し、`reqBarTimeFromRatio(ratio, range)` で時刻にする
- `range` はドラッグ開始時の `reqBarRange(reqBarState.patterns)` を保持する（ドラッグ中に軸が動くと座標がずれるため）
- 最小幅は15分。`start >= end` になる操作は無視する
- 変更は `p.start_time` / `p.end_time` に入れる（**曜日によらず共通**）
- 確定時（`pointerup`）に `toast('時間帯を変更しました（全曜日に反映されます）')` を出す
- `reqBarState.dirty = true` にする

**縦ドラッグと同様、`setReqBarCount` 相当の再描画でハンドルが作り直される点に注意。** 描画のたびにハンドル参照を取り直すか、ドラッグ中は再描画せず `pointerup` で確定する方式にすること。**選んだ方式と理由をレポートに書くこと。**

- [ ] **Step 6: CSS を足す**

```css
/* 左右のハンドルは色を変える。時間帯の変更は全曜日に影響するため、
   人数の変更（上端）と取り違えないようにする。 */
.rq-drag-start, .rq-drag-end {
  position: absolute; top: 0; bottom: 0; width: 12px;
  cursor: ew-resize; touch-action: none;
}
.rq-drag-start { left: -6px; }
.rq-drag-end { right: -6px; }
.rq-drag-start::after, .rq-drag-end::after {
  content: ''; position: absolute; top: 50%; left: 50%;
  width: 3px; height: 18px; margin: -9px 0 0 -1.5px; border-radius: 2px;
  background: var(--warning); opacity: .8;
}
```

- [ ] **Step 7: E2E とユニットテストが通ることを確認する**

Run:
```bash
node --check public/app.js
.venv/bin/python -m pytest tests/test_required_bar_geometry.py -v
npx playwright test e2e/required_staff_bar.spec.js
```
Expected: すべて PASS

- [ ] **Step 8: テストが実際に守っていることを確認する**

`reqBarTimeFromRatio` の `Math.round(... / 15) * 15` から `/ 15) * 15` を外し、「時間帯は15分単位にスナップする」と `test_snaps_to_15min` が**実際に落ちること**を確認する。確認したら戻す。実出力をレポートに貼ること。

- [ ] **Step 9: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
node --check public/app.js
git add public/app.js public/style.css e2e/required_staff_bar.spec.js tests/test_required_bar_geometry.py
git commit -m "feat(ui): 必要人数バーの左右ドラッグで時間帯を伸縮できるようにする

15分単位でスナップする。時間帯は全曜日共通なので、左右ハンドルは
人数用の上端ハンドルと色を分け、変更時に全曜日へ反映される旨を
トーストで明示する。"
```

---

### Task 7: 時間帯0件のときの誤表示を直す

設計書 Phase 4-1 の一部だが、Task 4 で同じ画面を触るためここで一緒に片付ける。

時間帯（`shift_patterns`）が0件のとき、`_computeHourlyGaps()` が空配列を返し（`public/app.js:798-799` 付近）、`loadShortage()` が**緑のチェックマークで「不足なし — 全時間帯充足」と表示する**（`public/app.js:1783` 付近）。新規店舗が最初に見る画面がこれで、画面が嘘をついている。

**Files:**
- Modify: `public/app.js`（`loadShortage` とダッシュボードKPI）
- Create: `tests/test_shortage_no_patterns.py`

**Interfaces:**
- Consumes: なし
- Produces: なし

- [ ] **Step 1: 現在の行番号を特定する**

`public/app.js` は Phase 1 で行が動いている。次で実際の位置を確認してからレポートに書くこと。

```bash
grep -n "_computeHourlyGaps\|全時間帯充足\|function loadShortage\|shortage-none" public/app.js
```

- [ ] **Step 2: 失敗するテストを書く**

Create `tests/test_shortage_no_patterns.py`:

```python
"""tests/test_shortage_no_patterns.py — 時間帯0件のときに「充足」と嘘をつかないこと。

実行: ./.venv/bin/python -m pytest tests/test_shortage_no_patterns.py -v

新規店舗は shift_patterns も shift_request_periods もゼロ件で始まる
（src/admin_api.py の店舗作成が shops と manager しか作らない）。
そのとき _computeHourlyGaps が空配列を返し、画面が緑のチェックマークで
「不足なし — 全時間帯充足」と表示していた。最初に見る画面が嘘をついていた。
"""
import json
import os

import pytest

from helpers import extract_js_function, run_js

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    with open(os.path.join(ROOT, "public/app.js"), encoding="utf-8") as f:
        return f.read()


class TestShortageMessageSource:
    """「全時間帯充足」の表示がパターン0件と条件分岐していること。"""

    def test_no_pattern_branch_exists(self):
        js = _src()
        # 「時間帯が未設定」を伝える文言が実装に存在すること
        assert "時間帯が未設定" in js, \
            "パターン0件のときの案内が無い（緑のチェックで「充足」と出てしまう）"

    def test_fulfilled_message_is_not_unconditional(self):
        """「全時間帯充足」がパターン0件でも出る書き方になっていないこと。"""
        js = _src()
        idx = js.find("全時間帯充足")
        assert idx > 0
        # 直前 600 文字の中にパターン件数の判定があること
        window = js[max(0, idx - 600):idx]
        assert ("patterns" in window and "length" in window), \
            "「全時間帯充足」の直前にパターン件数の判定が無い"
```

- [ ] **Step 3: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_shortage_no_patterns.py -v
```
Expected: 2件とも FAIL

- [ ] **Step 4: 実装を直す**

Step 1 で特定した `loadShortage` 相当の関数で、`appState.patterns` が空のときに「充足」ではなく案内を出す。

```js
    // 時間帯が1つも無いと _computeHourlyGaps は常に空配列を返す。
    // それを「不足なし」と表示すると、設定を何もしていない新規店舗に
    // 緑のチェックで「全時間帯充足」と出てしまう（画面が嘘をつく）。
    if (!(appState.patterns || []).length) {
      safeSetHTML(box, `<div class="info-box">
        <i class="bi bi-exclamation-triangle"></i> 時間帯が未設定です。
        設定 → シフト設定で登録すると、ここに不足が表示されます。
        <button class="btn btn-sm btn-light mt-2" id="shortageGoSettings">設定を開く</button>
      </div>`);
      box.querySelector('#shortageGoSettings')?.addEventListener('click', () => navigateTo('settings'));
      return;
    }
```

`appState.patterns` は `ensureBusinessHours()` が埋める。呼ばれていない経路がある場合は、先に `await ensureBusinessHours()` すること。**実際にどの関数が `appState.patterns` を埋めるかを確認してから書くこと。**

ダッシュボードのKPI（`〇枠不足 / 充足` を出している箇所）にも同じ判定を入れる。位置は Step 1 の grep で特定すること。

- [ ] **Step 5: E2E を足す**

`e2e/required_staff_bar.spec.js` に追記:

```js
test('時間帯0件のときシフト画面が「充足」と嘘をつかない', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  const d = await (await request.get('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
  })).json();
  for (const p of d.patterns) {
    await request.delete(`/api/shop/patterns/${p.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }

  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="shifts"]');
  await page.waitForSelector('#shortageBox');

  const text = await page.textContent('#shortageBox');
  expect(text).not.toContain('全時間帯充足');
  expect(text).toContain('時間帯が未設定');
});
```

- [ ] **Step 6: 全部が通ることを確認する**

Run:
```bash
node --check public/app.js
.venv/bin/python -m pytest tests/ -q
npx playwright test e2e/required_staff_bar.spec.js
npx playwright test
```
Expected: すべて PASS

- [ ] **Step 7: コミット**

```bash
git add public/app.js tests/test_shortage_no_patterns.py e2e/required_staff_bar.spec.js
git commit -m "fix(ui): 時間帯0件のときに「全時間帯充足」と嘘をつくのを直す

新規店舗は shift_patterns がゼロ件で始まるため、_computeHourlyGaps が
空配列を返し、緑のチェックマークで「不足なし — 全時間帯充足」と
表示されていた。店長が最初に見る画面が嘘をついていた。

パターン0件のときは設定への導線つきの案内を出す。"
```

---

## Self-Review

**設計書（Phase 2 該当部分）のカバレッジ**

| 設計書の項目 | 対応タスク |
|---|---|
| 2-1. 画面構成（曜日タブ + 1日表示） | Task 4 |
| 2-2. バーの描画と操作（高さ=人数、上下ドラッグ、左右ドラッグ、15分スナップ） | Task 3, 4, 5, 6 |
| 2-3. 保存を作り直す（一括API、DOM再パース廃止） | Task 2, 5 |
| 2-4. バグA（基本必要人数0を保存できない） | Task 1 Step 11-12 |
| 2-4. バグB（必要人数0が上限なし） | Task 1 Step 1-10 |
| 2-4. バグC（労働時間の警告が捨てられている） | Task 5 Step 3 |
| 2-5. テスト（pytest / E2E / unit） | 各タスク |
| 4-1. 「全時間帯充足」の誤表示 | Task 7 |

**設計書から変えた点**

設計書では Phase 4-1（「全時間帯充足」の誤表示）を Phase 4 に置いていたが、Task 4 で同じ画面を触るため Task 7 として前倒しした。Phase 4 の残りは予定どおり別計画で扱う。

**依存関係**

Task 1 → Task 2（0 を保存できることが前提）→ Task 5（一括APIを呼ぶ）
Task 3 → Task 4（純関数を使う）→ Task 5 → Task 6
Task 7 は独立（Task 4 と同じ画面を触るので後に置く）

**型・名前の整合**

- `reqBarRange` の戻り値 `{minH, maxH, rangeMin, rangeLen}` は Task 3 で定義し、Task 4・6 が同じキー名で使う
- `reqBarEffective` の戻り値 `{count, isOverride}` は Task 3 で定義し、Task 4・5 が使う
- `reqBarState` は Task 4 で定義し、Task 5・6 が使う
- `PUT /api/shop/patterns/bulk` のペイロード形は Task 2 で定義し、Task 5 の `saveReqBar` が同じ形で送る。E2E（Task 5 Step 1）も同じキー名を検証する
- `#reqBarTrack` / `.rq-bar[data-pid][data-name]` / `.rq-count[data-pid][data-name]` は Task 4 で定義し、Task 5・6 の E2E が使う

**Phase 1 の教訓の反映**

Phase 1 では「緑だが何も守っていないテスト」が5件見つかった。本計画では Task 3 Step 5、Task 5 Step 8、Task 6 Step 8 に「実装を一時的に壊してテストが落ちることを確認する」ステップを明示的に置いた。**このステップを飛ばしてはいけない。**

**残る不確実性**

Task 5・6 のドラッグ実装は、`setReqBarCount` が再描画してハンドル要素を作り直す点で壊れやすい。E2E が通らない場合の代替案（ドラッグ中は再描画せず `pointerup` で確定）を各タスクに明記した。実装者が方式を変えた場合はレポートに理由を書かせる。

Task 1 の `_check_slot_cap`（Step 10）は、実装を読んでからでないと修正内容が確定しない。そのため「まず該当箇所を特定してレポートに書く」ことを Step の冒頭に置いている。
