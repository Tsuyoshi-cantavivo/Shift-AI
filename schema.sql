-- ===========================================================
-- Cloudflare D1 (SQLite互換) 初期化用スキーマ
-- 適用: wrangler d1 execute SHIFT_DB --local --file=./schema.sql
-- JS版と同一スキーマ。weekdayは 0=日,1=月,...,6=土。
-- ===========================================================

CREATE TABLE IF NOT EXISTS system_admins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  name          TEXT DEFAULT 'システム管理者',
  created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shops (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_code     TEXT UNIQUE NOT NULL,
  shop_name     TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_active     INTEGER DEFAULT 1,
  settings      TEXT DEFAULT '{}',
  created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS staffs (
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
CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id);

CREATE TABLE IF NOT EXISTS shift_patterns (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id        INTEGER NOT NULL,
  pattern_name   TEXT NOT NULL,
  start_time     TEXT NOT NULL,
  end_time       TEXT NOT NULL,
  required_staff INTEGER DEFAULT 1,
  FOREIGN KEY (shop_id) REFERENCES shops(id)
);

-- -----------------------------------------------------------
-- shift_pattern_weekday_required: パターンごとの曜日別必要人数
-- 特定曜日の required_staff を上書きする（祝日運用や週末強化など）。
-- 行が無い曜日は shift_patterns.required_staff が適用される。
-- weekday: 0=日,1=月,...,6=土
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS shift_pattern_weekday_required (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern_id      INTEGER NOT NULL,
  shop_id         INTEGER NOT NULL,
  weekday         INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
  required_staff  INTEGER NOT NULL,
  UNIQUE(pattern_id, weekday),
  FOREIGN KEY (pattern_id) REFERENCES shift_patterns(id)
);
CREATE INDEX IF NOT EXISTS idx_pwd_shop ON shift_pattern_weekday_required(shop_id);
CREATE INDEX IF NOT EXISTS idx_pwd_pat ON shift_pattern_weekday_required(pattern_id);

CREATE TABLE IF NOT EXISTS fixed_shifts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_id   INTEGER NOT NULL,
  weekday    INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
  start_time TEXT NOT NULL,
  end_time   TEXT NOT NULL,
  FOREIGN KEY (staff_id) REFERENCES staffs(id)
);
CREATE INDEX IF NOT EXISTS idx_fixed_staff ON fixed_shifts(staff_id);

CREATE TABLE IF NOT EXISTS shift_request_periods (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    INTEGER NOT NULL,
  start_date TEXT NOT NULL,
  end_date   TEXT NOT NULL,
  deadline   TEXT NOT NULL,
  is_active  INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (shop_id) REFERENCES shops(id)
);
CREATE INDEX IF NOT EXISTS idx_periods_shop ON shift_request_periods(shop_id);

CREATE TABLE IF NOT EXISTS shifts (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id             INTEGER,
  staff_id            INTEGER NOT NULL,
  start_datetime      TEXT NOT NULL,
  end_datetime        TEXT NOT NULL,
  break_time_minutes  INTEGER DEFAULT 0,
  status              TEXT DEFAULT 'requested'
                        CHECK(status IN ('requested','confirmed','modifying')),
  reason              TEXT,
  availability        TEXT,   -- NULL(時間指定) / 'any'(いつでも可) / 'morning'(早番希望) / 'evening'(遅番希望)
  created_at          TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (staff_id) REFERENCES staffs(id)
);
CREATE INDEX IF NOT EXISTS idx_shifts_shop_date ON shifts(shop_id, start_datetime);
CREATE INDEX IF NOT EXISTS idx_shifts_staff ON shifts(staff_id);
CREATE INDEX IF NOT EXISTS idx_shifts_status ON shifts(status);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  role       TEXT NOT NULL CHECK(role IN ('admin','shop','staff')),
  user_id    INTEGER NOT NULL,
  shop_id    INTEGER,
  created_at TEXT DEFAULT (datetime('now')),
  expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(role, user_id);

-- -----------------------------------------------------------
-- 8. notifications: アプリ内通知
-- staff_id が NULL のものは店舗向け通知、staff_id 指定時は該当スタッフ向け。
-- type: 'confirmed'(確定通知) / 'deadline'(締切リマインド) / 'info'
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    INTEGER,
  staff_id   INTEGER,
  type       TEXT,
  title      TEXT,
  body       TEXT,
  is_read    INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notif_shop ON notifications(shop_id, is_read);
CREATE INDEX IF NOT EXISTS idx_notif_staff ON notifications(staff_id, is_read);

-- -----------------------------------------------------------
-- 9. change_requests: 確定後の変更/休み/追加 申請（店長承認制）
-- request_type: 'change'(時間変更) / 'cancel'(休み) / 'add'(追加希望)
-- status: 'pending'(承認待ち) / 'approved' / 'rejected'
-- 承認されると対象シフトへ反映（DB書き換え）される。
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS change_requests (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id        INTEGER,
  staff_id       INTEGER NOT NULL,
  shift_id       INTEGER,            -- 対象シフト(cancel/change)。add時はNULL
  request_type   TEXT NOT NULL CHECK(request_type IN ('change','cancel','add')),
  desired_start  TEXT,               -- 希望開始日時
  desired_end    TEXT,               -- 希望終了日時
  reason         TEXT,
  status         TEXT DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  created_at     TEXT DEFAULT (datetime('now')),
  resolved_at    TEXT,
  FOREIGN KEY (staff_id) REFERENCES staffs(id)
);
CREATE INDEX IF NOT EXISTS idx_creq_shop ON change_requests(shop_id, status);
CREATE INDEX IF NOT EXISTS idx_creq_staff ON change_requests(staff_id, status);

-- -----------------------------------------------------------
-- wish_history: スタッフが提出した希望シフトの永久履歴
--
-- 【設計意図】
-- shifts テーブルは「実際のシフト配置（confirmed/requested）」を持ち、
-- AI自動生成や一括確定で上書きされる。しかし「スタッフがこう希望した」
-- という事実は、シフトがどう確定しようと永久に保持すべき:
--   1. スタッフから「わたしこういう希望出したはず」と問い合わせ
--      が来たときに参照
--   2. AI自動生成を何度繰り返しても、入力（requests）として再利用
--      でき、希望が消失しない
--
-- 従来の reason ベース保存（confirmed の reason='希望シフト' 等 を
-- 持つものを再利用）は、merge や短縮で元の希望時間が失われるため
-- 不完全だった（インシデント対象）。wish_history は元の時間を保持。
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS wish_history (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id         INTEGER NOT NULL,
  staff_id        INTEGER NOT NULL,
  start_datetime  TEXT NOT NULL,
  end_datetime    TEXT NOT NULL,
  availability    TEXT,
  submitted_at    TEXT DEFAULT (datetime('now')),
  note            TEXT,
  FOREIGN KEY (staff_id) REFERENCES staffs(id)
);
CREATE INDEX IF NOT EXISTS idx_wish_shop_period ON wish_history(shop_id, start_datetime);
CREATE INDEX IF NOT EXISTS idx_wish_staff ON wish_history(staff_id);

-- -----------------------------------------------------------
-- shop_holidays: 店舗ごとの祝日・特別休業日
--
-- 【設計意図】
-- 「シフト時間設定」の「祝日」テンプレートをいつ適用するかを決定する。
-- 国民の祝日は店舗側で任意に登録し、シフト時間設定の「祝日」設定が
-- 適用される。曜日別設定(0-6)とは独立して運用される。
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS shop_holidays (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    INTEGER NOT NULL,
  holiday_date TEXT NOT NULL,
  note       TEXT,
  UNIQUE(shop_id, holiday_date),
  FOREIGN KEY (shop_id) REFERENCES shops(id)
);
CREATE INDEX IF NOT EXISTS idx_holidays_shop ON shop_holidays(shop_id, holiday_date);
