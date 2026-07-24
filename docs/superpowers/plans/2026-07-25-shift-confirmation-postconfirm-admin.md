# 超過確定の可視化・確定後変更・管理者画面拡充 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** シフトの超過確定を可視化（フラグ＋店長メモ）し、確定後変更を変更申請で運用可能にし、管理者画面に店舗・スタッフ管理と監査ログを追加する。

**Architecture:** Flask（`src/app.py` 単一ファイル API）＋ SQLite（`schema.sql` / 起動時 `ensure_db()` の冪等 `ALTER TABLE`）＋ 単一ページJS（`public/app.js`）。既存の超過判定ロジック（`_check_slot_cap` / `_count_over_cap_slots`）と変更申請フロー（`change_requests`）を再利用し、列追加・エンドポイント追加・フロント表示で拡張する。

**Tech Stack:** Python 3 / Flask / sqlite3 / pytest / 素のJS（フレームワークなし、Bootstrap Icons）。

## Global Constraints

- DBマイグレーションは `ensure_db()` 内で `PRAGMA table_info` によるガード付き冪等 `ALTER TABLE ... ADD COLUMN` として追加する。失敗しても業務停止しないよう try/except でログ出力（監査テーブル作成失敗は握り潰す）。
- 日時は `"YYYY-MM-DDTHH:MM:SS"`（ゼロ埋め）形式。時刻は15分単位が既存前提。
- 認可は既存 `require_auth([...])` / `_shop_ctx()` を使う。店舗系は `_shop_ctx()`、管理者系は `require_auth(["admin"])`。
- 超過フラグ・店長メモは**スタッフ画面に出さない**（店長のみ）。
- DB操作は既存ヘルパー `query_one` / `query_all` / `execute` / `insert_row` を使う。
- 既存テスト（`tests/`）を壊さないこと。特に `test_ai_draft_finalize.py`, `test_workflow_regression.py`, `test_admin_*`。
- コミットはタスクごとに行う。

## File Structure

- `schema.sql` — 新規DB向けに `shifts` へ列追加、`audit_logs` テーブル追加。
- `src/app.py` — `ensure_db()` にマイグレーション追記、`_flag_over_cap_shifts` / `audit` ヘルパー追加、`finalize` / `shop_creq_resolve` 改修、`note` PATCH・`audit-logs` GET・スタッフ汎用編集 PUT の各エンドポイント追加、既存管理者エンドポイントへ `audit()` 仕込み、CSVエクスポートに列追加。
- `public/app.js` — シフトカードの超過バッジ＋メモ表示・編集、確定後サマリ、変更申請バッジ、管理者のスタッフ管理UI・監査ログビューア。
- `tests/test_over_cap_finalize.py`（新規）、`tests/test_shift_note.py`（新規）、`tests/test_creq_postconfirm.py`（新規）、`tests/test_audit_log.py`（新規）、`tests/test_admin_staff_edit.py`（新規）。

---

## フェーズA: テーマ1（超過確定の可視化）

### Task A1: shifts への列追加（マイグレーション）

**Files:**
- Modify: `schema.sql`（`shifts` 定義）
- Modify: `src/app.py`（`ensure_db()` 内、既存 `shifts.updated_at` 追加ブロックの直後）
- Test: `tests/test_over_cap_finalize.py`

**Interfaces:**
- Produces: `shifts.over_cap_flag INTEGER DEFAULT 0`, `shifts.note TEXT` の2列。

- [ ] **Step 1: schema.sql に列追加**

`schema.sql` の `shifts` テーブル定義、`updated_at TEXT,` の直後に追加:

```sql
  over_cap_flag       INTEGER DEFAULT 0,
  note                TEXT,
```

- [ ] **Step 2: ensure_db() に冪等マイグレーション追加**

`src/app.py` の `ensure_db()` 内、`shifts.updated_at` を追加している try/except ブロックの直後に追記:

```python
    try:
        shift_columns = {row["name"] for row in query_all("PRAGMA table_info(shifts)")}
        if "over_cap_flag" not in shift_columns:
            execute("ALTER TABLE shifts ADD COLUMN over_cap_flag INTEGER DEFAULT 0")
            print("[ensure_db] OK: shifts.over_cap_flag added", flush=True)
        if "note" not in shift_columns:
            execute("ALTER TABLE shifts ADD COLUMN note TEXT")
            print("[ensure_db] OK: shifts.note added", flush=True)
    except Exception as e:
        print(f"[ensure_db] FAIL: shifts over_cap/note migration failed: {e}", flush=True)
        raise
```

- [ ] **Step 3: テストで列存在を確認（失敗を先に見る）**

`tests/test_over_cap_finalize.py` を新規作成。既存テストの `conftest.py` フィクスチャ（`client` 等）に倣う。まず列存在テスト:

```python
def test_shifts_has_over_cap_columns(client):
    from src.db import query_all
    cols = {r["name"] for r in query_all("PRAGMA table_info(shifts)")}
    assert "over_cap_flag" in cols
    assert "note" in cols
```

Run: `.venv/bin/pytest tests/test_over_cap_finalize.py::test_shifts_has_over_cap_columns -v`
Expected: PASS（`ensure_db()`/`init_schema` がテスト起動時に走るため）。落ちる場合は conftest のDB初期化経路を確認し、テストDBに `init_schema` が適用されていることを保証する。

- [ ] **Step 4: 既存テスト全体が壊れていないことを確認**

Run: `.venv/bin/pytest -q`
Expected: 既存テスト全PASS。

- [ ] **Step 5: Commit**

```bash
git add schema.sql src/app.py tests/test_over_cap_finalize.py
git commit -m "feat: shiftsにover_cap_flag/note列を追加（マイグレーション）"
```

### Task A2: `_flag_over_cap_shifts` ヘルパー＋finalize統合

**Files:**
- Modify: `src/app.py`（`_count_over_cap_slots` の直後にヘルパー追加、`shop_shifts_finalize` を改修）
- Test: `tests/test_over_cap_finalize.py`

**Interfaces:**
- Consumes: 既存 `shift_engine.load_weekday_overrides`, `shift_engine._day_requirements`, `shift_engine.GRAN`, `_iter_slots`（`_count_over_cap_slots` と同じ要件計算）。
- Produces: `_flag_over_cap_shifts(shop_id, start_iso, end_iso) -> int`（フラグを立てた件数を返す）。`shop_shifts_finalize` のレスポンスに `over_cap`（int）を追加。

- [ ] **Step 1: 失敗テストを書く**

`tests/test_over_cap_finalize.py` に追加。必要人数1のパターンに対し、同一時間帯へ2名のドラフトを作って finalize すると両方 confirmed になり、両方に `over_cap_flag=1` が付くこと。ヘルパー的なセットアップは既存 `tests/helpers.py` を参照（店舗・パターン・スタッフ・ドラフト作成）。

```python
def test_finalize_flags_over_cap_shifts(shop_client, seed_over_cap_drafts):
    # seed: required_staff=1 の時間帯に staff A/B の AIドラフト2件（requested）
    start, end = seed_over_cap_drafts["start_date"], seed_over_cap_drafts["end_date"]
    r = shop_client.post('/api/shop/shifts/finalize',
                         json={"start_date": start, "end_date": end})
    assert r.status_code == 200
    data = r.get_json()
    assert data["finalized"] == 2
    assert data["over_cap"] == 2
    from src.db import query_all
    flags = [row["over_cap_flag"] for row in
             query_all("SELECT over_cap_flag FROM shifts WHERE status='confirmed'")]
    assert flags == [1, 1]
```

（`shop_client` / `seed_over_cap_drafts` フィクスチャは conftest に用意。既存 conftest のパターンに合わせて作る。必要人数1のパターン + 同一枠2ドラフトを INSERT。）

Run: `.venv/bin/pytest tests/test_over_cap_finalize.py::test_finalize_flags_over_cap_shifts -v`
Expected: FAIL（`over_cap` キーが無い / フラグが立たない）。

- [ ] **Step 2: ヘルパーを実装**

`src/app.py`、`_count_over_cap_slots`（末尾 `return over_count`）の直後に追加。**スロットは `shift_engine._shift_slots(start_iso, end_iso, GRAN)` が返す「分単位int」のリスト**で、`req_map` も分単位intキー（`_check_slot_cap` と同一モデル）。スロット分は日をまたいで繰り返すため、カバレッジ・超過は必ず `(day, slot_min)` で管理する:

```python
def _flag_over_cap_shifts(shop_id, start_iso, end_iso):
    """期間内の confirmed シフトのうち、必要人数を超えるスロットに重なるものへ
    over_cap_flag=1 を立てる。超過に重ならないものは 0 にリセット。
    戻り値: フラグを立てたシフト件数。"""
    pats = query_all("SELECT id, start_time, end_time, required_staff FROM shift_patterns WHERE shop_id=?", (shop_id,))
    if not pats:
        return 0
    weekday_overrides = shift_engine.load_weekday_overrides(shop_id)
    rows = query_all(
        "SELECT id, start_datetime, end_datetime, reason, over_cap_flag FROM shifts "
        "WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=?",
        (shop_id, start_iso, end_iso))
    if not rows:
        return 0
    shift_slots = {}            # shift_id -> (day, [slot_min,...])
    coverage = {}               # (day, slot_min) -> count
    for r in rows:
        day = r["start_datetime"][:10]
        slots = shift_engine._shift_slots(r["start_datetime"], r["end_datetime"], shift_engine.GRAN)
        shift_slots[r["id"]] = (day, slots)
        for sl in slots:
            coverage[(day, sl)] = coverage.get((day, sl), 0) + 1
    req_cache = {}
    def _req_for(day):
        if day not in req_cache:
            wd = (datetime.strptime(day, "%Y-%m-%d").weekday() + 1) % 7
            applied = []
            for pat in pats:
                ov = weekday_overrides.get((pat.get("id"), wd))
                p = dict(pat)
                if ov is not None:
                    p["required_staff"] = ov
                applied.append(p)
            req_cache[day] = shift_engine._day_requirements(applied, shift_engine.GRAN, wd, weekday_overrides)
        return req_cache[day]
    over = set()                # (day, slot_min) が超過
    for (day, sl), cnt in coverage.items():
        req = _req_for(day).get(sl, 0)
        if req and req > 0 and cnt > req:
            over.add((day, sl))
    flagged = 0
    for r in rows:
        day, slots = shift_slots[r["id"]]
        is_over = any((day, sl) in over for sl in slots)
        new_flag = 1 if is_over else 0
        new_reason = r["reason"]
        if is_over:
            peak = max((coverage.get((day, sl), 0) for sl in slots), default=0)
            tag = f"必要人数超過（配置{peak}名の時間帯を含む）"
            if not (new_reason or "").endswith(tag):
                new_reason = (new_reason + " / " if new_reason else "") + tag
            flagged += 1
        if new_flag != (r.get("over_cap_flag") or 0) or new_reason != r["reason"]:
            execute("UPDATE shifts SET over_cap_flag=?, reason=? WHERE id=? AND shop_id=?",
                    (new_flag, new_reason, r["id"], shop_id))
    return flagged
```

**テストの規約（重要）:** 本リポジトリのテストは `client` フィクスチャ＋ `tests/helpers.py`（`insert_shop`/`insert_staff`/`insert_pattern`/`insert_request`/`make_session`/`auth`）を使い、`Authorization: Bearer` ヘッダで認証する（`shop_client`/`admin_client` のようなフィクスチャは存在しない）。各テストは `make_session("shop", staff_or_shop_id, shop_id)` などでトークンを作り、`client.post(url, json=..., headers=auth(token))` を呼ぶ。以降のタスクのテスト擬似コードもこの規約に読み替えること。

- [ ] **Step 3: finalize にフラグ付与を組み込む**

`shop_shifts_finalize` 内、全 `requested` を `confirmed` に UPDATE し終えた後、店舗通知の前に追加:

```python
    over_cap = _flag_over_cap_shifts(shop_id, start_d + "T00:00:00", end_d + "T23:59:59")
```

レスポンス2箇所（対象0件の early return は over_cap=0）に `"over_cap": over_cap` を追加。メッセージへ超過があれば追記:

```python
    msg = f"{finalized_count} 件のシフトを確定し、{len(finalized_staff)} 名のスタッフに通知しました。"
    if over_cap:
        msg += f"（うち {over_cap} 件が必要人数超過です）"
    return jsonify({"ok": True, "finalized": finalized_count,
                    "notified_staff": len(finalized_staff),
                    "over_cap": over_cap, "message": msg})
```

- [ ] **Step 4: テストを通す**

Run: `.venv/bin/pytest tests/test_over_cap_finalize.py -v`
Expected: PASS。超過なしケース（必要人数を満たすだけ）で `over_cap==0`・フラグ0のテストも追加して確認。

- [ ] **Step 5: 回帰確認 & Commit**

```bash
.venv/bin/pytest -q
git add src/app.py tests/test_over_cap_finalize.py
git commit -m "feat: 確定時に必要人数超過シフトへover_cap_flagを自動付与"
```

### Task A3: 店長メモ PATCH エンドポイント

**Files:**
- Modify: `src/app.py`（`shop_shift_draft_time_patch` の近く、シフト系エンドポイント群）
- Test: `tests/test_shift_note.py`

**Interfaces:**
- Produces: `PATCH /api/shop/shifts/<int:sid>/note`、body `{note}`、認可 `_shop_ctx()`。confirmed/その他問わず自店舗のシフトに `note` を設定/クリア（空文字は NULL）。

- [ ] **Step 1: 失敗テスト**

```python
def test_set_and_clear_shift_note(shop_client, seed_confirmed_shift):
    sid = seed_confirmed_shift["shift_id"]
    r = shop_client.patch(f'/api/shop/shifts/{sid}/note', json={"note": "新人研修のため増員"})
    assert r.status_code == 200
    from src.db import query_one
    assert query_one("SELECT note FROM shifts WHERE id=?", (sid,))["note"] == "新人研修のため増員"
    r2 = shop_client.patch(f'/api/shop/shifts/{sid}/note', json={"note": ""})
    assert r2.status_code == 200
    assert query_one("SELECT note FROM shifts WHERE id=?", (sid,))["note"] in (None, "")
```

Run: `.venv/bin/pytest tests/test_shift_note.py -v` → FAIL（404 / エンドポイント無し）。

- [ ] **Step 2: 実装**

```python
@app.patch("/api/shop/shifts/<int:sid>/note")
def shop_shift_note_patch(sid):
    _, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    note = (body.get("note") or "").strip() or None
    existing = query_one("SELECT id FROM shifts WHERE id=? AND shop_id=?", (sid, shop_id))
    if not existing:
        abort(404, description="シフトが見つかりません")
    execute("UPDATE shifts SET note=? WHERE id=? AND shop_id=?", (note, sid, shop_id))
    return jsonify({"ok": True, "note": note})
```

- [ ] **Step 3: テストを通す**

Run: `.venv/bin/pytest tests/test_shift_note.py -v` → PASS。

- [ ] **Step 4: Commit**

```bash
git add src/app.py tests/test_shift_note.py
git commit -m "feat: シフト店長メモPATCHエンドポイントを追加"
```

### Task A4: CSVエクスポートに超過フラグ・メモ列を追加

**Files:**
- Modify: `src/app.py`（`shop_shifts_export`）
- Test: `tests/test_over_cap_finalize.py`（エクスポート検証を追加）

**Interfaces:**
- Consumes: `shifts.over_cap_flag`, `shifts.note`（`SELECT sh.*` で取得済）。

- [ ] **Step 1: 失敗テスト**

超過フラグ・メモを持つ確定シフトをエクスポートし、CSV本文に `超過` 列と note が含まれること。

```python
def test_export_includes_flag_and_note(shop_client, seed_over_cap_confirmed):
    start, end = seed_over_cap_confirmed["start_date"], seed_over_cap_confirmed["end_date"]
    r = shop_client.get(f'/api/shop/shifts/export?start={start}&end={end}')
    assert r.status_code == 200
    text = r.data.decode('utf-8')
    assert "超過" in text.splitlines()[0]  # ヘッダ
    assert "メモ" in text.splitlines()[0]
```

Run → FAIL。

- [ ] **Step 2: 実装**

`shop_shifts_export` のヘッダ行末尾に `,超過,メモ` を追加し、各行の `cells` 末尾に:

```python
            "超過" if (r.get("over_cap_flag") or 0) else "",
            r.get("note") or "",
```

ヘッダ文字列も更新:

```python
    lines = ["日付,曜日,開始,終了,休憩(分),実働(分),深夜(分),スタッフコード,氏名,ロール,ステータス,超過,メモ"]
```

- [ ] **Step 3: テストを通す** → PASS。

- [ ] **Step 4: Commit**

```bash
git add src/app.py tests/test_over_cap_finalize.py
git commit -m "feat: CSVエクスポートに超過フラグ・店長メモ列を追加"
```

### Task A5: フロント（超過バッジ・メモ・確定後サマリ）

**Files:**
- Modify: `public/app.js`

**Interfaces:**
- Consumes: シフトオブジェクトの `over_cap_flag`, `note`；`PATCH /shop/shifts/<id>/note`；finalize レスポンスの `over_cap`。

- [ ] **Step 1: 確定後サマリ表示**

`public/app.js:1948-1950` の finalize 呼び出し後の toast を、超過件数を出すよう変更:

```javascript
      const r = await api('/shop/shifts/finalize', { method: 'POST', body: JSON.stringify({ start_date: start, end_date: end }) });
      const extra = r.over_cap ? `（必要人数超過 ${r.over_cap} 件。カードの⚠️で確認してください）` : '';
      toast((r.message || `${r.finalized}件を確定しました`) + extra, r.over_cap ? 'warning' : 'success');
```

- [ ] **Step 2: シフトカードにバッジ＋メモ表示**

店長側タイムライン/一覧のシフトカードを描画する関数を特定（`grep -n "shift" public/app.js` でカード生成箇所を探す。時刻や氏名を差し込んでいるテンプレートリテラル）。`over_cap_flag` が真なら `⚠️` バッジ（Bootstrap Icons `bi-exclamation-triangle`）、`note` があれば小さめのメモ行を追加。**スタッフ側の描画関数には追加しない**。

- [ ] **Step 3: メモ編集UI**

シフト詳細/カードのアクションに「メモ」ボタンを追加し、`prompt`（または既存の openModal パターン）で入力→`api('/shop/shifts/'+id+'/note', {method:'PATCH', body: JSON.stringify({note})})`→再描画。

- [ ] **Step 4: 手動確認**

`.venv/bin/python -m src.app` でローカル起動し、店長でログイン→AI生成→確定→カードに⚠️・メモ編集が動くことをブラウザで確認（サブエージェントのSTでも検証）。

- [ ] **Step 5: Commit**

```bash
git add public/app.js
git commit -m "feat: 店長画面に超過バッジ・店長メモ・確定後サマリを表示"
```

---

## フェーズB: テーマ2（確定後変更＝変更申請ベース）

### Task B1: 変更申請の承認/却下バックエンド改修

**Files:**
- Modify: `src/app.py`（`shop_creq_resolve`）
- Test: `tests/test_creq_postconfirm.py`

**Interfaces:**
- Consumes: `_flag_over_cap_shifts`, `notify`, `_check_staff_overlap`。
- Produces: 却下時 `notify(...)`；承認（change/add）後に該当日へ `_flag_over_cap_shifts` を実行。

- [ ] **Step 1: 失敗テスト**

(1) 却下でスタッフ通知が1件増える。(2) 確定シフトに対する change 承認で時間が更新される。(3) 承認で超過になる場合、更新後シフトに `over_cap_flag=1` が付く（重複しないスタッフ配置で必要人数1枠に承認追加した場合）。

```python
def test_reject_notifies_staff(shop_client, seed_pending_creq):
    crid = seed_pending_creq["creq_id"]; staff_id = seed_pending_creq["staff_id"]
    from src.db import query_one
    before = query_one("SELECT count(*) c FROM notifications WHERE staff_id=?", (staff_id,))["c"]
    r = shop_client.put(f'/api/shop/change-requests/{crid}', json={"action": "reject"})
    assert r.status_code == 200
    after = query_one("SELECT count(*) c FROM notifications WHERE staff_id=?", (staff_id,))["c"]
    assert after == before + 1
```

Run → FAIL（却下は通知しない）。

- [ ] **Step 2: 実装**

`shop_creq_resolve` の reject 分岐に通知を追加:

```python
    if body.get("action") == "reject":
        execute("UPDATE change_requests SET status='rejected', resolved_at=? WHERE id=?", (now, crid))
        notify(shop_id, cr["staff_id"], "info", "変更申請が却下されました", "ご申請は却下されました。詳細は店舗にご確認ください。")
```

change/add の承認 UPDATE/INSERT 完了後（`approved` に更新する直前）に、対象日の超過フラグを更新:

```python
        day = (cr.get("desired_start") or "")[:10]
        if day:
            _flag_over_cap_shifts(shop_id, day + "T00:00:00", day + "T23:59:59")
```

- [ ] **Step 3: テストを通す**

Run: `.venv/bin/pytest tests/test_creq_postconfirm.py -v` → PASS（3ケース）。

- [ ] **Step 4: 回帰 & Commit**

```bash
.venv/bin/pytest -q
git add src/app.py tests/test_creq_postconfirm.py
git commit -m "feat: 変更申請の却下通知＋承認時の超過フラグ連携"
```

### Task B2: フロント（変更申請バッジの明確化）

**Files:**
- Modify: `public/app.js`

**Interfaces:**
- Consumes: `GET /shop/change-requests`（既存モーダル `public/app.js:1460-1480`）。

- [ ] **Step 1: 保留件数バッジ**

店長ダッシュボードのクイックアクション（`public/app.js:1607`「変更申請を確認」ボタン付近）に、保留（pending）件数バッジを表示。ダッシュボード読込時に `GET /shop/change-requests` の pending 数を取得しボタンラベルへ反映。

- [ ] **Step 2: 手動確認**

スタッフで確定シフトへ変更申請→店長ダッシュボードにバッジ→モーダルで承認/却下→スタッフに通知が届くことを確認。

- [ ] **Step 3: Commit**

```bash
git add public/app.js
git commit -m "feat: 店長ダッシュボードに変更申請の保留件数バッジを表示"
```

---

## フェーズC: テーマ3（管理者画面：店舗・スタッフ管理＋監査ログ）

### Task C1: audit_logs テーブル＋`audit()`ヘルパー

**Files:**
- Modify: `schema.sql`（テーブル追加）、`src/app.py`（`ensure_db()` 冪等作成＋`audit()`ヘルパー、`notify` の近くに追加）
- Test: `tests/test_audit_log.py`

**Interfaces:**
- Produces: `audit(action, target_type=None, target_id=None, shop_id=None, detail=None)`。actor は `g.role` / `g.user` から解決（admin→name, shop→shop_name, staff→name）。失敗は握り潰す。

- [ ] **Step 1: schema.sql にテーブル追加**

`schema.sql` 末尾に追記:

```sql
CREATE TABLE IF NOT EXISTS audit_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_role  TEXT,
  actor_id    INTEGER,
  actor_name  TEXT,
  action      TEXT NOT NULL,
  target_type TEXT,
  target_id   INTEGER,
  shop_id     INTEGER,
  detail      TEXT,
  created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_shop ON audit_logs(shop_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action, created_at);
```

- [ ] **Step 2: ensure_db() で冪等作成（握り潰し）**

`ensure_db()` の末尾（`_normalize_datetime_data` の後）に:

```python
    try:
        execute("""CREATE TABLE IF NOT EXISTS audit_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_role TEXT, actor_id INTEGER,
          actor_name TEXT, action TEXT NOT NULL, target_type TEXT, target_id INTEGER,
          shop_id INTEGER, detail TEXT, created_at TEXT DEFAULT (datetime('now')))""")
        execute("CREATE INDEX IF NOT EXISTS idx_audit_shop ON audit_logs(shop_id, created_at)")
        execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action, created_at)")
    except Exception as e:
        print(f"[ensure_db] WARN: audit_logs setup failed (skipped): {e}", flush=True)
```

- [ ] **Step 3: `audit()` ヘルパー実装**

`notify` 関数の直後に追加:

```python
def audit(action, target_type=None, target_id=None, shop_id=None, detail=None):
    """監査ログを1件記録。失敗しても業務処理を止めない。"""
    try:
        role = getattr(g, "role", None)
        user = getattr(g, "user", None) or {}
        actor_id = user.get("id")
        if role == "shop":
            actor_name = user.get("shop_name")
        else:
            actor_name = user.get("name")
        insert_row("audit_logs", {
            "actor_role": role, "actor_id": actor_id, "actor_name": actor_name,
            "action": action, "target_type": target_type, "target_id": target_id,
            "shop_id": shop_id, "detail": detail})
    except Exception as e:
        print(f"[audit] WARN: failed to record {action}: {e}", flush=True)
```

- [ ] **Step 4: 失敗テスト→PASS**

```python
def test_audit_helper_records_row(app_ctx):
    from src.app import audit
    from src.db import query_one
    with app_ctx:  # g.role/g.user を admin でセットするフィクスチャ
        audit("test.action", target_type="shop", target_id=1, shop_id=1, detail="x")
    row = query_one("SELECT * FROM audit_logs WHERE action='test.action'")
    assert row and row["target_id"] == 1
```

Run: `.venv/bin/pytest tests/test_audit_log.py -v`

- [ ] **Step 5: Commit**

```bash
git add schema.sql src/app.py tests/test_audit_log.py
git commit -m "feat: audit_logsテーブルとauditヘルパーを追加"
```

### Task C2: 主要操作点に `audit()` を仕込む

**Files:**
- Modify: `src/app.py`（各エンドポイント）
- Test: `tests/test_audit_log.py`

**Interfaces:**
- Consumes: `audit(...)`。

- [ ] **Step 1: 失敗テスト（代表2点）**

ロール変更で `staff.role_change` が、finalize で `shift.finalize` が記録されること。

```python
def test_role_change_is_audited(admin_client, seed_staff):
    sid = seed_staff["shop_id"]; stid = seed_staff["staff_id"]
    admin_client.put(f'/api/admin/shops/{sid}/staffs/{stid}/role', json={"role": "employee"})
    from src.db import query_one
    assert query_one("SELECT id FROM audit_logs WHERE action='staff.role_change' AND target_id=?", (stid,))

def test_finalize_is_audited(shop_client, seed_over_cap_drafts):
    s, e = seed_over_cap_drafts["start_date"], seed_over_cap_drafts["end_date"]
    shop_client.post('/api/shop/shifts/finalize', json={"start_date": s, "end_date": e})
    from src.db import query_one
    assert query_one("SELECT id FROM audit_logs WHERE action='shift.finalize'")
```

Run → FAIL。

- [ ] **Step 2: 各所に `audit()` を追加**

- `shop_shifts_finalize`（成功 return 直前）: `audit("shift.finalize", target_type="shop", target_id=shop_id, shop_id=shop_id, detail=f"{start_d}〜{end_d} finalized={finalized_count} over_cap={over_cap}")`
- `shop_creq_resolve`（reject 分岐）: `audit("creq.reject", target_type="change_request", target_id=crid, shop_id=shop_id)`；承認分岐: `audit("creq.approve", target_type="change_request", target_id=crid, shop_id=shop_id, detail=cr["request_type"])`
- `admin_shop_staff_update_role`（成功 return 直前）: `audit("staff.role_change", target_type="staff", target_id=staff_id, shop_id=sid, detail=f"{old_role}->{new_role}")`
- スタッフ パスワードリセット（`/api/admin/shops/<sid>/staffs/<staff_id>/password`, 行1133）: `audit("staff.password_reset", target_type="staff", target_id=staff_id, shop_id=sid)`
- `admin_create_shop`（POST `/api/admin/shops`, 行647、作成後）: `audit("shop.create", target_type="shop", target_id=new_id, shop_id=new_id, detail=shop_name)`
- `admin_update_shop`（PUT, 行713）: `audit("shop.update", target_type="shop", target_id=sid, shop_id=sid, detail=f"is_active={1 if body.get('is_active') else 0}")`
- `admin_shop_staffs_post`（作成後, 行1154）: `audit("staff.create", target_type="staff", target_id=meta["last_row_id"], shop_id=sid, detail=body.get("name"))`

（`admin_create_shop` の関数名・新規ID取得方法は実装時に該当箇所を確認して合わせること。）

- [ ] **Step 3: テストを通す** → PASS。

- [ ] **Step 4: 回帰 & Commit**

```bash
.venv/bin/pytest -q
git add src/app.py tests/test_audit_log.py
git commit -m "feat: 主要操作点に監査ログを記録"
```

### Task C3: 監査ログ閲覧エンドポイント

**Files:**
- Modify: `src/app.py`
- Test: `tests/test_audit_log.py`

**Interfaces:**
- Produces: `GET /api/admin/audit-logs?shop=&action=&limit=`（`require_auth(["admin"])`、新しい順、既定 limit=100、上限500）。

- [ ] **Step 1: 失敗テスト**

```python
def test_admin_can_list_audit_logs(admin_client, seed_audit_rows):
    r = admin_client.get('/api/admin/audit-logs?limit=50')
    assert r.status_code == 200
    logs = r.get_json()["logs"]
    assert isinstance(logs, list) and len(logs) >= 1
    r2 = admin_client.get('/api/admin/audit-logs?action=shift.finalize')
    assert all(x["action"] == "shift.finalize" for x in r2.get_json()["logs"])
```

Run → FAIL。

- [ ] **Step 2: 実装**

```python
@app.get("/api/admin/audit-logs")
def admin_audit_logs():
    require_auth(["admin"])
    shop = request.args.get("shop")
    action = request.args.get("action")
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
    except (TypeError, ValueError):
        limit = 100
    where = []; params = []
    if shop:
        where.append("shop_id=?"); params.append(shop)
    if action:
        where.append("action=?"); params.append(action)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = query_all(f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT ?", tuple(params))
    return jsonify({"logs": rows})
```

- [ ] **Step 3: テストを通す** → PASS。

- [ ] **Step 4: Commit**

```bash
git add src/app.py tests/test_audit_log.py
git commit -m "feat: 監査ログ閲覧エンドポイントを追加"
```

### Task C4: 管理者スタッフ汎用編集エンドポイント

**Files:**
- Modify: `src/app.py`
- Test: `tests/test_admin_staff_edit.py`

**Interfaces:**
- Produces: `PUT /api/admin/shops/<int:sid>/staffs/<int:staff_id>`、body `{name, role, hourly_wage, min_hours_per_month, max_hours_per_month, is_resigned}`（部分更新可）。role の CHECK 準拠、student は max 80h 強制。`staff.update` を監査。

- [ ] **Step 1: 失敗テスト**

```python
def test_admin_edit_staff_fields(admin_client, seed_staff):
    sid = seed_staff["shop_id"]; stid = seed_staff["staff_id"]
    r = admin_client.put(f'/api/admin/shops/{sid}/staffs/{stid}',
                         json={"name": "改名太郎", "hourly_wage": 1200, "is_resigned": 1})
    assert r.status_code == 200
    from src.db import query_one
    row = query_one("SELECT name, hourly_wage, is_resigned FROM staffs WHERE id=?", (stid,))
    assert row["name"] == "改名太郎" and row["hourly_wage"] == 1200 and row["is_resigned"] == 1
```

Run → FAIL。

- [ ] **Step 2: 実装**

```python
@app.put("/api/admin/shops/<int:sid>/staffs/<int:staff_id>")
def admin_shop_staff_update(sid, staff_id):
    require_auth(["admin"])
    body = request.get_json(silent=True) or {}
    staff = query_one("SELECT * FROM staffs WHERE id=? AND shop_id=?", (staff_id, sid))
    if not staff:
        abort(404, description="スタッフが見つかりません")
    fields = {}
    if "name" in body and body.get("name"):
        fields["name"] = body["name"]
    if "role" in body:
        if body["role"] not in ("manager", "employee", "part_time", "student"):
            abort(400, description="role が不正です")
        fields["role"] = body["role"]
    if "hourly_wage" in body:
        fields["hourly_wage"] = int(body["hourly_wage"] or 0)
    if "min_hours_per_month" in body:
        fields["min_hours_per_month"] = int(body["min_hours_per_month"] or 0)
    if "max_hours_per_month" in body:
        fields["max_hours_per_month"] = int(body["max_hours_per_month"] or 0)
    if "is_resigned" in body:
        fields["is_resigned"] = 1 if body["is_resigned"] else 0
    # student は 80h 上限を強制
    if fields.get("role") == "student" and fields.get("max_hours_per_month", 0) > 80:
        fields["max_hours_per_month"] = 80
    if not fields:
        return jsonify({"ok": True, "updated": 0})
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE staffs SET {sets} WHERE id=? AND shop_id=?",
            tuple(fields.values()) + (staff_id, sid))
    audit("staff.update", target_type="staff", target_id=staff_id, shop_id=sid,
          detail=",".join(fields.keys()))
    return jsonify({"ok": True, "updated": len(fields)})
```

- [ ] **Step 3: テストを通す** → PASS。role 不正・student上限のテストも追加。

- [ ] **Step 4: 回帰 & Commit**

```bash
.venv/bin/pytest -q
git add src/app.py tests/test_admin_staff_edit.py
git commit -m "feat: 管理者によるスタッフ汎用編集エンドポイントを追加"
```

### Task C5: フロント（管理者スタッフ管理UI・監査ログビューア）

**Files:**
- Modify: `public/app.js`

**Interfaces:**
- Consumes: `GET /admin/shops/staffs/<sid>`, `PUT /admin/shops/<sid>/staffs/<staff_id>`, `PUT /admin/shops/<sid>`（is_active）, `GET /admin/audit-logs`。

- [ ] **Step 1: スタッフ管理UI強化**

管理者の店舗詳細（`public/app.js:3665` 付近で `admin/shops/staffs` を読む箇所）に、スタッフ一覧の**検索ボックス（氏名・ロールのフロント側フィルタ）**と、行ごとの「編集」ボタン（氏名・ロール・時給・上限下限・退職トグルをモーダルで編集し `PUT` 送信）を追加。

- [ ] **Step 2: 店舗 有効/停止トグル**

管理者の店舗一覧/詳細に is_active トグルを追加し `PUT /admin/shops/<sid>` を送信（`shop_name` は現値を渡す）。

- [ ] **Step 3: 監査ログビューア**

管理者画面に「監査ログ」タブ/セクションを追加。`GET /admin/audit-logs?shop=&action=&limit=` を叩き、日時・操作者・アクション・対象・詳細をテーブル表示。店舗・アクションのフィルタUI付き。

- [ ] **Step 4: 手動確認**

管理者ログイン→スタッフ編集・退職トグル・店舗停止→監査ログに反映されることを確認。

- [ ] **Step 5: Commit**

```bash
git add public/app.js
git commit -m "feat: 管理者画面にスタッフ管理強化・店舗停止・監査ログビューアを追加"
```

---

## フェーズD: 品質評価（サブエージェントによる多層テスト）

このフェーズは `superpowers:requesting-code-review` と並行し、サブエージェントを配置して以下を実施・報告する。実装完了後にオーケストレーターが dispatch する。

- **UT（単体）**: 上記各タスクの pytest を実行し `.venv/bin/pytest -q` 全PASSを確認。カバレッジ（`.coverage`）で新規関数（`_flag_over_cap_shifts`, `audit`, 新エンドポイント）がカバーされているか確認。
- **IT（結合）**: `tests/test_integration_flow.py` 系に沿って「希望提出→AI生成→ドラフト調整→確定（超過フラグ）→変更申請→承認→監査ログ」の一連フローを1テストで通す新規結合テストを追加。
- **ST（システム）**: Playwright（`e2e/`, `playwright.config.js`）で店長・スタッフ・管理者の3ロールについて主要動線をブラウザ実行。超過バッジ表示・メモ編集・変更申請承認・監査ログ表示のE2Eを追加。
- **受け入れ（Acceptance）**: 設計ドキュメントの各要件（テーマ1〜3）をチェックリスト化し、実挙動と突き合わせて合否判定。未達があれば起票。
- **品質評価レポート**: UT/IT/ST/受け入れの結果、カバレッジ、既知の残課題・リスクをまとめて報告。

各サブエージェントは読み取り＋テスト実行に限定し、コード修正が必要な指摘は本体（オーケストレーター）に戻して対応する。

---

## Self-Review メモ

- **Spec coverage**: テーマ1（A1-A5）、テーマ2（B1-B2）、テーマ3（C1-C5）、テスト方針（フェーズD）を網羅。設計の「スコープ外」（直接編集・カウンター提案・スタッフ公開）は実装しない。
- **Placeholder**: `_iter_slots`/要件マップのキー形式のみ「実装時に既存に厳密一致」と明記（既存 `_count_over_cap_slots` に完全準拠が要件）。それ以外は具体コード提示済み。
- **Type consistency**: `_flag_over_cap_shifts(shop_id, start_iso, end_iso)->int`、`audit(action, target_type, target_id, shop_id, detail)`、finalize レスポンス `over_cap` を全タスクで一貫使用。
