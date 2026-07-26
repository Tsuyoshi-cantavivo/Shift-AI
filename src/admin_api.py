"""admin_api.py - システム管理者向け API (/api/admin/*)。

src/app.py が4000行超で保守が難しくなっていたため切り出した。Blueprint は使わず、
register_admin_routes(app, ...) の中で既存と同じ @app.get/post デコレータで登録する
（url_prefix 等の新しい概念を持ち込まないため）。

app.py 側にしか無いヘルパ（require_auth / audit / summarize_shifts）は
循環 import を避けるためキーワード引数で受け取る。

db_module_get_conn / _restore_staffs_table_internal は admin_db_migrate /
admin_db_restore_staffs 専用の内部ヘルパで、app.py 側の状態には依存しないため
モジュールレベル関数としてここに置く（register_admin_routes の外）。
"""
from flask import request, jsonify, abort, g

from db import query_all, query_one, execute, insert_row
from auth import hash_password, verify_password, strip_password
from utils import calc_next_period, jst_now, parse_settings, validate_password
import json


def db_module_get_conn():
    """db モジュル経由で生のコネクションを取得（マイグレーション用）。"""
    import db as _db
    return _db.get_conn()


def _restore_staffs_table_internal(log):
    """staffs テーブル消失時の内部復元関数（log は破壊的に追記）。"""
    # 候補: staffs_migrate_backup, staffs_backup, staffs_new
    for backup_name in ("staffs_migrate_backup", "staffs_backup", "staffs_new"):
        try:
            rows = query_all(f"SELECT name FROM sqlite_master WHERE name='{backup_name}'")
            if not rows:
                continue
            log.append(f"復元元発見: {backup_name}")
            conn = db_module_get_conn()
            try:
                conn.execute("PRAGMA foreign_keys = OFF")
                cur = conn.cursor()
                # バックアップの列を取得
                cur.execute(f"PRAGMA table_info({backup_name})")
                cols = [r[1] for r in cur.fetchall()]
                log.append(f"{backup_name} の列: {cols}")
                # 新 staffs を作る
                cur.execute("DROP TABLE IF EXISTS staffs")
                cur.execute("""
                    CREATE TABLE staffs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      shop_id INTEGER NOT NULL,
                      staff_code TEXT NOT NULL,
                      password_hash TEXT NOT NULL,
                      name TEXT NOT NULL,
                      role TEXT DEFAULT 'part_time'
                        CHECK(role IN ('employee','part_time','manager','student')),
                      hourly_wage INTEGER DEFAULT 1000,
                      min_hours_per_month INTEGER DEFAULT 0,
                      max_hours_per_month INTEGER DEFAULT 160,
                      is_resigned INTEGER DEFAULT 0,
                      created_at TEXT DEFAULT (datetime('now')),
                      UNIQUE(shop_id, staff_code),
                      FOREIGN KEY (shop_id) REFERENCES shops(id)
                    )
                """)
                # バックアップからコピー（共通列のみ）
                target_cols = ['id', 'shop_id', 'staff_code', 'password_hash', 'name', 'role',
                               'hourly_wage', 'min_hours_per_month', 'max_hours_per_month',
                               'is_resigned', 'created_at']
                common_cols = [c for c in target_cols if c in cols]
                col_list = ', '.join(common_cols)
                if common_cols:
                    cur.execute(f"INSERT INTO staffs ({col_list}) SELECT {col_list} FROM {backup_name}")
                    cnt = cur.execute("SELECT COUNT(*) FROM staffs").fetchone()[0]
                    log.append(f"✓ {cnt} 行復元完了")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id)")
                conn.commit()
                log.append(f"✓ staffs を {backup_name} から復元しました")
                return True
            finally:
                try:
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            log.append(f"⚠ {backup_name} からの復元失敗: {e}")
            continue

    # どのバックアップも無い → shops から最低限の manager を作る
    log.append("バックアップが見つかりません。shops から最低限の manager を再構築します...")
    try:
        conn = db_module_get_conn()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS staffs")
            cur.execute("""
                CREATE TABLE staffs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  shop_id INTEGER NOT NULL,
                  staff_code TEXT NOT NULL,
                  password_hash TEXT NOT NULL,
                  name TEXT NOT NULL,
                  role TEXT DEFAULT 'part_time'
                    CHECK(role IN ('employee','part_time','manager','student')),
                  hourly_wage INTEGER DEFAULT 1000,
                  min_hours_per_month INTEGER DEFAULT 0,
                  max_hours_per_month INTEGER DEFAULT 160,
                  is_resigned INTEGER DEFAULT 0,
                  created_at TEXT DEFAULT (datetime('now')),
                  UNIQUE(shop_id, staff_code),
                  FOREIGN KEY (shop_id) REFERENCES shops(id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id)")
            # shops の各店舗に manager を PW 引き継ぎで作成
            cur.execute("SELECT id, shop_code, shop_name, password_hash FROM shops")
            shops = cur.fetchall()
            for shop in shops:
                cur.execute(
                    "INSERT OR IGNORE INTO staffs (shop_id, staff_code, password_hash, name, role, "
                    "hourly_wage, min_hours_per_month, max_hours_per_month) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (shop[0], "manager", shop[3], shop[2] + " 店主", "manager", 2000, 0, 200))
            conn.commit()
            log.append(f"✓ shops ({len(shops)} 店舗) から manager を再構築しました")
            return True
        finally:
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.close()
            except Exception:
                pass
    except Exception as e:
        log.append(f"✗ shops からの復元も失敗: {e}")
        return False


def register_admin_routes(app, *, require_auth, audit, summarize_shifts):
    """/api/admin/* の全ルートを app に登録する。"""

    @app.get("/api/admin/shops")
    def admin_shops():
        require_auth(["admin"])
        rows = query_all("SELECT id, shop_code, shop_name, is_active, settings, created_at FROM shops ORDER BY id")
        return jsonify({"shops": rows})


    @app.post("/api/admin/shops")
    def admin_create_shop():
        """店舗作成（店舗責任者アカウント同時作成対応）。

        body:
          - shop_code: 必須
          - shop_name: 必須
          - password: 必須（店铺自身のPW。後方互換用。空文字可）
          - settings: dict (optional)
          - manager_code: 店舗責任者のユーザーコード（管理者が任意指定）
          - manager_password: 店舗責任者のPW
          - manager_name: 店舗責任者の氏名

        店舗責任者は role='manager' の staffs 行として作成され、
        shop_code + manager_code + manager_password で即ログイン可能。
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        # 必須項目
        if not body.get("shop_code"):
            abort(400, description="店舗コードを入力してください")
        if not body.get("shop_name"):
            abort(400, description="店舗名を入力してください")
        shop_pw = body.get("password") or "shopdefault1"
        err = validate_password(shop_pw)
        if err:
            abort(400, description="店舗パスワード: " + err)
        # 重複チェック
        dup = query_one("SELECT id FROM shops WHERE shop_code=?", (body["shop_code"],))
        if dup:
            abort(400, description=f"店舗コード '{body['shop_code']}' は既に存在します")
        # 店舗責任者のバリデーション（必須）
        mgr_code = (body.get("manager_code") or "").strip()
        mgr_pw = body.get("manager_password") or ""
        mgr_name = (body.get("manager_name") or "").strip()
        if not mgr_code:
            abort(400, description="店舗責任者のユーザーIDを入力してください")
        if not mgr_name:
            abort(400, description="店舗責任者の氏名を入力してください")
        err = validate_password(mgr_pw)
        if err:
            abort(400, description="店舗責任者パスワード: " + err)
        # 店舗を作成
        meta = execute(
            "INSERT INTO shops (shop_code, shop_name, password_hash, settings) VALUES (?,?,?,?)",
            (body["shop_code"], body["shop_name"], hash_password(shop_pw),
             json.dumps(body.get("settings") or {}, ensure_ascii=False)))
        shop_id = meta["last_row_id"]
        # 店舗責任者を manager ロールで作成
        try:
            execute(
                "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role, "
                "hourly_wage, min_hours_per_month, max_hours_per_month) VALUES (?,?,?,?,?,?,?,?)",
                (shop_id, mgr_code, hash_password(mgr_pw), mgr_name, "manager",
                 body.get("manager_wage") or 2000, 0, 200))
        except Exception as e:
            # ロールバック: 店舗を削除
            execute("DELETE FROM shops WHERE id=?", (shop_id,))
            msg = str(e)
            if "UNIQUE" in msg.upper():
                abort(400, description=f"ユーザーID '{mgr_code}' は既に存在します（店舗コードと同じ値にするか、別のIDを指定してください）")
            abort(400, description="店舗責任者の作成に失敗しました: " + msg)
        audit("shop.create", target_type="shop", target_id=shop_id, shop_id=shop_id,
              detail=body.get("shop_name"))
        return jsonify({"ok": True, "id": shop_id, "shop_id": shop_id,
                        "manager_code": mgr_code, "manager_name": mgr_name})


    @app.put("/api/admin/shops/<int:sid>")
    def admin_update_shop(sid):
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        shop = query_one("SELECT id, shop_code, shop_name FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")

        # 部分更新。送られてきたキーだけを更新する。
        # NOTE: かつて body.get("shop_name") を無条件で UPDATE していたため、
        # 有効/無効トグル（shop_name を送らない）で店舗名が空文字に潰れる事故があった。
        sets, binds, changed = [], [], []
        if "shop_name" in body:
            name = (body.get("shop_name") or "").strip()
            if not name:
                raise ValueError("店舗名を入力してください")
            sets.append("shop_name=?"); binds.append(name); changed.append("shop_name")
        if "shop_code" in body:
            code = (body.get("shop_code") or "").strip()
            if not code:
                raise ValueError("店舗コードを入力してください")
            dup = query_one("SELECT id FROM shops WHERE shop_code=? AND id<>?", (code, sid))
            if dup:
                raise ValueError("その店舗コードは既に使われています")
            sets.append("shop_code=?"); binds.append(code); changed.append("shop_code")
        if "is_active" in body:
            is_active = 1 if body.get("is_active") else 0
            sets.append("is_active=?"); binds.append(is_active); changed.append(f"is_active={is_active}")

        if not sets:
            raise ValueError("更新する項目がありません")

        binds.append(sid)
        execute(f"UPDATE shops SET {','.join(sets)} WHERE id=?", tuple(binds))
        audit("shop.update", target_type="shop", target_id=sid, shop_id=sid,
              detail=",".join(changed))
        return jsonify({"ok": True})


    @app.get("/api/admin/shops/stats/<int:sid>")
    def admin_shop_stats(sid):
        require_auth(["admin"])
        sc = query_one("SELECT count(*) as c FROM staffs WHERE shop_id=? AND is_resigned=0", (sid,))
        shc = query_one("SELECT count(*) as c FROM shifts WHERE shop_id=? AND status='confirmed'", (sid,))
        return jsonify({"staff_count": sc["c"], "confirmed_count": shc["c"]})


    @app.get("/api/admin/shops/staffs/<int:sid>")
    def admin_shop_staffs(sid):
        require_auth(["admin"])
        rows = query_all("SELECT id, staff_code, name, role, hourly_wage, is_resigned FROM staffs WHERE shop_id=? ORDER BY role DESC, id", (sid,))
        return jsonify({"staffs": rows})


    @app.put("/api/admin/shops/<int:sid>/staffs/<int:staff_id>/role")
    def admin_shop_staff_update_role(sid, staff_id):
        """システム管理者がスタッフのロールを変更。

        body: {"role": "manager" | "employee" | "part_time" | "student"}
        ※ manager に変更すると、そのスタッフで店舗管理者ログインが可能に。
        ※ student に変更時は月80h上限を強制。
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        new_role = body.get("role")
        if new_role not in ("manager", "employee", "part_time", "student"):
            abort(400, description="role は manager / employee / part_time / student のいずれかを指定してください")
        # 存在チェック
        staff = query_one("SELECT id, role, shop_id FROM staffs WHERE id=? AND shop_id=?", (staff_id, sid))
        if not staff:
            abort(404, description="スタッフが見つかりません")
        old_role = staff["role"]
        # 学生の場合は月80h上限を強制
        extra = {}
        if new_role == "student":
            cur = query_one("SELECT max_hours_per_month FROM staffs WHERE id=?", (staff_id,))
            if cur and (cur.get("max_hours_per_month") or 0) > 80:
                try:
                    execute("UPDATE staffs SET max_hours_per_month=80 WHERE id=?", (staff_id,))
                    extra["max_hours_per_month"] = 80
                except Exception as e:
                    print(f"[admin_role_change] max_hours update failed: {e}", flush=True)
        # ロール変更実行（DBスキーマ古い場合のCHECK制約違反を分かりやすく）
        try:
            execute("UPDATE staffs SET role=? WHERE id=? AND shop_id=?", (new_role, staff_id, sid))
        except Exception as e:
            msg = str(e)
            print(f"[admin_role_change] UPDATE failed: {msg}", flush=True)
            if "CHECK" in msg.upper() and "role" in msg.upper():
                abort(500, description=(
                    "データベースのCHECK制約違反によりロール変更に失敗しました。"
                    "本番DBのスキーマが古い可能性があります（student ロール未対応）。"
                    "Railway側でマイグレーションを実行するか、DBをリセットしてください。"
                    f"（詳細: {msg}）"
                ))
            abort(500, description=f"ロール変更に失敗しました: {msg}")
        # 変更確認（本当に UPDATE されたか検証）
        verify = query_one("SELECT role FROM staffs WHERE id=?", (staff_id,))
        if not verify or verify["role"] != new_role:
            print(f"[admin_role_change] UPDATE silent failure: expected={new_role} actual={verify['role'] if verify else None}", flush=True)
            abort(500, description=f"ロール変更が反映されませんでした（DBの制約またはロックの可能性）。")
        # 既存セッションを無効化（role 変更後は再ログインを強制するため安全）
        execute("DELETE FROM sessions WHERE role IN ('shop','staff') AND user_id=?", (staff_id,))
        audit("staff.role_change", target_type="staff", target_id=staff_id, shop_id=sid,
              detail=f"{old_role}->{new_role}")
        return jsonify({"ok": True, "staff_id": staff_id, "old_role": old_role, "new_role": new_role,
                        "verified_role": verify["role"], **extra})


    @app.put("/api/admin/shops/<int:sid>/staffs/<int:staff_id>")
    def admin_shop_staff_update(sid, staff_id):
        """システム管理者がスタッフ属性を汎用編集する（部分更新可）。

        body: name / role / hourly_wage / min_hours_per_month /
              max_hours_per_month / is_resigned（いずれも任意）
        role は既存の専用エンドポイントと違いセッション無効化は行わない
        （氏名や時給の軽微な編集を想定）。student は月80h上限を強制。
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        staff = query_one("SELECT * FROM staffs WHERE id=? AND shop_id=?", (staff_id, sid))
        if not staff:
            abort(404, description="スタッフが見つかりません")
        fields = {}
        if body.get("name"):
            fields["name"] = body["name"]
        if "role" in body and body.get("role") is not None:
            if body["role"] not in ("manager", "employee", "part_time", "student"):
                abort(400, description="role は manager / employee / part_time / student のいずれかを指定してください")
            fields["role"] = body["role"]
        if "hourly_wage" in body:
            fields["hourly_wage"] = int(body["hourly_wage"] or 0)
        if "min_hours_per_month" in body:
            fields["min_hours_per_month"] = int(body["min_hours_per_month"] or 0)
        if "max_hours_per_month" in body:
            fields["max_hours_per_month"] = int(body["max_hours_per_month"] or 0)
        if "is_resigned" in body:
            fields["is_resigned"] = 1 if body["is_resigned"] else 0
        # student は月80h上限を強制（実効ロールで判定）
        effective_role = fields.get("role", staff["role"])
        if effective_role == "student":
            cur_max = fields.get("max_hours_per_month", staff.get("max_hours_per_month") or 0)
            if cur_max > 80:
                fields["max_hours_per_month"] = 80
        if not fields:
            return jsonify({"ok": True, "updated": 0})
        sets = ", ".join(f"{k}=?" for k in fields)
        execute(f"UPDATE staffs SET {sets} WHERE id=? AND shop_id=?",
                tuple(fields.values()) + (staff_id, sid))
        audit("staff.update", target_type="staff", target_id=staff_id, shop_id=sid,
              detail=",".join(fields.keys()))
        return jsonify({"ok": True, "updated": len(fields)})


    @app.get("/api/admin/audit-logs")
    def admin_audit_logs():
        """監査ログ一覧（新しい順）。shop / action でフィルタ可、既定 limit=100・上限500。"""
        require_auth(["admin"])
        shop = request.args.get("shop")
        action = request.args.get("action")
        try:
            limit = min(int(request.args.get("limit", 100)), 500)
        except (TypeError, ValueError):
            limit = 100
        where = []
        params = []
        if shop:
            where.append("shop_id=?")
            params.append(shop)
        if action:
            where.append("action=?")
            params.append(action)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        rows = query_all(f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT ?", tuple(params))
        return jsonify({"logs": rows})


    @app.get("/api/admin/debug/db-schema")
    def admin_debug_db_schema():
        """デバッグ用: 本番DBの staffs テーブル構造を確認（スキーマが古いかチェック）。"""
        require_auth(["admin"])
        try:
            rows = query_all("SELECT sql FROM sqlite_master WHERE name='staffs'")
            schema = rows[0]["sql"] if rows else "(staffs table not found)"
        except Exception as e:
            schema = f"(error: {e})"
        # staffs 行数と role 列の分布
        try:
            role_stats = query_all("SELECT role, COUNT(*) as cnt FROM staffs GROUP BY role")
        except Exception as e:
            role_stats = [{"role": "(error)", "cnt": 0, "error": str(e)}]
        # shop_holidays テーブルの存在確認
        try:
            holiday_table = query_all("SELECT name FROM sqlite_master WHERE name='shop_holidays'")
            has_holidays = len(holiday_table) > 0
        except Exception:
            has_holidays = False
        return jsonify({
            "staffs_schema": schema,
            "role_distribution": role_stats,
            "supports_student_role": "student" in schema.lower(),
            "has_shop_holidays_table": has_holidays,
        })


    @app.post("/api/admin/db/migrate")
    def admin_db_migrate():
        """本番DBを最新スキーマにマイグレーション（安全版）。

        【安全策】
        1. PRAGMA foreign_keys = OFF（接続単位）
        2. BEGIN TRANSACTION で囲む（DDLもロールバック可能）
        3. CREATE staffs_new → INSERT FROM staffs → DROP staffs → RENAME
        4. 既存 staffs の列構成を動的取得して互換性保持
        5. COMMIT 後 foreign_keys = ON に戻す
        """
        require_auth(["admin"])
        import sqlite3 as _sqlite3
        log = []
        # 1. 現在の staffs テーブル状態確認
        try:
            rows = query_all("SELECT sql FROM sqlite_master WHERE name='staffs'")
            cur_schema = rows[0]["sql"] if rows else ""
        except Exception as e:
            cur_schema = ""
            log.append(f"⚠ staffs スキーマ取得エラー: {e}")

        # 既存 staffs が無い or 壊れている場合、まず復元を試みる
        if not cur_schema or "staffs" not in cur_schema.lower():
            log.append("⚠ staffs テーブルが見つかりません。復元モードに移行します。")
            restore_result = _restore_staffs_table_internal(log)
            if not restore_result:
                return jsonify({"ok": False, "error": "staffs 復元に失敗しました", "log": log}), 500
            # 復元後のスキーマ再取得
            try:
                rows = query_all("SELECT sql FROM sqlite_master WHERE name='staffs'")
                cur_schema = rows[0]["sql"] if rows else ""
            except Exception:
                cur_schema = ""

        needs_rebuild = "student" not in (cur_schema or "").lower()
        if needs_rebuild:
            log.append("staffs テーブルを 'student' ロール対応版に再構築します...")
            conn = db_module_get_conn()
            try:
                # 外部キーを一時OFF（接続単位）
                conn.execute("PRAGMA foreign_keys = OFF")
                cur = conn.cursor()
                # 既存 staffs の列を動的取得（互換性のため）
                cur.execute("PRAGMA table_info(staffs)")
                existing_cols = [r[1] for r in cur.fetchall()]
                log.append(f"既存 staffs の列: {existing_cols}")
                # 必要な列だけ安全にコピー
                target_cols = ['id', 'shop_id', 'staff_code', 'password_hash', 'name', 'role',
                               'hourly_wage', 'min_hours_per_month', 'max_hours_per_month',
                               'is_resigned', 'created_at']
                common_cols = [c for c in target_cols if c in existing_cols]
                col_list = ', '.join(common_cols)
                log.append(f"コピー対象列: {common_cols}")

                cur.execute("BEGIN")
                # バックアップ
                cur.execute("DROP TABLE IF EXISTS staffs_migrate_backup")
                cur.execute("CREATE TABLE staffs_migrate_backup AS SELECT * FROM staffs")
                # 新スキーマで作成
                cur.execute("DROP TABLE IF EXISTS staffs_new")
                cur.execute("""
                    CREATE TABLE staffs_new (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      shop_id INTEGER NOT NULL,
                      staff_code TEXT NOT NULL,
                      password_hash TEXT NOT NULL,
                      name TEXT NOT NULL,
                      role TEXT DEFAULT 'part_time'
                        CHECK(role IN ('employee','part_time','manager','student')),
                      hourly_wage INTEGER DEFAULT 1000,
                      min_hours_per_month INTEGER DEFAULT 0,
                      max_hours_per_month INTEGER DEFAULT 160,
                      is_resigned INTEGER DEFAULT 0,
                      created_at TEXT DEFAULT (datetime('now')),
                      UNIQUE(shop_id, staff_code),
                      FOREIGN KEY (shop_id) REFERENCES shops(id)
                    )
                """)
                # データコピー（共通列のみ・足りない列はデフォルト値）
                if common_cols:
                    cur.execute(f"INSERT INTO staffs_new ({col_list}) SELECT {col_list} FROM staffs")
                    copy_count = cur.execute("SELECT COUNT(*) FROM staffs_new").fetchone()[0]
                    log.append(f"✓ {copy_count} 行コピー完了")
                # 入れ替え
                cur.execute("DROP TABLE staffs")
                cur.execute("ALTER TABLE staffs_new RENAME TO staffs")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_staffs_shop ON staffs(shop_id)")
                cur.execute("COMMIT")
                conn.commit()
                log.append("✓ staffs テーブルを再構築しました（student ロール対応）")
            except Exception as e:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                log.append(f"✗ staffs 再構築失敗: {e}")
                # staffs_backup から自動復元
                log.append("自動復元を試みます...")
                _restore_staffs_table_internal(log)
                return jsonify({"ok": False, "error": str(e), "log": log}), 500
            finally:
                try:
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.close()
                except Exception:
                    pass
        else:
            log.append("✓ staffs テーブルは既に 'student' ロール対応済み")

        # 2. shop_holidays テーブル確認
        try:
            holiday_rows = query_all("SELECT name FROM sqlite_master WHERE name='shop_holidays'")
            if not holiday_rows:
                conn = db_module_get_conn()
                try:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS shop_holidays (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          shop_id INTEGER NOT NULL,
                          holiday_date TEXT NOT NULL,
                          note TEXT,
                          UNIQUE(shop_id, holiday_date),
                          FOREIGN KEY (shop_id) REFERENCES shops(id)
                        )
                    """)
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_holidays_shop ON shop_holidays(shop_id, holiday_date)")
                    conn.commit()
                    log.append("✓ shop_holidays テーブルを新規作成しました")
                finally:
                    try: conn.close()
                    except Exception: pass
            else:
                log.append("✓ shop_holidays テーブルは存在します")
        except Exception as e:
            log.append(f"⚠ shop_holidays 確認/作成でエラー: {e}")

        # 3. 各店舗に manager スタッフが無ければ shops.password_hash を引き継いで作成
        try:
            shops = query_all("SELECT id, shop_code, shop_name, password_hash FROM shops")
            auto_created = 0
            for shop in shops:
                existing = query_one(
                    "SELECT id FROM staffs WHERE shop_id=? AND role='manager'",
                    (shop["id"],))
                if existing:
                    continue
                try:
                    execute(
                        "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role, "
                        "hourly_wage, min_hours_per_month, max_hours_per_month) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (shop["id"], "manager", shop["password_hash"],
                         shop["shop_name"] + " 店主", "manager", 2000, 0, 200))
                    auto_created += 1
                    log.append(f"✓ 店舗 '{shop['shop_code']}' に manager スタッフを自動作成（PW引継ぎ）")
                except Exception as e:
                    log.append(f"⚠ 店舗 '{shop['shop_code']}' の manager 作成スキップ: {e}")
            if auto_created == 0:
                log.append("✓ 全店舗に manager スタッフが存在します")
        except Exception as e:
            log.append(f"⚠ manager 自動作成でエラー: {e}")

        # 最終状態を確認
        try:
            final = query_all("SELECT sql FROM sqlite_master WHERE name='staffs'")
            final_schema = final[0]["sql"] if final else ""
        except Exception:
            final_schema = ""
        return jsonify({
            "ok": True,
            "log": log,
            "migrated_staffs_table": needs_rebuild,
            "final_schema": final_schema,
            "supports_student_role": "student" in final_schema.lower(),
        })


    @app.post("/api/admin/db/restore-staffs")
    def admin_db_restore_staffs():
        """staffs テーブル消失時の緊急復元（単独エンドポイント）。"""
        require_auth(["admin"])
        log = []
        ok = _restore_staffs_table_internal(log)
        return jsonify({"ok": ok, "log": log})


    @app.get("/api/admin/db/diagnostic")
    def admin_db_diagnostic():
        """DBの全テーブル一覧と各スキーマを表示（障害調査用）。"""
        require_auth(["admin"])
        try:
            tables = query_all("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
        return jsonify({"ok": True, "tables": tables})


    @app.put("/api/admin/shops/<int:sid>/staffs/<int:staff_id>/password")
    def admin_shop_staff_reset_password(sid, staff_id):
        """システム管理者がスタッフのパスワードをリセット。

        body: {"new_password": "..."}
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        pw = body.get("new_password") or ""
        err = validate_password(pw)
        if err:
            abort(400, description=err)
        staff = query_one("SELECT id FROM staffs WHERE id=? AND shop_id=?", (staff_id, sid))
        if not staff:
            abort(404, description="スタッフが見つかりません")
        execute("UPDATE staffs SET password_hash=? WHERE id=?", (hash_password(pw), staff_id))
        # パスワード変更後は既存セッションを無効化
        execute("DELETE FROM sessions WHERE role IN ('shop','staff') AND user_id=?", (staff_id,))
        audit("staff.password_reset", target_type="staff", target_id=staff_id, shop_id=sid)
        return jsonify({"ok": True})


    @app.post("/api/admin/shops/<int:sid>/staffs")
    def admin_shop_staffs_post(sid):
        """システム管理者が店舗にスタッフを追加。

        body:
          - staff_code: 必須（任意の文字列）
          - name: 必須
          - password: 必須
          - role: manager/employee/part_time/student（デフォルト part_time）
          - hourly_wage: 数値（デフォルト 1000）
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        if not body.get("staff_code"):
            abort(400, description="ユーザーコードを入力してください")
        if not body.get("name"):
            abort(400, description="氏名を入力してください")
        pw = body.get("password") or ""
        err = validate_password(pw)
        if err:
            abort(400, description=err)
        role = body.get("role") or "part_time"
        if role not in ("manager", "employee", "part_time", "student"):
            abort(400, description="role は manager / employee / part_time / student のいずれかを指定してください")
        # 店舗存在確認
        shop = query_one("SELECT id FROM shops WHERE id=?", (sid,))
        if not shop:
            abort(404, description="店舗が見つかりません")
        # 重複チェック
        dup = query_one("SELECT id FROM staffs WHERE shop_id=? AND staff_code=?", (sid, body["staff_code"]))
        if dup:
            abort(400, description=f"ユーザーコード '{body['staff_code']}' は既に存在します")
        # 学生アルバイトは80h上限
        max_hours = 80 if role == "student" else (body.get("max_hours_per_month") or 160)
        meta = execute(
            "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role, hourly_wage, "
            "min_hours_per_month, max_hours_per_month) VALUES (?,?,?,?,?,?,?,?)",
            (sid, body["staff_code"], hash_password(pw), body["name"], role,
             body.get("hourly_wage") or 1000, 0, max_hours))
        audit("staff.create", target_type="staff", target_id=meta["last_row_id"], shop_id=sid,
              detail=body.get("name"))
        return jsonify({"ok": True, "id": meta["last_row_id"], "role": role,
                        "staff_code": body["staff_code"], "name": body["name"]})


    @app.post("/api/admin/shops/<int:sid>/migrate-legacy-manager")
    def admin_shop_migrate_legacy_manager(sid):
        """旧仕様の店主ログインを新仕様の manager スタッフに昇格。

        shops テーブルの password_hash を引き継いだ manager ロールのスタッフを
        作成する。これにより、パスワードを再設定せずに新仕様へ移行できる。

        body:
          - staff_code: 必須（任意の文字列。例: 'manager', 'yamada' 等）
          - name: 未指定時は shop_name + ' 店主'
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        staff_code = (body.get("staff_code") or "").strip()
        if not staff_code:
            abort(400, description="ユーザーコードを入力してください")
        shop = query_one("SELECT * FROM shops WHERE id=?", (sid,))
        if not shop:
            abort(404, description="店舗が見つかりません")
        # 既に同じ staff_code が存在する場合はエラー
        dup = query_one("SELECT id FROM staffs WHERE shop_id=? AND staff_code=?", (sid, staff_code))
        if dup:
            abort(400, description=f"ユーザーコード '{staff_code}' は既に存在します")
        name = body.get("name") or (shop["shop_name"] + " 店主")
        # shops テーブルの password_hash を引き継ぐ
        meta = execute(
            "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role, "
            "hourly_wage, min_hours_per_month, max_hours_per_month) VALUES (?,?,?,?,?,?,?,?)",
            (sid, staff_code, shop["password_hash"], name, "manager",
             body.get("hourly_wage") or 2000, 0, body.get("max_hours_per_month") or 200))
        return jsonify({"ok": True, "id": meta["last_row_id"],
                        "shop_code": shop["shop_code"], "shop_name": shop["shop_name"],
                        "staff_code": staff_code, "name": name,
                        "note": "shops テーブルのパスワードを引き継ぎました。同じパスワードでログインできます。"})


    @app.get("/api/admin/shops/<int:sid>/periods/next")
    def admin_shop_next_period(sid):
        require_auth(["admin"])
        row = query_one(
            "SELECT start_date, end_date, deadline FROM shift_request_periods "
            "WHERE shop_id=? AND is_active=1 AND end_date>=date('now') ORDER BY end_date LIMIT 1",
            (sid,))
        if row:
            return jsonify(row)
        p = calc_next_period()
        return jsonify({"start_date": p["start_date"], "end_date": p["end_date"], "deadline": p["deadline"]})


    @app.get("/api/admin/shops/summary/<int:sid>")
    def admin_shop_summary(sid):
        require_auth(["admin"])
        start_d = request.args.get("start"); end_d = request.args.get("end")
        if not start_d or not end_d:
            abort(400, description="start, end が必要")
        shifts = query_all("SELECT sh.*, s.name as staff_name FROM shifts sh JOIN staffs s ON sh.staff_id=s.id WHERE sh.shop_id=? AND sh.start_datetime>=? AND sh.start_datetime<=?",
                           (sid, start_d + "T00:00:00", end_d + "T23:59:59"))
        shop = query_one("SELECT settings FROM shops WHERE id=?", (sid,))
        staffs = query_all("SELECT id, name, role, hourly_wage FROM staffs WHERE shop_id=?", (sid,))
        return jsonify(summarize_shifts(shifts, {s["id"]: s for s in staffs}, parse_settings(shop["settings"])))


    @app.get("/api/admin/notifications")
    def admin_notifs():
        require_auth(["admin"])
        # システム管理者向け通知は現状なし（空リストを返す）。
        # Phase 2 で一斉通知の配信履歴を返す実装に置き換える。
        return jsonify({"notifications": [], "unread": 0})


    @app.put("/api/admin/notifications/read-all")
    def admin_notifs_readall():
        require_auth(["admin"])
        return jsonify({"ok": True})


    @app.put("/api/admin/password")
    def admin_change_password():
        """システム管理者が自分のパスワードを変更する。

        【なぜ必要か】
          system_admins への UPDATE がコード全体でゼロで、変更手段が存在しなかった。
          /api/init の初期パスワードはランダム生成（S4）なので、手段が無いままだと
          発行された値を一生使い続けることになる。
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        current = body.get("current_password") or ""
        new_pw = body.get("new_password") or ""
        admin_id = (getattr(g, "user", None) or {}).get("id")
        row = query_one("SELECT id, admin_id, password_hash FROM system_admins WHERE id=?",
                        (admin_id,))
        if row is None:
            abort(404, description="管理者が見つかりません")
        if not verify_password(current, row["password_hash"]):
            raise ValueError("現在のパスワードが正しくありません")
        msg = validate_password(new_pw)
        if msg:
            raise ValueError(msg)
        execute("UPDATE system_admins SET password_hash=? WHERE id=?",
                (hash_password(new_pw), admin_id))
        # 他端末のセッションは失効させる。自分の今のセッションだけ残す
        # （変更直後に再ログインを強いられるのは体験が悪いため）。
        token = request.headers.get("Authorization", "")[7:]
        execute("DELETE FROM sessions WHERE role='admin' AND user_id=? AND token<>?",
                (admin_id, token))
        audit("admin.password_change", target_type="system_admin", target_id=admin_id)
        return jsonify({"ok": True})

