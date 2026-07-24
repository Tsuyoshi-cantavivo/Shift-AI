# 設計: 超過確定の可視化・確定後変更・管理者画面拡充

作成日: 2026-07-25

## 背景と目的

シフトSaaS（Flask + 単一ページJS）で、以下3つの課題を解決する。

1. **必要人数を超えた確定**: AI自動生成後に調整せず「確定」を押すと、必要人数を超過した枠でもそのまま確定してしまう。超過を検知して可視化したい。
2. **確定後の変更対応**: 確定シフトはロックされ店長が動かせない。確定後に変更要望が来ても反映する手段が使いづらい。既存の変更申請フローを実用化したい。
3. **システム管理者画面**: 機能が最小限。店舗・スタッフ管理の充実と、操作の監査ログを追加したい。

各テーマは独立性が高いため、実装は3フェーズに分割する（本ドキュメントは3テーマを1つの設計として扱い、実装計画でフェーズA/B/Cに分ける）。

## 現状の把握

- 確定処理 [`shop_shifts_finalize`](../../../src/app.py) (`/api/shop/shifts/finalize`) は、期間内の `requested` シフト（AIドラフト＋スタッフ希望）を必要人数チェックなしで全て `confirmed` に変換する。
- `shifts` テーブルに超過フラグ・メモ用の列は無い（`schema.sql`）。ステータスは `requested`/`confirmed`/`modifying`。
- 変更申請フロー `change_requests` は既に存在し、店長承認 [`shop_creq_resolve`](../../../src/app.py) (`/api/shop/change-requests/<id>`) で確定シフトを直接書き換え・削除・追加できる。ただし却下時にスタッフ通知が無く、承認時のcap超過チェックも無い。
- 超過判定ロジックは `_check_slot_cap` / `_count_over_cap_slots` として既存（[`src/app.py`](../../../src/app.py)）。
- 管理者エンドポイントは店舗/スタッフ/ロール/パスワード管理とサマリ閲覧のみ。監査ログのテーブル・仕組みは無い。
- マイグレーションは `ensure_db()` 内の冪等 `ALTER TABLE ... ADD COLUMN`（`PRAGMA table_info` でガード）で行う既存パターンがある。

## 決定した方針（ブレスト結果）

- 超過時の確定は **許可し、超過枠に自動フラグ＋店長の自由メモを付ける**（ブロックしない）。メモは店長側のみ表示。
- 確定後の変更は **確定シフトをロックのまま、変更申請の承認/却下（2択）でのみ反映**。店長発の直接編集は行わない。
- 管理者画面は **店舗・スタッフ管理の充実** と **監査ログ** を優先。

---

## テーマ1: 超過確定の可視化

### データモデル

`shifts` に冪等 `ALTER TABLE ADD COLUMN` で追加（`ensure_db()` に追記）:

- `over_cap_flag INTEGER DEFAULT 0` — 確定時にその枠が必要人数超過だった場合 1。
- `note TEXT` — 店長の自由メモ。

`schema.sql` の `shifts` 定義にも同列を追記（新規DB初期化用）。

### 確定処理の変更（`shop_shifts_finalize`）

1. 従来どおり期間内の `requested` を全て `confirmed` に変換する（**ブロックしない**）。
2. 変換後、期間全体の確定シフトのカバレッジを再計算する。`_count_over_cap_slots` と同じ要件計算（`shift_patterns` + 曜日別オーバーライド + granularity）で、**必要人数を超える時間スロットに重なる確定シフトへ `over_cap_flag=1`** を立てる。
   - 超過スロットに1つでも重なるシフトを対象とする（過剰フラグは許容 = 警告目的）。
   - 該当シフトの `reason` に「必要◯名/配置△名の時間帯を含む」を自動付記する（メモ `note` は上書きしない）。
3. レスポンスに `over_cap` 件数を追加し、確定直後に「◯件を確定しました。うち△件が必要人数超過です」とサマリ表示する。

新規ヘルパー `_flag_over_cap_shifts(shop_id, start_iso, end_iso)`:
- 期間内 `confirmed` を取得しスロット別カバレッジを構築。
- 各スロットで `coverage > required (>0)` の超過スロット集合を求める。
- 超過スロットに重なる各シフトに `over_cap_flag=1` と自動理由付記を UPDATE。
- 超過に重ならないシフトは `over_cap_flag=0`（再確定・再計算で解消され得るため明示リセット）。
- 戻り値: フラグを立てたシフト件数。

### 店長メモ

- 新規 `PATCH /api/shop/shifts/<id>/note`。body: `{note}`。`confirmed` シフトの `note` を設定/クリア。店舗コンテキスト（`_shop_ctx`）で認可。
- フロント（`public/app.js`）:
  - タイムライン/一覧のシフトカードで `over_cap_flag=1` のとき⚠️バッジ＋自動理由を表示。
  - `note` があればカードにメモ本文を表示。メモアイコンから編集（テキスト入力→PATCH）。
  - 印刷・CSVエクスポート（`/api/shop/shifts/export`）にもフラグ／メモ列を反映。
- スタッフ側画面には超過フラグ・メモを一切出さない。

---

## テーマ2: 確定後の変更対応（変更申請ベース）

確定シフトはロック維持。既存 `change_requests` を実用化する。

### バックエンド

- スタッフは確定後も「時間変更（change）／取消（cancel）／追加（add）」の変更申請を出せる（既存 `staff_creq_post` を活用、変更なし）。
- 店長は申請一覧に対し **承認／却下の2択**（既存 `shop_creq_resolve`）。微調整（カウンター提案）は行わない。
- 調整1: 承認で必要人数超過が発生する場合、**ブロックせずテーマ1の `over_cap_flag` を立てる**（`_flag_over_cap_shifts` を該当日に対して呼ぶ）。スタッフ同士の時間重複は従来どおりハードエラー（承認不可）。
- 調整2: **却下時にもスタッフへ通知**する（現状は承認時のみ `notify`）。

### フロント

- 店長ダッシュボード／通知に「変更申請 ◯件（pending）」バッジを表示し、申請インボックスを目立たせる。
- 各申請カードに元シフト・希望内容（種別／希望時間）・理由を表示し、承認/却下ボタンを置く。
- 承認/却下後に一覧とバッジを更新する。

---

## テーマ3: システム管理者画面の拡充

### 店舗・スタッフ管理の充実（UI整備が主、不足エンドポイントのみ追加）

- 店舗: 作成／名称・コード編集（既存 `PUT /api/admin/shops/<sid>`）／**有効・停止トグル**（`shops.is_active` を更新）／店舗別サマリ（既存 `summary`）。
- スタッフ: 店舗内の一覧・**検索/フィルタ**、編集（氏名・ロール・時給・上限下限時間・**退職フラグ `is_resigned`**）、パスワードリセット（既存）。
- 追加/変更するエンドポイント（実装時に既存を確認し、無い分を追加）:
  - スタッフ一覧の検索/フィルタは **フロント側フィルタ**で対応（既存 `GET /api/admin/shops/staffs/<sid>` の結果を氏名・ロールで絞り込み）。バックエンドは変更しない。
  - `PUT /api/admin/shops/<sid>` で `is_active`（有効/停止）を更新できるようにする。
  - スタッフ編集（`name`・`role`・`hourly_wage`・`min_hours_per_month`・`max_hours_per_month`・`is_resigned`）用に `PUT /api/admin/shops/<sid>/staffs/<staff_id>` を追加する（既存の role 専用エンドポイントは残し、汎用編集を追加）。

### 監査ログ（新規）

新テーブル（`schema.sql` + `ensure_db()` の冪等作成）:

```sql
CREATE TABLE IF NOT EXISTS audit_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_role  TEXT,           -- 'admin' / 'shop' / 'staff'
  actor_id    INTEGER,
  actor_name  TEXT,
  action      TEXT NOT NULL,  -- 例: 'shift.finalize', 'creq.approve', 'staff.role_change'
  target_type TEXT,           -- 'shift' / 'staff' / 'shop' / 'change_request'
  target_id   INTEGER,
  shop_id     INTEGER,
  detail      TEXT,           -- 補足（JSON文字列可）
  created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_shop ON audit_logs(shop_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action, created_at);
```

ヘルパー `audit(action, target_type=None, target_id=None, shop_id=None, detail=None)`:
- 現在の認証コンテキスト（`g.user` / role）から actor を解決して1行 INSERT。
- 失敗は握り潰す（監査記録の失敗が業務処理を止めないよう try/except でログ出力のみ）。

仕込む操作点（高価値のみ）:
- シフト確定（`shop_shifts_finalize`）: `shift.finalize`（件数・期間・超過件数を detail に）。
- 変更申請の承認/却下（`shop_creq_resolve`）: `creq.approve` / `creq.reject`。
- ロール変更（`/api/admin/shops/<sid>/staffs/<staff_id>/role`）: `staff.role_change`。
- パスワードリセット（`/api/admin/shops/<sid>/staffs/<staff_id>/password`）: `staff.password_reset`。
- 店舗の作成/更新/停止: `shop.create` / `shop.update` / `shop.deactivate`。
- スタッフ作成（`/api/admin/shops/<sid>/staffs`）: `staff.create`。

閲覧:
- 新規 `GET /api/admin/audit-logs?shop=&action=&limit=`（`require_auth(["admin"])`、既定 limit=100、新しい順）。
- 管理者画面に、店舗・アクションでフィルタできる一覧UIを追加。

---

## テスト方針

既存の pytest 群（`tests/`）に沿ってテストを追加する。

- テーマ1: `finalize` が超過時に確定を通し、超過シフトに `over_cap_flag=1` と自動理由を付けること。超過なしなら 0 のままであること。メモ PATCH の設定/クリア。エクスポートにメモ列が出ること。
- テーマ2: 確定シフトに対する変更申請の承認で確定シフトが更新され `over_cap` 発生時にフラグが立つこと。重複時は承認不可。却下でスタッフ通知が飛ぶこと。
- テーマ3: 監査ログが各操作点で記録されること。`audit-logs` のフィルタ。スタッフ退職トグル・店舗停止トグルの反映。

回帰: 既存の `test_ai_draft_finalize.py`, `test_workflow_regression.py`, `test_admin_*` を壊さないこと。

## スコープ外 / 非目標

- 店長による確定シフトの直接ドラッグ編集（明示的に不採用）。
- 承認時の時間微調整（カウンター提案）。
- 超過フラグ・メモのスタッフ公開。
- 課金/プラン管理、外部通知（メール/LINE等）。

## 実装時の決定（設計からの調整）

- **超過情報は `over_cap_flag` のみで表現し、`reason` は書き換えない。** 当初案では超過理由を `reason` に自動付記する予定だったが、`/api/staff/shifts` が `reason` を返すためスタッフへ超過情報が漏れる。スタッフ非公開の要件を優先し、店長 UI は `over_cap_flag` から⚠️バッジ・警告バナーを導出する。
- **確定シフトの直接編集は UI でロック。** 確定シフトのバーをタップすると、時間・取消は編集できずメモのみ編集可能なモーダルを表示し、時間変更・取消は「変更申請の承認」で反映する旨を明示する（下書き・調整待ちは従来どおり直接編集可）。

## リスクと留意点

- 超過フラグ付与は「超過スロットに重なる全シフト」を対象とするため過剰にフラグが付き得る（警告目的として許容）。
- 監査ログの仕込みは複数エンドポイントに分散するため、`audit()` を薄く保ち失敗を握り潰す。
- マイグレーションは本番（Railway等）で `ensure_db()` 起動時に走る。既存の冪等パターンを厳守し、失敗しても業務列以外は握り潰す（監査テーブル作成失敗など）。
