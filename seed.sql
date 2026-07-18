-- 初回セットアップ用 SQL
-- 使い方: wrangler d1 execute shift-db --remote --file=./seed.sql
--
-- ログイン: admin / admin123
-- ※ デプロイ直後にパスワードを変更してください

INSERT INTO system_admins (admin_id, password_hash, name)
VALUES ('admin', 'f51ca7439b1e84cfc71808e2908daa9c65f3037f7d562db311edeb4f7e409df8', 'システム管理者');
