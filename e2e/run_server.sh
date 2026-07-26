#!/bin/bash
# e2e/run_server.sh - Playwright e2e 用サーバー起動スクリプト
cd "$(dirname "$0")/.." || exit 1
echo "[e2e] CWD=$(pwd)" >&2
# iCloud同期の影響を受けないよう、e2e DBは /tmp 配下に配置する
E2E_DB_DIR="${TMPDIR:-/tmp}/shiftai_e2e"
mkdir -p "$E2E_DB_DIR"
export DB_PATH="$E2E_DB_DIR/shift_e2e.db"
export FLASK_DEBUG="${FLASK_DEBUG:-0}"
# e2e/helpers.js の ensureAdmin() が /api/init を叩くため、e2e 環境でのみ有効化する。
# 本番では既定 0（.env.example 参照）。
export ALLOW_INIT=1
echo "[e2e] DB_PATH=${DB_PATH}" >&2
# 既存のDBファイルをクリーンアップ（スキーマ更新を確実に反映）
if [ -f "$DB_PATH" ]; then
  echo "[e2e] Removing old $DB_PATH ($(stat -f%z "$DB_PATH" 2>/dev/null) bytes)" >&2
  rm -f "$DB_PATH"
fi
# スキーマを事前適用
# 併せて初期管理者(admin/admin123)を直接INSERTしておく。/api/init は S4修正で
# ランダムパスワードを生成するようになったため、そのままでは e2e 各spec が
# 決め打ちしている 'admin123' でのログインが軒並み失敗してしまう。ここで
# 既知パスワードの管理者を先に作っておけば、ensureAdmin() の /api/init 呼び出しは
# 「既に存在します」の no-op になり、既存 e2e spec の admin123 決め打ちを変えずに済む。
./.venv/bin/python -c "
import os, sys, sqlite3
sys.path.insert(0, 'src')
import db
db.init_schema('schema.sql')
from auth import hash_password
if not db.query_one('SELECT id FROM system_admins LIMIT 1'):
    db.execute(
        'INSERT INTO system_admins (admin_id, password_hash, name) VALUES (?,?,?)',
        ('admin', hash_password('admin123'), 'システム管理者'),
    )
conn = sqlite3.connect('$DB_PATH')
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
print('[init] tables after init_schema:', tables)
print('[init] file size:', os.path.getsize('$DB_PATH'))
" >&2
echo "[e2e] DB file size before exec: $(stat -f%z "$DB_PATH" 2>/dev/null) bytes" >&2
exec ./.venv/bin/python src/app.py




