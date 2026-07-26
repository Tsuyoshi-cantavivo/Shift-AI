# システム管理者コンソール 設計書

- 日付: 2026-07-26
- 対象: システム管理者（`sessions.role='admin'`）向け機能の全面拡充
- 前提の調査結果: 本文書の「1. 現状」に記載（2026-07-26 時点のコードを実地調査）

---

## 1. 現状

### 1.1 できること

システム管理者のナビゲーションは3項目（ホーム / 店舗 / 監査ログ、`public/app.js:482-486`）。
ホーム画面（`public/app.js:4439`）はボタン2つだけで、実質的なダッシュボードが存在しない。

API は `/api/admin/*` が19本（`src/app.py:723`〜`1421`）。店舗の作成・有効/無効切替、
全店舗のスタッフ管理（追加・編集・ロール変更・パスワードリセット）、店舗別の期間集計、
監査ログ閲覧、DBスキーマ診断とマイグレーション実行ができる。

### 1.2 セキュリティ上の欠陥（実地確認済み）

| ID | 内容 | 箇所 |
|---|---|---|
| S1 | 固定シフトの POST/PUT/DELETE が**完全に未認証**。`_shop_ctx()` の呼び忘れ。`fixed_shifts` に `shop_id` 列が無く `staff_id` だけで特定するため、誰でも任意店舗の固定シフトを作成・改変・削除できる | `src/app.py:2365-2383` |
| S1' | `POST/PUT /api/shop/shifts` が `staff_id` の所属店舗を検証していない。自店舗の `shop_id` を持ちながら他店舗スタッフを指す `shifts` 行を作れる | `src/app.py:2814, 2943` |
| S2 | システム管理者が**自分のパスワードを変更する手段が存在しない**。`system_admins` への UPDATE がコード全体でゼロ。`/api/init` が作る `admin123` のまま運用され続ける | — |
| S3 | `/api/admin/notifications` と `/api/admin/notifications/read-all` が `require_auth` 呼び忘れで未認証 | `src/app.py:1626, 1632` |
| S4 | `POST /api/init` が認証不要で公開されている。`system_admins` が空なら誰でも初期管理者を作れる | `src/app.py:594` |
| S5 | ログイン試行のレート制限・ロックアウトが無い。管理者ログインは「コード欄に `admin`」という推測容易なマジックワード | `src/app.py:638` |
| C | `require_auth` の後方互換フォールバックが `staffs.id` を `shops.id` とみなす。セッションの `shop_id` が指す店舗が削除済みの場合、**別テナントに着地し得る** | `src/app.py:111-112` |

### 1.3 データ破壊バグ

| ID | 内容 | 箇所 |
|---|---|---|
| B1 | 店舗一覧の有効/無効トグルが `shop_name: ''` を送信し、サーバが空値ガード無しで UPDATE するため**店舗名が消える**。管理者側に店舗名を編集し直す UI が無いため復旧が困難 | `public/app.js:4523` / `src/app.py:803` |

### 1.4 機能の欠落

- 全社ダッシュボードが無い
- 店舗の削除・アーカイブ・データエクスポートが無い（`DELETE /api/admin/shops/<id>` が存在しない）
- 管理者アカウントの追加・削除・一覧が無い（`system_admins` は `/api/init` の1回きりしか INSERT されない）
- 代理ログインが無く、管理者は `/api/shop/*` が全て403のため**顧客のシフト実データを一切見られない**
- 全店舗一斉通知が無い（管理者向け通知は空スタブ）
- 監査ログが弱い（CSV出力・期間フィルタ・ページング無し。記録箇所も9種のみで、ログインもシフトCRUDも未記録）
- マイグレーションの適用状態を記録するテーブルが無い。`migrations/0004_fix_student_role_check.sql:3-27` に
  「`0003` の前半が本番D1で失敗し後半だけ適用された」事故が記録されている

### 1.5 スコープ外（本設計では扱わない）

- **課金・プラン・契約**: `stripe|billing|plan|subscription|tenant` はコードベース全体で0ヒット。
  マネタイズ層は未着手だが、本設計の範囲外とする。
- **定期実行ジョブ**: バックグラウンドジョブ基盤が無いため、`notifications.type='deadline'` が
  定義済み（`schema.sql:127`）にもかかわらず締切リマインドは永久に発火しない。本設計では基盤を導入せず、
  ダッシュボードの「要対応」リストで管理者が能動的に気づける形に留める。
- **ICS購読トークンの失効対応**（`src/app.py:3926-3939`）: 別途 `calendar_subscriptions` の設計が
  `docs/superpowers/plans/2026-07-22-trustworthy-shift-publication.md:190` にあるため、そちらに委ねる。
- **パスワード変更時のセッション失効**（`src/app.py:1666, 3972`）: shop/staff 側の挙動であり、
  本設計では管理者パスワード変更時のみ対応する。

---

## 2. 決定事項

| 論点 | 決定 | 理由 |
|---|---|---|
| スコープの順序 | セキュリティ穴を塞ぐ（Phase 1）→ 運営機能（Phase 2） | 穴を残したまま管理機能を足すと攻撃面が広がるだけになる |
| 代理ログインの権限 | **閲覧のみ**（GET のみ許可） | 運営者が顧客の確定シフトを壊す事故を構造的に防ぐ。後から書き込みを開けることはできるが、逆は難しい |
| 代理ログインの実装 | admin セッションに `acting_shop_id` を持たせ、`require_auth` の一箇所だけで店舗権限に化けさせる | `require_auth` が全認可の唯一の関門。トークン差し替え方式は localStorage の二重管理が必要で、戻り忘れ事故が起きる |
| 店舗削除 | アーカイブ → 段階的な完全削除 | 「データを消してほしい」という顧客要望に応えつつ、誤クリックでの消失を防ぐ |
| マイグレーション | `schema_migrations` テーブルで適用状態を追跡し、管理画面から明示適用 | 過去に部分適用事故が起きており、ファイル単位の記録では同じ状態を検出できない |
| コード配置 | `src/admin_api.py` / `public/admin.js` に切り出し。Blueprint は導入しない | 既存135ルートを巻き込む大規模リファクタを避けつつ、肥大化した2ファイルから今回の追加分を隔離する |

---

## 3. アーキテクチャ

### 3.1 ファイル構成

| ファイル | 状態 | 役割 |
|---|---|---|
| `src/admin_api.py` | 新規 | `/api/admin/*` を全て収容。`register_admin_routes(app)` を `src/app.py` から1行で呼ぶ。既存19本も移設 |
| `src/migrator.py` | 新規 | `migrations/*.sql` のステートメント分割・冪等適用・適用状態の照会 |
| `public/admin.js` | 新規 | `SCREENS.admin*` を全て収容。`esc()`/`api()`/`openModal()`/`toast()`/`navToken()` は `app.js` のグローバルをそのまま利用 |
| `migrations/0005_admin_console.sql` | 新規 | 本設計のスキーマ変更 |
| `src/app.py` | 修正 | 管理者コード（約440行）を削除し `register_admin_routes(app)` を呼ぶ。`require_auth` に代理閲覧分岐を追加。Phase 1 の各修正 |
| `public/app.js` | 修正 | 管理画面コード（約490行）を削除。`NAV_DEFS.admin` を4項目に。代理閲覧バナーを追加 |
| `public/index.html` | 修正 | `admin.js` の `<script>` タグを追加 |
| `schema.sql` | 修正 | 新規環境向けに `schema_migrations` / `sessions.acting_shop_id` / `shops.is_archived` / `shops.archived_at` / `login_attempts` を反映 |

`src/admin_api.py` は Blueprint を使わず、以下の形で既存の書き味を保つ:

```python
def register_admin_routes(app):
    @app.get("/api/admin/shops")
    def admin_shops():
        require_auth(["admin"])
        ...
```

循環 import を避けるため、`require_auth` / `audit` / `notify` などの共有ヘルパは
`src/admin_api.py` が `src/app.py` から import するのではなく、`register_admin_routes(app, deps)` の
形で辞書として受け取る。`deps` には `require_auth`, `audit`, `notify`, `_create_session` を渡す。

### 3.2 静的アセット配信への影響

`_index_html_with_asset_version()`（`src/app.py:4011`）は `app.js` と `style.css` にのみ
mtime ベースの `?v=` を付けている。`admin.js` も同様に付与し、`static_files()` の
短期キャッシュ判定（`src/app.py:4046`）にも `admin.js` を加える。付け忘れると
「新しい `app.js` と古い `admin.js`」の組み合わせが発生する。

### 3.3 テストへの影響

`tests/test_design_tokens.py:16` は `public/app.js` を文字列として読み、廃止済みデザイン
トークン（`--navy` 等）の再導入を禁止している。管理画面コードが `admin.js` に移ると
この検査から外れてしまうため、走査対象に `public/admin.js` を追加する。

---

## 4. スキーマ変更

`migrations/0005_admin_console.sql`:

```sql
-- 1. マイグレーション適用状態の追跡（ステートメント単位）
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT NOT NULL,
  stmt_index INTEGER NOT NULL,
  applied_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (filename, stmt_index)
);

-- 2. 代理閲覧（admin セッションが一時的に見ている店舗）
ALTER TABLE sessions ADD COLUMN acting_shop_id INTEGER;

-- 3. 店舗アーカイブ
ALTER TABLE shops ADD COLUMN is_archived INTEGER DEFAULT 0;
ALTER TABLE shops ADD COLUMN archived_at TEXT;

-- 4. ログイン試行のレート制限
CREATE TABLE IF NOT EXISTS login_attempts (
  attempt_key  TEXT PRIMARY KEY,
  fail_count   INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  updated_at   TEXT
);
```

**ステートメント単位で記録する理由**: `0003` の事故は「前半（`staffs` 再構築）が失敗し、
後半（`shop_holidays` 作成）だけが通った」形だった。ファイル単位の記録ではこの状態を
「未適用」とも「適用済み」とも正しく表現できない。

**列を追加しないもの**:

- `fixed_shifts.shop_id` は追加しない。`staffs` 経由の JOIN で所属を検証できるため
  (`SELECT fs.id FROM fixed_shifts fs JOIN staffs s ON fs.staff_id=s.id WHERE fs.id=? AND s.shop_id=?`)、
  本番D1でのテーブル再構築という最も危険な操作を避けられる。
- `notifications.type` と `audit_logs.actor_role` には CHECK 制約が無い（`schema.sql:132, 222`）ため、
  一斉通知の `type='announcement'` と代理閲覧の監査記録は列追加なしで載る。

---

## 5. Phase 1 — 緊急修正

### 5.1 S1: 固定シフトの認証追加

`shop_fixed_post` / `shop_fixed_put` / `shop_fixed_del`（`src/app.py:2365-2383`）の先頭に
`_shop_ctx()` を追加する。さらに所属検証を加える:

- POST: `staff_id` が自店舗のスタッフであること
- PUT / DELETE: `fid` の固定シフトが自店舗スタッフのものであること

他店舗のリソースを指した場合は 404 を返す（403 ではなく 404 とするのは、既存の IDOR
マスク方針に合わせるため。`src/app.py:2239` と同じ）。

### 5.2 S1': シフト作成/更新の staff_id 検証

`POST/PUT /api/shop/shifts`（`src/app.py:2814, 2943`）に、`/api/shop/wishes/bulk`
（`src/app.py:3712`）と同じ所属検証を追加する。退職者（`is_resigned=1`）の扱いも
`wishes/bulk` に揃える。

### 5.3 S2: 管理者パスワード変更

`PUT /api/admin/password` を新設。

- 現在のパスワード必須
- 新パスワードは既存の強度ルールに準拠（フロントの `pw-rules` 表示と同じ基準）
- 成功時、**自分の現在のセッションを除く** その管理者の全セッションを削除

### 5.4 S3: 管理者通知の認証追加

`/api/admin/notifications` と `/api/admin/notifications/read-all`（`src/app.py:1626, 1632`）に
`require_auth(["admin"])` を追加。実装は Phase 2 の一斉通知の配信履歴を返す形に作り直す。

### 5.5 S4: /api/init の既定無効化

`POST /api/init` は「`system_admins` が空である」**かつ**「環境変数 `ALLOW_INIT=1`」の
両方を満たすときのみ管理者を作成する。既定は無効。

初期パスワードの `admin123` 固定をやめ、`secrets.token_urlsafe(12)` で生成した値を
レスポンスで1回だけ返す。`.env.example` に `ALLOW_INIT` の説明を追記する。

### 5.6 S5: ログイン試行のレート制限

`login_attempts` テーブルで管理する。

- キー: `<remote_addr>|<shop_code>|<user_code>`（管理者ログインは `user_code='admin'` に正規化）
- 15分間に10回失敗で15分ロック。ロック中は 429 を返す
- ログイン成功でそのキーの行を削除
- **バックグラウンドジョブが無いため**、ログイン処理のついでに `locked_until` が
  過ぎた古い行を削除する（1リクエストあたりの削除は上限を設けない。行数が
  ログイン頻度に比例する程度で、肥大化しないため）

**テーブルの作成方法**: `login_attempts` は Phase 1 で必要になるが、`src/migrator.py` は
Phase 2 で導入される。Phase 1 では `ensure_db()` に `CREATE TABLE IF NOT EXISTS` を
追加する（`audit_logs` が既に同じ形で作られている。`src/app.py:4097-4106`）。
`migrations/0005_admin_console.sql` にも同じ DDL を含めるが、`IF NOT EXISTS` のため
二重適用にならない。

### 5.7 B1: 店舗更新の部分更新化

`admin_update_shop`（`src/app.py:798`）を、リクエストに含まれるキーだけ UPDATE する
形に変更する。`shop_name` は空文字・空白のみを拒否（400）。`shop_code` の変更も
サポートし、他店舗との重複を拒否する。

フロントのトグル（`public/app.js:4523`）は `{is_active: ...}` だけを送るよう修正する。

### 5.8 C: require_auth フォールバックの削除

`src/app.py:111-112` の後方互換フォールバックを削除する。旧店主ログインは
`_create_session("shop", shop["id"], shop["id"], ...)`（`src/app.py:671`）で
`shop_id` を正しく入れているため、このフォールバックは既に不要。

### 5.9 監査ログの記録追加

以下を `audit()` の呼び出し対象に加える:

| action | 契機 |
|---|---|
| `auth.login` / `auth.login_failed` / `auth.logout` | ログイン成功・失敗・ログアウト |
| `admin.impersonate_start` / `admin.impersonate_end` | 代理閲覧の開始・終了 |
| `shop.archive` / `shop.unarchive` / `shop.delete` / `shop.export` | 店舗ライフサイクル操作 |
| `admin.migrate` | マイグレーション適用 |
| `admin.announce` | 一斉通知の配信 |
| `admin.password_change` / `admin.create` / `admin.delete` | 管理者アカウント操作 |

`audit()` は `g.role` / `g.user` から actor を解決する（`src/app.py:128-145`）が、
ログイン失敗時は認証コンテキストが無い。この場合 `actor_role='anonymous'`,
`actor_name` に入力されたコードを記録する（パスワードは記録しない）。

`AUDIT_ACTION_LABELS`（`public/app.js:4664`）に新しい action の日本語ラベルを追加する。

---

## 6. Phase 2 — 運営機能

### 6.1 ナビゲーション

`NAV_DEFS.admin`（`public/app.js:482`）を4項目にする:
ホーム / 店舗 / 監査ログ / システム。

### 6.2 全社ダッシュボード（`SCREENS.adminHome`）

`GET /api/admin/dashboard` が返すもの:

- KPI: 総店舗数（稼働 / 停止 / アーカイブの内訳）、総スタッフ数、今月の確定シフト件数、要対応件数
- 要対応リスト（4種）:
  1. 募集期間の締切を過ぎているのにシフトが未確定の店舗
  2. `manager` ロールのスタッフが1人もいない店舗（＝誰もログインできない）
  3. 30日以上どのユーザーもログインしていない店舗
  4. 未適用のマイグレーションがある
- 直近の監査ログ10件

アーカイブ済み店舗は KPI の内訳にのみ現れ、要対応リストには含めない。

### 6.3 店舗一覧（`SCREENS.adminShops`）

- アーカイブ済みの表示/非表示トグル（既定は非表示）
- 店舗名・店舗コードでの絞り込み
- 行に最終ログイン日を追加

### 6.4 店舗詳細（`SCREENS.adminShopDetail`）

タブ4枚に再構成する。タブの実装は `SCREENS.settings`（`public/app.js:3679-3693`）の
既存パターンを踏襲する。

| タブ | 内容 |
|---|---|
| 概要 | 既存の期間集計＋manager一覧＋最終ログイン。「この店舗を代理閲覧」ボタン |
| スタッフ | 既存機能をそのまま移設 |
| 設定 | 店舗名・店舗コードの編集、`settings` の編集（`src/utils.py:382-386` が扱う10キー） |
| 危険な操作 | アーカイブ / 復元 / エクスポート / 完全削除 |

### 6.5 代理閲覧（閲覧のみ）

**API**

- `POST /api/admin/impersonate/<shop_id>` — 自分のセッション行に `acting_shop_id` を立てる
- `DELETE /api/admin/impersonate` — 解除

**`require_auth` の拡張**（`src/app.py:84`）

ロール判定（`if role not in allowed`）の**手前**に分岐を1つ追加する:

```python
acting = session.get("acting_shop_id")
if role == "admin" and acting and "shop" in allowed and "admin" not in allowed:
    if request.method != "GET":
        abort(403, description="代理閲覧中はデータを変更できません")
    shop = query_one("SELECT * FROM shops WHERE id=?", (acting,))
    if shop is None:
        abort(409, description="代理閲覧中の店舗が見つかりません")
    g.role = "shop"
    g.user = strip_password(shop)
    g.shop_id = acting
    g.impersonating = True
    return "shop", g.user, acting
```

`"admin" not in allowed` を条件に含めているため:

- `/api/admin/*`（`require_auth(["admin"])`）は代理中も管理者として動く
  → 「管理者に戻る」が確実に押せる
- `require_auth(["staff"])` は対象外 → スタッフには化けない
- `require_auth(["admin","shop","staff"])` である `/api/me` も管理者のまま動く
  → `impersonating` 情報を返せる

**`/api/me` の拡張**

`acting_shop_id` が立っている場合、`impersonating: {shop_id, shop_name}` を返す。

**フロント**

- ヘッダ直下に固定の警告バー「◯◯店を代理閲覧中（閲覧のみ）／ 管理者に戻る」
- 代理中はナビを `NAV_DEFS.shop` に切り替える
- 「管理者に戻る」は `DELETE /api/admin/impersonate` を呼び、`adminShopDetail` に戻る

**監査**: 開始・終了とも記録する。代理中の GET は記録しない（量が多く、閲覧のみで
副作用が無いため）。

### 6.6 監査ログ強化（`SCREENS.adminAudit`）

- フィルタ: 期間（開始/終了）、操作者名の部分一致、店舗、アクション
- ページング: `before_id` カーソル方式の「もっと見る」（`limit` の上限500は維持）
- CSV ダウンロード: `=` `+` `-` `@` で始まるセルは先頭に `'` を付ける
  （Formula Injection 対策。`tests/test_security.py` に既存テストがあるので方針を揃える）

### 6.7 システム画面（`SCREENS.adminSystem`、新規）

3ブロック構成。

**管理者アカウント**
- 一覧 / 追加 / 削除 / 自分のパスワード変更
- 最後の1人は削除できない（400）
- 自分自身も削除できない（400）

**マイグレーション**
- 適用済み / 未適用の一覧
- 「未適用を適用」ボタン
- 既存のDB診断（`admin_debug_db_schema`, `admin_db_diagnostic`）もここへ集約

**一斉通知**
- `POST /api/admin/announcements`
- body: `{shop_ids: number[] | null, audience: 'managers' | 'all', title: string, body: string}`
  - `shop_ids: null` は全店舗（アーカイブ済みと `is_active=0` は除外）
  - `audience='managers'` → 店舗ごとに `staff_id IS NULL` の行を1件（既存の店舗向け通知の形。`src/app.py:1622` 参照）
  - `audience='all'` → 上記に加え、在籍スタッフ全員に `staff_id` 指定の行
- `notifications` への挿入は **1文のまとめ INSERT**（`INSERT INTO ... VALUES (...),(...),...`）
  にする。D1 は REST API の1往復＝1クエリのため、1件ずつ挿すと配信が実用速度にならない
- 1回の配信で挿入する行数の上限は 5000。超える場合は 400 を返し、店舗を分けるよう促す
- `created_at` は配信ごとに1つの値を計算し、そのバッチの全行に同じ値を明示的に入れる
  （`datetime('now')` 任せにしない）
- `GET /api/admin/notifications` は配信履歴を返す。`type='announcement'` の行を
  `(created_at, title)` でグルーピングし、配信ごとに「日時 / 件名 / 到達店舗数 / 到達人数」を返す。
  この2列で一意になるのは上記の `created_at` 明示指定によって保証される

### 6.8 店舗のアーカイブ・エクスポート・完全削除

**アーカイブ** `POST /api/admin/shops/<sid>/archive`
- `is_archived=1`, `archived_at`, `is_active=0` を設定
- その店舗の全セッションを削除（ログイン中のユーザーを追い出す）
- ログイン時の検索条件（`src/app.py:657, 669`）に `sh.is_archived=0` を追加する

**復元** `POST /api/admin/shops/<sid>/unarchive`
- `is_archived=0`, `archived_at=NULL`。`is_active` は 0 のまま（明示的に有効化させる）

**エクスポート** `GET /api/admin/shops/<sid>/export`
- 当該 `shop_id` に属する全テーブルの行を JSON 1ファイルで返す
- `Content-Disposition: attachment; filename="shop-<code>-<date>.json"`
- スタッフの `password_hash` は含めない
- ZIP にはしない（依存追加を避けるため）

**完全削除** `DELETE /api/admin/shops/<sid>`
- 条件: `is_archived=1` **かつ** リクエストの `confirm_code` が `shop_code` と一致
- UI 側は削除モーダルでエクスポートを1回押すまで削除ボタンを無効化する
- 削除順（FK の依存順）:
  `sessions` → `notifications` → `change_requests` → `wish_history` → `shifts` →
  `fixed_shifts`（`staff_id` 経由）→ `shift_pattern_weekday_required` → `shift_patterns` →
  `shift_request_periods` → `shop_holidays` → `staffs` → `shops`
- **`audit_logs` は削除しない**。運営の記録であり、`shop_id` は残したまま
  `detail` に店舗コードを保持する

### 6.9 マイグレーション管理（`src/migrator.py`）

**インターフェース**

| 関数 | 役割 |
|---|---|
| `split_statements(sql)` | コメントを除去し、セミコロンでステートメントに分割 |
| `status()` | `migrations/*.sql` を走査し、各ステートメントの適用済み/未適用を返す |
| `apply_pending()` | 未適用を順に実行し、成功したものを `schema_migrations` に記録 |

**起動時の自動適用はしない。** 本番D1で意図せずDDLが走るのを避けるため、
管理画面のボタンによる明示適用のみとする。新規環境は `schema.sql` が
`init_schema()`（`src/db.py:165`）で当たるため問題ない。

**冪等性の規約**（今後のマイグレーションにも適用）
- `CREATE TABLE` / `CREATE INDEX` は `IF NOT EXISTS` を付ける
- `ALTER TABLE ADD COLUMN` は `PRAGMA table_info` で列の有無を確認してからスキップ判定する
  （`ALTER TABLE` 自体は冪等でないため、`migrator` がガードする）

**既存マイグレーションの扱い**
`0002`〜`0004` は再実行が危険（`0004` は `staffs` の再構築を含む）。`schema_migrations` が
空のときに一度だけ、これらを全ステートメント「適用済み」として記録してから運用を開始する。
このバックフィルは `apply_pending()` の初回呼び出し時ではなく、`status()` / `apply_pending()`
のどちらからも呼ばれる `_backfill_legacy()` で行う。

---

## 7. エラー処理

既存方針を踏襲する。

- 入力エラーは `ValueError` を raise → 400 JSON（`src/app.py:64-80` の errorhandler）
- 認可エラーは `abort(403)`、他テナントのリソースは `abort(404)`
- `try/except` で握り潰さず、ログ出力して `raise` で伝播（`.opencode/skills/shift-saas-dev/SKILL.md` の規約）

**トランザクションが張れない点への対処**

`execute()` は毎回 `commit()` する（`src/db.py:138`）ため、複数ステートメントに
またがる原子性が無い。以下の2操作は途中で失敗し得るため、**どこまで進んだかを
レスポンスに含め、再実行で続きから進める**設計にする:

- 店舗の完全削除: 削除済みテーブル名の配列を返す。再実行すれば残りが消える
  （既に消えたテーブルへの DELETE は0件で成功するため冪等）
- マイグレーション適用: 適用できたステートメントは `schema_migrations` に記録済みなので、
  再実行すれば失敗した箇所から再開する

---

## 8. テスト

| ファイル | 状態 | 内容 |
|---|---|---|
| `tests/test_admin_console.py` | 新規 | 新API の正常系・権限（shop/staff からは403）・入力検証 |
| `tests/test_impersonation.py` | 新規 | 代理の開始/解除、GET可・書き込み403、admin API は管理者のまま、staff API は化けない、他ロールは代理不可、対象店舗が消えた場合 |
| `tests/test_migrator.py` | 新規 | ステートメント分割、冪等ガード、適用記録、途中失敗時に成功分だけ記録される、レガシーのバックフィル |
| `tests/test_security.py` | 追記 | **fixed-shifts 3本が未認証で401になる回帰テスト**、shifts の staff_id 越境、CSV Formula Injection、ログインのレート制限 |
| `tests/test_design_tokens.py` | 修正 | 走査対象に `public/admin.js` を追加 |
| `tests/conftest.py` | 修正 | `_TABLES` に `login_attempts` と `schema_migrations` を追加 |
| `e2e/admin-console.spec.js` | 新規 | ログイン → ダッシュボード → 店舗詳細 → 代理閲覧 → 管理者に戻る → 監査ログCSV |

`tests/conftest.py:30-43` の `_TABLES` は `db_reset` フィクスチャがテストごとに DELETE する
テーブルの一覧である。`login_attempts` を加え忘れると、あるテストのログイン失敗が次の
テストにロック状態として漏れ、原因の分かりにくい失敗を生む。

**完了条件**: 既存709テストを含む全ユニットテストが通り、`node --check public/app.js` と
`node --check public/admin.js` が通り、Playwright E2E が通ること。

`tests/test_security.py:184` の `test_login_brute_force_no_lockout` は「50回失敗しても
400 が返り続け、その後すぐ正しいパスワードでログインできる」ことを**脆弱性として記録している**
テストである。レート制限の実装で必ず落ちるので、`test_login_brute_force_locks_out` に
改名し、「10回失敗した時点で 429 になり、正しいパスワードでもログインできない」ことを
確認する内容に書き換える。

---

## 9. 実装順序

Phase 1 と Phase 2 は独立にデプロイ可能。Phase 1 を先に本番反映する。

**Phase 1**（緊急修正）
1. S1 / S1' — 未認証エンドポイントとテナント越境の修正 ＋ 回帰テスト
2. C — `require_auth` フォールバック削除
3. B1 — 店舗更新の部分更新化（サーバ・フロント両方）
4. S4 — `/api/init` の既定無効化
5. S5 — ログインのレート制限（`login_attempts` は `ensure_db()` で作成。5.6 参照）
6. 5.9 — 監査ログの記録追加（ログイン系）

**Phase 2**（運営機能）
7. `src/migrator.py` ＋ `migrations/0005_admin_console.sql` ＋ 適用UI
8. `src/admin_api.py` / `public/admin.js` への切り出し（機能追加なし、純粋な移設）
9. S2 / S3 — 管理者パスワード変更、管理者アカウント管理、`adminSystem` 画面
10. 代理閲覧
11. 店舗ライフサイクル（設定編集 / アーカイブ / エクスポート / 完全削除）
12. 全社ダッシュボード
13. 監査ログ強化（フィルタ / ページング / CSV）
14. 一斉通知
15. E2E テスト

手順8（切り出し）を機能追加より前に置くのは、移設と機能変更を同じコミットに混ぜると
差分レビューが不可能になるため。
