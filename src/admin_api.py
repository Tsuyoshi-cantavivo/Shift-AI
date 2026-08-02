"""admin_api.py - システム管理者向け API (/api/admin/*)。

src/app.py が4000行超で保守が難しくなっていたため切り出した。Blueprint は使わず、
register_admin_routes(app, ...) の中で既存と同じ @app.get/post デコレータで登録する
（url_prefix 等の新しい概念を持ち込まないため）。

app.py 側にしか無いヘルパ（require_auth / audit / summarize_shifts / csv_safe）は
循環 import を避けるためキーワード引数で受け取る。csv_safe は CSV の
Formula Injection 対策関数の実体で、二重管理を避けるため再実装しない。

【削除済み】POST /api/admin/db/migrate と POST /api/admin/db/restore-staffs、および
その内部ヘルパ（db_module_get_conn / _restore_staffs_table_internal）は削除した。
どちらも db.get_conn() → _get_local_conn() を使っており、この関数は DB_MODE を
無視して常に sqlite3.connect(DB_PATH) を返す。つまり本番（d1モード）で叩くと
本番D1ではなくコンテナ内のローカルSQLiteに対して DROP TABLE staffs が走り、
しかも ok: true を返していた。フロントからの呼び出し元は0件で、スキーマ変更の
正規の経路は src/migrator.py（GET/POST /api/admin/migrations）。
読み取り専用の GET /api/admin/db/diagnostic はシステム画面のDB診断タブが
使っているため残している。
"""
from flask import request, jsonify, abort, g, Response

from db import query_all, query_one, execute, insert_row
from auth import hash_password, verify_password, strip_password
from utils import (calc_next_period, jst_now, parse_settings, validate_password,
                    sanitize_login_code, LOGIN_CODE_MAX, validate_known_settings_values,
                    validate_numeric_field, SETTINGS_KEYS)
import json
import re
import secrets
import migrator
from datetime import datetime


# Cloudflare D1 は「1クエリあたりのバインドパラメータ100個」を上限としており、
# 超えるとクエリ自体が拒否される（公式ドキュメント記載）。本番は DB_MODE=d1 なので、
# 束ねた INSERT や IN (...) の展開は必ずこの数以下に分割する必要がある。
# ローカル SQLite は 500,000 変数まで許すため、行数を増やしただけのテストでは
# この壁を絶対に検出できない（テストは「1回あたりのバインド数」を数えている）。
D1_MAX_BINDS = 100


def chunked(seq, size):
    """seq を size 件ずつのリストに切り出して yield する（バインド上限対策）。"""
    if size < 1:
        raise ValueError("チャンクサイズは1以上で指定してください")
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def register_admin_routes(app, *, require_auth, audit, summarize_shifts, csv_safe):
    """/api/admin/* の全ルートを app に登録する。"""

    @app.get("/api/admin/dashboard")
    def admin_dashboard():
        """全社ダッシュボード。KPI・要対応リスト・直近の監査ログをまとめて返す。

        店舗数だけループして店舗ごとにクエリを投げる素朴な実装。本番は D1
        （1文ごとにHTTP往復）のため店舗数が増えるとレイテンシが線形に増える点は
        既知の懸念（現状は2店舗のみで実用上問題ない）。将来的に件数が増えたら
        集計クエリへの書き換えを検討する。
        """
        require_auth(["admin"])
        now = jst_now()
        today = now.strftime("%Y-%m-%d")
        month_start = now.strftime("%Y-%m-01")
        # 翌月月初（年またぎを考慮）。「今月の確定シフト」の上限に使う。
        # この製品の通常運用では締切前に翌月分を先に確定するため、上限が無いと
        # 「今月」の看板で翌月分まで数えてしまい、当月の確定状況を過大評価する
        # （レビューで実DB上62件がすべて8月分＝翌月分だったことが判明した不具合）。
        next_year = now.year + 1 if now.month == 12 else now.year
        next_month = 1 if now.month == 12 else now.month + 1
        month_end = f"{next_year:04d}-{next_month:02d}-01"

        shops = query_all("SELECT id, shop_name, shop_code, is_active, "
                          "COALESCE(is_archived,0) AS is_archived FROM shops")
        active = [s for s in shops if not s["is_archived"] and s["is_active"]]
        inactive = [s for s in shops if not s["is_archived"] and not s["is_active"]]
        archived = [s for s in shops if s["is_archived"]]
        live_ids = {s["id"] for s in shops if not s["is_archived"]}

        staffs_total = query_one(
            "SELECT COUNT(*) AS c FROM staffs WHERE is_resigned=0")["c"]
        confirmed = query_one(
            "SELECT COUNT(*) AS c FROM shifts WHERE status='confirmed' "
            "AND start_datetime>=? AND start_datetime<?",
            (month_start + "T00:00:00", month_end + "T00:00:00"))["c"]

        attention = []

        # 1. 締切を過ぎているのにシフトが未確定の店舗
        rows = query_all(
            "SELECT p.shop_id, p.start_date, p.end_date, p.deadline "
            "FROM shift_request_periods p WHERE p.is_active=1 AND p.deadline < ?", (today,))
        by_id = {s["id"]: s for s in shops}
        for p in rows:
            if p["shop_id"] not in live_ids:
                continue
            has = query_one(
                "SELECT 1 AS x FROM shifts WHERE shop_id=? AND status='confirmed' "
                "AND start_datetime>=? AND start_datetime<=? LIMIT 1",
                (p["shop_id"], p["start_date"] + "T00:00:00", p["end_date"] + "T23:59:59"))
            if not has:
                shop = by_id.get(p["shop_id"], {})
                attention.append({"kind": "deadline_passed", "shop_id": p["shop_id"],
                                  "shop_name": shop.get("shop_name"),
                                  "detail": f"締切 {p['deadline']} を過ぎていますが未確定です"})

        # 2. manager が1人もいない店舗（＝誰もログインできない）
        for s in shops:
            if s["is_archived"]:
                continue
            has_mgr = query_one(
                "SELECT 1 AS x FROM staffs WHERE shop_id=? AND role='manager' AND is_resigned=0 LIMIT 1",
                (s["id"],))
            if not has_mgr:
                attention.append({"kind": "no_manager", "shop_id": s["id"],
                                  "shop_name": s["shop_name"],
                                  "detail": "店舗管理者がいないため誰もログインできません"})

        # 3. 30日以上どのユーザーもログインしていない店舗
        for s in shops:
            if s["is_archived"]:
                continue
            last = query_one(
                "SELECT MAX(created_at) AS last FROM audit_logs "
                "WHERE shop_id=? AND action='auth.login'", (s["id"],))
            last_at = (last or {}).get("last")
            if not last_at:
                continue  # ログイン記録が一度も無い場合は判定しない（導入直後の誤検知を避ける）
            try:
                days = (now - datetime.strptime(last_at, "%Y-%m-%d %H:%M:%S")).days
            except (ValueError, TypeError):
                continue
            if days >= 30:
                attention.append({"kind": "stale_login", "shop_id": s["id"],
                                  "shop_name": s["shop_name"],
                                  "detail": f"{days}日間ログインがありません"})

        # 4. 未適用のマイグレーション
        pending = [m for m in migrator.status() if not m["applied"]]
        if pending:
            attention.append({"kind": "pending_migration", "shop_id": None, "shop_name": None,
                              "detail": f"未適用のマイグレーションが {len(pending)} 件あります"})

        recent = query_all(
            "SELECT created_at, actor_role, actor_name, action, detail FROM audit_logs "
            "ORDER BY id DESC LIMIT 10")

        return jsonify({
            "kpi": {"shops_total": len(shops), "shops_active": len(active),
                    "shops_inactive": len(inactive), "shops_archived": len(archived),
                    "staffs_total": staffs_total, "confirmed_this_month": confirmed,
                    "attention_count": len(attention)},
            "attention": attention,
            "recent_audit": recent,
        })

    @app.get("/api/admin/shops")
    def admin_shops():
        require_auth(["admin"])
        # is_archived も返す。管理画面（店舗詳細タブ）がアーカイブ済みバッジの表示や
        # 「エクスポート必須→完全削除」の活性判定に使うため、フィルタ用途だけでなく
        # 各行の値としても必要。COALESCE で NULL を 0 に正規化するのは下の WHERE と同じ理由。
        cols = "id, shop_code, shop_name, is_active, settings, created_at, COALESCE(is_archived,0) AS is_archived"
        # アーカイブ済みは既定で隠す。運営が普段見るのは稼働中の店舗だけのため。
        # COALESCE を使うのは、ALTER TABLE ADD COLUMN 以前からある行の is_archived が
        # NULL になり得るため。
        if request.args.get("include_archived") == "1":
            rows = query_all(f"SELECT {cols} FROM shops ORDER BY is_archived, id")
        else:
            rows = query_all(f"SELECT {cols} FROM shops WHERE COALESCE(is_archived,0)=0 ORDER BY id")
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
        # settings の値の型検証（保存型XSS対策）。ここは PUT /api/admin/shops/<id>/settings
        # と違い未知キーは拒否しない ——
        # tests/test_admin_staff_apis.py::test_admin_create_and_update_shop が
        # settings={"x": 1}（未知キー）の作成を 200 で期待する既存契約があるため。
        # 既知キーの値だけ型を締めれば、汚染レコードは作れなくなる。
        if body.get("settings"):
            validate_known_settings_values(body["settings"])
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


    # shops.settings で受け付けるキー。src/utils.py の SETTINGS_KEYS をそのまま使う
    # （店舗側 PUT /api/shop/settings の値検証と定義を共有し、キー集合がドリフトしないようにする）。
    # 未知のキーを弾くのは、タイプミスが黙って保存されてシフト生成に効かない事故を防ぐため。
    _SETTINGS_KEYS = SETTINGS_KEYS

    @app.post("/api/admin/shops/<int:sid>/archive")
    def admin_shop_archive(sid):
        require_auth(["admin"])
        shop = query_one("SELECT id, shop_code FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        now = jst_now().strftime("%Y-%m-%d %H:%M:%S")
        execute("UPDATE shops SET is_archived=1, archived_at=?, is_active=0 WHERE id=?", (now, sid))
        # ログイン中のユーザーを追い出す（アーカイブ後もセッションが生きていると
        # 停止したはずの店舗が操作できてしまう）
        execute("DELETE FROM sessions WHERE shop_id=?", (sid,))
        audit("shop.archive", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={shop['shop_code']}")
        return jsonify({"ok": True})


    @app.post("/api/admin/shops/<int:sid>/unarchive")
    def admin_shop_unarchive(sid):
        require_auth(["admin"])
        shop = query_one("SELECT id, shop_code FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        # is_active は 0 のまま。復元と再稼働は別の判断なので明示的に有効化させる。
        execute("UPDATE shops SET is_archived=0, archived_at=NULL WHERE id=?", (sid,))
        audit("shop.unarchive", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={shop['shop_code']}")
        return jsonify({"ok": True})


    # ---- 店舗のエクスポートと完全削除 ----
    # 店舗に紐づくテーブル。削除は FK の依存順（子→親）に実行する。
    # 依存関係（schema.sql の FOREIGN KEY 定義）:
    #   shift_pattern_weekday_required → shift_patterns
    #   change_requests / wish_history / shifts / fixed_shifts → staffs
    #   staffs / shift_patterns / shift_request_periods / shop_holidays → shops
    # sessions / notifications は FK を持たないが、店舗の痕跡なので同時に消す。
    # fixed_shifts は shop_id を持たず staff_id 経由でしか辿れないため別扱い。
    # audit_logs はここに含めない（下の delete のコメント参照）。
    _SHOP_SCOPED_TABLES = [
        "sessions", "notifications", "change_requests", "wish_history", "shifts",
        "shift_pattern_weekday_required", "shift_patterns", "shift_request_periods",
        "shop_holidays",
    ]

    # staff_id 列を持つテーブル（schema.sql で全数確認）。shop_id だけで絞ると
    # 孤児行（shop_id が NULL や不整合で staff_id だけが対象店舗を指す行）を消し残す。
    # shifts / change_requests / wish_history は staffs への FK を持つため、消し残すと
    # 直後の DELETE FROM staffs が FK 違反で落ち、「子テーブルだけ消えて店舗行は残る」
    # 状態から API では二度と完了できなくなる（手動SQLでしか直せない）。
    # notifications は FK が無いので削除を止めはしないが、消えたスタッフ宛の通知が
    # 残り続けるゴミなので同じ条件で掃除する。
    # ? は 2 個しか増えないので D1 のバインド上限(100)には触れない。
    _STAFF_LINKED_TABLES = {"notifications", "change_requests", "wish_history", "shifts"}
    _STAFF_SUBQUERY = "staff_id IN (SELECT id FROM staffs WHERE shop_id=?)"

    # shift_pattern_weekday_required も同じ構図。shop_id は NOT NULL で、挿入経路
    # （src/app.py のパターン曜日別必要人数の保存）は必ずパターンの所有店舗を検証してから
    # 同じ shop_id を入れるので到達経路は無いが、pattern_id だけが対象店を指す行が
    # あると DELETE FROM shift_patterns が FK 違反で詰まる。他の4テーブルと形を
    # 揃えておかないと、読む人が「なぜここだけ違うのか」を毎回確認することになる。
    _PATTERN_LINKED_TABLES = {"shift_pattern_weekday_required"}
    _PATTERN_SUBQUERY = "pattern_id IN (SELECT id FROM shift_patterns WHERE shop_id=?)"

    def _scope_clause(table):
        """テーブルの行を店舗に結びつける WHERE 句と、必要な ? の数を返す。"""
        if table in _STAFF_LINKED_TABLES:
            return f"(shop_id=? OR {_STAFF_SUBQUERY})", 2
        if table in _PATTERN_LINKED_TABLES:
            return f"(shop_id=? OR {_PATTERN_SUBQUERY})", 2
        return "shop_id=?", 1

    def _collect_shop_data(sid):
        """店舗に属する全行を dict で集める。password_hash は含めない。"""
        shop = query_one("SELECT * FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        data = {"shop": strip_password(shop), "exported_at": jst_now().strftime("%Y-%m-%d %H:%M:%S")}
        data["staffs"] = [strip_password(r) for r in
                          query_all("SELECT * FROM staffs WHERE shop_id=?", (sid,))]
        for table in _SHOP_SCOPED_TABLES:
            if table == "sessions":
                continue  # セッションは機微情報で復元価値も無い
            # 削除と同じ条件で引く。ここだけ shop_id で絞ると、削除される孤児行が
            # バックアップに入らない（消える行はすべて控えに残す必要がある）。
            clause, n = _scope_clause(table)
            data[table] = query_all(f"SELECT * FROM {table} WHERE {clause}", (sid,) * n)
        data["fixed_shifts"] = query_all(
            "SELECT fs.* FROM fixed_shifts fs JOIN staffs s ON fs.staff_id=s.id WHERE s.shop_id=?",
            (sid,))
        return data

    def _safe_filename_part(code, sid):
        """Content-Disposition の filename に埋め込める形に落とす。

        shop_code は運営が自由入力できるため、" が入るとヘッダのクォートが壊れ、
        日本語が入ると環境によってファイル名が化ける。英数字と - _ 以外は _ にする。
        """
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", code or "")
        return safe or str(sid)

    @app.get("/api/admin/shops/<int:sid>/export")
    def admin_shop_export(sid):
        require_auth(["admin"])
        data = _collect_shop_data(sid)
        code = data["shop"].get("shop_code") or str(sid)
        audit("shop.export", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={code}")
        body = json.dumps(data, ensure_ascii=False, indent=2)
        filename = f"shop-{_safe_filename_part(code, sid)}-{jst_now().strftime('%Y%m%d')}.json"
        resp = Response(body, content_type="application/json; charset=utf-8")
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    @app.delete("/api/admin/shops/<int:sid>")
    def admin_shop_delete(sid):
        require_auth(["admin"])
        shop = query_one("SELECT id, shop_code, is_archived FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        # アーカイブ済みを必須にするのは、削除の直前に「もう使っていない」という
        # 判断が1回入るようにするため（1操作で顧客データが消えるのを防ぐ二段構え）。
        if not shop.get("is_archived"):
            raise ValueError("先にアーカイブしてください（誤削除を防ぐため）")
        body = request.get_json(silent=True) or {}
        # 店舗コードの手入力一致を求めるのは、隣の行を押し間違えても消えないようにするため。
        if (body.get("confirm_code") or "").strip() != shop["shop_code"]:
            raise ValueError("店舗コードが一致しません")
        # エクスポート済みを必須にする（is_archived / confirm_code に続く3つ目の条件）。
        # このルールは public/admin.js のボタン disabled にしか無く、直接APIを叩けば
        # 控えを1度も取らずに完全削除が通っていた。ページをリロードしてタブを開き直す
        # だけでも画面の状態は初期化されるため、UI 側だけでは守れない。
        # 対象店舗の shop.export 監査が1件でもあることを条件にする（他店舗の
        # エクスポートで解錠されないよう target_id と shop_id の両方で絞る）。
        if query_one("SELECT id FROM audit_logs WHERE action='shop.export' "
                     "AND target_id=? AND shop_id=? LIMIT 1", (sid, sid)) is None:
            raise ValueError(
                "先にエクスポートしてください（取り返しのつかない操作のため、"
                "控えの無い削除は許可していません）")

        code = shop["shop_code"]
        # 破壊を始める「前」に監査へ1行残す。execute() は毎回 commit するため
        # トランザクションが張れず（src/db.py の execute）、途中で失敗すると
        # 子テーブルだけ消えた状態で終わる。完了後にしか記録しないと、その状態で
        # 「誰がいつどの店舗を消し始めたか」がどこにも残らない。
        # audit_logs は消さない。運営の記録であり、店舗が消えた事実こそ残す必要がある。
        # 店舗行が消えると shop_id から店舗コードを引けなくなるため、detail に残す。
        # 書く前の最大 id を控える。「今回のリクエストで書けた行」だけを根拠にするため。
        # action と target_id だけで引くと、DELETE が落ちた前回の試行が残した行を
        # 自分の記録と誤認する。すると監査が壊れたままの再実行で店舗が完全に消え、
        # 記録上は「開始したが未完了」なのに実データだけ消えた状態になる。
        high_water = (query_one("SELECT MAX(id) AS m FROM audit_logs") or {}).get("m") or 0
        audit("shop.delete", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={code} の完全削除を開始")
        # audit() は失敗しても業務を止めない設計（src/app.py）なので、握り潰された
        # 場合に備えて書けたことを確認する。記録が残せないなら削除しない
        # ＝「記録の無い破壊」を作らない。
        if query_one("SELECT id FROM audit_logs WHERE action='shop.delete' AND target_id=? "
                     "AND id>? ORDER BY id DESC LIMIT 1", (sid, high_water)) is None:
            abort(500, description="監査ログを記録できなかったため削除を中止しました")

        # NOTE: 上記のとおりトランザクションが張れず、本番の D1 は1文ごとに
        # ネットワーク往復するので途中で失敗し得る。どこまで消したかを返して
        # 再実行できるようにする。既に消えたテーブルへの DELETE は0件で成功するため
        # 再実行は冪等。
        #
        # 全ての DELETE を shop_id（fixed_shifts は staff_id 経由）で絞る。
        # 1つでも条件が外れると無関係の顧客のデータが道連れで消える。
        deleted = []
        counts = {}

        def _run(table, sql, params):
            meta = execute(sql, params)
            deleted.append(table)
            counts[table] = meta.get("changes") or 0

        # fixed_shifts は shop_id を持たないので staff_id 経由で先に消す
        _run("fixed_shifts",
             f"DELETE FROM fixed_shifts WHERE {_STAFF_SUBQUERY}", (sid,))
        for table in _SHOP_SCOPED_TABLES:
            clause, n = _scope_clause(table)
            _run(table, f"DELETE FROM {table} WHERE {clause}", (sid,) * n)
        _run("staffs", "DELETE FROM staffs WHERE shop_id=?", (sid,))
        # staffs_migrate_backup は旧 POST /api/admin/db/migrate が
        # `CREATE TABLE ... AS SELECT * FROM staffs` で作っていた全テナントの控え。
        # エンドポイント自体は削除済み（孤児かつ本番で破壊的だったため）だが、
        # 既に作られた表は自然には消えず、password_hash と氏名を保持し続ける。
        # 削除対象一覧に入っていなかったため、「完全削除」後も削除テナントの
        # 認証情報と PII が残っていた。表が存在するDBでだけ該当店舗の行を消す。
        if query_one("SELECT name FROM sqlite_master WHERE type='table' "
                     "AND name='staffs_migrate_backup'"):
            _run("staffs_migrate_backup",
                 "DELETE FROM staffs_migrate_backup WHERE shop_id=?", (sid,))
        # 物理削除であること（論理削除にしないこと）が重要。require_auth の代理閲覧経路は
        # 「SELECT * FROM shops WHERE id=? が行を引けない」ことを条件に 409 を返して
        # 運営者を脱出させている。論理削除にするとその防御が効かず、削除済みの店舗を
        # 代理閲覧し続けられてしまう。
        _run("shops", "DELETE FROM shops WHERE id=?", (sid,))

        # 何件消したかを残す。店舗行が消えた後はこれが唯一の規模の証跡になる
        # （「本当にこの店だけだったか」を後から確かめる材料）。
        summary = ",".join(f"{t}={counts[t]}" for t in deleted)
        audit("shop.delete_done", target_type="shop", target_id=sid, shop_id=sid,
              detail=f"shop_code={code} の完全削除が完了 {summary}")
        return jsonify({"ok": True, "deleted": deleted, "counts": counts})


    @app.put("/api/admin/shops/<int:sid>/settings")
    def admin_shop_update_settings(sid):
        require_auth(["admin"])
        shop = query_one("SELECT id, settings FROM shops WHERE id=?", (sid,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        body = request.get_json(silent=True) or {}
        # JSON配列・文字列・数値等が送られると body.keys() が AttributeError で
        # 500（生のPython例外文字列漏洩）になっていた。admin_update_shop と同じく
        # 型不正は 400 として扱う（"key" in body 方式ではなく明示チェックにする）。
        if not isinstance(body, dict):
            raise ValueError("settings は JSON オブジェクトで指定してください")
        unknown = set(body.keys()) - _SETTINGS_KEYS
        if unknown:
            raise ValueError(f"未知の設定キーです: {', '.join(sorted(unknown))}")
        # 値の型検証（保存型XSS対策）。管理コンソールの設定タブが shop.settings を
        # JSON.parse() して無エスケープで描画していた箇所があり、数値キーに文字列
        # （HTMLタグを含む値）が保存されると発火した。ここは全キーが既知（直前の
        # unknown チェック済み）なので、body の全キーが検証対象になる。
        validate_known_settings_values(body)
        merged = parse_settings(shop.get("settings"))
        merged.update(body)
        execute("UPDATE shops SET settings=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), sid))
        audit("shop.update", target_type="shop", target_id=sid, shop_id=sid,
              detail="settings:" + ",".join(sorted(body.keys())))
        return jsonify({"ok": True, "settings": merged})


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

        body: {"role": "manager" | "employee" | "part_time" | "student" | "foreign_worker"}
        ※ manager に変更すると、そのスタッフで店舗管理者ログインが可能に。
        ※ student に変更時は月80h上限を強制。
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        new_role = body.get("role")
        if new_role not in ("manager", "employee", "part_time", "student", "foreign_worker"):
            abort(400, description="role は manager / employee / part_time / student / foreign_worker のいずれかを指定してください")
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
            if body["role"] not in ("manager", "employee", "part_time", "student", "foreign_worker"):
                abort(400, description="role は manager / employee / part_time / student / foreign_worker のいずれかを指定してください")
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


    def _audit_filters():
        """クエリ文字列から WHERE 句と bind を組み立てる。

        列名は固定文字列のみを使い、外部入力から組み立てない。値は必ず
        プレースホルダで束縛する（SQL インジェクション対策）。
        """
        where, binds = [], []
        shop = request.args.get("shop")
        if shop:
            where.append("shop_id=?"); binds.append(int(shop))
        action = request.args.get("action")
        if action:
            where.append("action=?"); binds.append(action)
        start = request.args.get("start")
        if start:
            where.append("created_at>=?"); binds.append(start + " 00:00:00")
        end = request.args.get("end")
        if end:
            where.append("created_at<=?"); binds.append(end + " 23:59:59")
        actor = request.args.get("actor")
        if actor:
            where.append("actor_name LIKE ?"); binds.append(f"%{actor}%")
        return where, binds

    @app.get("/api/admin/audit-logs")
    def admin_audit_logs():
        """監査ログ一覧（新しい順）。

        shop / action / start / end / actor でフィルタ可。before_id カーソル方式の
        ページングで、既定 limit=100・上限500。has_more は limit+1 件取って
        超えた分があるかどうかで判定する。
        """
        require_auth(["admin"])
        where, binds = _audit_filters()
        before_id = request.args.get("before_id")
        if before_id:
            where.append("id<?"); binds.append(int(before_id))
        try:
            limit = min(int(request.args.get("limit", 100)), 500)
        except (TypeError, ValueError):
            limit = 100
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        # has_more の判定のため1件多く取る
        rows = query_all(f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT ?",
                          tuple(binds) + (limit + 1,))
        has_more = len(rows) > limit
        return jsonify({"logs": rows[:limit], "has_more": has_more})

    @app.get("/api/admin/audit-logs.csv")
    def admin_audit_logs_csv():
        """監査ログの CSV ダウンロード。画面のページングとは無関係にフィルタ結果全件（上限5000）。

        actor_name にはログイン失敗時の入力コードがそのまま入る（Phase 1）ため
        攻撃者が内容を制御しうる値。csv_safe（= app.py の _csv_safe）で
        Formula Injection (CWE-1236) を防ぐ。
        """
        require_auth(["admin"])
        where, binds = _audit_filters()
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = query_all(f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT 5000",
                          tuple(binds))
        header = ["日時", "操作者ロール", "操作者", "操作", "対象種別", "対象ID", "店舗ID", "詳細"]
        lines = [",".join(csv_safe(h) for h in header)]
        for r in rows:
            lines.append(",".join(csv_safe(v) for v in [
                r.get("created_at"), r.get("actor_role"), r.get("actor_name"), r.get("action"),
                r.get("target_type"), r.get("target_id"), r.get("shop_id"), r.get("detail")]))
        # Excel が UTF-8 と判定できるよう BOM を付ける
        body = "﻿" + "\r\n".join(lines)
        filename = f"audit-logs-{jst_now().strftime('%Y%m%d')}.csv"
        resp = Response(body, content_type="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp


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
            "supports_foreign_worker_role": "foreign_worker" in schema.lower(),
            "has_shop_holidays_table": has_holidays,
        })


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
          - role: manager/employee/part_time/student/foreign_worker（デフォルト part_time）
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
        if role not in ("manager", "employee", "part_time", "student", "foreign_worker"):
            abort(400, description="role は manager / employee / part_time / student / foreign_worker のいずれかを指定してください")
        # 店舗存在確認
        shop = query_one("SELECT id FROM shops WHERE id=?", (sid,))
        if not shop:
            abort(404, description="店舗が見つかりません")
        # 重複チェック
        dup = query_one("SELECT id FROM staffs WHERE shop_id=? AND staff_code=?", (sid, body["staff_code"]))
        if dup:
            abort(400, description=f"ユーザーコード '{body['staff_code']}' は既に存在します")
        # 数値フィールドの型検証（保存型XSS の入口封じ）。店舗側の
        # POST /api/shop/staffs と同じ穴がここにもあった。
        wage = validate_numeric_field(body.get("hourly_wage"), "時給")
        max_in = validate_numeric_field(body.get("max_hours_per_month"), "月間上限時間")
        # 学生アルバイトは80h上限
        max_hours = 80 if role == "student" else (max_in or 160)
        meta = execute(
            "INSERT INTO staffs (shop_id, staff_code, password_hash, name, role, hourly_wage, "
            "min_hours_per_month, max_hours_per_month) VALUES (?,?,?,?,?,?,?,?)",
            (sid, body["staff_code"], hash_password(pw), body["name"], role,
             wage or 1000, 0, max_hours))
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


    # ---- 代理閲覧（閲覧のみ） ----
    # 状態は「自分のセッション行」に持たせる。トークンを差し替える方式だと
    # フロントが2本のトークンを二重管理することになり、戻し忘れ事故が起きるため。
    @app.post("/api/admin/impersonate/<int:shop_id>")
    def admin_impersonate_start(shop_id):
        require_auth(["admin"])
        shop = query_one("SELECT id, shop_code, shop_name, is_active, is_archived FROM shops WHERE id=?",
                         (shop_id,))
        if shop is None:
            abort(404, description="店舗が見つかりません")
        # require_auth が読んだ sessions 行を使い回す（本番D1は1文ごとにHTTP往復する）。
        # UPDATE を token で絞るのは必須。role='admin' で絞ると他の運営者のセッションまで
        # 巻き込み、押していない管理者が勝手に代理状態になる。
        sess = getattr(g, "session", None) or {}
        token = sess.get("token")
        prev = sess.get("acting_shop_id")
        # 解除せずに別店舗へ乗り換えると impersonate_start が2行並び、1店舗目を
        # いつ抜けたのかがログから追えない。先に旧店舗の end を記録してから切り替える。
        if prev and prev != shop_id:
            audit("admin.impersonate_end", target_type="shop", target_id=prev, shop_id=prev,
                  detail="別店舗の代理閲覧開始に伴い終了")
        execute("UPDATE sessions SET acting_shop_id=? WHERE token=?", (shop_id, token))
        # 停止中(is_active=0)の店舗にも入れる。停止は「新規ログインの遮断」であって
        # 既存セッションの遮断ではなく、require_auth の通常 shop 経路も is_active を
        # 見ていないため一貫している。サポート用途では停止中こそ中を見たい。
        # ただし運営者が承知の上で入ったことがログから読めるよう detail に明記する。
        suffix = "" if shop.get("is_active") else "（停止中）"
        # アーカイブ済みも同じ方針：完全削除の前に中身を確認するサポート業務が
        # 成立する必要があるため、閲覧自体は止めない。ただし detail に明記する。
        archived_suffix = "（アーカイブ済み）" if shop.get("is_archived") else ""
        audit("admin.impersonate_start", target_type="shop", target_id=shop_id, shop_id=shop_id,
              detail=f"{shop['shop_code']} の代理閲覧を開始（閲覧のみ）{suffix}{archived_suffix}")
        return jsonify({"ok": True, "shop": {"id": shop["id"], "shop_code": shop["shop_code"],
                                             "shop_name": shop["shop_name"]}})


    @app.delete("/api/admin/impersonate")
    def admin_impersonate_stop():
        require_auth(["admin"])
        # 開始と同じく token で絞る。role='admin' で絞ると他の運営者の代理まで
        # 解除してしまい、サポート中の別担当者が突然店舗データを見失う。
        sess = getattr(g, "session", None) or {}
        token = sess.get("token")
        acting = sess.get("acting_shop_id")
        execute("UPDATE sessions SET acting_shop_id=NULL WHERE token=?", (token,))
        # 代理していなかった場合は監査ログを汚さない（解除は何度押しても安全）
        if acting:
            audit("admin.impersonate_end", target_type="shop", target_id=acting, shop_id=acting,
                  detail="代理閲覧を終了")
        return jsonify({"ok": True})


    _ANNOUNCE_MAX_ROWS = 5000

    @app.post("/api/admin/announcements")
    def admin_announce():
        """全店舗一斉通知。宛先は「全店舗/選択店舗」×「店舗管理者のみ/全スタッフ」。"""
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        title = (body.get("title") or "").strip()
        text = (body.get("body") or "").strip()
        audience = body.get("audience") or "managers"
        if not title:
            raise ValueError("件名を入力してください")
        if audience not in ("managers", "all"):
            raise ValueError("配信対象が不正です")

        shop_ids = body.get("shop_ids")
        if shop_ids is not None:
            # shop_ids は IN (...) の展開に使うため、値を束縛する前に
            # 中身が整数であることを検証する（文字列や配列混入を防ぐ）。
            if not isinstance(shop_ids, list):
                raise ValueError("shop_ids は整数の配列で指定してください")
            if not shop_ids:
                # None（未指定＝全店舗）と [] を区別する。「店舗を選ぶ」で1件も
                # 選ばずに送信した誤操作を、全店舗配信にフォールバックさせない
                # ための明示的なガード（レビュー指摘: 空配列が全店舗配信になる事故）。
                raise ValueError("配信先の店舗を選択してください")
            if not all(isinstance(x, int) and not isinstance(x, bool) for x in shop_ids):
                raise ValueError("shop_ids は整数の配列で指定してください")
            # IN (...) は指定件数ぶん ? を展開するため、店舗数が D1 のバインド上限を
            # 超えると D1 がクエリを拒否する。上限以下に分割して引く。
            shops = []
            for chunk in chunked(shop_ids, D1_MAX_BINDS):
                shops += query_all(
                    "SELECT id FROM shops WHERE COALESCE(is_archived,0)=0 AND is_active=1 "
                    "AND id IN ({})".format(",".join("?" * len(chunk))), tuple(chunk))
        else:
            shops = query_all("SELECT id FROM shops WHERE COALESCE(is_archived,0)=0 AND is_active=1")
        if not shops:
            raise ValueError("配信先の店舗がありません")

        # created_at はバッチで1つの値に揃える。配信履歴の表示に使うため、
        # datetime('now') 任せにすると行ごとにずれる。
        stamp = jst_now().strftime("%Y-%m-%d %H:%M:%S")
        # batch_id は配信履歴のグルーピング専用キー。created_at は秒精度で
        # バッチ内は1つの値に揃えているため、同一秒に同一件名で2回配信すると
        # (created_at, title) だけでは1件に統合されてしまう
        # （レビュー指摘: 連打・連続配信での履歴統合）。ランダムトークンで
        # 確実に別バッチとして区別する。
        batch_id = secrets.token_hex(8)
        rows = [(s["id"], None) for s in shops]
        recipients = 0
        if audience == "all":
            ids = [s["id"] for s in shops]
            staffs = []
            # ここも IN (...) の展開。店舗数が100を超えると D1 が拒否するため分割する。
            for chunk in chunked(ids, D1_MAX_BINDS):
                staffs += query_all(
                    "SELECT id, shop_id FROM staffs WHERE is_resigned=0 AND shop_id IN ({})".format(
                        ",".join("?" * len(chunk))), tuple(chunk))
            rows += [(st["shop_id"], st["id"]) for st in staffs]
            recipients = len(staffs)

        if len(rows) > _ANNOUNCE_MAX_ROWS:
            raise ValueError(f"配信件数が多すぎます（{len(rows)}件）。店舗を分けて配信してください")

        # D1 は REST API の1往復＝1クエリのため、1件ずつ挿すと配信が実用速度にならない。
        # まとめ INSERT にするが、1行あたり 7 バインドなので D1 のバインド上限
        # （1クエリ100個）に収まる行数（=14行）ずつに分割する。分割しないと
        # 稼働2店舗・スタッフ17名（19行=133バインド）の本番規模で即座に破綻する。
        binds_per_row = 7
        rows_per_query = D1_MAX_BINDS // binds_per_row
        for chunk in chunked(rows, rows_per_query):
            placeholders = ",".join(["(?,?,?,?,?,0,?,?)"] * len(chunk))
            binds = []
            for shop_id, staff_id in chunk:
                binds += [shop_id, staff_id, "announcement", title, text, stamp, batch_id]
            execute("INSERT INTO notifications "
                    "(shop_id, staff_id, type, title, body, is_read, created_at, batch_id) "
                    "VALUES " + placeholders, tuple(binds))

        audit("admin.announce", target_type="announcement", detail=
              f"{title} / 店舗{len(shops)}件 / 対象{'全員' if audience == 'all' else '店舗管理者'}")
        return jsonify({"ok": True, "shops": len(shops), "recipients": recipients})


    @app.get("/api/admin/notifications")
    def admin_notifs():
        """一斉通知の配信履歴。

        batch_id で束ねる（Task 14 の一斉通知は必ず batch_id を発行する）。
        created_at はバッチ内で1つの値に揃えているため MIN() で代表値を取れば足りる
        が、同一秒・同一件名の別バッチが1件に潰れないよう GROUP BY は batch_id で行う。
        """
        require_auth(["admin"])
        rows = query_all(
            "SELECT MIN(created_at) AS created_at, MIN(title) AS title, "
            "COUNT(DISTINCT shop_id) AS shops, "
            "SUM(CASE WHEN staff_id IS NOT NULL THEN 1 ELSE 0 END) AS recipients "
            "FROM notifications WHERE type='announcement' "
            "GROUP BY batch_id ORDER BY MAX(id) DESC LIMIT 100")
        return jsonify({"announcements": rows})


    @app.put("/api/admin/notifications/read-all")
    def admin_notifs_readall():
        require_auth(["admin"])
        # 管理者は配信履歴を見るだけで、自分宛の未読という概念が無い。
        # フロントの共通ヘッダが呼ぶため、互換のために残す。
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

    def _current_admin_id():
        """require_auth(["admin"]) 済みの前提で、自分の system_admins.id を返す。"""
        return (getattr(g, "user", None) or {}).get("id")

    @app.get("/api/admin/admins")
    def admin_list_admins():
        """システム管理者アカウントの一覧（パスワードハッシュは含めない）。"""
        require_auth(["admin"])
        rows = query_all("SELECT id, admin_id, name, created_at FROM system_admins ORDER BY id")
        return jsonify({"admins": rows})

    @app.post("/api/admin/admins")
    def admin_create_admin():
        """2人目以降の運営者を追加する。

        【なぜ必要か】
          system_admins は /api/init で1行作られるのみで、これまで運営者を
          増やす手段が無かった（1人の運営者が退職・異動すると引き継げない）。
        """
        require_auth(["admin"])
        body = request.get_json(silent=True) or {}
        # sanitize_login_code は login() と同じ正規化（前後空白・改行除去、非文字列を
        # str() で吸収）。admin_id は監査ログや将来のログイン試行キーにそのまま入るため、
        # login() 側の入力と同じ扱いにしておく必要がある。
        new_id = sanitize_login_code(body.get("admin_id"))
        name = (body.get("name") or "").strip() or "システム管理者"
        pw = body.get("password") or ""
        if not new_id:
            raise ValueError("管理者IDを入力してください")
        # login() は shop_code/user_code が LOGIN_CODE_MAX 文字を超えると常に400で
        # 弾く。ここで同じ上限を掛けておかないと、作成はできるが二度とログイン
        # できない管理者行を作ってしまう。
        if len(new_id) > LOGIN_CODE_MAX:
            raise ValueError(f"管理者IDは{LOGIN_CODE_MAX}文字以内で入力してください")
        if query_one("SELECT id FROM system_admins WHERE admin_id=?", (new_id,)):
            raise ValueError("その管理者IDは既に使われています")
        msg = validate_password(pw)
        if msg:
            raise ValueError(msg)
        try:
            meta = execute("INSERT INTO system_admins (admin_id, password_hash, name) VALUES (?,?,?)",
                           (new_id, hash_password(pw), name))
        except Exception as e:
            # 事前の重複チェックと INSERT の間に別リクエストが同じ admin_id を
            # 作ってしまう競合の保険。UNIQUE制約違反は生SQLエラーを返さず400にする
            # （admin_create_shop の manager_code 重複と同じ扱い）。
            if "UNIQUE" in str(e).upper():
                raise ValueError("その管理者IDは既に使われています")
            raise
        audit("admin.create", target_type="system_admin", target_id=meta["last_row_id"],
              detail=f"admin_id={new_id}")
        return jsonify({"ok": True, "id": meta["last_row_id"]})

    @app.delete("/api/admin/admins/<int:aid>")
    def admin_delete_admin(aid):
        """運営者を削除する。最後の1人・自分自身は削除できない。"""
        require_auth(["admin"])
        me = _current_admin_id()
        target = query_one("SELECT id, admin_id FROM system_admins WHERE id=?", (aid,))
        if target is None:
            abort(404, description="管理者が見つかりません")
        if aid == me:
            raise ValueError("自分自身は削除できません")
        # 【なぜ SELECT COUNT(*) →  DELETE の2段構えにしないか】
        #   2人の管理者が互いを同時に削除しようとした場合、check-then-act だと
        #   両方が「まだ2人いる」を見た状態のまま削除を実行してしまい、管理者が
        #   0人になる事故が起こり得る（本番は D1 REST 経由で1文ごとにネットワーク
        #   往復があり、ローカル SQLite よりこの競合窓が桁違いに広い）。
        #   _record_login_failure の UPSERT と同じ考え方で、「2人以上いるときだけ
        #   削除する」条件を DELETE 文自体に埋め込み、判定と実行をDB側で原子化する。
        meta = execute(
            "DELETE FROM system_admins WHERE id=? AND "
            "(SELECT COUNT(*) FROM system_admins) > 1", (aid,))
        if meta["changes"] == 0:
            raise ValueError("最後の管理者は削除できません")
        # 削除対象のセッションを全て失効させる（削除後もトークンが生きたままだと
        # require_auth が system_admins を引けず 401 になるはずが、削除前に
        # 発行された古いキャッシュ等で誤用されるリスクを避けるため明示的に消す）。
        execute("DELETE FROM sessions WHERE role='admin' AND user_id=?", (aid,))
        audit("admin.delete", target_type="system_admin", target_id=aid,
              detail=f"admin_id={target['admin_id']}")
        return jsonify({"ok": True})

    @app.get("/api/admin/migrations")
    def admin_migrations_status():
        """マイグレーションの適用状況一覧（ファイル×ステートメント単位）。"""
        require_auth(["admin"])
        rows = migrator.status()
        return jsonify({"migrations": rows,
                        "pending": sum(1 for r in rows if not r["applied"])})

    @app.post("/api/admin/migrations/apply")
    def admin_migrations_apply():
        """未適用のマイグレーションを順に適用する（失敗時はそこで中断）。"""
        require_auth(["admin"])
        result = migrator.apply_pending()
        detail = (f"applied={len(result['applied'])} skipped={len(result['skipped'])}"
                  f" failed={'yes' if result['failed'] else 'no'}")
        audit("admin.migrate", target_type="schema", detail=detail)
        return jsonify(result)

