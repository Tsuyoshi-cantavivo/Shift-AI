# 外国籍アルバイトロールと週28時間上限 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `staffs.role` に「外国籍アルバイト」(`foreign_worker`) を追加し、そのロールのスタッフに週28時間の上限を自動生成・手動入力の両方で効かせる。

**Architecture:** 週28hの判定は「任意の連続7日間」で行う純関数モジュール `src/weekly_hours.py` に切り出し、シフト自動生成（`src/shift_engine.py`）と手動入力API（`src/app.py`）の両方から同じ関数を呼ぶ。判定ロジックを2箇所に書くと必ず食い違うため。既存の学生ロール（`student`＝月80h上限）と同じ場所・同じ作法に揃える。

**Tech Stack:** Python Flask / Vanilla JS / SQLite (本番 Cloudflare D1) / pytest / Playwright

## Global Constraints

- **新しい依存パッケージを追加しない。** `requirements.txt` は Flask, python-dotenv, requests, pytest, gunicorn のみ
- コード内コメントは日本語。「なぜ」を書く
- コミットメッセージは `feat:` / `fix:` / `test:` / `docs:` プレフィックス + 日本語サマリ、末尾に `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Python の実行は必ず `.venv/bin/python`
- `public/app.js` / `public/admin.js` を編集したら必ず `node --check <file>` を通す
- **Playwright は必ずフォアグラウンドで実行する**（`timeout` を 600000 に）
- **基準値（この計画の開始時点）**: pytest `1260 passed, 1 skipped` / E2E `145 passed` / `tests/run_tests.py` EXIT 0
- 各タスクの最後に pytest 全件・`tests/run_tests.py` を確認する
- **テストは「実装のどの行を消せば落ちるか」を言える形で書く。** 各テストについて**守りたい実装の1行だけを消して**赤になることを確認し、実出力をレポートに貼ること
- **他ロール（`employee` / `part_time` / `manager` / `student`）の挙動を1バイトも変えない。** 変わっていないことを既存テストで確認する

## 設計書

`docs/superpowers/specs/2026-08-02-foreign-worker-role-design.md`

## 前提となる調査結果（コードで確認済み・行番号は 2026-08-02 時点）

- `staffs.role` の CHECK は `('employee','part_time','manager','student')`（`schema.sql:34`）
- ロール値の列挙は5箇所: `src/app.py:1806`（スタッフ作成）、`src/admin_api.py:556`（ロール変更）、`:618`（スタッフ編集）、`:803`（スタッフ作成）。`src/app.py` のスタッフ更新（`shop_staffs_put`）は**ロールを変更しない**（現在値を読むだけ）
- `src/app.py` の手動シフト操作で `_check_student_only_shift` を呼ぶのは3箇所: `:2689`（POST 作成・400を返す）、`:2761`（PUT 更新・400）、`:2818`（PATCH draft-time 移動・**409**を返す）
- 既存のエラー応答は `{"error": msg, "student_only": True}` の形。`tests/test_integration_flow.py:125` がこの形を固定している
- `shifts` に `over_cap_flag INTEGER DEFAULT 0` がある（`schema.sql:106`）。承諾フラグも同じ形にする
- `auto_generate(shop_id, settings, start_date, end_date)`（`src/shift_engine.py:255`）は `from db import query_all` で DB を読む。過去90日の確定シフトを既に読んでいる（`:278`）ので、6日前からの読み込みは同じ作法で足せる
- `can_place()`（`:358`）は `(bool, 理由コード)` を返す。理由コードは `rest_request` / `min_daily` / `max_daily` / `already_working`
- `place()`（`:429`）が配置を確定し `minutes_by_staff` を更新する。週の集計もここで更新する
- `migrations/` の最新は `0006_notifications_batch_id.sql`。`ALTER TABLE ... ADD COLUMN` は `src/migrator.py` が扱える構文（`_ADD_COLUMN_RE`）
- `src/migrator.py:27` の `LEGACY_FILES` は 0002〜0004 のみ。0007 は通常のマイグレーションとして適用される
- `tests/helpers.py:29` の `insert_staff(shop_id, code, name, role="part_time", wage=1100, minh=0, maxh=160, password="pt001pass")`
- `tests/test_design_tokens.py` の `LIGHT_EXPECTED` / `DARK_EXPECTED`（`:22`/`:38`）と `TestContrast.LIGHT_PAIRS`（`:224`）が色を固定している。閾値は WCAG AA 4.5:1

## スコープ外

- 在留資格の種別・在留期限の管理
- 長期休業期間の週40時間特例
- 既存 `student` ロールへの週上限の適用
- 0007 適用前に登録済みのシフトの遡及チェック

---

### Task 1: 週28時間の判定（純関数）

依存を増やさない純関数モジュール。自動生成と手動入力の両方から使う。

**Files:**
- Create: `src/weekly_hours.py`
- Create: `tests/test_weekly_hours.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - `WEEKLY_CAP_MINUTES = 28 * 60`
  - `minutes_by_day(spans) -> dict[str, int]` — `spans` は `(start_iso, end_iso, break_minutes)` のタプル列。`{"YYYY-MM-DD": 実働分}` を返す
  - `exceeds_weekly_cap(day_minutes, target_day=None, cap_minutes=WEEKLY_CAP_MINUTES) -> tuple[str, str, int] | None` — 超過する連続7日窓があれば `(窓開始日, 窓終了日, 合計分)`、無ければ `None`

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_weekly_hours.py`:

```python
"""tests/test_weekly_hours.py — 外国籍アルバイトの週28時間上限を判定する純関数。

実行: ./.venv/bin/python -m pytest tests/test_weekly_hours.py -v

背景: 資格外活動許可で働く在留資格（留学・家族滞在）は入管法上1週間28時間以内。
超えると本人の在留資格だけでなく、雇用主も不法就労助長罪の対象になる。

不変量: 「1週間」は暦週ではなく **任意の連続7日間**。入管の運用が
「どの曜日から起算しても1週間28時間以内」であるため、暦週で数えると
前の週の後半に28h・次の週の前半に28h という組み方（連続7日で56h）が
素通りする。この抜け穴を塞ぐことがこのモジュールの存在理由。
"""
import pytest

from src.weekly_hours import WEEKLY_CAP_MINUTES, minutes_by_day, exceeds_weekly_cap


def _span(day, start_hhmm, end_hhmm, brk=0, end_day=None):
    """(start_iso, end_iso, break_minutes) を組み立てる。"""
    return (f"{day}T{start_hhmm}:00", f"{end_day or day}T{end_hhmm}:00", brk)


class TestMinutesByDay:
    def test_single_shift(self):
        assert minutes_by_day([_span("2026-08-10", "09:00", "17:00")]) == {"2026-08-10": 480}

    def test_break_is_subtracted(self):
        """28hは実労働時間の上限。休憩込みで数えると本来働ける時間を不当に削る。"""
        assert minutes_by_day([_span("2026-08-10", "09:00", "18:00", brk=60)]) == {"2026-08-10": 480}

    def test_same_day_shifts_are_summed(self):
        r = minutes_by_day([
            _span("2026-08-10", "09:00", "12:00"),
            _span("2026-08-10", "18:00", "21:00"),
        ])
        assert r == {"2026-08-10": 360}

    def test_overnight_shift_is_split_by_day(self):
        """日をまたぐシフトは各日に分けて数える（実労働時間で見るため）。"""
        r = minutes_by_day([_span("2026-08-10", "22:00", "06:00", end_day="2026-08-11")])
        assert r == {"2026-08-10": 120, "2026-08-11": 360}

    def test_overnight_break_comes_off_the_first_day(self):
        r = minutes_by_day([_span("2026-08-10", "22:00", "06:00", brk=60, end_day="2026-08-11")])
        assert r == {"2026-08-10": 60, "2026-08-11": 360}

    def test_overnight_break_longer_than_first_day_spills_over(self):
        """開始日の分を超える休憩は翌日から引く（開始日が負にならないこと）。"""
        r = minutes_by_day([_span("2026-08-10", "23:30", "06:00", brk=60, end_day="2026-08-11")])
        assert r == {"2026-08-10": 0, "2026-08-11": 330}

    def test_ends_exactly_at_midnight_does_not_touch_next_day(self):
        r = minutes_by_day([_span("2026-08-10", "20:00", "00:00", end_day="2026-08-11")])
        assert r == {"2026-08-10": 240}

    def test_empty_input(self):
        assert minutes_by_day([]) == {}


class TestExceedsWeeklyCap:
    def test_under_cap_returns_none(self):
        dm = {"2026-08-10": 27 * 60 + 54}  # 27.9h
        assert exceeds_weekly_cap(dm) is None

    def test_exactly_at_cap_is_allowed(self):
        """ちょうど28時間は「28時間以内」なので許される。"""
        dm = {"2026-08-10": WEEKLY_CAP_MINUTES}
        assert exceeds_weekly_cap(dm) is None

    def test_one_minute_over_is_detected(self):
        dm = {"2026-08-10": WEEKLY_CAP_MINUTES + 1}
        hit = exceeds_weekly_cap(dm)
        assert hit is not None
        assert hit[2] == WEEKLY_CAP_MINUTES + 1

    def test_seven_day_window_is_inclusive(self):
        """7日窓は両端を含む（8/10〜8/16 が1つの窓）。"""
        dm = {"2026-08-10": 14 * 60, "2026-08-16": 15 * 60}  # 計29h
        assert exceeds_weekly_cap(dm) is not None

    def test_eight_days_apart_is_not_one_window(self):
        """8日離れていれば同じ窓に入らない（8/10 と 8/17）。"""
        dm = {"2026-08-10": 14 * 60, "2026-08-17": 15 * 60}
        assert exceeds_weekly_cap(dm) is None

    def test_calendar_week_boundary_hole_is_closed(self):
        """暦週で数える実装に退化したら落ちるテスト。

        2026-08-10(月)〜16(日) で28h、17(月)〜23(日) で28h。暦週で見れば
        どちらも上限ちょうどで合法だが、8/14(金)〜8/20(木) の連続7日間は
        28h+28h の大半が集まり28hを超える。入管の運用ではこれは違反。
        """
        dm = {
            "2026-08-14": 14 * 60, "2026-08-15": 14 * 60,   # 前の週の後半に28h
            "2026-08-17": 14 * 60, "2026-08-18": 14 * 60,   # 次の週の前半に28h
        }
        hit = exceeds_weekly_cap(dm)
        assert hit is not None, "暦週でなく任意の連続7日間で数えていない"
        assert hit[2] == 56 * 60

    def test_target_day_limits_windows_to_those_containing_it(self):
        """target_day を渡すと、その日を含む7通りの窓だけを見る。"""
        dm = {"2026-08-01": 40 * 60, "2026-08-20": 60}
        # 8/20 を含む窓（8/14〜8/26 の範囲）に 8/01 は入らない
        assert exceeds_weekly_cap(dm, target_day="2026-08-20") is None
        # target_day 無しなら 8/01 の窓が超過として見つかる
        assert exceeds_weekly_cap(dm) is not None

    def test_returns_the_worst_window(self):
        """超過する窓が複数あるときは合計が最大の窓を返す（店長に最も厳しい窓を見せる）。"""
        dm = {"2026-08-10": 29 * 60, "2026-08-20": 35 * 60}
        hit = exceeds_weekly_cap(dm)
        assert hit[2] == 35 * 60
        assert hit[0] <= "2026-08-20" <= hit[1]

    def test_window_bounds_are_returned(self):
        dm = {"2026-08-10": 30 * 60}
        start, end, total = exceeds_weekly_cap(dm)
        assert start <= "2026-08-10" <= end
        # 窓は7日間（両端含む）
        from datetime import date
        d0 = date.fromisoformat(start)
        d1 = date.fromisoformat(end)
        assert (d1 - d0).days == 6

    def test_empty_input(self):
        assert exceeds_weekly_cap({}) is None
        assert exceeds_weekly_cap({}, target_day="2026-08-10") is None

    def test_custom_cap(self):
        dm = {"2026-08-10": 41 * 60}
        assert exceeds_weekly_cap(dm, cap_minutes=40 * 60) is not None
        assert exceeds_weekly_cap(dm, cap_minutes=42 * 60) is None
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_weekly_hours.py -v
```
Expected: 全件 FAIL（`ModuleNotFoundError: No module named 'src.weekly_hours'`）

`from src.weekly_hours import ...` が通らない場合は、他のテストのインポート作法（`tests/test_name_match.py` は `from src.name_match import ...`）に合わせること。

- [ ] **Step 3: 実装する**

Create `src/weekly_hours.py`:

```python
"""weekly_hours.py - 外国籍アルバイト（資格外活動許可）の週28時間上限を判定する。

【なぜ必要か】
  資格外活動許可で働く在留資格（留学・家族滞在）は入管法上1週間28時間以内。
  超えると本人の在留資格が取り消されうるだけでなく、雇用主も不法就労助長罪
  （入管法73条の2）の対象になる。既存の労働制約は月単位（学生の月80h）しか
  無く、月の前半に集中させる組み方が素通りしていた。

【なぜ「任意の連続7日間」か】
  入管の運用は「どの曜日から起算しても1週間28時間以内」。暦週（月曜起算）で
  数えると、前の週の後半に28h・次の週の前半に28h という組み方が両方とも
  「週28h以内」を満たしてしまい、連続7日間では56hになる。この抜け穴を塞ぐ。

【なぜ純関数として切り出すか】
  同じ判定がシフト自動生成（shift_engine.py）と手動入力API（app.py）の両方で
  必要になる。どちらかに書いて他方で書き直すと、片方だけ直したときに食い違う。

外部依存なし（標準ライブラリのみ）。
"""
from datetime import date, timedelta

# 入管法上の上限。分で持つ（このモジュールの計算単位が分のため）。
WEEKLY_CAP_MINUTES = 28 * 60

# 窓の長さ（日数）。両端を含んで7日間。
_WINDOW_DAYS = 7


def _hhmm_to_min(iso):
    """ISO datetime の "HH:MM" 部分を分に直す。"""
    return int(iso[11:13]) * 60 + int(iso[14:16])


def minutes_by_day(spans):
    """(start_iso, end_iso, break_minutes) の列を {"YYYY-MM-DD": 実働分} に畳む。

    日をまたぐシフト（22:00〜翌6:00）は各日に分けて計上する。労基・入管が見るのは
    実労働時間であり、開始日にまとめて計上すると「その日8時間」という誤った形に
    なるため。shift_engine.py も日またぎを日ごとに扱っており、数え方を揃えないと
    生成と検証で結果がズレる。

    休憩は開始日から差し引く（休憩は勤務の途中に取るため）。開始日の実働を
    超える休憩は翌日から差し引く（開始日が負にならないようにする）。
    """
    out = {}
    for start_iso, end_iso, brk in spans:
        d0 = start_iso[:10]
        d1 = end_iso[:10]
        s_min = _hhmm_to_min(start_iso)
        e_min = _hhmm_to_min(end_iso)
        brk = int(brk or 0)
        if d0 == d1:
            out[d0] = out.get(d0, 0) + max(0, e_min - s_min - brk)
            continue
        # 日またぎ: 開始日は 24:00 まで、終了日は 00:00 から
        first = max(0, 1440 - s_min)
        second = e_min
        # 休憩を開始日から引き、足りない分を終了日から引く
        take = min(brk, first)
        first -= take
        second = max(0, second - (brk - take))
        out[d0] = out.get(d0, 0) + first
        if second > 0:
            out[d1] = out.get(d1, 0) + second
    return out


def exceeds_weekly_cap(day_minutes, target_day=None, cap_minutes=WEEKLY_CAP_MINUTES):
    """連続7日間の合計が cap を超える窓を探す。

    戻り値: 超過する窓があれば (窓開始日, 窓終了日, 合計分)、無ければ None。
    超過する窓が複数あるときは合計が最大の窓を返す（店長には最も厳しい窓を見せる）。

    target_day を渡すと、その日を含む7通りの窓だけを調べる。シフトを1件足すときは
    その日を含む窓しか合計が変わらないため、これで十分。
    省略すると、データに現れるすべての日を起点とする窓を調べる（生成結果全体の検証）。
    """
    if not day_minutes:
        return None
    if target_day:
        base = date.fromisoformat(target_day)
        starts = [base - timedelta(days=i) for i in range(_WINDOW_DAYS)]
    else:
        starts = [date.fromisoformat(d) for d in day_minutes]
    worst = None
    for st in starts:
        total = 0
        for i in range(_WINDOW_DAYS):
            total += day_minutes.get((st + timedelta(days=i)).isoformat(), 0)
        if total > cap_minutes and (worst is None or total > worst[2]):
            worst = (st.isoformat(), (st + timedelta(days=_WINDOW_DAYS - 1)).isoformat(), total)
    return worst
```

- [ ] **Step 4: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_weekly_hours.py -v
.venv/bin/python -m pytest tests/ -q
```
Expected: `tests/test_weekly_hours.py` 全件 PASS。全体は 基準値1260 + 新規分。

FAIL する場合、**テストの期待値を緩めないこと。** テストが表現している要件（28hちょうどは許す／暦週の抜け穴を塞ぐ）は設計書の決定事項そのものなので、実装を直すこと。

- [ ] **Step 5: テストが実際に守っていることを確認する**

次を1つずつ一時的に壊し、**対応するテストが赤になること**を確認する。確認したら戻し、実出力をレポートに貼る。

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `exceeds_weekly_cap` の `starts` を「暦週の月曜」だけにする | `test_calendar_week_boundary_hole_is_closed` |
| `total > cap_minutes` を `total >= cap_minutes` に | `test_exactly_at_cap_is_allowed` |
| `_WINDOW_DAYS` を 6 にする | `test_seven_day_window_is_inclusive` |
| `minutes_by_day` の日またぎ分岐（`if d0 == d1` の else 側）を消して開始日にまとめる | `test_overnight_shift_is_split_by_day` |
| `minutes_by_day` の `- brk` を消す | `test_break_is_subtracted` |
| `worst is None or total > worst[2]` を `worst is None` に | `test_returns_the_worst_window` |

- [ ] **Step 6: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/weekly_hours.py tests/test_weekly_hours.py
git commit -m "feat: 週28時間上限の判定を純関数として追加

資格外活動許可で働く在留資格（留学・家族滞在）は入管法上1週間28時間以内で、
超えると雇用主も不法就労助長罪の対象になる。既存の労働制約は月単位しか無く、
月の前半に集中させる組み方が素通りしていた。

「1週間」は暦週ではなく任意の連続7日間で数える。入管の運用が「どの曜日から
起算しても1週間28時間以内」であり、暦週だと前の週の後半に28h・次の週の前半に
28h という組み方（連続7日で56h）が通ってしまうため。

自動生成と手動入力の両方から使うため純関数として切り出す。外部依存なし。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `foreign_worker` ロールを受け入れる（DB + API）

**Files:**
- Create: `migrations/0007_add_foreign_worker_role.sql`
- Modify: `schema.sql:34`（CHECK 制約）、`schema.sql:106` 付近（`shifts` に列追加）
- Modify: `src/app.py:1806`（スタッフ作成のロール検証）
- Modify: `src/admin_api.py:556, 618, 803`（ロール検証3箇所）、`:743`（スキーマ検出）
- Create: `tests/test_foreign_worker_role.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - `staffs.role` に `'foreign_worker'` が保存できる
  - `shifts.weekly_cap_ack INTEGER DEFAULT 0` — 店長が週28h超過を承諾して保存したシフトの印

- [ ] **Step 1: 失敗するテストを書く**

Create `tests/test_foreign_worker_role.py`:

```python
"""tests/test_foreign_worker_role.py — 外国籍アルバイトロールと週28時間上限。

実行: ./.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v

設計書: docs/superpowers/specs/2026-08-02-foreign-worker-role-design.md

このロールに付く制約は週28hのみ。学生ロールの月80h上限・学生のみシフト禁止は
重ねない（月80hは週換算18.6hで、重ねると週28hの判定がほぼ発火しなくなるため）。
"""
import pytest

from helpers import insert_shop, insert_staff, make_session, auth


class TestForeignWorkerRoleAccepted:
    def test_create_staff_with_foreign_worker_role(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "FW1", "name": "外国籍太郎",
            "password": "Fwk12345", "role": "foreign_worker",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)
        from db import query_one
        row = query_one("SELECT role, max_hours_per_month FROM staffs WHERE id=?",
                        (r.get_json()["id"],))
        assert row["role"] == "foreign_worker"

    def test_foreign_worker_is_not_capped_at_80_hours(self):
        """学生の月80h上限は重ねない（週28hだけが効く）。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "FW2", "外国籍花子", "foreign_worker", 1100, 0, 160)
        from db import query_one
        row = query_one("SELECT max_hours_per_month FROM staffs WHERE id=?", (sid,))
        assert row["max_hours_per_month"] == 160

    def test_unknown_role_is_still_rejected(self):
        """新しい値を足しても、でたらめなロールは拒否され続けること。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        import app as appmod
        with appmod.app.test_client() as c:
            r = c.post("/api/shop/staffs", json={
                "staff_code": "XX1", "name": "不正", "password": "Xxx12345",
                "role": "not_a_role",
            }, headers=auth(tok))
            assert r.status_code == 400


class TestShiftsWeeklyCapAckColumn:
    def test_column_exists_with_default_zero(self):
        """承諾フラグの列があり、既定が0であること。"""
        from db import query_all, execute, query_one
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "FW3", "外国籍次郎", "foreign_worker")
        meta = execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,'confirmed')",
            (shop_id, sid, "2026-08-10T09:00:00", "2026-08-10T17:00:00"))
        row = query_one("SELECT weekly_cap_ack FROM shifts WHERE id=?", (meta["last_row_id"],))
        assert row["weekly_cap_ack"] == 0
```

`insert_shop` / `make_session` / `auth` の引数は `tests/helpers.py` で確認すること。`client` フィクスチャは `tests/conftest.py` にある。

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v
```
Expected: 全件 FAIL（CHECK 制約違反、`no such column: weekly_cap_ack`）

- [ ] **Step 3: `schema.sql` を更新する**

`schema.sql:34` の CHECK を次にする:

```sql
  role                  TEXT DEFAULT 'part_time'
                          CHECK(role IN ('employee','part_time','manager','student','foreign_worker')),
```

`shifts` テーブル（`schema.sql:95-111`）の `over_cap_flag` の次の行に追加:

```sql
  weekly_cap_ack      INTEGER DEFAULT 0,  -- 週28h超過を店長が承諾して保存した場合 1
```

- [ ] **Step 4: マイグレーションを書く**

Create `migrations/0007_add_foreign_worker_role.sql`:

```sql
-- 0007_add_foreign_worker_role.sql
-- 外国籍アルバイト（資格外活動許可）ロールの追加と、週28h超過の承諾フラグ。
--
-- 背景:
--   資格外活動許可で働く在留資格（留学・家族滞在）は入管法上1週間28時間以内。
--   超えると本人の在留資格だけでなく、雇用主も不法就労助長罪の対象になる。
--   ロールで識別できるようにし、週上限を自動生成・手動入力の両方で効かせる。
--
-- 注意:
--   SQLite は CHECK 制約を ALTER で変更できないため staffs の再構築が必要。
--   migrations/0004 は 0003 の再構築が途中までしか適用されずテーブルが
--   壊れた事故のやり直しだった。同じ轍を踏まないよう、再構築は
--   「新テーブル作成 → コピー → 旧削除 → リネーム → インデックス再作成」を
--   この1ファイルで完結させる（途中で失敗しても旧テーブルは消えない順序）。
--
-- 適用方法:
--   ローカル: python src/migrator.py apply
--   本番D1  : 管理者画面「システム」→ マイグレーション → 未適用を適用
--
-- 適用後の確認:
--   SELECT sql FROM sqlite_master WHERE name='staffs'  → CHECK に 'foreign_worker' があること
--   PRAGMA table_info(shifts)                          → weekly_cap_ack があること

-- ---- 1. staffs を 'foreign_worker' を許容する CHECK で再構築 ----
CREATE TABLE IF NOT EXISTS staffs_new_0007 (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id               INTEGER NOT NULL,
  staff_code            TEXT NOT NULL,
  password_hash         TEXT NOT NULL,
  name                  TEXT NOT NULL,
  role                  TEXT DEFAULT 'part_time'
                          CHECK(role IN ('employee','part_time','manager','student','foreign_worker')),
  hourly_wage           INTEGER DEFAULT 1000,
  min_hours_per_month   INTEGER DEFAULT 0,
  max_hours_per_month   INTEGER DEFAULT 160,
  is_resigned           INTEGER DEFAULT 0,
  created_at            TEXT DEFAULT (datetime('now')),
  UNIQUE(shop_id, staff_code),
  FOREIGN KEY (shop_id) REFERENCES shops(id)
);

INSERT INTO staffs_new_0007 (id, shop_id, staff_code, password_hash, name, role,
                             hourly_wage, min_hours_per_month, max_hours_per_month,
                             is_resigned, created_at)
  SELECT id, shop_id, staff_code, password_hash, name, role,
         hourly_wage, min_hours_per_month, max_hours_per_month,
         is_resigned, created_at
  FROM staffs;

DROP TABLE staffs;
ALTER TABLE staffs_new_0007 RENAME TO staffs;
CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id);

-- ---- 2. 週28h超過の承諾フラグ ----
ALTER TABLE shifts ADD COLUMN weekly_cap_ack INTEGER DEFAULT 0;
```

**`src/migrator.py` の `LEGACY_FILES` には追加しないこと。** あれは 0004 以前の「再実行が危険な既存適用済みファイル」の一覧であり、0007 は通常のマイグレーションとして適用される。

- [ ] **Step 5: ロール検証にコードを追加する**

`src/app.py:1806`:

```python
    if role not in ("employee", "part_time", "manager", "student", "foreign_worker"):
        abort(400, description="ロールは employee / part_time / manager / student / foreign_worker のいずれかを指定してください")
```

コメント（`:1804`）も `'employee' / 'part_time' / 'manager' / 'student' / 'foreign_worker' 以外は拒否` に更新する。

`src/admin_api.py` の3箇所（`:556`, `:618`, `:803`）も同様に `"foreign_worker"` を許可リストに加え、エラーメッセージを揃える。**`student` の月80h強制ロジックには手を触れないこと**（`foreign_worker` に月上限の特別扱いは無い）。

`src/admin_api.py:743` の隣に、スキーマ検出を1行足す:

```python
            "supports_foreign_worker_role": "foreign_worker" in schema.lower(),
```

- [ ] **Step 6: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tests/run_tests.py
```
Expected: すべて PASS。

**既存テストが落ちたら内容を必ずレポートに記録してから対処すること。** 特にスタッフ一覧は `ORDER BY role DESC` なので、`foreign_worker` は `employee` と `manager` の間に入る。並び順に依存した既存テストがあれば、その事実をレポートに書くこと（テストを緩めるのではなく、並び順の変化が意図どおりかを判断する）。

- [ ] **Step 7: マイグレーションが実際に適用できることを確認する**

Run:
```bash
.venv/bin/python src/migrator.py status
.venv/bin/python src/migrator.py apply
.venv/bin/python src/migrator.py status
sqlite3 shift.db "SELECT sql FROM sqlite_master WHERE name='staffs';" | grep foreign_worker
sqlite3 shift.db "PRAGMA table_info(shifts);" | grep weekly_cap_ack
```
Expected: 0007 が未適用→適用済みになり、CHECK と列が実際に入っていること。実出力をレポートに貼る。

`src/migrator.py` のサブコマンド名が `status` / `apply` でない場合は `.venv/bin/python src/migrator.py --help` で確認すること。

- [ ] **Step 8: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `schema.sql` の CHECK から `'foreign_worker'` を消す | `test_create_staff_with_foreign_worker_role` |
| `src/app.py` の許可リストから `"foreign_worker"` を消す | `test_create_staff_with_foreign_worker_role` |
| `schema.sql` から `weekly_cap_ack` の行を消す | `test_column_exists_with_default_zero` |

- [ ] **Step 9: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add schema.sql migrations/0007_add_foreign_worker_role.sql src/app.py src/admin_api.py tests/test_foreign_worker_role.py
git commit -m "feat(api): 外国籍アルバイトロールを追加する

資格外活動許可で働く在留資格の週28時間上限を効かせるため、staffs.role に
foreign_worker を追加する。学生ロールの月80h上限は重ねない（週28hは月換算で
約120hであり、月80hを重ねると週の判定がほぼ発火しなくなるため）。

SQLite は CHECK を ALTER できないため staffs の再構築になる。0004 が
0003 の部分適用事故のやり直しだった教訓から、再構築の全手順を1ファイルで
完結させた。併せて、店長が週28h超過を承諾して保存したシフトの印として
shifts.weekly_cap_ack を追加する（既存の over_cap_flag と同じ形）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: 自動生成で週28時間を超えない

**Files:**
- Modify: `src/shift_engine.py:255`（`auto_generate` 冒頭に実績の読み込み）、`:358`（`can_place`）、`:429`（`place`）、`:848` 付近（検証）
- Modify: `tests/test_foreign_worker_role.py`（追記）

**Interfaces:**
- Consumes: Task 1 の `minutes_by_day` / `exceeds_weekly_cap` / `WEEKLY_CAP_MINUTES`、Task 2 の `foreign_worker` ロール
- Produces: `auto_generate` の warnings に `{"type": "weekly_cap_overflow", "staff_id": int, "name": str, "window_start": str, "window_end": str, "minutes": int, "message": str}` が加わる

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_foreign_worker_role.py` の末尾に追記:

```python
class TestEngineWeeklyCap:
    def _shop_with_pattern(self):
        """毎日 09:00-18:00 に1人必要な店を作る（1日8h実働）。"""
        from helpers import insert_pattern
        shop_id = insert_shop()
        insert_pattern(shop_id, "終日", "09:00", "18:00", 1)
        return shop_id

    def test_engine_does_not_place_over_28h_in_any_7_days(self):
        """外国籍アルバイト1人だけの店では、どの連続7日間も28hを超えないこと。"""
        from shift_engine import auto_generate
        from weekly_hours import minutes_by_day, exceeds_weekly_cap
        shop_id = self._shop_with_pattern()
        insert_staff(shop_id, "FW10", "外国籍A", "foreign_worker", 1100, 0, 160)
        r = auto_generate(shop_id, {}, "2026-08-01", "2026-08-31")
        spans = [(c["start"], c["end"], c.get("break") or 0) for c in r["confirmed"]]
        hit = exceeds_weekly_cap(minutes_by_day(spans))
        assert hit is None, f"週28hを超える配置が生成された: {hit}"

    def test_engine_counts_previous_month_shifts(self):
        """月をまたぐ7日窓のため、生成範囲の6日前からの確定シフトを見ること。

        7/29-7/31 に各8h（計24h）の確定シフトがあるとき、8/1 に8h入れると
        7/29〜8/4 の窓が32hになる。前月末を見ない実装ではこれを見逃す。
        """
        from db import execute
        from shift_engine import auto_generate
        shop_id = self._shop_with_pattern()
        sid = insert_staff(shop_id, "FW11", "外国籍B", "foreign_worker", 1100, 0, 160)
        for d in ("2026-07-29", "2026-07-30", "2026-07-31"):
            execute(
                "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, "
                "break_time_minutes, status) VALUES (?,?,?,?,60,'confirmed')",
                (shop_id, sid, f"{d}T09:00:00", f"{d}T18:00:00"))
        r = auto_generate(shop_id, {}, "2026-08-01", "2026-08-07")
        placed = [c for c in r["confirmed"] if c["staff_id"] == sid]
        aug_first_window = [c for c in placed if c["start"][:10] <= "2026-08-04"]
        # 7/29〜7/31 で24h使っているので、8/1〜8/4 に入れられるのは残り4h=8hシフト0本
        assert not aug_first_window, \
            f"前月末の実績を見ずに配置した: {[c['start'] for c in aug_first_window]}"

    def test_other_roles_are_not_limited_by_28h(self):
        """part_time は週28hの制約を受けないこと（他ロールの挙動は変えない）。"""
        from shift_engine import auto_generate
        from weekly_hours import minutes_by_day, exceeds_weekly_cap
        shop_id = self._shop_with_pattern()
        insert_staff(shop_id, "PT10", "パートA", "part_time", 1100, 0, 160)
        r = auto_generate(shop_id, {}, "2026-08-01", "2026-08-31")
        spans = [(c["start"], c["end"], c.get("break") or 0) for c in r["confirmed"]]
        hit = exceeds_weekly_cap(minutes_by_day(spans))
        assert hit is not None, \
            "パートにも週28hがかかっている（このロールは制約対象外のはず）"
```

`insert_pattern` の引数は `tests/helpers.py:37` で確認すること。1日8h実働（9:00-18:00, 休憩60分）なので、28h ÷ 8h = 3.5 → 連続7日間に入れられるのは3本まで。

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v -k EngineWeeklyCap
```
Expected: `test_engine_does_not_place_over_28h_in_any_7_days` と `test_engine_counts_previous_month_shifts` が FAIL、`test_other_roles_are_not_limited_by_28h` は PASS（制約が未実装なので当然通る。実装後も通り続けることが「他ロールを巻き込んでいない」ことの検証になる）。

- [ ] **Step 3: 実績の読み込みと集計の器を足す**

`src/shift_engine.py` の import に追加:

```python
from weekly_hours import WEEKLY_CAP_MINUTES, minutes_by_day, exceeds_weekly_cap
```

`auto_generate` の `staff_role = {...}`（`:316` 付近、学生の月80h強制の直前）の後に追加:

```python
    # 【週28h上限】外国籍アルバイト（資格外活動許可）は入管法上どの連続7日間も
    # 28時間以内。月上限だけでは月前半に集中させる組み方が素通りするため、
    # 日ごとの実働分を持ち回って配置のたびに窓を検査する。
    foreign_ids = {sid for sid, role in staff_role.items() if role == "foreign_worker"}
    weekly_minutes = {sid: {} for sid in foreign_ids}
    if foreign_ids:
        # 月初の週は前月末を含む窓を持つ。生成範囲の6日前からの確定シフトを
        # 「既に働いた実績」として入れておかないと、月をまたいだ超過を見逃す。
        lookback = (datetime.strptime(start_date, "%Y-%m-%d").date() - timedelta(days=6)).isoformat()
        past = query_all(
            "SELECT staff_id, start_datetime, end_datetime, break_time_minutes FROM shifts "
            "WHERE shop_id=? AND status='confirmed' "
            "AND start_datetime>=? AND start_datetime<?",
            (shop_id, lookback + "T00:00:00", start_date + "T00:00:00"))
        for row in past:
            if row["staff_id"] not in foreign_ids:
                continue
            acc = weekly_minutes[row["staff_id"]]
            for d, m in minutes_by_day([(row["start_datetime"], row["end_datetime"],
                                         row["break_time_minutes"] or 0)]).items():
                acc[d] = acc.get(d, 0) + m
```

`timedelta` を import に足すこと（`from datetime import datetime, timedelta`。現在は `from datetime import datetime` のみ）。

- [ ] **Step 4: `can_place` に週上限チェックを足す**

`can_place` の `max_daily` チェックの直後（`role_max` の比較の後）に追加:

```python
        # 【週28h上限】外国籍アルバイトのみ。このシフトを足したとき、それを含む
        # どれかの連続7日間が28hを超えるなら配置しない（学生の月80hと同じ振る舞い）。
        if staff_id in foreign_ids:
            cand = dict(weekly_minutes[staff_id])
            for d, m in minutes_by_day([(start_iso, end_iso, compute_break_minutes(work))]).items():
                cand[d] = cand.get(d, 0) + m
            if exceeds_weekly_cap(cand, target_day=day):
                return False, "weekly_cap"
```

`compute_break_minutes` は既に `src/utils.py` から import されている（`place()` が使っている）。

`can_place` の docstring の【労基法コンプライアンス】の節に、週28hの1行を足すこと。

- [ ] **Step 5: `place` で集計を更新する**

`place()` の `minutes_by_staff[staff_id] += work` の直後に追加:

```python
        # 週28h判定のための日別実働を更新する（can_place が次の配置で参照する）
        if staff_id in foreign_ids:
            acc = weekly_minutes[staff_id]
            for d, m in minutes_by_day([(start_iso, end_iso, compute_break_minutes(work))]).items():
                acc[d] = acc.get(d, 0) + m
```

- [ ] **Step 6: 生成結果を検証して警告に積む**

学生ルール検証（`:846` 付近の `student_only_days` の処理）の直後に追加:

```python
    # -----------------------------------------------------------
    # 外国籍アルバイトの週28h検証（二重の安全弁）
    # can_place で弾いているはずだが、固定シフト(fixed_shifts)など
    # can_place を通らない経路があるため、結果に対しても検査する。
    # -----------------------------------------------------------
    for sid in foreign_ids:
        hit = exceeds_weekly_cap(weekly_minutes[sid])
        if not hit:
            continue
        w_start, w_end, total = hit
        warnings.append({
            "type": "weekly_cap_overflow",
            "staff_id": sid,
            "name": name_map.get(sid, ""),
            "window_start": w_start, "window_end": w_end, "minutes": total,
            "message": (
                f"{name_map.get(sid, '')}さんは {w_start}〜{w_end} の7日間で "
                f"{total // 60}時間{total % 60}分になり、週28時間の上限を超えています。"
            ),
        })
```

- [ ] **Step 7: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tests/run_tests.py
```
Expected: すべて PASS。

- [ ] **Step 8: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| `can_place` の `return False, "weekly_cap"` を `pass` にする | `test_engine_does_not_place_over_28h_in_any_7_days` |
| 前月末の読み込み（Step 3 の `past` ループ）を消す | `test_engine_counts_previous_month_shifts` |
| `place()` の集計更新を消す | `test_engine_does_not_place_over_28h_in_any_7_days`（2本目以降が野放しになる） |
| `foreign_ids` の条件を `role != "manager"` などに広げる | `test_other_roles_are_not_limited_by_28h` |

実出力をレポートに貼ること。

- [ ] **Step 9: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/shift_engine.py tests/test_foreign_worker_role.py
git commit -m "feat(engine): 自動生成が外国籍アルバイトを週28時間以内に収める

どの連続7日間も28hを超えないよう、配置のたびに日別実働の窓を検査する。
超える配置は行わないので、結果は既存の人員不足として画面に出る（学生の
月80h上限と同じ振る舞い）。

月初の週は前月末を含む窓を持つため、生成範囲の6日前からの確定シフトを
実績として読み込む。これが無いと月をまたいだ超過を見逃す。

固定シフトなど can_place を通らない経路があるため、生成結果に対しても
週超過を検査して警告に積む（二重の安全弁）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: 手動入力の週28時間チェックと承諾

**Files:**
- Modify: `src/app.py`（`_check_student_only_shift` の直後に `_check_weekly_cap` を新設、`:2689` / `:2761` / `:2818` の3箇所で呼ぶ、POST/PUT の INSERT/UPDATE に `weekly_cap_ack`）
- Modify: `tests/test_foreign_worker_role.py`（追記）

**Interfaces:**
- Consumes: Task 1 の `minutes_by_day` / `exceeds_weekly_cap`、Task 2 の `weekly_cap_ack` 列
- Produces:
  - `_check_weekly_cap(shop_id, staff_id, start_iso, end_iso, break_minutes, exclude_id=None) -> (bool, str | None, dict | None)`
  - API レスポンス `{"error": msg, "weekly_cap_exceeded": True, "detail": {...}}`（POST/PUT は 400、PATCH draft-time は 409）
  - リクエストに `weekly_cap_confirmed: true` があれば保存し、`shifts.weekly_cap_ack=1` を立てる

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_foreign_worker_role.py` の末尾に追記:

```python
class TestManualShiftWeeklyCap:
    def _setup(self):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "FW20", "外国籍C", "foreign_worker", 1100, 0, 160)
        tok = make_session("shop", shop_id, shop_id)
        return shop_id, sid, tok

    def _add_confirmed(self, shop_id, sid, day, start="09:00", end="18:00", brk=60):
        from db import execute
        return execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, "
            "break_time_minutes, status) VALUES (?,?,?,?,?,'confirmed')",
            (shop_id, sid, f"{day}T{start}:00", f"{day}T{end}:00", brk))["last_row_id"]

    def test_post_rejects_shift_over_28h(self, client):
        """既に24h入っている週に、さらに8hを手で足すと拒否されること。"""
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)   # 8h × 3 = 24h
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 400, r.get_data(as_text=True)
        body = r.get_json()
        assert body.get("weekly_cap_exceeded") is True
        # 店長が判断できるよう、どの7日間が何時間になるのかを返すこと
        assert body["detail"]["window_start"] <= "2026-08-13" <= body["detail"]["window_end"]
        assert body["detail"]["minutes"] == 32 * 60

    def test_post_allows_shift_within_28h(self, client):
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11"):
            self._add_confirmed(shop_id, sid, d)   # 16h
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)

    def test_post_with_confirmation_saves_and_marks_ack(self, client):
        """店長が承諾すれば保存でき、承諾した印が残ること。"""
        from db import query_one
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
            "weekly_cap_confirmed": True,
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)
        row = query_one("SELECT weekly_cap_ack FROM shifts WHERE id=?", (r.get_json()["id"],))
        assert row["weekly_cap_ack"] == 1, "承諾の印が残っていない"

    def test_within_cap_does_not_mark_ack(self, client):
        """上限内で保存したシフトに承諾の印が付かないこと（印の意味が薄まる）。"""
        from db import query_one
        shop_id, sid, tok = self._setup()
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
            "weekly_cap_confirmed": True,
        }, headers=auth(tok))
        row = query_one("SELECT weekly_cap_ack FROM shifts WHERE id=?", (r.get_json()["id"],))
        assert row["weekly_cap_ack"] == 0

    def test_put_rejects_extending_over_28h(self, client):
        """更新で長さを伸ばして超える場合も拒否されること。自分自身は二重計上しない。"""
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)
        target = self._add_confirmed(shop_id, sid, "2026-08-13", "09:00", "12:00", brk=0)  # 3h
        # 3h → 8h に伸ばすと窓が32hになる
        r = client.put(f"/api/shop/shifts/{target}", json={
            "start_datetime": "2026-08-13T09:00:00", "end_datetime": "2026-08-13T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 400
        assert r.get_json().get("weekly_cap_exceeded") is True

    def test_put_keeping_same_length_is_allowed(self, client):
        """自分自身を二重に数えていたら、変更なしの更新すら弾かれてしまう。"""
        shop_id, sid, tok = self._setup()
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)
        target = self._add_confirmed(shop_id, sid, "2026-08-14", "09:00", "13:00", brk=0)  # 4h
        r = client.put(f"/api/shop/shifts/{target}", json={
            "start_datetime": "2026-08-14T09:00:00", "end_datetime": "2026-08-14T13:00:00",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)

    def test_other_roles_are_not_blocked(self, client):
        """part_time は週28hで弾かれないこと。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "PT20", "パートB", "part_time", 1100, 0, 160)
        tok = make_session("shop", shop_id, shop_id)
        for d in ("2026-08-10", "2026-08-11", "2026-08-12"):
            self._add_confirmed(shop_id, sid, d)
        r = client.post("/api/shop/shifts", json={
            "staff_id": sid, "start_datetime": "2026-08-13T09:00:00",
            "end_datetime": "2026-08-13T18:00:00",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)
```

**注意**: `POST /api/shop/shifts` は必要人数超過チェック（`over_cap`）を先に通る。パターンを作っていない店では必要人数の制約が無いため通過するはずだが、もし 400 `over_cap` で落ちる場合は `insert_pattern` で余裕のあるパターンを足すこと。落ちた場合の実出力をレポートに書くこと。

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v -k ManualShiftWeeklyCap
```
Expected: `test_post_rejects_shift_over_28h` / `test_post_with_confirmation_saves_and_marks_ack` / `test_put_rejects_extending_over_28h` が FAIL。他は PASS。

- [ ] **Step 3: 判定ヘルパを実装する**

`src/app.py` の `_check_student_only_shift` の直後に追加:

```python
def _check_weekly_cap(shop_id, staff_id, start_iso, end_iso, break_minutes, exclude_id=None):
    """外国籍アルバイトの週28時間上限に触れるかを判定する。

    資格外活動許可で働く在留資格（留学・家族滞在）は入管法上、どの連続7日間も
    28時間以内。超えると本人の在留資格だけでなく、雇用主も不法就労助長罪の
    対象になる。判定そのものは src/weekly_hours.py の純関数に委ねる
    （シフト自動生成と同じ関数を使い、生成と手入力で結果が食い違わないようにする）。

    戻り値: (is_ng: bool, message: str or None, detail: dict or None)
    """
    target = query_one("SELECT role FROM staffs WHERE id=? AND shop_id=?", (staff_id, shop_id))
    if not target or target["role"] != "foreign_worker":
        return (False, None, None)
    day = (start_iso or "")[:10]
    if not day:
        return (False, None, None)
    # その日を含む7日窓は day-6 〜 day+6 の範囲にしか広がらないので、
    # この範囲の確定シフトだけを集めれば足りる。
    base = date.fromisoformat(day)
    lo = (base - timedelta(days=6)).isoformat()
    hi = (base + timedelta(days=7)).isoformat()
    rows = query_all(
        "SELECT id, start_datetime, end_datetime, break_time_minutes FROM shifts "
        "WHERE shop_id=? AND staff_id=? AND status='confirmed' "
        "AND start_datetime>=? AND start_datetime<?",
        (shop_id, staff_id, lo + "T00:00:00", hi + "T00:00:00"))
    spans = []
    for r in rows:
        # 更新・移動のときは自分自身を除く（除かないと変更前と変更後を二重に数え、
        # 長さを変えない更新すら弾かれる）。
        if exclude_id and str(r["id"]) == str(exclude_id):
            continue
        spans.append((r["start_datetime"], r["end_datetime"], r["break_time_minutes"] or 0))
    spans.append((start_iso, end_iso, break_minutes or 0))
    hit = exceeds_weekly_cap(minutes_by_day(spans), target_day=day)
    if not hit:
        return (False, None, None)
    w_start, w_end, total = hit
    detail = {"window_start": w_start, "window_end": w_end, "minutes": total,
              "cap_minutes": WEEKLY_CAP_MINUTES}
    msg = (f"{w_start}〜{w_end} の7日間で{total // 60}時間{total % 60}分になり、"
           f"外国籍アルバイトの週28時間の上限を超えます。")
    return (True, msg, detail)
```

`src/app.py` の import に追加:

```python
from weekly_hours import WEEKLY_CAP_MINUTES, minutes_by_day, exceeds_weekly_cap
```

`date` / `timedelta` が未 import なら足すこと（既存の import 行を確認する）。

- [ ] **Step 4: POST（作成）に組み込む**

`src/app.py:2689` の学生チェックの直後に追加:

```python
    # 週28h上限（外国籍アルバイト）。店長が承諾した場合のみ通す。
    weekly_ack = 1 if body.get("weekly_cap_confirmed") else 0
    weekly_ng, weekly_msg, weekly_detail = _check_weekly_cap(
        shop_id, staff_id, start_dt, end_dt, brk)
    if weekly_ng and not weekly_ack:
        print(f"[SHIFT POST] weekly_cap: {weekly_msg} staff_id={staff_id}", flush=True)
        return jsonify({"error": weekly_msg, "weekly_cap_exceeded": True,
                        "detail": weekly_detail}), 400
    # 上限内なら承諾フラグは立てない（印の意味を薄めないため）
    if not weekly_ng:
        weekly_ack = 0
```

INSERT を `weekly_cap_ack` を含む形に変える:

```python
    meta = execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason, availability, weekly_cap_ack) VALUES (?,?,?,?,?,?,?,?,?)",
                   (shop_id, staff_id, start_dt, end_dt, brk, body.get("status") or "confirmed", body.get("reason") or "手動追加", body.get("availability"), weekly_ack))
```

- [ ] **Step 5: PUT（更新）に組み込む**

`src/app.py:2761` の学生チェックの直後に、同じ形で追加する。`exclude_id=sid` を渡すこと。

```python
    weekly_ack = 1 if body.get("weekly_cap_confirmed") else 0
    weekly_ng, weekly_msg, weekly_detail = _check_weekly_cap(
        shop_id, staff_id, body["start_datetime"], body["end_datetime"], brk, exclude_id=sid)
    if weekly_ng and not weekly_ack:
        print(f"[SHIFT PUT sid={sid}] weekly_cap: {weekly_msg} staff_id={staff_id}", flush=True)
        return jsonify({"error": weekly_msg, "weekly_cap_exceeded": True,
                        "detail": weekly_detail}), 400
    if not weekly_ng:
        weekly_ack = 0
```

UPDATE に `weekly_cap_ack=?` を足す:

```python
    execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, status=?, reason=?, weekly_cap_ack=? WHERE id=? AND shop_id=?",
            (body["start_datetime"], body["end_datetime"], brk, body.get("status") or "confirmed", body.get("reason") or "手動調整", weekly_ack, sid, shop_id))
```

- [ ] **Step 6: PATCH draft-time（移動）に組み込む**

`src/app.py:2818` の学生チェックの直後に追加する。**この経路だけは 409 を返す**（既存の `student_only` と `overlap` が 409 のため、揃える）。

```python
    weekly_ng, weekly_msg, weekly_detail = _check_weekly_cap(
        shop_id, draft["staff_id"], start_datetime, end_datetime,
        compute_break_minutes(minutes_between(start_datetime, end_datetime)), exclude_id=sid)
    if weekly_ng and not (request.get_json(silent=True) or {}).get("weekly_cap_confirmed"):
        return jsonify({"error": weekly_msg, "weekly_cap_exceeded": True,
                        "detail": weekly_detail}), 409
```

この経路の UPDATE には `weekly_cap_ack` を足さない（ドラフトの時間調整であり、承諾の印は確定時の POST/PUT で付く）。**この判断をレポートに書くこと。**

- [ ] **Step 7: テストが通ることを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tests/run_tests.py
```
Expected: すべて PASS。

- [ ] **Step 8: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| POST の `if weekly_ng and not weekly_ack:` の return を消す | `test_post_rejects_shift_over_28h` |
| `_check_weekly_cap` の `exclude_id` の分岐（`continue`）を消す | `test_put_keeping_same_length_is_allowed` |
| INSERT の `weekly_cap_ack` を常に 0 にする | `test_post_with_confirmation_saves_and_marks_ack` |
| `if not weekly_ng: weekly_ack = 0` を消す | `test_within_cap_does_not_mark_ack` |
| `_check_weekly_cap` のロール判定を `!= "part_time"` に変える | `test_other_roles_are_not_blocked` |

実出力をレポートに貼ること。

- [ ] **Step 9: コミット**

```bash
.venv/bin/python -m pytest tests/ -q
git add src/app.py tests/test_foreign_worker_role.py
git commit -m "feat(api): 手動シフト入力で外国籍アルバイトの週28時間を検査する

作成・更新・移動の3経路で、どの連続7日間も28時間を超えないか検査する。
判定はシフト自動生成と同じ純関数を使い、生成と手入力で結果が食い違わない
ようにした。

超過時は weekly_cap_exceeded と、どの7日間が何時間になるのかを返す
（店長が判断できる材料が無ければダイアログを出す意味がない）。
weekly_cap_confirmed で店長が承諾した場合は保存し、そのシフトに
weekly_cap_ack を立てる。後から在留資格の照会を受けたときに、承諾して
入れたシフトを見分けられるようにするため。

更新・移動では自分自身を集計から除く。除かないと変更前と変更後を二重に
数え、長さを変えない更新すら弾かれる。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: ロールの表示（色・ラベル・選択肢）

**Files:**
- Modify: `public/style.css`（`:38` 付近のライト、`:125` 付近のダーク、`:945` / `:993` / `:1276` / `:1282` / `:1297` のセレクタ群）
- Modify: `public/app.js`（`:156` `roleClass`、`:168` `roleShort`、`:287` `roleLabel`、`:1148` / `:1565` の凡例）
- Modify: `public/admin.js`（`:617` / `:654` / `:736` のロール選択肢）
- Modify: `tests/test_design_tokens.py`（`LIGHT_EXPECTED` / `DARK_EXPECTED` / `LIGHT_PAIRS`）

**Interfaces:**
- Consumes: Task 2 の `foreign_worker` ロール
- Produces: CSS クラス `role-foreign`、トークン `--role-foreign` / `--role-foreign-ink`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_design_tokens.py` の `LIGHT_EXPECTED` に追加:

```python
    "role-foreign": "#EFD6C4", "role-foreign-ink": "#4A3323",
```

`DARK_EXPECTED` に追加:

```python
    "role-foreign": "#4C3B2E", "role-foreign-ink": "#EBD2BC",
```

`TestContrast.LIGHT_PAIRS` に追加:

```python
        ("外国籍バー", "role-foreign-ink", "role-foreign"),
```

- [ ] **Step 2: テストが失敗することを確認する**

Run:
```bash
.venv/bin/python -m pytest tests/test_design_tokens.py -v
```
Expected: `role-foreign` が `public/style.css` に無いため FAIL。

- [ ] **Step 3: CSS を追加する**

`public/style.css` の `:root`（ライト）の `--role-student-ink` の次に:

```css
  --role-foreign:        #EFD6C4;  /* 陶土・非常勤（外国籍アルバイト） */
  --role-foreign-ink:    #4A3323;
```

`html[data-theme="dark"]` の `--role-student-ink` の次に:

```css
  --role-foreign:        #4C3B2E;
  --role-foreign-ink:    #EBD2BC;
```

`:945` の行と同じ形で追加:

```css
.chip.role-foreign,   .dot.role-foreign,   .tl-bar.role-foreign,   .tl-role-badge.role-foreign   { background: var(--role-foreign);   color: var(--role-foreign-ink); }
```

`:993`:

```css
.tl-legend i.lg-role-foreign   { background: var(--role-foreign); }
```

`@media print` の3箇所（`:1276` / `:1282` / `:1297`）にも、既存の `role-student` の行と同じ形で `role-foreign`（`#EFD6C4` / `#4A3323`）を追加する。**印刷は常にライトの値を使う**のが既存の作法。

`:351` のグラデーション（`var(--role-student) 82% 100%`）は凡例のグラデーション帯。既存の4ロールで100%まで使い切っているため、**5色に割り直すか、現状のまま触らないかを判断してレポートに書くこと。** 触らない場合、外国籍アルバイトの色が帯に現れないだけで機能上の欠落は無い。

- [ ] **Step 4: JS のラベルを追加する**

`public/app.js:156` の `roleClass`:

```js
    case 'foreign_worker': return 'role-foreign';
```

`:168` の `roleShort`（バッジ用の略称。既存は「店長/社員/パート/学生」）:

```js
    case 'foreign_worker': return '外国籍';
```

`:287` の `roleLabel`（正式名称）:

```js
    : role === 'foreign_worker' ? '外国籍アルバイト'
```

`:1148` と `:1565` の凡例に、`role-student` の隣に追加:

```html
<span><i class="lg-role-foreign"></i>外国籍</span>
```

`public/admin.js` の3箇所（`:617` / `:654` / `:736`）に選択肢を追加:

```js
    { v: 'foreign_worker', label: '外国籍アルバイト（foreign_worker・週28h上限）' },
```

`:736` は `<option>` の形なので、その形に合わせること:

```html
<option value="foreign_worker">外国籍アルバイト（foreign_worker・週28h上限）</option>
```

`:628` の説明文にも週28hの一文を足すこと（既存は学生の月80hだけを説明している）。

- [ ] **Step 5: テストが通ることを確認する**

Run:
```bash
node --check public/app.js && node --check public/admin.js
.venv/bin/python -m pytest tests/test_design_tokens.py -v
.venv/bin/python -m pytest tests/ -q
```
Expected: すべて PASS。コントラスト検査（ライト 8.4:1 / ダーク 7.3:1）を満たすことを確認する。

**コントラスト検査に落ちた場合、テストの閾値を緩めないこと。** 色を調整し、調整後の実測値をレポートに書くこと。

- [ ] **Step 6: コミット**

```bash
git add public/style.css public/app.js public/admin.js tests/test_design_tokens.py
git commit -m "feat(ui): 外国籍アルバイトロールの表示を追加する

既存4ロールが登場するすべての箇所（色トークン・チップ/バー/バッジ・凡例・
印刷用の色指定・管理コンソールの選択肢）に足す。

色は既存の体系（寒色＝常勤、暖色＝非常勤）に従い陶土系にした。パートの黄・
学生の桜と区別でき、コントラストはライト8.4:1／ダーク7.3:1でWCAG AAを満たす。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 手動追加の確認ダイアログ（フロント + E2E）

**Files:**
- Modify: `public/app.js`（シフト手動追加・編集の保存ハンドラ）
- Create: `e2e/foreign_worker_weekly_cap.spec.js`

**Interfaces:**
- Consumes: Task 4 の `{"weekly_cap_exceeded": true, "detail": {...}}`、Task 5 の表示
- Produces: なし

**調査済みの前提**（実装者はこれを再調査しなくてよい）:

- シフト保存の呼び出しは5箇所。POST が `public/app.js:1614`（タイムラインの手動追加）、`:1667`（空きクリック追加）、`:1724`（隙間埋め）、`:2476`（「追加」モーダル）。PUT が `:1800`（シフト編集）
- 既存の「確認して再送」は `:2479` にある。`catch (e)` で `e.message.includes('必要人数')` を見て `confirm()` し、`{ ...payload, force: true }` で再送している。**この作法に揃える**
- `api()`（`:42`）は失敗時に `throw new Error(data.error)` するだけで、**レスポンスボディの他のフィールドを捨てている**。`weekly_cap_exceeded` / `detail` を読むには `api()` を拡張する必要がある

- [ ] **Step 1: `api()` にレスポンスボディを添える**

`public/app.js:48-51` を次にする:

```js
  if (!res.ok) {
    if (res.status === 401) logoutLocal();
    // サーバが返す構造化情報（weekly_cap_exceeded / detail 等）を捨てない。
    // 従来は message だけを投げていたため、呼び出し側は文言の部分一致でしか
    // エラー種別を判定できなかった（:2479 の includes('必要人数')）。
    const err = new Error(data.error || ('HTTP ' + res.status));
    err.status = res.status;
    err.data = data;
    throw err;
  }
```

`message` は従来と同じなので、既存の呼び出し側（`includes('必要人数')` を含む）の挙動は変わらない。

- [ ] **Step 2: E2E の失敗テストを書く**

Create `e2e/foreign_worker_weekly_cap.spec.js`。既存の `e2e/wish_image_import.spec.js` の作法（`ensureShop` / `loginAsManager` / `attachConsoleCollector`、`page.route` でのスタブ）に揃える。

`POST /api/shop/shifts` を `page.route` でスタブし、**1回目は 400 + `{"error": "...", "weekly_cap_exceeded": true, "detail": {...}}`、2回目は 200** を返す。実サーバの週計算に依存させない（このファイルが検証するのはフロントの確認と再送の動線であり、判定そのものは `tests/test_foreign_worker_role.py` の責務）。

`confirm()` は Playwright の `page.on('dialog', ...)` で受ける。

検証すること:

1. **承諾すると `weekly_cap_confirmed: true` を付けて再送される**
   - `page.on('dialog', d => d.accept())`
   - 2回目のリクエストの `postDataJSON()` に `weekly_cap_confirmed === true` があること
   - 1回目のボディには**入っていない**こと（最初から付けて送っていたら、サーバの検査が常に素通りする）
2. **ダイアログの文面にどの7日間が何時間になるかが出る**
   - `d.message()` に `detail.window_start` と `window_end` の日付、および時間数が含まれること
3. **キャンセルすると再送されない**
   - `page.on('dialog', d => d.dismiss())`
   - `POST /api/shop/shifts` が1回しか飛ばないこと
4. **上限内のときはダイアログが出ない**
   - スタブを最初から 200 にし、ダイアログが1度も発火しないこと（`dialogCount === 0`）

- [ ] **Step 3: テストが失敗することを確認する**

Run:
```bash
npx playwright test e2e/foreign_worker_weekly_cap.spec.js --reporter=list
```
Expected: 1〜3 が FAIL（確認も再送も未実装なので、400 が toast に出て終わる）。4 は PASS。

- [ ] **Step 4: 実装する**

保存呼び出しが5箇所あるため、同じ確認処理を5回書かず共通ヘルパにする。`public/app.js` の `api()` の近くに追加:

```js
/* 週28h上限（外国籍アルバイト）の確認付きシフト保存。
   サーバが weekly_cap_exceeded を返したら、どの7日間が何時間になるのかを見せて
   店長に確認し、承諾されたときだけ weekly_cap_confirmed を足して再送する。
   在留資格の変更直後などアプリが把握していない事情がありうるため強行の道は
   残すが、無断で通すと不法就労のシフトが黙って確定する。
   戻り値: 保存できたらサーバの応答、店長がキャンセルしたら null。 */
async function saveShiftWithWeeklyCapConfirm(path, payload, method = 'POST') {
  try {
    return await api(path, { method, body: JSON.stringify(payload) });
  } catch (e) {
    if (!e.data || !e.data.weekly_cap_exceeded) throw e;
    // 既存の「必要人数超過」確認（:2479）と同じ confirm を使う。
    if (!confirm(e.message + '\n\nそれでも登録しますか？')) return null;
    return await api(path, { method, body: JSON.stringify({ ...payload, weekly_cap_confirmed: true }) });
  }
}
```

5箇所の呼び出しを差し替える。`:2476` の例:

```js
          const r = await saveShiftWithWeeklyCapConfirm('/shop/shifts', payload);
          if (!r) return;   // 店長がキャンセルした（モーダルは開いたまま残す）
          close(); toast('追加しました', 'success'); navigateTo('shifts');
```

`:1614` / `:1667` / `:1724` / `:1800` も同様に、`await api('/shop/shifts', {...})` を
`await saveShiftWithWeeklyCapConfirm('/shop/shifts', <payloadオブジェクト>)` にし、
直後に `if (!r) return;` を足す。**インラインで組み立てているリクエストボディは、
一度 `const payload = {...}` に括り出してから渡すこと**（再送で同じボディが必要なため）。

`:1800` の PUT は `saveShiftWithWeeklyCapConfirm(`/shop/shifts/${s.id}`, payload, 'PUT')`。

`:2479` の `force: true` による既存の「必要人数超過」確認は**そのまま残す**（別の制約であり、両方が同時に起きることもある）。

- [ ] **Step 5: テストが通ることを確認する**

Run:
```bash
node --check public/app.js
npx playwright test e2e/foreign_worker_weekly_cap.spec.js --reporter=list
npx playwright test
.venv/bin/python -m pytest tests/ -q
```
Expected: すべて PASS。E2E 全体は 基準値145 + 新規分。

- [ ] **Step 6: テストが実際に守っていることを確認する**

| 壊す箇所 | 落ちるはずのテスト |
|---|---|
| 再送時の `weekly_cap_confirmed: true` を消す | 1（承諾しても保存できない） |
| `if (!confirm(...)) return null;` を `confirm` を呼ばず常に再送に変える | 3（キャンセルしても保存される） |
| `api()` の `err.data = data;` を消す | 1・2・3（種別を判定できずダイアログが出ない） |
| 最初のリクエストに `weekly_cap_confirmed: true` を付ける | 1（1回目のボディに入っていないことの assert が落ちる） |

**Playwright の Locator API はフォーカスやクリックの競合を自己修復します。** 「ダイアログが出た」ことの検証は、要素の可視性ではなく**再送リクエストの有無とボディ**で裏を取ること。

- [ ] **Step 7: コミット**

```bash
git add public/app.js e2e/foreign_worker_weekly_cap.spec.js
git commit -m "feat(ui): 週28時間を超える手動追加に確認ダイアログを出す

外国籍アルバイトの週上限を超えるシフトは、店長が内容を確認して承諾した
ときだけ保存する。在留資格の変更直後などアプリが把握していない事情が
ありうるため強行の道は残すが、どの7日間が何時間になるのかを必ず見せる。

承諾して保存したシフトにはサーバ側で印（weekly_cap_ack）が付く。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review

**設計書のカバレッジ**

| 設計書の項目 | 対応タスク |
|---|---|
| 1. ロール定義（CHECK・マイグレーション・色トークン） | Task 2（DB）、Task 5（色） |
| 2. 週28時間の判定（純関数） | Task 1 |
| 3. 自動生成（実績の先読み・can_place・検証） | Task 3 |
| 4. 手動入力（3経路・承諾フラグ） | Task 4 |
| 5. スタッフ登録・編集 API | Task 2 |
| 6. UI（色・ラベル・凡例・選択肢・確認ダイアログ） | Task 5、Task 6 |
| 7. テスト | 各タスク |

**依存関係**

Task 1 → Task 3、Task 4
Task 2 → Task 3、Task 4、Task 5
Task 4 + Task 5 → Task 6

**設計書から変えた点**

無し。

**判断を実装者に委ねた点**

- Task 5 Step 3: 凡例のグラデーション帯（`style.css:351`）を5色に割り直すか触らないか。現状は4ロールで100%を使い切っている。既存コードを見てから決めるのが妥当なため、判断と理由をレポートに書かせる形にした

**確認ダイアログに `confirm()` を使う判断について**

既存の「必要人数超過」確認（`public/app.js:2479`）が `confirm()` を使っており、揃えた。
独自モーダルにすると同種の確認が2つの見た目で現れることになる。
E2E は `page.on('dialog', ...)` で受けられるため検証もできる。

**残る不確実性**

- Task 4 のテストが `POST /api/shop/shifts` の必要人数超過チェック（`over_cap`）に先に引っかかる可能性がある。パターンを作っていない店では必要人数の制約が無いため通過するはずだが、実際に落ちたらパターンを足して回避し、その事実をレポートに書く
- Task 2 のマイグレーションは `staffs` の再構築を含む。ローカルの `shift.db` で実際に適用して確認するステップ（Task 2 Step 7）を必ず実行すること
