#!/bin/bash
# e2e/run_server.sh - Playwright e2e 用サーバー起動スクリプト
cd "$(dirname "$0")/.." || exit 1
echo "[e2e] CWD=$(pwd)" >&2
# iCloud同期の影響を受けないよう、e2e DBは /tmp 配下に配置する
E2E_DB_DIR="${TMPDIR:-/tmp}/shiftai_e2e"
mkdir -p "$E2E_DB_DIR"
export DB_PATH="$E2E_DB_DIR/shift_e2e.db"
export FLASK_DEBUG="${FLASK_DEBUG:-0}"
echo "[e2e] DB_PATH=${DB_PATH}" >&2
# 既存のDBファイルをクリーンアップ（スキーマ更新を確実に反映）
if [ -f "$DB_PATH" ]; then
  echo "[e2e] Removing old $DB_PATH ($(stat -f%z "$DB_PATH" 2>/dev/null) bytes)" >&2
  rm -f "$DB_PATH"
fi
# スキーマを事前適用
./.venv/bin/python -c "
import os, sys, sqlite3
sys.path.insert(0, 'src')
import db
db.init_schema('schema.sql')
conn = sqlite3.connect('$DB_PATH')
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
print('[init] tables after init_schema:', tables)
print('[init] file size:', os.path.getsize('$DB_PATH'))
" >&2
echo "[e2e] DB file size before exec: $(stat -f%z "$DB_PATH" 2>/dev/null) bytes" >&2
exec ./.venv/bin/python src/app.py




