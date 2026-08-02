# スタッフの働き方の変化に気づく（気にかけたい人） 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 勤務データの変化（出勤の減少・直前の変更や取消の増加）を検出し、店長が本人に声をかけるきっかけをダッシュボードに出す。

**Architecture:** 検出は外部依存の無い純関数モジュール `src/staff_attention.py` に閉じ込め、DBアクセスは `src/app.py` の新エンドポイントが、声かけの言い回しは `src/ai.py` が担う。この3層を分けるのは、しきい値の変更・DBスキーマの変更・LLMの有無が互いに影響しないようにするため。ダッシュボードは該当者がいるときだけカードを出す。

**Tech Stack:** Python Flask / Vanilla JS / SQLite (本番 Cloudflare D1) / pytest / Playwright

## Global Constraints

- **新しい依存パッケージを追加しない。** `requirements.txt` は Flask, python-dotenv, requests, pytest, gunicorn のみ
- コード内コメントは日本語。「なぜ」を書く
- コミットメッセージは `feat:` / `fix:` / `test:` / `docs:` プレフィックス + 日本語サマリ、末尾に `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Python の実行は必ず `.venv/bin/python`
- `public/app.js` を編集したら必ず `node --check public/app.js` を通す
- **Playwright は必ずフォアグラウンドで実行する**（`timeout` を 600000 に）。撮影用スクリプトは通常スイートから除外済み
- **基準値（この計画の開始時点）**: pytest `1295 passed, 1 skipped` / E2E `154 passed` / `tests/run_tests.py` EXIT 0
- 各タスクの最後に pytest 全件・`tests/run_tests.py` を確認する
- **テストは「実装のどの行を消せば落ちるか」を言える形で書く。** 各テストについて**守りたい実装の1行だけを消して**赤になることを確認し、実出力をレポートに貼ること
- **原因や状態を断定する文言を出力しない。** 「離職」「メンタル」「やる気」などの語を、AIプロンプト・フォールバック文言・画面のどこにも入れない（設計書「スコープを明確に区切る」）

## 設計書

`docs/superpowers/specs/2026-08-02-staff-attention-design.md`

## 前提となる調査結果（コードで確認済み・行番号は 2026-08-02 時点）

- 既存のAI分析は `POST /api/shop/ai/review`（`src/app.py:3045`）→ `ai.review_shift_balance`（`src/ai.py:399`）。**その期間のスナップショットのみ**を見ており、過去との比較は無い
- `ai.call_llm(system_prompt, user_prompt, temperature=0.3)`（`src/ai.py:122`）。未設定/失敗時は `None` を返す
- `ai.is_llm_available()`（`src/ai.py:42`）
- `review_shift_balance` は LLM が使えないとき定型文へフォールバックし、戻り値に `source: "llm" | "rule_based"` を含める（`src/ai.py:411-432`）。**この二段構えを踏襲する**
- `change_requests` は `staff_id` / `request_type('change','cancel','add')` / `created_at TEXT DEFAULT (datetime('now'))` を持つ（`schema.sql:157-170`）
- `shifts` は `status('requested','confirmed','modifying')` と `start_datetime TEXT`（`schema.sql:95-112`）
- ダッシュボードの右カラムは `#dashRight`。`SCREENS.dashboard`（`public/app.js:1978`）の中で、`/shop/ai/review` を呼んで「AIからの提案」カードを組み立てている（`public/app.js:2055-2066`）
- `tests/conftest.py:14` が `os.environ["LLM_API_KEY"] = ""` を設定するため、**テストは既定でLLM未接続（フォールバック）経路を通る**
- `tests/helpers.py` に `insert_shop` / `insert_staff` / `insert_request`(status='requested') はあるが、**confirmed のシフトを入れるヘルパは無い**。このプランでは各テスト内で `db.execute` で直接入れる
- `utils.jst_now()` / `utils.jst_today()`（`src/utils.py:11,16`）

## スコープ外

- 働く時間帯の変化・希望の出し方の変化の検出
- スタッフ本人への表示・通知
- しきい値の画面からの調整

---

### Task 1: 変化を検出する純関数

**Files:**
- Create: `src/staff_attention.py`
- Create: `tests/test_staff_attention.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - 定数 `RECENT_DAYS=30` / `BASE_DAYS=60` / `MIN_BASE_ATTENDANCE=4` / `DROP_RATIO=0.6` / `MIN_RECENT_REQUESTS=3` / `REQUEST_SPIKE_RATIO=2.0`
  - `find_attention(staff_rows, shift_rows, request_rows, today) -> list[dict]`
    - 戻り値の要素: `{"staff_id": int, "name": str, "reasons": [...], "score": float}`
    - `reasons` の要素: `{"type": "attendance_drop", "recent": int, "base": float}` または `{"type": "request_spike", "recent": int, "base": float}`

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_staff_attention.py`:

```python
"""tests/test_staff_attention.py — スタッフの働き方の変化を検出する純関数。

実行: ./.venv/bin/python -m pytest tests/test_staff_attention.py -v

設計書: docs/superpowers/specs/2026-08-02-staff-attention-design.md

この関数が返すのは「データがこう変わった」という事実だけで、原因や状態
（離職・体調など）は判定しない。判定しているように見える戻り値を足さないこと。
"""
from datetime import date, timedelta

import pytest

from staff_attention import (
    MIN_BASE_ATTENDANCE, DROP_RATIO, MIN_RECENT_REQUESTS, REQUEST_SPIKE_RATIO,
    find_attention,
)

TODAY = "2026-08-31"


def _d(days_ago):
    """TODAY から days_ago 日前の "YYYY-MM-DD"。"""
    return (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()


def _staff(sid, name, resigned=0):
    return {"id": sid, "name": name, "is_resigned": resigned}


def _shifts(staff_id, days_ago_list):
    """指定の「何日前」に confirmed シフトが1本ずつあることにする。"""
    return [{"staff_id": staff_id, "start_datetime": f"{_d(x)}T09:00:00"} for x in days_ago_list]


def _requests(staff_id, days_ago_list):
    return [{"staff_id": staff_id, "created_at": f"{_d(x)}T12:00:00"} for x in days_ago_list]


class TestAttendanceDrop:
    def test_clear_drop_is_detected(self):
        """基準30日あたり10日 → 直近3日。誰が見ても減っている。"""
        # 直近30日(0-29日前)に3日、その前60日(30-89日前)に20日 → base=10.0
        recent = [1, 5, 9]
        base = list(range(30, 50))
        r = find_attention([_staff(1, "田中")], _shifts(1, recent + base), [], TODAY)
        assert len(r) == 1
        reason = r[0]["reasons"][0]
        assert reason["type"] == "attendance_drop"
        assert reason["recent"] == 3
        assert reason["base"] == 10.0

    def test_ratio_boundary_is_not_detected(self):
        """ちょうど DROP_RATIO ぶん残っていれば「減った」としない。

        base=10.0 に対し recent=6（= 10.0*0.6）。境界は検出しない側に倒す
        （わずかな揺れで毎月名前が出ると、カードが出ること自体の意味が薄れる）。
        """
        base = list(range(30, 50))          # 20日 → base 10.0
        recent = [1, 3, 5, 7, 9, 11]        # 6日
        r = find_attention([_staff(1, "田中")], _shifts(1, recent + base), [], TODAY)
        assert r == []

    def test_just_below_boundary_is_detected(self):
        base = list(range(30, 50))          # base 10.0
        recent = [1, 3, 5, 7, 9]            # 5日 < 6
        r = find_attention([_staff(1, "田中")], _shifts(1, recent + base), [], TODAY)
        assert len(r) == 1

    def test_infrequent_staff_is_ignored(self):
        """もともと月1〜2日の人は対象外（0日になっても騒がない）。"""
        base = [35, 60]                      # 2日 → base 1.0（MIN_BASE_ATTENDANCE 未満）
        r = find_attention([_staff(1, "田中")], _shifts(1, base), [], TODAY)
        assert r == []

    def test_same_day_two_shifts_count_as_one(self):
        """同じ日に2本入っていても1日と数える（中抜けを二重に数えない）。"""
        base = [{"staff_id": 1, "start_datetime": f"{_d(x)}T09:00:00"} for x in range(30, 50)]
        base += [{"staff_id": 1, "start_datetime": f"{_d(x)}T18:00:00"} for x in range(30, 50)]
        # 直近は 5日 × 2本
        recent = [{"staff_id": 1, "start_datetime": f"{_d(x)}T09:00:00"} for x in (1, 3, 5, 7, 9)]
        recent += [{"staff_id": 1, "start_datetime": f"{_d(x)}T18:00:00"} for x in (1, 3, 5, 7, 9)]
        r = find_attention([_staff(1, "田中")], base + recent, [], TODAY)
        assert len(r) == 1
        assert r[0]["reasons"][0]["recent"] == 5
        assert r[0]["reasons"][0]["base"] == 10.0

    def test_resigned_staff_is_ignored(self):
        base = list(range(30, 50))
        r = find_attention([_staff(1, "田中", resigned=1)], _shifts(1, base), [], TODAY)
        assert r == []

    def test_new_staff_without_history_is_ignored(self):
        """基準期間に1日も出ていない人は比べる過去がない（入ったばかり）。"""
        r = find_attention([_staff(1, "新人")], _shifts(1, [1, 2, 3]), [], TODAY)
        assert r == []

    def test_no_shifts_at_all_is_ignored(self):
        r = find_attention([_staff(1, "田中")], [], [], TODAY)
        assert r == []


class TestRequestSpike:
    def test_spike_from_zero_is_detected(self):
        """それまで変更申請ゼロの人が直近30日で3件。"""
        base = list(range(30, 50))  # 出勤は十分あって減っていない
        recent = list(range(0, 20, 2))
        shifts = _shifts(1, recent + base)
        r = find_attention([_staff(1, "田中")], shifts, _requests(1, [2, 8, 14]), TODAY)
        assert len(r) == 1
        reason = [x for x in r[0]["reasons"] if x["type"] == "request_spike"][0]
        assert reason["recent"] == 3
        assert reason["base"] == 0.0

    def test_below_minimum_is_ignored(self):
        """2件では騒がない（たまたま重なることがある）。"""
        base = list(range(30, 50))
        recent = list(range(0, 20, 2))
        shifts = _shifts(1, recent + base)
        r = find_attention([_staff(1, "田中")], shifts, _requests(1, [2, 8]), TODAY)
        assert r == []

    def test_same_level_as_before_is_ignored(self):
        """もともと申請が多い人は、同じ水準なら「増えた」としない。"""
        base = list(range(30, 50))
        recent = list(range(0, 20, 2))
        shifts = _shifts(1, recent + base)
        # 基準60日で8件 → base 4.0。直近4件は 2倍(8件)に届かない
        reqs = _requests(1, [2, 8, 14, 20]) + _requests(1, [32, 38, 44, 50, 56, 62, 68, 74])
        r = find_attention([_staff(1, "田中")], shifts, reqs, TODAY)
        assert r == []

    def test_doubled_is_detected(self):
        base = list(range(30, 50))
        recent = list(range(0, 20, 2))
        shifts = _shifts(1, recent + base)
        # 基準60日で4件 → base 2.0。直近5件は 2.0*2=4.0 以上
        reqs = _requests(1, [1, 5, 9, 13, 17]) + _requests(1, [32, 40, 50, 60])
        r = find_attention([_staff(1, "田中")], shifts, reqs, TODAY)
        assert len(r) == 1
        assert r[0]["reasons"][0]["type"] == "request_spike"


class TestOrderingAndShape:
    def test_sorted_by_severity_then_staff_id(self):
        """変化の大きい順。同点は staff_id 昇順（表示順が毎回変わらないこと）。"""
        base = list(range(30, 50))          # base 10.0
        shifts = _shifts(1, [1] + base) + _shifts(2, [1, 3, 5] + base) + _shifts(3, [1] + base)
        staffs = [_staff(3, "C"), _staff(1, "A"), _staff(2, "B")]
        r = find_attention(staffs, shifts, [], TODAY)
        # 1日まで減った A(1) と C(3) が同点で先、次に 3日の B(2)
        assert [x["staff_id"] for x in r] == [1, 3, 2]

    def test_result_has_no_diagnosis_fields(self):
        """原因や状態を断定するフィールドを持たない（設計書のスコープ）。"""
        base = list(range(30, 50))
        r = find_attention([_staff(1, "田中")], _shifts(1, [1] + base), [], TODAY)
        assert set(r[0].keys()) == {"staff_id", "name", "reasons", "score"}

    def test_empty_inputs(self):
        assert find_attention([], [], [], TODAY) == []
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_staff_attention.py -v
```
Expected: 全件 FAIL（`ModuleNotFoundError: No module named 'staff_attention'`）

- [ ] **Step 3: 実装する**

Create `src/staff_attention.py`:

```python
"""staff_attention.py - スタッフの働き方の変化を検出する。

【なぜ必要か】
  ダッシュボードのAI分析は、その期間のシフトだけを見て偏りや連勤を指摘する。
  見ているのはスナップショットで、過去と比べた変化は見ていない。そのため
  「いつも入っていた人が最近少ない」という、人が離れていく前の変化に気づけない。

【この関数がしないこと】
  原因や状態（離職の意思・体調・人間関係）は判定しない。勤務データから
  分かるのは「働き方が変わった」という事実だけで、そこに解釈を足すと
  外れた決めつけを店長に渡すことになる。返すのは数値と種別だけにする。

【なぜ純関数として切り出すか】
  判定は「何日から何日までを、どう数えて、どのしきい値で比べるか」の塊。
  DBアクセスやLLMと混ざると、しきい値を変えたときの影響が読めなくなる。

外部依存なし（標準ライブラリのみ）。
"""
from datetime import date, timedelta

RECENT_DAYS = 30           # 直近としてみる日数
BASE_DAYS = 60             # 比較の基準に使う、その前の日数
MIN_BASE_ATTENDANCE = 4    # 基準期間の30日あたり出勤日数がこれ未満の人は対象外
DROP_RATIO = 0.6           # 直近が基準のこの割合を下回ったら「減った」
MIN_RECENT_REQUESTS = 3    # 直近30日の変更・取消がこれ未満なら対象外
REQUEST_SPIKE_RATIO = 2.0  # 直近が基準のこの倍数以上なら「増えた」

# 基準期間は直近期間の何倍か（30日あたりへ換算するのに使う）
_BASE_SCALE = BASE_DAYS / RECENT_DAYS


def _in_range(day, lo, hi):
    """lo <= day <= hi（すべて "YYYY-MM-DD" の文字列比較で足りる）。"""
    return lo <= day <= hi


def find_attention(staff_rows, shift_rows, request_rows, today):
    """気にかけたいスタッフを、変化の大きい順に返す。

    staff_rows:   [{"id", "name", "is_resigned"}]
    shift_rows:   [{"staff_id", "start_datetime"}]（確定シフトのみ）
    request_rows: [{"staff_id", "created_at"}]（変更・取消の申請）
    today:        "YYYY-MM-DD"

    戻り値: [{"staff_id", "name", "reasons", "score"}]
      reasons: [{"type": "attendance_drop"|"request_spike", "recent": int, "base": float}]
    """
    t = date.fromisoformat(today)
    recent_lo = (t - timedelta(days=RECENT_DAYS - 1)).isoformat()
    recent_hi = today
    base_lo = (t - timedelta(days=RECENT_DAYS + BASE_DAYS - 1)).isoformat()
    base_hi = (t - timedelta(days=RECENT_DAYS)).isoformat()

    # スタッフごとに「出勤した日の集合」と「申請日の一覧」を期間別に集める。
    # 出勤は日の集合にする（同じ日に2本入っていても1日）。
    recent_days = {}
    base_days = {}
    for sh in shift_rows:
        day = (sh.get("start_datetime") or "")[:10]
        sid = sh.get("staff_id")
        if not day or sid is None:
            continue
        if _in_range(day, recent_lo, recent_hi):
            recent_days.setdefault(sid, set()).add(day)
        elif _in_range(day, base_lo, base_hi):
            base_days.setdefault(sid, set()).add(day)

    recent_reqs = {}
    base_reqs = {}
    for rq in request_rows:
        day = (rq.get("created_at") or "")[:10]
        sid = rq.get("staff_id")
        if not day or sid is None:
            continue
        if _in_range(day, recent_lo, recent_hi):
            recent_reqs[sid] = recent_reqs.get(sid, 0) + 1
        elif _in_range(day, base_lo, base_hi):
            base_reqs[sid] = base_reqs.get(sid, 0) + 1

    out = []
    for s in staff_rows:
        if s.get("is_resigned"):
            continue
        sid = s.get("id")
        base_att_raw = len(base_days.get(sid, ()))
        # 基準期間に1日も出ていない人は、比べる過去がない（入ったばかり）
        if base_att_raw == 0:
            continue
        base_att = base_att_raw / _BASE_SCALE       # 30日あたりへ換算
        recent_att = len(recent_days.get(sid, ()))

        reasons = []
        score = 0.0
        if base_att >= MIN_BASE_ATTENDANCE and recent_att < base_att * DROP_RATIO:
            reasons.append({"type": "attendance_drop",
                            "recent": recent_att, "base": round(base_att, 1)})
            score = max(score, 1 - recent_att / base_att)

        r_recent = recent_reqs.get(sid, 0)
        r_base = base_reqs.get(sid, 0) / _BASE_SCALE
        if r_recent >= MIN_RECENT_REQUESTS and (r_base == 0 or r_recent >= r_base * REQUEST_SPIKE_RATIO):
            reasons.append({"type": "request_spike",
                            "recent": r_recent, "base": round(r_base, 1)})
            # 0除算を避けつつ 0〜1 に収める（基準0件のときは分母を最小件数にする）
            score = max(score, min(1.0, r_recent / max(r_base, MIN_RECENT_REQUESTS) / REQUEST_SPIKE_RATIO))

        if reasons:
            out.append({"staff_id": sid, "name": s.get("name"),
                        "reasons": reasons, "score": round(score, 3)})

    # 変化の大きい順。同点は staff_id 昇順（実行のたびに順番が入れ替わらない）
    out.sort(key=lambda x: (-x["score"], x["staff_id"]))
    return out
```

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_staff_attention.py -v
.venv/bin/python -m pytest tests/ -q
```
Expected: すべて PASS。

**FAIL する場合、テストのしきい値を緩めないこと。** 境界の扱い（ちょうどは検出しない）は
設計書の決定事項なので、実装を直すこと。

- [ ] **Step 5: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `base_att >= MIN_BASE_ATTENDANCE` の条件を消す | `test_infrequent_staff_is_ignored` |
| `recent_att < base_att * DROP_RATIO` を `<=` にする | `test_ratio_boundary_is_not_detected` |
| `if s.get("is_resigned"): continue` を消す | `test_resigned_staff_is_ignored` |
| `if base_att_raw == 0: continue` を消す | `test_new_staff_without_history_is_ignored` |
| 出勤日を `set` でなく件数で数える（`.add(day)` → カウンタ） | `test_same_day_two_shifts_count_as_one` |
| `r_recent >= MIN_RECENT_REQUESTS` を消す | `test_below_minimum_is_ignored` |
| `out.sort(...)` を消す | `test_sorted_by_severity_then_staff_id` |

実出力をレポートに貼ること。

- [ ] **Step 6: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/staff_attention.py tests/test_staff_attention.py
git commit -m "feat: スタッフの働き方の変化を検出する純関数を追加

ダッシュボードのAI分析はその期間のスナップショットしか見ておらず、
「いつも入っていた人が最近少ない」という変化に気づけない。直近30日と
その前60日（30日あたりへ換算）を比べ、出勤の減少と変更・取消の増加を検出する。

返すのは数値と種別だけで、原因や状態は判定しない。勤務データから分かるのは
働き方が変わった事実だけで、そこに解釈を足すと外れた決めつけになるため。

もともと月4日未満の人、退職者、基準期間に出勤が無い新人は対象外。
境界（ちょうど4割減）は検出しない側に倒す。わずかな揺れで毎月名前が出ると、
カードが出ること自体の意味が薄れるため。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: 声かけの文言

**Files:**
- Modify: `src/ai.py`（`review_shift_balance` の直後に追加）
- Create: `tests/test_staff_attention_message.py`

**Interfaces:**
- Consumes: Task 1 の `reasons` の形
- Produces:
  - `ai.suggest_attention_message(name, reasons) -> (str, str)` — `(message, source)`。`source` は `"llm"` または `"rule_based"`
  - `ai.ATTENTION_FALLBACK` — 種別ごとの定型文の辞書（テストから参照する）

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_staff_attention_message.py`:

```python
"""tests/test_staff_attention_message.py — 声かけ文言の生成。

実行: ./.venv/bin/python -m pytest tests/test_staff_attention_message.py -v

conftest が LLM_API_KEY="" にするため、既定ではフォールバック経路を通る。
LLM経路は monkeypatch で差し替えて検証する。

不変量: 原因や状態を断定する語（離職・メンタル・やる気など）を出力しない。
勤務データから分かるのは「働き方が変わった」事実だけで、断定は店長に
外れた決めつけを渡すことになる（設計書「スコープを明確に区切る」）。
"""
import pytest

from src import ai

# 出してはいけない語。プロンプト・フォールバック文言・LLM出力の検査に使う。
FORBIDDEN = ["離職", "退職", "メンタル", "やる気", "不満", "病気", "うつ"]

DROP = [{"type": "attendance_drop", "recent": 4, "base": 10.0}]
SPIKE = [{"type": "request_spike", "recent": 5, "base": 1.0}]


class TestFallback:
    def test_drop_returns_rule_based_message(self):
        msg, source = ai.suggest_attention_message("田中太郎", DROP)
        assert source == "rule_based"
        assert msg

    def test_spike_returns_rule_based_message(self):
        msg, source = ai.suggest_attention_message("田中太郎", SPIKE)
        assert source == "rule_based"
        assert msg

    def test_fallback_has_no_diagnosis_words(self):
        for reasons in (DROP, SPIKE):
            msg, _ = ai.suggest_attention_message("田中太郎", reasons)
            for w in FORBIDDEN:
                assert w not in msg, f"定型文に断定的な語が入っている: {w} / {msg}"

    def test_unknown_reason_type_does_not_crash(self):
        msg, source = ai.suggest_attention_message("田中太郎", [{"type": "unknown_thing"}])
        assert isinstance(msg, str)

    def test_empty_reasons_does_not_crash(self):
        msg, source = ai.suggest_attention_message("田中太郎", [])
        assert isinstance(msg, str)


class TestLlmPath:
    def _use_llm(self, monkeypatch, reply, capture=None):
        monkeypatch.setattr(ai, "is_llm_available", lambda: True)

        def fake(system_prompt, user_prompt, temperature=0.3):
            if capture is not None:
                capture.append((system_prompt, user_prompt))
            return reply

        monkeypatch.setattr(ai, "call_llm", fake)

    def test_llm_reply_is_used(self, monkeypatch):
        self._use_llm(monkeypatch, "最近シフトが少なめですが、ご都合はいかがですか。")
        msg, source = ai.suggest_attention_message("田中太郎", DROP)
        assert source == "llm"
        assert "ご都合" in msg

    def test_prompt_forbids_diagnosis(self, monkeypatch):
        """断定を禁じる指示がプロンプトに入っていること。"""
        cap = []
        self._use_llm(monkeypatch, "ok", cap)
        ai.suggest_attention_message("田中太郎", DROP)
        system_prompt = cap[0][0]
        assert "断定" in system_prompt, "原因を断定しない指示がプロンプトに無い"

    def test_llm_output_with_diagnosis_word_is_rejected(self, monkeypatch):
        """LLMが断定的な語を返したら採用せず、定型文へ落とす。

        プロンプトで禁じても、モデルは指示を外すことがある。画面に出る直前で
        機械的に弾かないと、店長に決めつけを渡してしまう。
        """
        self._use_llm(monkeypatch, "田中太郎さんは離職の可能性があります。面談してください。")
        msg, source = ai.suggest_attention_message("田中太郎", DROP)
        assert source == "rule_based", "断定的な語を含むLLM出力が採用されている"
        assert "離職" not in msg

    def test_llm_failure_falls_back(self, monkeypatch):
        self._use_llm(monkeypatch, None)
        msg, source = ai.suggest_attention_message("田中太郎", DROP)
        assert source == "rule_based"
        assert msg
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_staff_attention_message.py -v
```
Expected: 全件 FAIL（`AttributeError: module 'src.ai' has no attribute 'suggest_attention_message'`）

- [ ] **Step 3: 実装する**

`src/ai.py` の `review_shift_balance` の直後（`:433` 付近）に追加:

```python
# ===========================================================
# スタッフの働き方の変化に添える「声かけの例」
# ===========================================================
# 原因や状態を断定する語。プロンプトで禁じてもモデルは指示を外すことがあるため、
# 画面に出る直前で機械的に弾く（店長に決めつけを渡さないための最後の関門）。
_ATTENTION_FORBIDDEN = ("離職", "退職", "辞め", "メンタル", "うつ", "病気",
                        "やる気", "不満", "怠", "サボ")

ATTENTION_FALLBACK = {
    "attendance_drop": "最近シフトが少なめですが、ご都合はいかがですか。"
                       "入りたい曜日や時間が変わっていたら教えてください。",
    "request_spike": "予定の変更が続いているようですが、無理のない範囲で組めていますか。"
                     "組みにくい曜日があれば教えてください。",
}
_ATTENTION_DEFAULT = "最近の働き方に変化があるようです。困っていることがないか伺ってみてください。"


def _attention_fallback(reasons):
    for r in (reasons or []):
        msg = ATTENTION_FALLBACK.get(r.get("type"))
        if msg:
            return msg
    return _ATTENTION_DEFAULT


def suggest_attention_message(name, reasons):
    """勤務データの変化から、店長が本人に尋ねるときの声かけ例を返す。

    戻り値: (message, source)。source は "llm" または "rule_based"。

    原因は書かせない。分かっているのは「働き方が変わった」という事実だけで、
    理由は本人にしか分からない（学業・家庭・本人の希望など）。断定した文面を
    渡すと、店長が誤った前提で話を始めることになる。
    """
    fallback = _attention_fallback(reasons)
    if not is_llm_available():
        return fallback, "rule_based"
    facts = []
    for r in (reasons or []):
        if r.get("type") == "attendance_drop":
            facts.append(f"出勤日数が30日あたり{r.get('base')}日から直近30日で{r.get('recent')}日に減った")
        elif r.get("type") == "request_spike":
            facts.append(f"シフトの変更・取消の申請が直近30日で{r.get('recent')}件（以前は30日あたり{r.get('base')}件）")
    if not facts:
        return fallback, "rule_based"
    system_prompt = (
        "あなたは飲食・小売店の店長の相談相手です。スタッフの勤務データの変化を受けて、"
        "店長が本人に事情を尋ねるときの声かけを1つだけ提案してください。\n"
        "次を必ず守ってください。\n"
        "- 変化の原因を断定しないこと（本人の希望・学業・家庭など理由は分かりません）\n"
        "- 離職や体調、意欲についての推測を書かないこと\n"
        "- 評価や叱責にならない、相手を気づかう言い方にすること\n"
        "- 出力は声かけの文面のみ。前置きや解説を付けないこと。1〜2文。")
    user_prompt = f"スタッフ: {name}\n勤務データの変化:\n" + "\n".join("・" + f for f in facts)
    reply = call_llm(system_prompt, user_prompt, temperature=0.4)
    if not reply:
        return fallback, "rule_based"
    reply = reply.strip()
    if any(w in reply for w in _ATTENTION_FORBIDDEN):
        return fallback, "rule_based"
    return reply, "llm"
```

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_staff_attention_message.py -v
.venv/bin/python -m pytest tests/ -q
```
Expected: すべて PASS。

- [ ] **Step 5: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `if any(w in reply for w in _ATTENTION_FORBIDDEN)` の分岐を消す | `test_llm_output_with_diagnosis_word_is_rejected` |
| システムプロンプトから「断定しないこと」の行を消す | `test_prompt_forbids_diagnosis` |
| `if not reply: return fallback` を消す | `test_llm_failure_falls_back` |
| `ATTENTION_FALLBACK` の文面に「離職」を入れる | `test_fallback_has_no_diagnosis_words` |

実出力をレポートに貼ること。

- [ ] **Step 6: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/ai.py tests/test_staff_attention_message.py
git commit -m "feat(ai): 働き方の変化に添える声かけ文言を生成する

店長が本人に事情を尋ねるときの一言をLLMに作らせる。原因の断定・体調や
意欲の推測・評価的な表現をシステムプロンプトで禁じる。

プロンプトで禁じてもモデルは指示を外すことがあるため、出力に断定的な語が
含まれていたら採用せず定型文へ落とす。画面に出る直前で機械的に弾かないと、
店長に決めつけを渡すことになる。LLM未接続時も同じ定型文を返す。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: API エンドポイント

**Files:**
- Modify: `src/app.py`（`shop_ai_review`（`:3045`）の直後に追加）
- Create: `tests/test_staff_attention_api.py`

**Interfaces:**
- Consumes: Task 1 の `find_attention`、Task 2 の `suggest_attention_message`
- Produces: `POST /api/shop/staff-attention`
  - レスポンス: `{"items": [{"staff_id", "name", "reasons", "headline", "detail", "message"}], "source": "llm"|"rule_based"}`

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_staff_attention_api.py`:

```python
"""tests/test_staff_attention_api.py — 気にかけたい人の API。

実行: ./.venv/bin/python -m pytest tests/test_staff_attention_api.py -v
"""
from datetime import date, timedelta

import pytest

from db import execute
from helpers import insert_shop, insert_staff, make_session, auth
from utils import jst_today


def _day(days_ago):
    return (jst_today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _add_shift(shop_id, staff_id, days_ago):
    d = _day(days_ago)
    execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
        "VALUES (?,?,?,?,'confirmed')",
        (shop_id, staff_id, f"{d}T09:00:00", f"{d}T17:00:00"))


class TestStaffAttentionApi:
    def _tok(self, shop_id):
        return make_session("shop", shop_id, shop_id)

    def test_requires_shop_role(self, client):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        tok = make_session("staff", sid, shop_id)
        r = client.post("/api/shop/staff-attention", headers=auth(tok))
        assert r.status_code in (401, 403)

    def test_returns_empty_when_nothing_changed(self, client):
        """安定して出ている人は挙がらない（平常時は何も出さない）。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        # 直近も基準期間も同じくらい出ている
        for x in list(range(1, 30, 3)) + list(range(31, 89, 3)):
            _add_shift(shop_id, sid, x)
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_id)))
        assert r.status_code == 200
        assert r.get_json()["items"] == []

    def test_detects_attendance_drop(self, client):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        for x in range(31, 71):        # 基準期間に多く出勤
            _add_shift(shop_id, sid, x)
        _add_shift(shop_id, sid, 3)    # 直近は1日だけ
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_id)))
        d = r.get_json()
        assert len(d["items"]) == 1
        item = d["items"][0]
        assert item["staff_id"] == sid
        assert item["name"] == "田中太郎"
        assert item["reasons"][0]["type"] == "attendance_drop"
        # 店長が判断できる材料（事実）と声かけの例が入っていること
        assert item["headline"]
        assert item["detail"]
        assert item["message"]

    def test_falls_back_without_llm(self, client):
        """conftest が LLM_API_KEY="" にするため、既定でフォールバック経路。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        for x in range(31, 71):
            _add_shift(shop_id, sid, x)
        _add_shift(shop_id, sid, 3)
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_id)))
        assert r.get_json()["source"] == "rule_based"

    def test_does_not_include_other_shops(self, client):
        """他店舗のスタッフが混ざらないこと。"""
        shop_a = insert_shop(code="SHOPA")
        shop_b = insert_shop(code="SHOPB", name="別店舗")
        sid_b = insert_staff(shop_b, "P9", "他店の人")
        for x in range(31, 71):
            _add_shift(shop_b, sid_b, x)
        _add_shift(shop_b, sid_b, 3)
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_a)))
        assert r.get_json()["items"] == []

    def test_no_diagnosis_words_in_response(self, client):
        """レスポンスのどこにも断定的な語が出ないこと。"""
        import json
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "P1", "田中太郎")
        for x in range(31, 71):
            _add_shift(shop_id, sid, x)
        _add_shift(shop_id, sid, 3)
        r = client.post("/api/shop/staff-attention", headers=auth(self._tok(shop_id)))
        body = json.dumps(r.get_json(), ensure_ascii=False)
        for w in ("離職", "退職", "メンタル", "やる気"):
            assert w not in body, f"レスポンスに断定的な語が含まれる: {w}"
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_staff_attention_api.py -v
```
Expected: 全件 FAIL（404）

- [ ] **Step 3: 実装する**

`src/app.py` の import に追加:

```python
from staff_attention import find_attention, RECENT_DAYS, BASE_DAYS
```

`shop_ai_review` の直後に追加:

```python
@app.post("/api/shop/staff-attention")
def shop_staff_attention():
    """働き方に変化のあるスタッフと、声かけの例を返す。

    出すのは「データがこう変わった」という事実と尋ね方の例だけで、原因や
    状態（離職の意思・体調など）は判定しない。勤務データから分かるのは
    働き方の変化だけであり、そこに解釈を足すと店長に決めつけを渡すことになる。
    詳細は docs/superpowers/specs/2026-08-02-staff-attention-design.md。
    """
    shop, shop_id, _ = _shop_ctx()
    today = jst_today().strftime("%Y-%m-%d")
    since = (jst_today() - timedelta(days=RECENT_DAYS + BASE_DAYS)).strftime("%Y-%m-%d")
    staffs = query_all("SELECT id, name, is_resigned FROM staffs WHERE shop_id=?", (shop_id,))
    shifts = query_all(
        "SELECT staff_id, start_datetime FROM shifts "
        "WHERE shop_id=? AND status='confirmed' AND start_datetime>=?",
        (shop_id, since + "T00:00:00"))
    reqs = query_all(
        "SELECT staff_id, created_at FROM change_requests "
        "WHERE shop_id=? AND created_at>=?",
        (shop_id, since + " 00:00:00"))
    found = find_attention(staffs, shifts, reqs, today)

    headline = {"attendance_drop": "出勤が減っています",
                "request_spike": "予定の変更が続いています"}
    items = []
    source = "rule_based"
    for f in found:
        details = []
        for r in f["reasons"]:
            if r["type"] == "attendance_drop":
                details.append(f"以前は30日あたり{r['base']}日 → 直近30日は{r['recent']}日")
            elif r["type"] == "request_spike":
                details.append(f"直近30日で{r['recent']}件（以前は30日あたり{r['base']}件）")
        msg, src = ai.suggest_attention_message(f["name"], f["reasons"])
        if src == "llm":
            source = "llm"
        items.append({
            "staff_id": f["staff_id"], "name": f["name"], "reasons": f["reasons"],
            "headline": headline.get(f["reasons"][0]["type"], "働き方に変化があります"),
            "detail": " / ".join(details),
            "message": msg,
        })
    return jsonify({"items": items, "source": source})
```

**`created_at` の比較について**: `change_requests.created_at` は `datetime('now')` 既定なので
`"YYYY-MM-DD HH:MM:SS"`（Tなし）の形式。文字列比較のため、`since + " 00:00:00"` と
半角スペースで比較する（シフト側の `T` とは形式が違う）。

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_staff_attention_api.py -v
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tests/run_tests.py
```
Expected: すべて PASS。

- [ ] **Step 5: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `WHERE shop_id=?` を staffs のクエリから外す | `test_does_not_include_other_shops` |
| `_shop_ctx()` を消す | `test_requires_shop_role` |
| `find_attention(...)` の戻りを無視して全スタッフを返す | `test_returns_empty_when_nothing_changed` |
| `detail` を空文字にする | `test_detects_attendance_drop` |

実出力をレポートに貼ること。

- [ ] **Step 6: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/app.py tests/test_staff_attention_api.py
git commit -m "feat(api): 気にかけたいスタッフを返すエンドポイントを追加

POST /api/shop/staff-attention。過去90日の確定シフトと変更申請を読み、
純関数 find_attention に判定を委ね、声かけの例を添えて返す。

既存の /api/shop/dashboard には混ぜない。ダッシュボードAPIは既に多くを
返しており、LLM呼び出しを含むこの処理を足すと画面全体の表示がLLMの
応答時間に引きずられるため。

change_requests.created_at は datetime('now') 既定でTを含まない形式なので、
比較する文字列もスペース区切りにする（シフト側とは形式が違う）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: ダッシュボードのカード

**Files:**
- Modify: `public/app.js`（`SCREENS.dashboard` の右カラム、`:2055-2066` 付近）
- Modify: `public/style.css`
- Create: `e2e/staff_attention.spec.js`

**Interfaces:**
- Consumes: Task 3 の `POST /api/shop/staff-attention`
- Produces: なし

- [ ] **Step 1: E2E の失敗テストを書く**

Create `e2e/staff_attention.spec.js`:

**API は `page.route` でスタブする。** 実データで検出条件を満たす状態を作るには90日分の
シフトを投入する必要があり、テストが遅く壊れやすくなる。判定そのものは
`tests/test_staff_attention.py` の責務で、ここで確かめるのは表示の有無と中身。

```js
/**
 * e2e/staff_attention.spec.js — ダッシュボードの「気にかけたい人」カード。
 *
 * 判定ロジックは tests/test_staff_attention.py の責務。ここで確かめるのは
 * 「該当者がいるときだけ出るか」「事実と声かけが出るか」「決めつけを渡して
 * いないか」の3点。API は page.route でスタブする。
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);

const SHOP = {
  shopCode: `ATN_${RUN_ID}`,
  shopName: '気づきテスト店',
  managerCode: `atnmgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

const ITEM = {
  staff_id: 101,
  name: '田中太郎',
  reasons: [{ type: 'attendance_drop', recent: 4, base: 10.0 }],
  headline: '出勤が減っています',
  detail: '以前は30日あたり10.0日 → 直近30日は4日',
  message: '最近シフトが少なめですが、ご都合はいかがですか。',
};

/** /api/shop/staff-attention をスタブする。status を渡すと失敗を再現できる。 */
function stubAttention(page, items, status = 200) {
  return page.route(
    (url) => url.pathname.endsWith('/api/shop/staff-attention'),
    (route) => (status === 200
      ? route.fulfill({ json: { items, source: 'rule_based' } })
      : route.fulfill({ status, json: { error: 'failed' } })),
  );
}

async function openDashboard(page) {
  await loginAsManager(page, {
    shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword,
  });
  // ログイン直後がダッシュボード。右カラムの描画完了を「AIからの提案」で待つ。
  await page.waitForSelector('#dashRight', { timeout: 15000 });
  await expect(page.locator('#dashRight')).toContainText('AIからの提案', { timeout: 15000 });
}

test.describe('ダッシュボード: 気にかけたい人', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  // ==========================================================
  // ケース1: 該当者がいなければカードごと出さない
  // ==========================================================
  test('該当者がいないときはカードごと出ない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, []);
    await openDashboard(page);
    // toBeHidden() は要素が無くても真になるため、件数で見る
    await expect(page.locator('#dashAttention')).toHaveCount(0);
    await expect(page.locator('#dashRight')).not.toContainText('気にかけたい人');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース2: 該当者がいれば、名前・事実・声かけが出る
  // ==========================================================
  test('該当者がいるとカードが出て、名前・事実・声かけが表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, [ITEM]);
    await openDashboard(page);

    const box = page.locator('#dashAttention');
    await expect(box).toHaveCount(1);
    await expect(box).toContainText('田中太郎');
    await expect(box).toContainText('出勤が減っています');
    // 店長が判断できる材料（事実）が出ていること
    await expect(box).toContainText('直近30日は4日');
    // 声かけの例が出ていること
    await expect(box).toContainText('ご都合はいかがですか');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース3: 決めつけを戒める但し書きが常に出る
  // ==========================================================
  test('決めつけずに伺うよう促す但し書きが出る', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, [ITEM]);
    await openDashboard(page);
    await expect(page.locator('#dashAttention')).toContainText('決めつけずに伺ってください');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース4: 原因や状態を断定する語を画面に出さない
  // ==========================================================
  test('断定的な語がカードに出ない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, [ITEM]);
    await openDashboard(page);
    const text = await page.locator('#dashAttention').textContent();
    for (const w of ['離職', '退職', 'メンタル', 'やる気']) {
      expect(text).not.toContain(w);
    }
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース5: 取得に失敗してもダッシュボードの他は出る
  // ==========================================================
  test('取得に失敗してもダッシュボードの他の部分は表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, [], 500);
    await openDashboard(page);   // 「AIからの提案」が出ることを待っている
    await expect(page.locator('#dashAttention')).toHaveCount(0);
    await expect(page.locator('#dashRight')).toContainText('クイック操作');
  });
});
```


- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
npx playwright test e2e/staff_attention.spec.js --reporter=list
```
Expected: 1・5 は PASS（カードが無いのが正しい状態のため）、2・3・4 が FAIL。

- [ ] **Step 3: 実装する**

`public/app.js` の右カラム組み立て（`:2055` 付近、`const rightBox = ...` の直後）を次にする。
既存の `aiAdvice` の取得はそのまま残し、その手前に気にかけたい人の取得を足す:

```js
    // 気にかけたい人（働き方の変化）。該当者がいるときだけカードを出す。
    // 毎回何か出ると見流されるので、出ること自体に意味を持たせる。
    let attention = [];
    try {
      const at = await api('/shop/staff-attention', { method: 'POST' });
      attention = at.items || [];
    } catch { /* 取得できなくてもダッシュボードの他は出す */ }
```

カードの HTML を組み立てるヘルパを、`SCREENS.dashboard` の直前に追加:

```js
/* 気にかけたい人カード。該当者がゼロなら空文字（カードごと出さない）。
   出すのは「データがこう変わったか」という事実と声かけの例だけ。原因や状態は
   書かない（勤務データから分かるのは働き方の変化だけで、理由は本人にしか
   分からない）。末尾の但し書きは常に添える。 */
function _dashAttentionCard(items) {
  if (!items || !items.length) return '';
  const rows = items.map((it) => `
    <div class="dash-attention-row">
      <div class="dash-attention-head"><b>${esc(it.name)}</b>さん — ${esc(it.headline)}</div>
      <div class="dash-attention-fact">${esc(it.detail)}</div>
      <div class="dash-attention-msg"><i class="bi bi-chat-left-quote"></i> ${esc(it.message)}</div>
    </div>`).join('');
  // bi-person-heart は Bootstrap Icons 1.11.3（public/index.html:10）に存在する
  return card(sectionTitle('bi-person-heart', '気にかけたい人', badge('AI', 'ai')) +
    `<div id="dashAttention">${rows}
      <div class="dash-attention-note">変化には本人の希望や学業・家庭の事情など理由があります。決めつけずに伺ってください。</div>
    </div>`);
}
```

`rightBox.innerHTML` の組み立てで、「AIからの提案」カードの**前**に差し込む:

```js
    if (rightBox) rightBox.innerHTML =
      _dashAttentionCard(attention) +
      card(sectionTitle('bi-stars', 'AIからの提案', badge('AI', 'ai')) + ...
```

`public/style.css` に追加（既存トークンのみ使用）:

```css
/* ダッシュボード「気にかけたい人」。該当者がいるときだけ出るカードなので、
   目に入る強さは持たせつつ、警告色（danger）は使わない。これは異常の通知
   ではなく、声をかけるきっかけの提示だから。 */
.dash-attention-row { padding: 8px 0; border-bottom: 1px solid var(--rule); }
.dash-attention-row:last-of-type { border-bottom: none; }
.dash-attention-head { font-size: .9rem; margin-bottom: 2px; }
.dash-attention-fact { font-size: .82rem; color: var(--ink-2); }
.dash-attention-msg {
  font-size: .82rem; color: var(--ink-2); background: var(--zebra);
  border-radius: var(--radius-sm); padding: 6px 8px; margin-top: 6px;
}
.dash-attention-note {
  font-size: .76rem; color: var(--ink-3); margin-top: 10px; line-height: 1.6;
}
```

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
node --check public/app.js
.venv/bin/python -m pytest tests/ -q
npx playwright test e2e/staff_attention.spec.js --reporter=list
npx playwright test
```
Expected: すべて PASS。E2E 全体は 基準値154 + 新規分。

- [ ] **Step 5: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `if (!items || !items.length) return '';` を消す | 1（該当者ゼロでもカードが出る） |
| `dash-attention-note` の但し書きを消す | 3 |
| `_dashAttentionCard(attention) +` を消す | 2 |
| `try/catch` を外す | 5（APIが落ちるとダッシュボード全体が止まる） |

実出力をレポートに貼ること。

- [ ] **Step 6: コミット**

```bash
git add public/app.js public/style.css e2e/staff_attention.spec.js
git commit -m "feat(ui): ダッシュボードに「気にかけたい人」を出す

働き方に変化のあるスタッフがいるときだけカードを出す。毎回何か出ると
見流されるので、出ること自体に意味を持たせる。

出すのは事実（出勤日数がどう変わったか）と声かけの例だけで、原因や状態は
書かない。末尾に「変化には本人の希望や学業・家庭の事情など理由があります。
決めつけずに伺ってください」を常に添える。

取得に失敗してもカードを出さないだけで、ダッシュボードの他の部分は表示する。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review

**設計書のカバレッジ**

| 設計書の項目 | 対応タスク |
|---|---|
| 1. 検出ロジック（純関数・しきい値・除外・並び順） | Task 1 |
| 2. 声かけの文言（LLM + フォールバック + 断定禁止） | Task 2 |
| 3. API（`POST /api/shop/staff-attention`） | Task 3 |
| 4. 画面（該当者がいるときだけ・固定の但し書き） | Task 4 |
| 5. テスト（純関数の境界値・API・E2E） | 各タスク |
| スコープ（心の状態を判定しない） | Task 1 の `test_result_has_no_diagnosis_fields`、Task 2 の禁止語フィルタ、Task 3 の `test_no_diagnosis_words_in_response`、Task 4 の E2E 4 |

**依存関係**

Task 1 → Task 3
Task 2 → Task 3
Task 3 → Task 4

**設計書から変えた点**

設計書では `suggest_attention_message` の戻り値を文字列としていたが、`(message, source)` の
タプルに変えた。API が `source`（llm / rule_based）を返す必要があり、既存の
`review_shift_balance` も同じ情報を返しているため。

**設計書に無かったが足したもの**

LLM 出力の**禁止語フィルタ**（Task 2）。設計書は「プロンプトで断定を禁じる」までだったが、
モデルは指示を外すことがある。画面に出る直前で機械的に弾かないと、設計書が掲げた
「決めつけを渡さない」が保証されない。テストで固定した。

**残る不確実性**

- しきい値（月4日・4割減・3件・2倍）は実データで検証していない。運用して多すぎ／少なすぎが
  分かった時点で調整する。設計書にもその旨を記載済み
- `change_requests.created_at` の形式（スペース区切り）は SQLite の `datetime('now')` 既定に
  依存する。D1 でも同じ形式であることは既存データで確認が必要（Task 3 Step 4 の全件テストで
  既存機能が壊れないことは確認できるが、本番データでの形式確認は別途）
