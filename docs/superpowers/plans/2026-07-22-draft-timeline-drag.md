# ドラフトタイムライン直接調整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AIドラフトだけを公開済みシフトに触れず15分刻みでドラッグ・伸縮でき、パストラバーサルを防いだ状態で画面確認できるようにする。

**Architecture:** 既存の `status='requested'` かつ `reason LIKE 'AIドラフト%'` の行を、この縦切りのドラフトとして扱う。時間更新は専用 PATCH API に限定し、共通の汎用 PUT や `auto_adjust` を経由しない。タイムラインは Pointer Events で見た目だけを更新し、指・マウスを離した時に一度だけ API 保存する。

**Tech Stack:** Python 3.11、Flask、SQLite / Cloudflare D1互換スキーマ、Vanilla JavaScript、CSS、pytest、In-app Browser。

## Global Constraints

- 公開済み・手動作成・スタッフ希望のシフトはドラッグ API とポインター編集の対象にしない。
- 時刻はサーバー・画面とも15分単位、最短15分に限定する。
- ドラッグ保存は `auto_adjust` を使わず、他スタッフのシフトを変更しない。
- 全てのドラフト更新は認証済みの自店舗に限定し、古い `updated_at` は409で拒否する。
- 既存の未コミット変更は、この worktree と実装コミットに混ぜない。

---

## File Structure

| File | Responsibility |
|---|---|
| `src/app.py` | 静的配信の安全なパス判定、既存DBの `updated_at` 移行、ドラフト時間 PATCH API |
| `schema.sql` | 新規DBの `shifts.updated_at` 定義 |
| `public/app.js` | ドラフトバーのハンドル、PC/モバイル Pointer Events、保存・1回の戻す操作 |
| `public/style.css` | ドラッグ可能、保存中、拒否時、ハンドルの視覚表現 |
| `tests/test_security.py` | 静的パス脱出を拒否する回帰テスト |
| `tests/test_draft_timeline.py` | ドラフト専用 PATCH API の認可、15分制約、競合、保存不変性 |

## Public Interfaces

~~~text
PATCH /api/shop/shifts/<shift_id>/draft-time
Authorization: Bearer <shop-session>
{
  "start_datetime": "2026-08-03T09:15:00",
  "end_datetime": "2026-08-03T17:15:00",
  "updated_at": "2026-08-02 12:34:56"
}

200 {"ok": true, "shift": {"id": 1, "start_datetime": "2026-08-03T09:15:00", "end_datetime": "2026-08-03T17:15:00", "updated_at": "2026-08-03T08:00:00.123456"}}
400 invalid time shape or non-15-minute time
409 stale draft, non-draft shift, overlap, cap, or student-supervision blocker
~~~

### Task 1: 静的ファイルのルート外読出しを遮断する

**Files:**
- Modify: `src/app.py:9,3380-3392`
- Modify: `tests/test_security.py:405-416`

**Consumes:** `PUBLIC_DIR`、Werkzeug の `safe_join`。

**Produces:** URLパスが `PUBLIC_DIR` の外を指す場合に404を返す `static_files`。

- [ ] **Step 1: 既存の回帰テストを失敗として再現する**

Run: `/Users/tsuyoshi/Library/Mobile Documents/com~apple~CloudDocs/Desktop/dev/shift_saas_flask/.venv/bin/python -m pytest tests/test_security.py::TestPathTraversal::test_static_file_traversal -q`

Expected: FAIL。`../../../etc/passwd` 応答に `root:` が含まれる。

- [ ] **Step 2: 最小の安全なパス判定を実装する**

```python
from werkzeug.utils import safe_join

@app.get("/<path:path>")
def static_files(path):
    if path.startswith("api/"):
        abort(404, description="Not Found")
    full = safe_join(PUBLIC_DIR, path)
    if full is None:
        abort(404, description="Not Found")
    if os.path.isfile(full):
        return send_file(full)
    return _index_html_with_asset_version()
```

`app.js` と `style.css` の `Cache-Control` 分岐はこの `full` を使ったまま維持する。

- [ ] **Step 3: セキュリティテストを通す**

Run: `/Users/tsuyoshi/Library/Mobile Documents/com~apple~CloudDocs/Desktop/dev/shift_saas_flask/.venv/bin/python -m pytest tests/test_security.py::TestPathTraversal::test_static_file_traversal tests/test_qa_comprehensive.py::TestSecurityAdvanced::test_path_traversal_in_static_files -q`

Expected: `2 passed`。

### Task 2: ドラフト時間を安全に更新するAPIを追加する

**Files:**
- Modify: `schema.sql:93-105`
- Modify: `src/app.py:213-244, 2730-2815, 3395-3445`
- Create: `tests/test_draft_timeline.py`

**Consumes:** `_shop_ctx`, `_check_slot_cap`, `_check_staff_overlap`, `_check_student_only_shift`, `parse_iso`, `compute_break_minutes`。

**Produces:** `PATCH /api/shop/shifts/<id>/draft-time` と `shifts.updated_at`。

- [ ] **Step 1: 失敗するドラフト更新テストを書く**

```python
def test_draft_time_patch_updates_only_ai_draft_at_15_minute_boundary(client):
    shop_id, staff_id, shift_id, token = make_ai_draft()
    current = client.get(f"/api/shop/shifts?start={MON}&end={MON}", headers=auth(token)).get_json()["shifts"][0]
    response = client.patch(f"/api/shop/shifts/{shift_id}/draft-time", json={
        "start_datetime": f"{MON}T09:15:00", "end_datetime": f"{MON}T17:15:00",
        "updated_at": current["updated_at"] or current["created_at"],
    }, headers=auth(token))
    assert response.status_code == 200
    row = dbmod.query_one("SELECT status,reason,start_datetime,end_datetime FROM shifts WHERE id=?", (shift_id,))
    assert (row["status"], row["reason"][:6], row["start_datetime"], row["end_datetime"]) == ("requested", "AIドラフト", f"{MON}T09:15:00", f"{MON}T17:15:00")

def test_draft_time_patch_rejects_confirmed_non_quarter_hour_and_stale_update(client):
    shop_id, staff_id, shift_id, token = make_ai_draft()
    current = client.get(f"/api/shop/shifts?start={MON}&end={MON}", headers=auth(token)).get_json()["shifts"][0]
    rejected = client.patch(f"/api/shop/shifts/{shift_id}/draft-time", json={
        "start_datetime": f"{MON}T09:07:00", "end_datetime": f"{MON}T17:00:00",
        "updated_at": current["updated_at"] or current["created_at"],
    }, headers=auth(token))
    assert rejected.status_code == 400
    saved = client.patch(f"/api/shop/shifts/{shift_id}/draft-time", json={
        "start_datetime": f"{MON}T09:15:00", "end_datetime": f"{MON}T17:15:00",
        "updated_at": current["updated_at"] or current["created_at"],
    }, headers=auth(token))
    assert saved.status_code == 200
    stale = client.patch(f"/api/shop/shifts/{shift_id}/draft-time", json={
        "start_datetime": f"{MON}T09:30:00", "end_datetime": f"{MON}T17:30:00",
        "updated_at": current["updated_at"] or current["created_at"],
    }, headers=auth(token))
    assert stale.status_code == 409
    dbmod.execute("UPDATE shifts SET status='confirmed' WHERE id=?", (shift_id,))
    locked = client.patch(f"/api/shop/shifts/{shift_id}/draft-time", json={
        "start_datetime": f"{MON}T09:30:00", "end_datetime": f"{MON}T17:30:00",
        "updated_at": saved.get_json()["shift"]["updated_at"],
    }, headers=auth(token))
    assert locked.status_code == 409
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `/Users/tsuyoshi/Library/Mobile Documents/com~apple~CloudDocs/Desktop/dev/shift_saas_flask/.venv/bin/python -m pytest tests/test_draft_timeline.py -q`

Expected: FAIL。専用 PATCH ルートが存在しない。

- [ ] **Step 3: スキーマ互換と専用更新を実装する**

`schema.sql` の `shifts` に `updated_at TEXT` を追加する。`ensure_db()` は `PRAGMA table_info(shifts)` で旧DBを確認し、列がない場合だけ `ALTER TABLE shifts ADD COLUMN updated_at TEXT` を実行する。

PATCH ルートは次を順に行う。

```python
draft = query_one("SELECT * FROM shifts WHERE id=? AND shop_id=?", (sid, shop_id))
if not draft:
    abort(404, description="シフトが見つかりません")
if draft["status"] != "requested" or not (draft.get("reason") or "").startswith("AIドラフト"):
    return jsonify({"error": "AIドラフトだけを直接調整できます"}), 409
```

入力開始・終了は `parse_iso` で検査し、秒が0、分が15の倍数、終了が開始より15分以上後であることを確認する。`_check_slot_cap(shop_id, start_datetime, end_datetime, exclude_id=sid)`、`_check_staff_overlap(shop_id, staff_id, start_datetime, end_datetime, exclude_id=sid, include_requested=True)`、`_check_student_only_shift` が問題を返す場合は409を返す。`updated_at` または `created_at` がリクエスト値と異なる場合も409を返す。成功時だけ対象行を更新し、`status` と `reason` を変えず、`break_time_minutes` とミリ秒を含む新しい `updated_at` を返す。

- [ ] **Step 4: APIテストを通す**

Run: `/Users/tsuyoshi/Library/Mobile Documents/com~apple~CloudDocs/Desktop/dev/shift_saas_flask/.venv/bin/python -m pytest tests/test_draft_timeline.py tests/test_ai_draft_finalize.py -q`

Expected: PASS。

### Task 3: ドラフトバーを直接操作できるようにする

**Files:**
- Modify: `public/app.js:951-1233`
- Modify: `public/style.css:619-677`
- Test: In-app Browser manual acceptance on the local worktree server

**Consumes:** `openDayTimeline`, `_extMinFromIso`, `_extHourToIsoTime`, `api`、Task 2のPATCH API。

**Produces:** ドラフトバー本体の移動、両端ハンドルの伸縮、保存エラー時の復元、直前1回の戻す操作。

- [ ] **Step 1: フロントエンドの受入条件を先に固定する**

`openDayTimeline` の出力で AIドラフトだけに `data-draft-editable="true"` と左右ハンドルを付け、確定バーにはハンドルを出さないことを、ブラウザのDOMと画面で確認する。

- [ ] **Step 2: ポインター操作と保存を実装する**

`isAiDraftShift(s)` は `status === 'requested'` かつ `reason` が `AIドラフト` から始まる時だけ真を返す。該当バーは本体ドラッグで開始・終了を同じ差分だけ移動し、左右12pxのハンドルで片側だけを伸縮する。

ブラウザ側も15分単位に丸め、最短15分、タイムライン表示範囲内に収める。PCは pointerdown で直ちに操作し、タッチは300ms後に開始する。操作中はCSSだけを更新し、pointerupで以下を一度だけ送る。

```javascript
await api(`/shop/shifts/${shift.id}/draft-time`, {
  method: 'PATCH',
  body: JSON.stringify({ start_datetime, end_datetime, updated_at: shift.updated_at || shift.created_at }),
});
```

保存中は二重操作を止める。409/400時は元の位置へ戻して理由を表示し、成功時だけレスポンスの `updated_at` を保持する。直前の成功操作だけを戻せるボタンを表示し、同じPATCH APIで復元する。

- [ ] **Step 3: スタイルと構文を検証する**

`tl-bar-draft` に `touch-action: pan-y`、ハンドル、ドラッグ中、保存中、拒否時のスタイルを追加する。確定バーの `cursor` と操作を変更しない。

Run: `node --check public/app.js`

Expected: exit 0。

- [ ] **Step 4: In-app Browserで受入確認する**

worktreeのFlaskサーバーを別ポートで起動し、AIドラフトを作成してシフト画面を開く。次を確認する。

1. AIドラフトだけに左右ハンドルと「ドラッグで調整」の案内が見える。
2. バー移動と両端伸縮が15分単位で保存される。
3. 確定バーはドラッグできない。
4. 不正操作は元位置へ戻り、直前の成功操作は1回だけ戻せる。

### Task 4: 全体回帰を確認し、意図した変更だけを記録する

**Files:**
- Modify: Task 1-3で変更したファイルのみ

- [ ] **Step 1: 全テストを実行する**

Run: `/Users/tsuyoshi/Library/Mobile Documents/com~apple~CloudDocs/Desktop/dev/shift_saas_flask/.venv/bin/python -m pytest tests -q`

Expected: `0 failed`。

- [ ] **Step 2: 差分を確認してコミットする**

Run: `git diff --check && git status --short`

Stage only: `schema.sql src/app.py public/app.js public/style.css tests/test_security.py tests/test_draft_timeline.py docs/superpowers/plans/2026-07-22-draft-timeline-drag.md`

Commit: `feat: enable direct adjustment of AI draft shifts`
