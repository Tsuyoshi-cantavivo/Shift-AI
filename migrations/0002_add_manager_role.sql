-- ===========================================================
-- マイグレーション 0002: staffs.role に 'manager' を追加
--
-- 【目的】
--   ログイン画面をタブなしの単一フォーム（店舗コード+ユーザーコード+PW）に
--   統一するため、店舗管理者を staffs ロール 'manager' として表現する。
--
-- 【やること】
--   1. staffs.role の CHECK 制約を ('employee','part_time','manager') に拡張
--      ※ SQLite は CHECK の ALTER ができないため TABLE 再構築
--   2. 各 shops に対し manager ロールのスタッフを 1 名自動作成
--      staff_code='manager', password_hash=shops.password_hash を引き継ぎ
--      → 旧店主は shop_code + 'manager' + 従来PW でそのままログイン可能
--
-- 【D1 適用手順】
--   wrangler d1 execute shift-db --remote --file=./migrations/0002_add_manager_role.sql
--   ※ D1 は BEGIN TRANSACTION/COMMIT 禁止・PRAGMA も一部不可のため
--      これらは使わず、--file 全体が自動でアトミック実行される。
--
-- 【後方互換】
--   shops テーブルは残し、/api/login で user_code == shop_code の場合のみ
--   フォールバック検索する（旧店主ログインの逃げ道）。
-- ===========================================================

-- ---- 1. staffs を 'manager' を許容する CHECK で再構築 ----
CREATE TABLE IF NOT EXISTS staffs_new (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id               INTEGER NOT NULL,
  staff_code            TEXT NOT NULL,
  password_hash         TEXT NOT NULL,
  name                  TEXT NOT NULL,
  role                  TEXT DEFAULT 'part_time'
                          CHECK(role IN ('employee','part_time','manager')),
  hourly_wage           INTEGER DEFAULT 1000,
  min_hours_per_month   INTEGER DEFAULT 0,
  max_hours_per_month   INTEGER DEFAULT 160,
  is_resigned           INTEGER DEFAULT 0,
  created_at            TEXT DEFAULT (datetime('now')),
  UNIQUE(shop_id, staff_code),
  FOREIGN KEY (shop_id) REFERENCES shops(id)
);

INSERT INTO staffs_new (id, shop_id, staff_code, password_hash, name, role,
                        hourly_wage, min_hours_per_month, max_hours_per_month,
                        is_resigned, created_at)
SELECT id, shop_id, staff_code, password_hash, name, role,
       hourly_wage, min_hours_per_month, max_hours_per_month,
       is_resigned, created_at FROM staffs;

DROP TABLE staffs;

ALTER TABLE staffs_new RENAME TO staffs;

CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id);

-- ---- 2. 各店舗に manager ロールのスタッフを自動作成（店パスワード引継ぎ） ----
INSERT INTO staffs (shop_id, staff_code, password_hash, name, role,
                    hourly_wage, min_hours_per_month, max_hours_per_month)
SELECT s.id, 'manager', s.password_hash, s.shop_name || ' 店主', 'manager',
       2000, 0, 200
FROM shops s
WHERE NOT EXISTS (
  SELECT 1 FROM staffs st
  WHERE st.shop_id = s.id AND st.staff_code = 'manager'
);

-- ---- 検証（参考） ----
-- SELECT sh.shop_code, st.staff_code, st.name, st.role
--   FROM staffs st JOIN shops sh ON st.shop_id=sh.id
--   WHERE st.role='manager';
