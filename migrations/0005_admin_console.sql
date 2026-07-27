-- 0005_admin_console.sql
-- システム管理者コンソール（設計書: docs/superpowers/specs/2026-07-26-admin-console-design.md）
--
-- 適用方法:
--   ローカル: python src/migrator.py apply
--   本番D1  : 管理者画面「システム」→ マイグレーション → 未適用を適用
--
-- 【規約】今後のマイグレーションは冪等な形で書くこと。
--   - CREATE TABLE / CREATE INDEX は IF NOT EXISTS を付ける
--   - ALTER TABLE ADD COLUMN は IF NOT EXISTS を書けないため、migrator が
--     PRAGMA table_info で列の有無を見てスキップする

-- 1. マイグレーション適用状態の追跡（migrator 自身も CREATE するが、
--    新規環境で schema.sql だけを流した場合のために定義を残す）
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT NOT NULL,
  stmt_index INTEGER NOT NULL,
  applied_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (filename, stmt_index)
);

-- 2. ログイン試行のレート制限（Phase 1 で ensure_db が作るが、記録として残す）
-- blocked_logged は「ロック期間中の 429 を監査ログに1回だけ記録する」ためのフラグ。
-- Phase 1 で後から追加したため、既存DBには ensure_db の ALTER で追従させている。
CREATE TABLE IF NOT EXISTS login_attempts (
  attempt_key    TEXT PRIMARY KEY,
  fail_count     INTEGER NOT NULL DEFAULT 0,
  locked_until   TEXT,
  updated_at     TEXT,
  blocked_logged INTEGER NOT NULL DEFAULT 0
);

-- 3. 代理閲覧: admin セッションが一時的に見ている店舗
ALTER TABLE sessions ADD COLUMN acting_shop_id INTEGER;

-- 4. 店舗アーカイブ
ALTER TABLE shops ADD COLUMN is_archived INTEGER DEFAULT 0;
ALTER TABLE shops ADD COLUMN archived_at TEXT;
