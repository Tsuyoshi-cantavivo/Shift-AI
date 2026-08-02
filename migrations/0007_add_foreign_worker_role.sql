-- ===========================================================
-- マイグレーション 0007: staffs.role に 'foreign_worker' を追加し、
--                        shifts に weekly_cap_ack を追加する
--
-- 【背景】
--   資格外活動許可で働く在留資格（留学・家族滞在）は入管法上1週間28時間以内。
--   超えると本人の在留資格だけでなく、雇用主も不法就労助長罪の対象になる。
--   ロールで識別できるようにし、週上限を自動生成・手動入力の両方で効かせる。
--   weekly_cap_ack は、店長が週28h超過を承諾して保存したシフトの印
--   （既存の over_cap_flag と同じ形）。
--
-- 【DROP TABLE staffs が素直に通らない理由 — 0004 の記録より】
--   shifts / wish_history / fixed_shifts / change_requests が staffs を参照する
--   行を持つため、FK が有効なままでは DROP TABLE staffs が
--   「FOREIGN KEY constraint failed」で失敗する。
--   PRAGMA defer_foreign_keys / PRAGMA foreign_keys=OFF はどちらも D1 で使えない
--   （詳細は migrations/0004_fix_student_role_check.sql のヘッダに記録がある）。
--   0003 はこれで前半だけが落ち、staffs が古い CHECK のまま残る事故になった。
--
-- 【本マイグレーションの方法 — 0004 と同じ】
--   FK を無効化せず、DROP TABLE の時点で参照行が1件も無い状態を作る。
--     1. staffs を参照する4テーブルの中身を一時テーブルへ退避し、空にする
--     2. staffs を再構築する（この時点で参照行ゼロなので FK 違反は起きない）
--     3. 退避した行を書き戻し、一時テーブルを捨てる
--     4. 最後に shifts へ列を足す
--   staffs を参照するのは shifts / fixed_shifts / change_requests / wish_history の
--   4つだけで、この4テーブルを参照するテーブルは存在しない（schema.sql で確認済み）。
--
--   4 を最後に置くのは順序上の要請。`INSERT INTO shifts SELECT * FROM _mig_shifts`
--   は列構成が一致している必要があり、先に ALTER すると列数が合わなくなる。
--
-- 【適用】
--   ローカル: .venv/bin/python src/migrator.py apply
--   本番D1  : 管理者画面「システム」→ マイグレーション → 未適用を適用
--             （または wrangler d1 execute shift-db --remote --file=./migrations/0007_add_foreign_worker_role.sql）
--
-- 【適用後の検証】
--   SELECT sql FROM sqlite_master WHERE name='staffs'  → CHECK に 'foreign_worker' があること
--   PRAGMA table_info(shifts)                          → weekly_cap_ack があること
--   SELECT COUNT(*) FROM staffs / shifts / wish_history / fixed_shifts / change_requests
--                                                      → 適用前と同じであること
--   PRAGMA foreign_key_check                           → 0件であること
--   SELECT name FROM sqlite_master WHERE name LIKE '_mig_%' → 0件であること
-- ===========================================================

-- ---- 0. 前回の失敗による残骸があれば掃除（いずれも他から参照されない）----
DROP TABLE IF EXISTS staffs_new_0007;
DROP TABLE IF EXISTS _mig07_shifts;
DROP TABLE IF EXISTS _mig07_wish;
DROP TABLE IF EXISTS _mig07_fixed;
DROP TABLE IF EXISTS _mig07_creq;

-- ---- 1. staffs を参照する行を退避して空にする ----
CREATE TABLE _mig07_shifts AS SELECT * FROM shifts;
CREATE TABLE _mig07_wish   AS SELECT * FROM wish_history;
CREATE TABLE _mig07_fixed  AS SELECT * FROM fixed_shifts;
CREATE TABLE _mig07_creq   AS SELECT * FROM change_requests;

DELETE FROM shifts;
DELETE FROM wish_history;
DELETE FROM fixed_shifts;
DELETE FROM change_requests;

-- ---- 2. staffs を 'foreign_worker' を許容する CHECK で再構築 ----
CREATE TABLE staffs_new_0007 (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id               INTEGER NOT NULL,
  staff_code            TEXT NOT NULL,
  password_hash         TEXT NOT NULL,
  name                  TEXT NOT NULL,
  role                  TEXT DEFAULT 'part_time'
                          CHECK(role IN ('employee','part_time','manager','student','foreign_worker')),
  hourly_wage           INTEGER DEFAULT 1000,
  min_hours_per_month   INTEGER DEFAULT 0,
  max_hours_per_month   INTEGER DEFAULT 160,
  is_resigned           INTEGER DEFAULT 0,
  created_at            TEXT DEFAULT (datetime('now')),
  UNIQUE(shop_id, staff_code),
  FOREIGN KEY (shop_id) REFERENCES shops(id)
);

INSERT INTO staffs_new_0007 (id, shop_id, staff_code, password_hash, name, role,
                             hourly_wage, min_hours_per_month, max_hours_per_month,
                             is_resigned, created_at)
SELECT id, shop_id, staff_code, password_hash, name, role,
       hourly_wage, min_hours_per_month, max_hours_per_month,
       is_resigned, created_at FROM staffs;

DROP TABLE staffs;

ALTER TABLE staffs_new_0007 RENAME TO staffs;

CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id);

-- ---- 3. 退避した行を書き戻す ----
INSERT INTO shifts          SELECT * FROM _mig07_shifts;
INSERT INTO wish_history    SELECT * FROM _mig07_wish;
INSERT INTO fixed_shifts    SELECT * FROM _mig07_fixed;
INSERT INTO change_requests SELECT * FROM _mig07_creq;

DROP TABLE _mig07_shifts;
DROP TABLE _mig07_wish;
DROP TABLE _mig07_fixed;
DROP TABLE _mig07_creq;

-- ---- 4. 週28h超過の承諾フラグ（書き戻しの後に足す。順序の理由はヘッダ参照）----
ALTER TABLE shifts ADD COLUMN weekly_cap_ack INTEGER DEFAULT 0;
