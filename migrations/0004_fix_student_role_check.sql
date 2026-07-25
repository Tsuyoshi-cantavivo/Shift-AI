-- ===========================================================
-- マイグレーション 0004: staffs.role に 'student' を追加（0003 前半のやり直し）
--
-- 【背景 — なぜ 0003 が効いていないか】
--   0003 の前半（staffs 再構築）は D1 で失敗していた。
--     DROP TABLE staffs
--       → shifts / wish_history / fixed_shifts が staffs を参照する行を持つため
--         FOREIGN KEY constraint failed (19)
--     ALTER TABLE staffs_new RENAME TO staffs
--       → staffs が残っているため「already exists」で失敗
--   その結果 staffs は CHECK(role IN ('employee','part_time','manager')) のまま残り、
--   0003 の後半（shop_holidays）だけが適用された。
--   0002 が通ったのは、実行時点で staffs を参照するデータがまだ無かったため。
--
-- 【試して失敗した方法 — 繰り返さないこと】
--   PRAGMA defer_foreign_keys = on を使う方法は D1 で失敗する。
--   実行すると次のエラーで全体がロールバックされる:
--     "D1 DB was reset and rolled back to its last known good state because the
--      application left the database in a state where constraints were violated:
--      FOREIGN KEY constraint failed"
--   理由は SQLite の仕様。DROP TABLE の暗黙 DELETE で FK 違反カウンタが
--   参照行数ぶん増えるが、その後 RENAME で staffs を復活させてもカウンタは戻らず、
--   トランザクション終了時に違反が残ったと判定されるため。
--   D1 は PRAGMA foreign_keys 自体を切り替えられない（暗黙トランザクションのため）ので、
--   SQLite 公式手順の foreign_keys=OFF も使えない。
--
-- 【本マイグレーションの方法】
--   FK を無効化せず、DROP TABLE の時点で参照行が 1 件も無い状態を作る。
--     1. staffs を参照する 4 テーブルの中身を一時テーブルへ退避し、空にする
--     2. staffs を再構築する（この時点で参照行ゼロなので FK 違反は起きない）
--     3. 退避した行を書き戻し、一時テーブルを捨てる
--   staffs を参照するのは shifts / fixed_shifts / change_requests / wish_history の
--   4 つだけで、この 4 テーブルを参照するテーブルは存在しない（schema.sql で確認済み）。
--   よって退避による波及はない。
--
-- 【安全性】
--   wrangler d1 execute --file は失敗時に DB 全体を直前の状態へロールバックする
--   （defer_foreign_keys 版の失敗で実証済み）。
--   ローカル SQLite で foreign_keys=ON のまま本 SQL を検証し、件数・データ内容・
--   FK 整合性・インデックスがすべて保持されることを確認済み。
--
-- 【適用】
--   wrangler d1 execute shift-db --remote --file=./migrations/0004_fix_student_role_check.sql
--
-- 【適用後の検証】
--   SELECT sql FROM sqlite_master WHERE name='staffs'   → CHECK に 'student' があること
--   SELECT COUNT(*) FROM staffs / shifts / wish_history / fixed_shifts
--                                                       → 22 / 121 / 39 / 14 のままであること
--   PRAGMA foreign_key_check                            → 0 件であること
--   SELECT name FROM sqlite_master WHERE name LIKE '_mig_%' → 0 件であること
-- ===========================================================

-- ---- 0. 前回の失敗による残骸があれば掃除（いずれも他から参照されない）----
DROP TABLE IF EXISTS staffs_new;
DROP TABLE IF EXISTS _mig_shifts;
DROP TABLE IF EXISTS _mig_wish;
DROP TABLE IF EXISTS _mig_fixed;
DROP TABLE IF EXISTS _mig_creq;

-- ---- 1. staffs を参照する行を退避して空にする ----
CREATE TABLE _mig_shifts AS SELECT * FROM shifts;
CREATE TABLE _mig_wish   AS SELECT * FROM wish_history;
CREATE TABLE _mig_fixed  AS SELECT * FROM fixed_shifts;
CREATE TABLE _mig_creq   AS SELECT * FROM change_requests;

DELETE FROM shifts;
DELETE FROM wish_history;
DELETE FROM fixed_shifts;
DELETE FROM change_requests;

-- ---- 2. staffs を 'student' を許容する CHECK で再構築 ----
CREATE TABLE staffs_new (
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

INSERT INTO staffs_new (id, shop_id, staff_code, password_hash, name, role,
                        hourly_wage, min_hours_per_month, max_hours_per_month,
                        is_resigned, created_at)
SELECT id, shop_id, staff_code, password_hash, name, role,
       hourly_wage, min_hours_per_month, max_hours_per_month,
       is_resigned, created_at FROM staffs;

DROP TABLE staffs;

ALTER TABLE staffs_new RENAME TO staffs;

CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id);

-- ---- 3. 退避した行を書き戻す ----
INSERT INTO shifts          SELECT * FROM _mig_shifts;
INSERT INTO wish_history    SELECT * FROM _mig_wish;
INSERT INTO fixed_shifts    SELECT * FROM _mig_fixed;
INSERT INTO change_requests SELECT * FROM _mig_creq;

DROP TABLE _mig_shifts;
DROP TABLE _mig_wish;
DROP TABLE _mig_fixed;
DROP TABLE _mig_creq;
