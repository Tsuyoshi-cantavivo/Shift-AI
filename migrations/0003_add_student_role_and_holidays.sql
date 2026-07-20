-- ===========================================================
-- マイグレーション 0003: 学生アルバイトロール追加 + 祝日テーブル
--
-- 【目的】
--   1. staffs.role に 'student' (学生アルバイト) を追加
--      ※ 月80h上限・学生のみシフト禁止の制約を持つ
--   2. shop_holidays テーブルを新設（店舗ごとの祝日・特別休業日）
--      ※ シフト時間設定の「祝日」テンプレート適用日に使用
--
-- 【D1 適用手順】
--   wrangler d1 execute shift-db --remote --file=./migrations/0003_add_student_role_and_holidays.sql
-- ===========================================================

-- ---- 1. staffs を 'student' を許容する CHECK で再構築 ----
CREATE TABLE IF NOT EXISTS staffs_new (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id               INTEGER NOT NULL,
  staff_code            TEXT NOT NULL,
  password_hash         TEXT NOT NULL,
  name                  TEXT NOT NULL,
  role                  TEXT DEFAULT 'part_time'
                          CHECK(role IN ('employee','part_time','manager','student')),
  hourly_wage           INTEGER DEFAULT 1000,
  min_hours_per_month   INTEGER DEFAULT 0,
  max_hours_per_month   INTEGER DEFAULT 160,
  is_resigned           INTEGER DEFAULT 0,
  created_at            TEXT DEFAULT (datetime('now')),
  UNIQUE(shop_id, staff_code),
  FOREIGN KEY (shop_id) REFERENCES shops(id)
);

INSERT OR IGNORE INTO staffs_new (id, shop_id, staff_code, password_hash, name, role,
                        hourly_wage, min_hours_per_month, max_hours_per_month,
                        is_resigned, created_at)
SELECT id, shop_id, staff_code, password_hash, name, role,
       hourly_wage, min_hours_per_month, max_hours_per_month,
       is_resigned, created_at FROM staffs;

DROP TABLE IF EXISTS staffs;

ALTER TABLE staffs_new RENAME TO staffs;

CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id);

-- ---- 2. shop_holidays テーブル新設 ----
CREATE TABLE IF NOT EXISTS shop_holidays (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    INTEGER NOT NULL,
  holiday_date TEXT NOT NULL,
  note       TEXT,
  UNIQUE(shop_id, holiday_date),
  FOREIGN KEY (shop_id) REFERENCES shops(id)
);
CREATE INDEX IF NOT EXISTS idx_holidays_shop ON shop_holidays(shop_id, holiday_date);
