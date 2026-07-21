---
name: shift-saas-dev
description: "shift_saas_flask プロジェクト開発のためのスキル。Flask + SQLite/Cloudflare D1 + Vanilla JS で構築されたシフト管理 SaaS。コード規約・デバッグ手順・テスト実行・シフトエンジン理解に使用。シフト生成、AI、DB、フロントエンド修正時に参照。"
---

# shift_saas_flask 開発スキル

## プロジェクト概要

- **スタック**: Python Flask + SQLite (本番は Cloudflare D1) + Vanilla JS + Bootstrap 5
- **エントリ**: `src/app.py` (3320行・全APIルーティング), `public/app.js` (3700行・全画面)
- **コアロジック**: `src/shift_engine.py` (シフト自動生成), `src/ai.py` (LLM連携)
- **DBヘルパー**: `src/db.py` (local/D1 モード自動切替)
- **認証**: `src/auth.py` (PBKDF2-HMAC-SHA256)

## 必須コマンド

```bash
# テスト
.venv/bin/python -m pytest tests/ -q          # 全ユニットテスト
.venv/bin/python tests/run_tests.py            # シフトエンジン不変量テスト

# 構文チェック
node --check public/app.js
.venv/bin/python -c "import ast; ast.parse(open('src/app.py').read())"

# アプリ起動 (ローカル)
PORT=5555 FLASK_DEBUG=1 .venv/bin/python src/app.py

# 本番DB参照 (Cloudflare D1 REST API経由)
echo "SELECT ..." | python3 /tmp/d1query.py
```

## コード規約

- **コメント**: 日本語で記述。「なぜ」に焦点を当てる（Whatはコード見れば分かる）
- **コミットメッセージ**: `fix:` / `feat:` / `refactor:` プレフィックス+日本語サマリ
- **エラー処理**: `try/except` は握り潰さずログ出力。`raise` で伝播
- **DBアクセス**: `query_all` / `query_one` / `execute` を使用（local/D1 自動切替）
- **時刻**: ISO形式 `YYYY-MM-DDTHH:MM:SS` 必ずゼロ埋め（`utils.norm_dt_iso`/`norm_hhmm` 使用）
- **日またぎ**: `utils.combine_dt_overnight` を使用（end <= start で翌日補正）
- **フロントエンド**: Vanilla JS・モジュール化なし・グローバル関数で画面ID=`SCREENS.<name>`

## 重要なインシデント履歴（再発防止）

1. **DB時刻ゼロ埋め**: `T7:00:00` のような非ゼロ埋めで `hm()` が NaN 化 → 全バー消失
2. **希望表消滅**: shifts.requested ベース → wish_history ベースに変更で解決
3. **重なりパターン過大カウント**: compute_shortage_unique_hours で解決
4. **22hシフト労基法違反**: shift_engine.py:634 で max_employee_daily を強制

## シフトエンジン理解 (`src/shift_engine.py`)

`auto_generate(shop_id, settings, start_date, end_date)` の処理順序:

1. **Step1** (廃止): 固定シフトの厳守配置は行わない（候補扱い）
2. **Step2a**: 時間指定希望（availability無し）を最優先配置
3. **Step2b**: 柔軟希望（availability='any'/'morning'/'evening'）を不足パターンに配置
4. **Step2.5**: 固定シフトを候補として配置（希望が無い日のみ）
5. **Step3**: 社員+「いつでも」バイトによる不足補填（max_employee_daily=13h上限）
6. **shortage_list**: パターン別不足（詳細表示用）
7. **shortage_unique**: 時間帯別一意不足（カウント用）

### 主要制約

- `can_place()`: rest/min_daily(PTのみ)/max_daily(全ロール)/monthly_cap/cap/重複チェック
- `cap_ok()`: スロット(15分単位)ごとの上限人数厳守
- `_day_shortage_segments()`: 連続不足区間の抽出
- `compute_break_minutes()`: 6h→45/8h→60/10h→90/12h→120/14h→150分

## デバッグ手順

### 1. 本番データの再現

```python
# /tmp/clone_prod.py で本番D1→ローカルSQLiteにクローン
# staffs の password_hash を test1234 に統一更新すればブラウザ試験可能
sqlite3 shift.db "UPDATE staffs SET password_hash='$(python3 -c 'import hashlib; print(hashlib.pbkdf2_hmac("sha256", b"test1234", b"shift_saas_salt_v1", 50000, 32).hex())')' WHERE shop_id=1;"
```

### 2. Playwright でのUI試験

- `localStorage` に `shift_token`/`shift_role`/`shift_user` を設定でログイン省略可能
- `await page.evaluate(...)` でDOM直接操作（モーダルのダブルクリック等）
- スクリーンショットは `<name>.png` で保存（プロジェクトルート）

### 3. シフト生成のドライラン

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:5555/api/login ...)
curl -s -X POST http://127.0.0.1:5555/api/shop/shifts/auto \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2026-08-03","end_date":"2026-08-03","dry_run":true}'
```

## よくあるトラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| タイムラインのバーが全部消える | DBの時刻がゼロ埋めされてない | `norm_dt_iso`/`norm_hhmm` で正規化 |
| 希望が表示されない | shifts.requested を見てる | wish_history ベースに変更 |
| 社員に22hシフトが付く | max_daily 未適用 | `can_place` でロール別上限チェック |
| 不足枠が過大カウント | パターン重なり | `compute_shortage_unique_hours` 使用 |

## Git運用

- 直接 `main` に push（PR運用ではない）
- コミットメッセージは詳細に（インシデント対策は特に）
- `git log --oneline -10` で最近の文脈を把握

## 本番環境

- **ホスティング**: Railway (`.venv/bin/gunicorn` で起動)
- **DB**: Cloudflare D1 (REST API 経由で `DB_MODE=d1`)
- **店舗**: MS_LakeTown (ミニストップイオンレイクタウン店) がメイン利用店
- **フロントエンド**: Cloudflare Pages でホスト（`.cf-pages.json`）
