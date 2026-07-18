# ShiftAI — AIシフト自動作成SaaS

## 概要

Flask + SQLite（Cloudflare D1互換）で構築されたシフト管理SaaS。
AI（LLM API）による自然言語希望解析・シフト自動生成機能を搭載。

## 技術スタック

| レイヤー | 技術 |
|---------|------|
| フロントエンド | Vanilla JS + Bootstrap 5 + Chart.js |
| バックエンド | Python Flask |
| データベース | SQLite / Cloudflare D1 |
| AI | OpenAI互換 Chat Completions API |

## 初回セットアップ

### 必要なもの
- Python 3.10+
- pip

### ローカル開発

```bash
# 1. リポジトリをクローン
git clone https://github.com/あなたのユーザー名/shift_saas_flask.git
cd shift_saas_flask

# 2. 仮想環境作成
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. パッケージインストール
pip install -r requirements.txt

# 4. 環境変数設定
cp .env.example .env
# .env を編集して FLASK_SECRET と LLM_API_KEY を設定

# 5. 起動
python src/app.py
# http://localhost:8000 を開く

# 6. 初回ログイン
# POST /api/init を実行（curl または画面から）
curl -X POST http://localhost:8000/api/init
# → admin / admin123 でログイン → パスワードを変更
```

## Cloudflare Pages + D1 デプロイ手順

### ステップ1: GitHubにプッシュ

```bash
git init
git add .
git commit -m "初回コミット"
git branch -M main
git remote add origin https://github.com/あなたのユーザー名/shift_saas_flask.git
git push -u origin main
```

### ステップ2: Cloudflare D1 データベース作成

1. Cloudflareダッシュボード (https://dash.cloudflare.com) にログイン
2. 左メニュー「Workers & Pages」→「D1」
3.「データベースを作成」をクリック
4. データベース名: `shift-db` を入力して作成
5. 作成後に表示される `database_id` をメモ

### ステップ3: スキーマ初期化

CloudflareダッシュボードのD1コンソールで：

```sql
-- schema.sql の内容を全て貼り付けて実行
```

または wrangler CLI で：

```bash
npx wrangler d1 execute shift-db --remote --file=./schema.sql
npx wrangler d1 execute shift-db --remote --file=./seed.sql
```

### ステップ4: Cloudflare Pages プロジェクト作成

1. Cloudflareダッシュボード「Workers & Pages」→「アプリを作成」
2.「Pages」タブ→「Gitに接続」
3. GitHubリポジトリを選択
4. ビルド設定:
   - **ビルドコマンド**: （空欄）
   - **出力ディレクトリ**: `public`
   - **環境変数**: なし
5.「保存してデプロイ」をクリック

### ステップ5: 管理者ログイン

1. デプロイ完了後、PagesのURLにアクセス
2. `admin` / `admin123` でログイン
3. **すぐにパスワードを変更**

## テスト実行

```bash
# 全テスト
.venv/bin/python -m pytest tests/ -v

# 個別
.venv/bin/python -m pytest tests/test_operational_flows.py -v
```

## ディレクトリ構成

```
shift_saas_flask/
├── src/
│   ├── app.py          # Flask メインアプリ（ルーティング・API）
│   ├── shift_engine.py # シフト自動生成エンジン
│   ├── ai.py           # LLM API 連携
│   ├── auth.py         # 認証（PBKDF2）
│   ├── db.py           # SQLite/D1 アクセス
│   └── utils.py        # ユーティリティ
├── public/
│   ├── index.html      # SPA エントリーポイント
│   ├── app.js          # フロントエンドJS
│   └── style.css       # スタイル
├── tests/              # テストコード
├── schema.sql          # DB初期化スキーマ
├── seed.sql            # 初期管理者データ
├── requirements.txt    # Python依存パッケージ
└── .env.example        # 環境変数テンプレート
```
