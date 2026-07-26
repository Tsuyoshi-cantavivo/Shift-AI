"""app.py - ShiftAI Flask アプリ（メイン）。

ルーティング・認証・全APIエンドポイントを提供。
起動: python src/app.py  (または flask --app src.app run)
"""
import os
import json
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from flask import (Flask, request, jsonify, abort, Response, send_file, g)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import safe_join
from dotenv import load_dotenv

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # src/ をモジュールパスに追加

from db import query_all, query_one, execute, insert_row, init_schema
from auth import hash_password, verify_password, gen_token, strip_password
from utils import (
    calc_next_period, jst_now, jst_today, minutes_between, compute_break_minutes,
    night_minutes, validate_password, parse_settings, build_ics, parse_iso, normalize_iso,
    norm_hhmm, norm_dt_iso, add_days, build_staff_tendency, combine_dt_overnight,
)
import shift_engine
import ai
import holidays_jp

# .env を読み込むが、既に環境変数が設定されている場合は上書きしない。
# （テスト・E2Eで外部からDB_PATH等を与える場合、.env の値で潰されないように）
load_dotenv(override=False)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # プロジェクトルート
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False  # 日本語をそのまま返す

# Railway 等のエッジプロキシ配下では REMOTE_ADDR がプロキシのIPに潰れ、
# 全利用者が同一クライアント扱いになる。そのままだとログインのレート制限
# （_login_attempt_key）が機能せず、第三者が10回失敗を送るだけで正規の
# 管理者を締め出せるアカウントロックアウトDoSが成立する。
# x_for=1 は X-Forwarded-For の「最右」＝直近のプロキシが記録した実クライアントIP
# のみを採用するため、クライアントが自分でヘッダを付けても偽装にはならない。
# 前提: 信頼できるプロキシ1段の背後で動かすこと（多段構成なら x_for を増やす）。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)


# ===========================================================
# エラーハンドラ（JSONで統一）
# ===========================================================
def _csv_safe(value):
    """CSV セルの Formula Injection (=cmd|..., +1, @SUM 等) 対策。

    セル先頭が =, +, -, @, tab(\\t), 改行(\\r, \\n) で始まる場合は
    先頭にシングルクォートを前置して Excel/Sheets で数式として解釈されないようにする。
    また、値にカンマ/ダブルクォート/改行を含む場合はダブルクォートで囲む。
    """
    if value is None:
        return ""
    s = str(value)
    # Formula Injection 対策: 危険な先頭文字を前置逃げ
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        s = "'" + s
    # CSV 標準エスケープ: " / カンマ / 改行 を含むなら "..." で囲む
    if any(c in s for c in (",", '"', "\n", "\r")):
        s = '"' + s.replace('"', '""') + '"'
    return s


@app.errorhandler(HTTPException)
def handle_http(e):
    return jsonify({"error": e.description}), e.code


@app.errorhandler(ValueError)
def handle_ve(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(Exception)
def handle_exc(e):
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    return jsonify({"error": "サーバーエラー: " + str(e)}), 500


# ===========================================================
# 認証ヘルパ
# ===========================================================
# ログイン試行のレート制限
_LOGIN_MAX_FAILS = 10        # この回数失敗したらロック
_LOGIN_WINDOW_MIN = 15       # 失敗カウントを保持する時間（分）
_LOGIN_LOCK_MIN = 15         # ロックする時間（分）
_LOGIN_CODE_MAX = 64         # 店舗コード／ユーザーコードの最大長


def _sanitize_login_code(value):
    """ログイン入力のコードを正規化する（前後の空白除去 ＋ 改行の除去）。

    【なぜ改行を落とすか】
      失敗したコードは監査ログの actor_name とレート制限キーにそのまま入る。
      改行が生で残ると、監査ログを1行1レコードとして読む運用（将来のCSV出力を含む）で
      攻撃者が偽の行を差し込めてしまう。UI 側は esc() を通すので XSS にはならないが、
      ログ偽装は残るため入口で落とす。

    str() を挟むのは、JSON で文字列以外（数値・配列・オブジェクト）を送られたときに
    .strip() が AttributeError になり、未認証クライアントに 500 が返るのを避けるため。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.replace("\r", "").replace("\n", "").strip()


def _login_attempt_key(shop_code, user_code):
    """レート制限のキー。管理者ログインは user_code を 'admin' に正規化する。

    管理者は「店舗コード欄・ユーザーコード欄のどちらかに admin」で試行できる
    （login() 参照）ため、正規化しないと同じ管理者アカウントに対して
    2つの別キーが立ち、許される試行回数が2倍になってしまう。

    【トレードオフ】
      正規化の結果、同一IPからの管理者ログインは admin_id の違いに関わらず
      1つのバケツを共有する。system_admins は複数行を許すテーブルなので、
      管理者が複数いる環境では、ある管理者への総当たりが他の管理者まで
      巻き添えでロックする。試行回数を2倍にしないことを優先した判断。
      admin_id 単位に分けるには login() 側の「どちらかの欄に admin」という
      マジックワード仕様そのものを見直す必要がある（Phase 2 で再検討）。
    """
    ip = request.remote_addr or "-"
    if shop_code == "admin" or user_code == "admin":
        return f"{ip}|admin"
    return f"{ip}|{shop_code}|{user_code}"


def _check_login_lock(key):
    """ロック中なら 429 で中断する。期限切れの行はここで掃除する。

    定期実行のバックグラウンドジョブが無い構成のため、行の掃除は
    「そのキーに触ったログイン処理のついで」に行う。放置すると
    login_attempts が古い行で膨らみ続ける。
    """
    now = jst_now()
    row = query_one("SELECT fail_count, locked_until, updated_at FROM login_attempts WHERE attempt_key=?",
                    (key,))
    if not row:
        return
    locked_until = row.get("locked_until")
    if locked_until:
        try:
            lock_dt = datetime.strptime(locked_until, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            lock_dt = None
        if lock_dt and now < lock_dt:
            # ブロックした試行も監査に残す（残さないと、攻撃はちょうど _LOGIN_MAX_FAILS 件で
            # 途切れて見え、その後の継続や規模が後から一切分からない）。
            # ただしロック中は何百回でもリクエストが来るため、記録は「そのロック期間中に1回だけ」。
            # SELECT してから UPDATE すると並列時に複数回記録され得るので、
            # blocked_logged=0 を条件にした1文の UPDATE が当たったときだけ記録する。
            claimed = execute(
                "UPDATE login_attempts SET blocked_logged=1 "
                "WHERE attempt_key=? AND blocked_logged=0", (key,))
            if claimed.get("changes"):
                audit("auth.login_blocked", actor_role="anonymous", actor_name=key,
                      detail=f"ロック中のログイン試行をブロック（{_LOGIN_LOCK_MIN}分）")
            abort(429, description="ログイン試行が多すぎます。しばらく待ってからお試しください")
    # ロック期限切れ、または最終試行がウィンドウ外なら行を捨てる
    updated = row.get("updated_at")
    stale = True
    if updated:
        try:
            stale = (now - datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")) > timedelta(minutes=_LOGIN_WINDOW_MIN)
        except (ValueError, TypeError):
            stale = True
    if stale or locked_until:
        execute("DELETE FROM login_attempts WHERE attempt_key=?", (key,))


def _record_login_failure(key):
    """失敗を1回数える。上限に達したらロック時刻を設定する。

    【なぜ UPSERT か】
      SELECT でカウントを読んでから UPDATE/INSERT する2段構えだと、その間に
      到達した並列リクエストが同じ古い値を読み、失敗が取りこぼされる。
      さらに行がまだ無い状態で同時到達すると INSERT が主キー衝突し、
      未認証クライアントに 500 と生の SQL エラーを返してしまう。
      本番は DB_MODE=d1（REST API 経由）で1文ごとにネットワーク往復が挟まり、
      ローカル SQLite より競合ウィンドウが桁違いに広いため実害が出る。
      加算を1文の UPSERT にまとめて DB 側で原子的に行う。
    """
    now = jst_now()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    execute(
        "INSERT INTO login_attempts (attempt_key, fail_count, updated_at) VALUES (?,1,?) "
        "ON CONFLICT(attempt_key) DO UPDATE SET "
        "fail_count=login_attempts.fail_count+1, updated_at=excluded.updated_at",
        (key, now_s))
    # 上限到達時のみロック時刻を立てる。locked_until IS NULL を条件にすることで、
    # ロック中にさらに失敗してもロック期限が延び続けること（無期限ロック）を防ぐ。
    lock_until_s = (now + timedelta(minutes=_LOGIN_LOCK_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    # blocked_logged=0 も同時に戻す。新しいロック期間ごとに監査へ1件だけ残すため。
    execute("UPDATE login_attempts SET locked_until=?, blocked_logged=0 "
            "WHERE attempt_key=? AND fail_count>=? AND locked_until IS NULL",
            (lock_until_s, key, _LOGIN_MAX_FAILS))


def _clear_login_failures(key):
    """ログイン成功時に失敗カウントを消す。"""
    execute("DELETE FROM login_attempts WHERE attempt_key=?", (key,))


def require_auth(allowed):
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        abort(401, description="認証が必要です")
    session = query_one("SELECT * FROM sessions WHERE token=?", (token,))
    if not session:
        abort(401, description="セッションが無効です")
    if session.get("expires_at"):
        # NOTE: かつて bare except Exception: pass で HTTPException を握り潰し、
        # 期限切れトークンが有効扱いになる脆弱性があった。ValueError のみ捕捉する。
        try:
            expired = jst_now() > datetime.strptime(session["expires_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            expired = False  # 不正フォーマットは警告を出さず無効扱い（運用時は別途ログ）
        if expired:
            abort(401, description="セッションの有効期限が切れました")
    role = session["role"]
    if role not in allowed:
        abort(403, description="権限がありません")
    # 参照先の行が引けないセッションは 401 にする（role 3種で対称にすること）。
    # g.user = strip_password(None) = None のまま先へ進むと、後段の staff["id"] 等が
    # TypeError になり、削除済みスタッフ／管理者のトークンで 500 が返っていた。
    if role == "admin":
        user = query_one("SELECT id, admin_id, name FROM system_admins WHERE id=?", (session["user_id"],))
        if user is None:
            abort(401, description="セッションの管理者が見つかりません")
    elif role == "shop":
        # user_id は従来 shops.id（旧店主）または staffs.id（manager ロール）。
        # shop_id を使って店舗情報を取得する。
        # NOTE: かつて shop が引けないとき user_id を shops.id とみなすフォールバックが
        # あったが、manager セッションでは user_id が staffs.id のため、staffs.id と同値の
        # shops.id を持つ別テナントに着地し得た。旧店主ログインも _create_session で
        # shop_id を正しく入れている（src/app.py:671）ため、フォールバックは削除した。
        user = query_one("SELECT * FROM shops WHERE id=?", (session.get("shop_id"),))
        if user is None:
            abort(401, description="セッションの店舗が見つかりません")
    else:
        user = query_one("SELECT * FROM staffs WHERE id=?", (session["user_id"],))
        if user is None:
            abort(401, description="セッションのスタッフが見つかりません")
    g.role = role
    g.user = strip_password(user)
    g.shop_id = session.get("shop_id")
    return role, g.user, session.get("shop_id")


def notify(shop_id, staff_id, ntype, title, body):
    """アプリ内通知を1件作成。"""
    insert_row("notifications", {"shop_id": shop_id, "staff_id": staff_id, "type": ntype,
                                "title": title, "body": body})


def audit(action, target_type=None, target_id=None, shop_id=None, detail=None,
          actor_role=None, actor_id=None, actor_name=None):
    """監査ログを1件記録。失敗しても業務処理を止めない。

    actor は既定で現在の認証コンテキスト(g.role / g.user)から解決する。
    shop の g.user は shops 行なので氏名は shop_name、それ以外は name。

    ログイン失敗のように認証コンテキストが存在しない場面では、
    actor_role / actor_name を明示的に渡す（g より優先される）。
    """
    try:
        role = actor_role if actor_role is not None else getattr(g, "role", None)
        user = getattr(g, "user", None) or {}
        if actor_id is None:
            actor_id = user.get("id")
        if actor_name is None:
            g_role = getattr(g, "role", None)
            actor_name = user.get("shop_name") if g_role == "shop" else user.get("name")
        insert_row("audit_logs", {
            "actor_role": role, "actor_id": actor_id, "actor_name": actor_name,
            "action": action, "target_type": target_type, "target_id": target_id,
            "shop_id": shop_id, "detail": detail,
            "created_at": jst_now().strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        print(f"[audit] WARN: failed to record {action}: {e}", flush=True)


def summarize_shifts(shifts, staffs_by_id, settings=None):
    settings = settings or {}
    night_rate = settings.get("night_premium_rate") or 1.0
    transport_per_day = settings.get("transport_per_day") or 0
    agg = {}
    for sh in shifts:
        sid = sh["staff_id"]
        a = agg.setdefault(sid, {"days": set(), "conf_min": 0, "req_min": 0, "night_min": 0})
        work = minutes_between(sh["start_datetime"], sh["end_datetime"]) - (sh.get("break_time_minutes") or 0)
        work = max(0, work)
        if sh.get("status") == "confirmed":
            a["conf_min"] += work
            a["days"].add(sh["start_datetime"][:10])
            a["night_min"] += night_minutes(sh["start_datetime"], sh["end_datetime"])
        elif sh.get("status") == "requested":
            a["req_min"] += work
    result = []
    for sid, a in agg.items():
        st = staffs_by_id.get(sid, {})
        wage = st.get("hourly_wage") or 0
        conf_h = round(a["conf_min"] / 60 * 10) / 10
        req_h = round(a["req_min"] / 60 * 10) / 10
        proj_h = round((conf_h + req_h) * 10) / 10
        night_h = round(a["night_min"] / 60 * 10) / 10
        base_pay = int(conf_h * wage)
        night_premium = int(night_h * wage * (night_rate - 1))
        transport = len(a["days"]) * transport_per_day
        result.append({"staff_id": sid, "name": st.get("name", "?"), "role": st.get("role", "part_time"),
                       "hourly_wage": wage, "days": len(a["days"]), "confirmed_hours": conf_h,
                       "requested_hours": req_h, "projected_hours": proj_h, "night_hours": night_h,
                       "base_pay": base_pay, "night_premium": night_premium, "transport": transport,
                       "pay": base_pay + night_premium + transport,
                       "projected_pay": int(proj_h * wage) + transport + int(night_h * wage * (night_rate - 1))})
    result.sort(key=lambda x: (0 if x["role"] == "employee" else 1, -x["pay"]))
    return {"staff": result, "total_hours": round(sum(r["confirmed_hours"] for r in result) * 10) / 10,
            "total_projected_hours": round(sum(r["projected_hours"] for r in result) * 10) / 10,
            "total_pay": sum(r["pay"] for r in result),
            "total_projected_pay": sum(r["projected_pay"] for r in result)}


def _check_slot_cap(shop_id, start_iso, end_iso, exclude_id=None, force=False):
    """配置先時間帯のスロット上限チェック（時間単位・検証Aと同等ロジック）。

    従来はパターン実質カバー(50%)集計だったため部分重複で誤判定する問題があった。
    shift_engine のスロットベース集計に一本化し、手動追加でも時間単位の上限を厳守する。
    曜日別必要人数オーバーライドを適用済みの要件を使用する。
    """
    if force:
        return (False, None, 0)
    pats = query_all("SELECT id, start_time, end_time, required_staff FROM shift_patterns WHERE shop_id=?", (shop_id,))
    if not pats:
        return (False, None, 0)
    weekday_overrides = shift_engine.load_weekday_overrides(shop_id)
    day = start_iso[:10]
    wd = (datetime.strptime(day, "%Y-%m-%d").weekday() + 1) % 7  # 0=日
    # 曜日オーバーライドをパターンへ反映
    applied = []
    for pat in pats:
        ov = weekday_overrides.get((pat.get("id"), wd))
        p = dict(pat)
        if ov is not None:
            p["required_staff"] = ov
        applied.append(p)
    req_map = shift_engine._day_requirements(applied, shift_engine.GRAN, wd, weekday_overrides)
    slots = shift_engine._shift_slots(start_iso, end_iso, shift_engine.GRAN)
    # シフトが触れるスロットのうち最も厳しい要件
    max_req = 0
    for sl in slots:
        r = req_map.get(sl, 0)
        if r > max_req:
            max_req = r
    if max_req == 0:
        return (False, None, 0)
    rows = query_all("SELECT id, start_datetime, end_datetime FROM shifts WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=?",
                     (shop_id, day + "T00:00:00", day + "T23:59:59"))
    coverage = {}
    for r in rows:
        if exclude_id and str(r["id"]) == str(exclude_id):
            continue
        for sl in shift_engine._shift_slots(r["start_datetime"], r["end_datetime"], shift_engine.GRAN):
            coverage[sl] = coverage.get(sl, 0) + 1
    for sl in slots:
        r = req_map.get(sl, 0)
        if r > 0 and coverage.get(sl, 0) + 1 > r:
            return (True, r, coverage.get(sl, 0))
    return (False, max_req, max((coverage.get(sl, 0) for sl in slots), default=0))


def _check_staff_overlap(shop_id, staff_id, start_iso, end_iso, exclude_id=None, include_requested=False):
    """同一スタッフの同一日内で時間帯が重なる既存シフトがあるか（中抜け・重複防止）。

    戻り値: (overlaps: bool, conflicting_shift: dict or None)
      - confirmed/modifying シフトを必ずチェック
      - include_requested=True のときは requested も含めてチェック（希望提出の重複防止用）
    """
    day = (start_iso or "")[:10]
    if not day:
        return (False, None)
    statuses = "('confirmed','modifying')" if not include_requested else "('confirmed','modifying','requested')"
    rows = query_all(
        f"SELECT id, start_datetime, end_datetime, reason, status FROM shifts "
        f"WHERE staff_id=? AND shop_id=? AND status IN {statuses} "
        f"AND start_datetime>=? AND start_datetime<=?",
        (staff_id, shop_id, day + "T00:00:00", day + "T23:59:59"))
    try:
        s_new = parse_iso(start_iso); e_new = parse_iso(end_iso)
    except Exception:
        return (False, None)
    for r in rows:
        if exclude_id and str(r["id"]) == str(exclude_id):
            continue
        try:
            s = parse_iso(r["start_datetime"]); e = parse_iso(r["end_datetime"])
        except Exception:
            continue
        # 半開区間 [s_new, e_new) と [s, e) の交差判定（境界接触=隣接は重複ではない）
        if s_new < e and s < e_new:
            return (True, r)
    return (False, None)


def _try_merge_adjacent(shop_id, staff_id, start_iso, end_iso):
    """同一スタッフの同日で隣接する confirmed があれば統合。
    隣接 = 既存の終了=新規の開始（前隣接）or 既存の開始=新規の終了（後隣接）。
    戻り値: (merged: bool, shift_id or None) — 統合した場合は (True, 既存shift_id)。
    """
    day = start_iso[:10]
    # 後隣接: 既存.start == 新規.end → 既存を前に延長（新規の開始を既存の開始にする）
    after = query_one(
        "SELECT id, start_datetime, end_datetime FROM shifts "
        "WHERE staff_id=? AND shop_id=? AND status IN ('confirmed','modifying') "
        "AND start_datetime=? AND start_datetime>=? AND start_datetime<=?",
        (staff_id, shop_id, end_iso, day + "T00:00:00", day + "T23:59:59"))
    if after:
        new_start = start_iso
        new_end = after["end_datetime"]
        work = minutes_between(new_start, new_end)
        execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, reason='隣接統合' WHERE id=?",
                (new_start, new_end, compute_break_minutes(work), after["id"]))
        return True, after["id"]
    # 前隣接: 既存.end == 新規.start → 既存を後ろに延長（新規の終了を既存の終了にする）
    before = query_one(
        "SELECT id, start_datetime, end_datetime FROM shifts "
        "WHERE staff_id=? AND shop_id=? AND status IN ('confirmed','modifying') "
        "AND end_datetime=? AND start_datetime>=? AND start_datetime<=?",
        (staff_id, shop_id, start_iso, day + "T00:00:00", day + "T23:59:59"))
    if before:
        new_start = before["start_datetime"]
        new_end = end_iso
        work = minutes_between(new_start, new_end)
        execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, reason='隣接統合' WHERE id=?",
                (new_start, new_end, compute_break_minutes(work), before["id"]))
        return True, before["id"]
    return False, None


def _shorten_to_cap(shop_id, staff_id, start_dt, end_dt, exclude_id=None):
    """target を cap 内に収まるよう短縮。営業時間全体から cap 内の最長連続区間を探す。
    target期間内に配置不可の場合は、営業時間全体（朝〜夜）から配置可能な時間帯を探す。
    戻り値: (new_start_iso, new_end_iso) or None（短縮不可）
    """
    pats = query_all("SELECT id, start_time, end_time, required_staff FROM shift_patterns WHERE shop_id=?", (shop_id,))
    if not pats:
        return None
    weekday_overrides = shift_engine.load_weekday_overrides(shop_id)
    wd = (datetime.strptime(start_dt[:10], "%Y-%m-%d").weekday() + 1) % 7
    applied = []
    for pat in pats:
        ov = weekday_overrides.get((pat.get("id"), wd))
        p = dict(pat)
        if ov is not None:
            p["required_staff"] = ov
        applied.append(p)
    req_map = shift_engine._day_requirements(applied, shift_engine.GRAN, wd, weekday_overrides)
    day = start_dt[:10]
    existing = query_all(
        "SELECT id, start_datetime, end_datetime FROM shifts WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=? AND staff_id!=?",
        (shop_id, day + "T00:00:00", day + "T23:59:59", staff_id))
    coverage = {}
    for ex in existing:
        if exclude_id and str(ex.get("id")) == str(exclude_id):
            continue
        for sl in shift_engine._shift_slots(ex["start_datetime"], ex["end_datetime"], shift_engine.GRAN):
            coverage[sl] = coverage.get(sl, 0) + 1
    # 営業時間全体のスロットで配置可能区間を探す（target期間に限定しない）
    all_slots = sorted(req_map.keys())
    best_start = None; best_len = 0; cur_start = None; cur_len = 0
    for sl in all_slots:
        req_s = req_map.get(sl, 0)
        can_place = (req_s == 0) or (coverage.get(sl, 0) + 1 <= req_s)
        if can_place:
            if cur_start is None:
                cur_start = sl
            cur_len += shift_engine.GRAN
        else:
            if cur_len > best_len:
                best_start = cur_start; best_len = cur_len
            cur_start = None; cur_len = 0
    if cur_len > best_len:
        best_start = cur_start; best_len = cur_len
    if best_start is not None and best_len >= 60:
        new_end_min = best_start + best_len
        ns = f"{start_dt[:10]}T{best_start // 60:02d}:{best_start % 60:02d}:00"
        ne = f"{start_dt[:10]}T{new_end_min // 60:02d}:{new_end_min % 60:02d}:00"
        return (ns, ne)
    return None


def _count_over_cap_slots(shop_id, start_iso, end_iso, exclude_id=None):
    """target を +1 したとき cap を超過するスロット数を返す（0 なら cap 内）。"""
    pats = query_all("SELECT id, start_time, end_time, required_staff FROM shift_patterns WHERE shop_id=?", (shop_id,))
    if not pats:
        return 0
    weekday_overrides = shift_engine.load_weekday_overrides(shop_id)
    day = start_iso[:10]
    wd = (datetime.strptime(day, "%Y-%m-%d").weekday() + 1) % 7
    applied = []
    for pat in pats:
        ov = weekday_overrides.get((pat.get("id"), wd))
        p = dict(pat)
        if ov is not None:
            p["required_staff"] = ov
        applied.append(p)
    req_map = shift_engine._day_requirements(applied, shift_engine.GRAN, wd, weekday_overrides)
    slots = shift_engine._shift_slots(start_iso, end_iso, shift_engine.GRAN)
    rows = query_all("SELECT id, start_datetime, end_datetime FROM shifts WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=?",
                     (shop_id, day + "T00:00:00", day + "T23:59:59"))
    coverage = {}
    for r in rows:
        if exclude_id and str(r["id"]) == str(exclude_id):
            continue
        for sl in shift_engine._shift_slots(r["start_datetime"], r["end_datetime"], shift_engine.GRAN):
            coverage[sl] = coverage.get(sl, 0) + 1
    over_count = 0
    for sl in slots:
        req = req_map.get(sl, 0)
        if req > 0 and coverage.get(sl, 0) + 1 > req:
            over_count += 1
    return over_count


def _flag_over_cap_shifts(shop_id, start_iso, end_iso):
    """期間内の confirmed シフトのうち、必要人数を超えるスロットに重なるものへ
    over_cap_flag=1 を立てる。超過に重ならないものは 0 にリセットする。

    スロットは shift_engine._shift_slots が返す「分単位int」で、日をまたいで
    繰り返すため coverage/over は (day, slot_min) で管理する（_check_slot_cap と同一モデル）。
    戻り値: フラグを立てたシフト件数。
    """
    pats = query_all("SELECT id, start_time, end_time, required_staff FROM shift_patterns WHERE shop_id=?", (shop_id,))
    if not pats:
        return 0
    weekday_overrides = shift_engine.load_weekday_overrides(shop_id)
    rows = query_all(
        "SELECT id, start_datetime, end_datetime, over_cap_flag FROM shifts "
        "WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=?",
        (shop_id, start_iso, end_iso))
    if not rows:
        return 0
    shift_slots = {}            # shift_id -> (day, [slot_min,...])
    coverage = {}               # (day, slot_min) -> count
    for r in rows:
        day = r["start_datetime"][:10]
        slots = shift_engine._shift_slots(r["start_datetime"], r["end_datetime"], shift_engine.GRAN)
        shift_slots[r["id"]] = (day, slots)
        for sl in slots:
            coverage[(day, sl)] = coverage.get((day, sl), 0) + 1
    req_cache = {}

    def _req_for(day):
        if day not in req_cache:
            wd = (datetime.strptime(day, "%Y-%m-%d").weekday() + 1) % 7
            applied = []
            for pat in pats:
                ov = weekday_overrides.get((pat.get("id"), wd))
                p = dict(pat)
                if ov is not None:
                    p["required_staff"] = ov
                applied.append(p)
            req_cache[day] = shift_engine._day_requirements(applied, shift_engine.GRAN, wd, weekday_overrides)
        return req_cache[day]

    over = set()                # (day, slot_min) が超過
    for (day, sl), cnt in coverage.items():
        req = _req_for(day).get(sl, 0)
        if req and req > 0 and cnt > req:
            over.add((day, sl))
    # 注意: reason はスタッフ側 API も返すため書き換えない（超過情報は over_cap_flag のみで表現）。
    # 店長 UI は over_cap_flag から警告表示を導出する。
    flagged = 0
    for r in rows:
        day, slots = shift_slots[r["id"]]
        is_over = any((day, sl) in over for sl in slots)
        new_flag = 1 if is_over else 0
        if is_over:
            flagged += 1
        if new_flag != (r.get("over_cap_flag") or 0):
            execute("UPDATE shifts SET over_cap_flag=? WHERE id=? AND shop_id=?",
                    (new_flag, r["id"], shop_id))
    return flagged


def _find_shorten_candidate(o, target_start_iso, target_end_iso, shop_id, exclude_id=None):
    """対象シフト o を短縮する最適な (new_s, new_e) を探す。

    候補（4h 以上を確保できるもの）:
      1. target の前: o.start 〜 target.start  （target と重ならない）
      2. target の後: target.end 〜 o.end       （target と重ならない）
      3. o の前半4h: o.start 〜 o.start+4h     （target と部分重なり OK）
      4. o の後半4h: o.end-4h 〜 o.end         （target と部分重なり OK）

    選択基準:
      - cap 超過スロット数を最も減らす候補を選ぶ（0 になれば完全解消）。
      - 複数社員の累積短縮が必要なケースでも、各 o が「cap 超過を減らす方向」に
        短縮されるため、最終的に cap 内に収まる。
    """
    from datetime import timedelta
    try:
        o_s = parse_iso(o["start_datetime"])
        o_e = parse_iso(o["end_datetime"])
        t_s = parse_iso(target_start_iso)
        t_e = parse_iso(target_end_iso)
    except Exception:
        return None

    candidates = []
    # 1. target の前
    pre_min = (t_s - o_s).total_seconds() / 60
    if pre_min >= 4 * 60:
        candidates.append((o["start_datetime"], target_start_iso))
    # 2. target の後
    post_min = (o_e - t_e).total_seconds() / 60
    if post_min >= 4 * 60:
        candidates.append((target_end_iso, o["end_datetime"]))
    # 3. o の前半4h（target と部分重なりを許容）
    if (o_e - o_s).total_seconds() / 60 >= 4 * 60:
        front_end = (o_s + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S")
        candidates.append((o["start_datetime"], front_end))
        # 4. o の後半4h
        back_start = (o_e - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S")
        candidates.append((back_start, o["end_datetime"]))

    if not candidates:
        return None

    # ベースライン: o を短縮しない状態での超過スロット数
    base_over = _count_over_cap_slots(shop_id, target_start_iso, target_end_iso, exclude_id=exclude_id)

    # 各候補で cap 超過スロット数を測定 → 最も減らすものを選ぶ
    best_pair = None
    best_over = base_over  # 短縮前と同等かより悪いなら採用しない
    for new_s, new_e in candidates:
        new_work = minutes_between(new_s, new_e)
        if new_work < 4 * 60:
            continue
        execute("UPDATE shifts SET start_datetime=?, end_datetime=? WHERE id=?",
                (new_s, new_e, o["id"]))
        cur_over = _count_over_cap_slots(shop_id, target_start_iso, target_end_iso, exclude_id=exclude_id)
        if cur_over < best_over:
            best_over = cur_over
            best_pair = (new_s, new_e)
        # 戻す（best_pair が確定したら最後に UPDATE し直す）
        execute("UPDATE shifts SET start_datetime=?, end_datetime=? WHERE id=?",
                (o["start_datetime"], o["end_datetime"], o["id"]))

    if best_pair is not None:
        # best_pair を確定 UPDATE
        new_s, new_e = best_pair
        execute("UPDATE shifts SET start_datetime=?, end_datetime=? WHERE id=?",
                (new_s, new_e, o["id"]))
        return best_pair
    return None


def _auto_adjust_for_overlap(shop_id, target_staff_id, target_start_iso, target_end_iso, exclude_id=None):
    """target シフトを配置するため、cap 超過を解消するのに必要な他シフトを短縮（自動調整）。

    ユーザー要望「シフトが被っているなら基本的に社員の時間を減らして調整すべき」を実装。
    ※ 社員(employee)を優先的に短縮（バイトは最後）。
    ※ cap 超過が解消したら短縮を停止（過剰短縮を防ぐ）。
    ※ 短縮候補は「前詰め/後ろ詰め/前半4h/後半4h」の4パターンを試す
       （target に完全包含されるシフトも短縮可能）。
    戻り値: adjustments = [{shift_id, staff_id, name, role, old_start, old_end, new_start, new_end, message}]
    """
    day = (target_start_iso or "")[:10]
    if not day:
        return []
    try:
        target_s = parse_iso(target_start_iso); target_e = parse_iso(target_end_iso)
    except Exception:
        return []
    others = query_all(
        "SELECT sh.id, sh.staff_id, s.name, s.role, sh.start_datetime, sh.end_datetime "
        "FROM shifts sh JOIN staffs s ON sh.staff_id=s.id "
        "WHERE sh.shop_id=? AND sh.status='confirmed' AND sh.staff_id!=? "
        "AND sh.start_datetime>=? AND sh.start_datetime<=?",
        (shop_id, target_staff_id, day + "T00:00:00", day + "T23:59:59"))
    # target と重なるシフトを候補とする
    candidates = []
    for o in others:
        if exclude_id and str(o["id"]) == str(exclude_id):
            continue
        try:
            o_s = parse_iso(o["start_datetime"]); o_e = parse_iso(o["end_datetime"])
        except Exception:
            continue
        if o_s < target_e and o_e > target_s:
            candidates.append(o)
    # 社員優先で短縮（ユーザー要望: 社員の時間を減らして調整）
    candidates.sort(key=lambda o: (0 if o["role"] == "employee" else 1, o["id"]))
    adjustments = []
    for o in candidates:
        # 現状で target を仮配置したとき cap 超過が残っているか？（都度DB更新済みの状態で評価）
        over, _req, _cur = _check_slot_cap(shop_id, target_start_iso, target_end_iso, exclude_id=exclude_id)
        if not over:
            break  # cap 超過なし → 残り候補は短縮しない
        # o を短縮する最適候補を探す（仮 UPDATE → チェック → 戻す/確定 を内部で実施）
        pair = _find_shorten_candidate(o, target_start_iso, target_end_iso, shop_id, exclude_id)
        if pair is None:
            continue  # この o では解消不可 → 次の候補へ
        new_s, new_e = pair
        # break 計算して最終 UPDATE（break_time_minutes を補正）
        new_work = minutes_between(new_s, new_e)
        brk = compute_break_minutes(new_work)
        execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=? WHERE id=? AND shop_id=?",
                (new_s, new_e, brk, o["id"], shop_id))
        adjustments.append({
            "shift_id": o["id"], "staff_id": o["staff_id"], "name": o["name"], "role": o["role"],
            "old_start": o["start_datetime"], "old_end": o["end_datetime"],
            "new_start": new_s, "new_end": new_e,
            "message": f"{o['name']}さんのシフトを {o['start_datetime'][11:16]}-{o['end_datetime'][11:16]} → {new_s[11:16]}-{new_e[11:16]} に短縮しました。",
        })
    return adjustments


# ===========================================================
# ヘルスチェック
# ===========================================================
@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "shift-saas-flask", "lang": "python", "now_jst": jst_now().isoformat()})


# ===========================================================
# 初期化（デモデータ）
# ===========================================================
@app.post("/api/init")
def handle_init():
    """初回セットアップ: 管理者が未登録の場合のみ、初期管理者を作成。

    ※ 認証不要のエンドポイントなので、環境変数 ALLOW_INIT=1 のときだけ有効にする。
       既定で無効なのは、DBリセット直後に第三者が初期管理者を作れてしまうため。
    ※ 初期パスワードはランダム生成し、このレスポンスで1回だけ返す（保存も再表示もしない）。
    """
    if os.getenv("ALLOW_INIT") != "1":
        # 未認証で叩けるエンドポイントなので、有効化条件（環境変数名）はレスポンスに出さない。
        # 運用者向けの案内はサーバログにだけ出す。
        print("[init] blocked: 初期セットアップは無効。有効化するには環境変数 ALLOW_INIT=1 "
              "を設定してから再起動し、セットアップ後に必ず戻すこと。", flush=True)
        abort(403, description="初期セットアップは無効です")
    msg = {"admin": "", "shop": "", "logins": {}}
    if not query_one("SELECT id FROM system_admins LIMIT 1"):
        initial_pw = secrets.token_urlsafe(12)
        execute("INSERT INTO system_admins (admin_id, password_hash, name) VALUES (?,?,?)",
                ("admin", hash_password(initial_pw), "システム管理者"))
        msg["admin"] = "管理者を作成しました。このパスワードは再表示されません。"
        msg["logins"] = {"admin": {"id": "admin", "password": initial_pw}}
        return jsonify({"ok": True, "message": "初期管理者を作成しました", "details": msg,
                        "logins": msg["logins"]})
    return jsonify({"ok": True, "message": "管理者は既に存在します。ログインしてください。",
                    "details": msg, "logins": {}})


# ===========================================================
# ログイン（ID統合）/ ログアウト / me
# ===========================================================
@app.post("/api/login")
def login():
    """統一ログイン（店舗コード + ユーザーコード + パスワード）。

    【仕様】
      - システム管理者: ユーザーコード に "admin" を指定（店舗コードは任意）。
        ※ admin_id が "admin" 以外の場合は、店舗コード側に admin_id を入れてもOK。
        ※ 店舗コードを空にした場合、ユーザーコードに admin_id を直接指定してもよい
          （Phase 2 で複数管理者に対応。system_admins に一致する行がある場合のみ
          管理者ログインとして扱う）。
      - 店舗管理者: staffs.role='manager' のスタッフ → role='shop' セッション。
      - 一般スタッフ: staffs.role='employee'/'part_time' → role='staff' セッション。
      - 後方互換: user_code == shop_code の場合、shops テーブルでの旧店主ログイン可。

    【背景】
      かつて staff_code 単独で検索したため別店舗同コードで誤ログインする致命的
      バグがあった。本仕様では (shop_code, staff_code) の複合キーで一意特定し、
      さらに 'manager' ロールで店舗権限も一本化する。
    """
    body = request.get_json(silent=True) or {}
    shop_code = _sanitize_login_code(body.get("shop_code") or body.get("id"))
    user_code = _sanitize_login_code(body.get("user_code") or body.get("staff_code"))
    pw = body.get("password") or ""

    # 【なぜ長さ上限か】
    #   shop_code / user_code は無制限だと、そのまま attempt_key（login_attempts の
    #   主キー）と audit_logs.actor_name に入る。毎回違う値を送るだけでキーが分散し、
    #   ロックが一度も発動しないまま未認証クライアントが行を無限に増やせてしまう
    #   （本番は Cloudflare D1 で書き込み課金・ストレージ上限がある）。
    #   正当な店舗コード・ユーザーコードが64文字を超えることは無いので、
    #   切り詰めず 400 で弾く（＝1行も書かずに帰す）。
    #   入力不備であって認証試行ではないため、_record_login_failure は呼ばない。
    if len(shop_code) > _LOGIN_CODE_MAX or len(user_code) > _LOGIN_CODE_MAX:
        raise ValueError(f"店舗コード・ユーザーコードは{_LOGIN_CODE_MAX}文字以内で入力してください")
    if not pw:
        raise ValueError("パスワードを入力してください")

    # 認証を試みる前にロック状態を確認する（正しいパスワードでもロック中は通さない）
    attempt_key = _login_attempt_key(shop_code, user_code)
    _check_login_lock(attempt_key)

    # ---- システム管理者 ("admin" マジックワード) ----
    if user_code == "admin" or shop_code == "admin":
        # もう片方のフィールドが "admin" 以外の値なら、それを admin_id として試す。
        # 見つからなければ "admin" にフォールバック（「どちらかに admin を入れるだけ」の体験）。
        other = user_code if user_code != "admin" else shop_code
        admin_id_guess = other if other and other != "admin" else "admin"
        admin = query_one("SELECT * FROM system_admins WHERE admin_id=?", (admin_id_guess,))
        if not admin and admin_id_guess != "admin":
            admin = query_one("SELECT * FROM system_admins WHERE admin_id=?", ("admin",))
        if admin and verify_password(pw, admin["password_hash"]):
            _clear_login_failures(attempt_key)
            audit("auth.login", target_type="system_admin", target_id=admin["id"],
                  actor_role="admin", actor_id=admin["id"], actor_name=admin.get("name"),
                  detail=f"admin_id={admin['admin_id']}")
            return jsonify(_create_session("admin", admin["id"], None, admin))
        _record_login_failure(attempt_key)
        audit("auth.login_failed", actor_role="anonymous",
              actor_name=admin_id_guess, detail="管理者ログイン失敗")
        raise ValueError("管理者IDまたはパスワードが正しくありません")

    # ---- システム管理者（店舗コード無し・ユーザーコードに admin_id を直接指定）----
    # 【なぜ必要か】
    #   上の分岐は「どちらかの欄に "admin" という語」を要求するため、admin_id が
    #   "admin" 以外の2人目以降の管理者（Phase 2 で追加可能になった）はそのままでは
    #   ログインできない。店舗コードを空にしたままユーザーコードだけで system_admins
    #   を引けた場合に限り管理者ログインとして扱う（店舗・スタッフは必ず shop_code
    #   を要するため、ここで衝突する余地は無い）。
    if not shop_code and user_code:
        admin = query_one("SELECT * FROM system_admins WHERE admin_id=?", (user_code,))
        if admin:
            if verify_password(pw, admin["password_hash"]):
                _clear_login_failures(attempt_key)
                audit("auth.login", target_type="system_admin", target_id=admin["id"],
                      actor_role="admin", actor_id=admin["id"], actor_name=admin.get("name"),
                      detail=f"admin_id={admin['admin_id']}")
                return jsonify(_create_session("admin", admin["id"], None, admin))
            _record_login_failure(attempt_key)
            audit("auth.login_failed", actor_role="anonymous",
                  actor_name=user_code, detail="管理者ログイン失敗")
            raise ValueError("管理者IDまたはパスワードが正しくありません")

    # 入力不備は認証の試行ではないので失敗としては数えない
    if not shop_code or not user_code:
        raise ValueError("店舗コードとユーザーコードを入力してください")

    # ---- 店舗管理者 / スタッフ: (shop_code, user_code) で一意検索 ----
    staff = query_one(
        "SELECT s.* FROM staffs s JOIN shops sh ON s.shop_id=sh.id "
        "WHERE sh.shop_code=? AND s.staff_code=? AND s.is_resigned=0 AND sh.is_active=1",
        (shop_code, user_code))
    if staff and verify_password(pw, staff["password_hash"]):
        _clear_login_failures(attempt_key)
        audit("auth.login", target_type="staff", target_id=staff["id"], shop_id=staff["shop_id"],
              actor_role="shop" if staff["role"] == "manager" else "staff",
              actor_id=staff["id"], actor_name=staff.get("name"),
              detail=f"role={staff['role']}")
        if staff["role"] == "manager":
            # manager は店舗権限(shopping) → user オブジェクトは shops 行を返す
            shop = query_one("SELECT * FROM shops WHERE id=?", (staff["shop_id"],))
            return jsonify(_create_session("shop", staff["id"], staff["shop_id"], shop))
        # 一般スタッフ
        return jsonify(_create_session("staff", staff["id"], staff["shop_id"], staff))

    # ---- 後方互換: shops テーブルによる旧店主ログイン（user_code == shop_code の場合） ----
    if user_code == shop_code:
        shop = query_one("SELECT * FROM shops WHERE shop_code=? AND is_active=1", (shop_code,))
        if shop and verify_password(pw, shop["password_hash"]):
            _clear_login_failures(attempt_key)
            audit("auth.login", target_type="shop", target_id=shop["id"], shop_id=shop["id"],
                  actor_role="shop", actor_id=shop["id"], actor_name=shop.get("shop_name"),
                  detail="旧仕様の店主ログイン")
            return jsonify(_create_session("shop", shop["id"], shop["id"], shop))

    _record_login_failure(attempt_key)
    audit("auth.login_failed", actor_role="anonymous",
          actor_name=f"{shop_code}/{user_code}", detail="店舗またはスタッフのログイン失敗")
    raise ValueError("店舗コード・ユーザーコードまたはパスワードが正しくありません")


def _create_session(role, user_id, shop_id, user):
    token = gen_token()
    expires = (jst_now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    if shop_id is None:
        execute("INSERT INTO sessions (token, role, user_id, shop_id, expires_at) VALUES (?,?,?,NULL,?)",
                (token, role, user_id, expires))
    else:
        execute("INSERT INTO sessions (token, role, user_id, shop_id, expires_at) VALUES (?,?,?,?,?)",
                (token, role, user_id, shop_id, expires))
    return {"token": token, "role": role, "user": strip_password(user)}


def _session_actor_name(session):
    """セッション行から操作者の表示名を引く（引けなければ None）。

    shop ロールの user_id は staffs.id（manager）と shops.id（旧店主）が混在するため、
    require_auth と同じく shop_id を使って店舗名を引く。
    """
    try:
        role = session.get("role")
        if role == "admin":
            row = query_one("SELECT name FROM system_admins WHERE id=?", (session.get("user_id"),))
            return row.get("name") if row else None
        if role == "shop":
            row = query_one("SELECT shop_name FROM shops WHERE id=?", (session.get("shop_id"),))
            return row.get("shop_name") if row else None
        row = query_one("SELECT name FROM staffs WHERE id=?", (session.get("user_id"),))
        return row.get("name") if row else None
    except Exception:
        return None  # 監査のための付加情報なので、引けなくてもログアウト自体は通す


@app.post("/api/logout")
def logout():
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if token:
        session = query_one("SELECT role, user_id, shop_id FROM sessions WHERE token=?", (token,))
        if session:
            # トークン削除の前に監査する（削除後だと誰がログアウトしたか分からなくなるため）
            # logout は require_auth を通らないので g.user が無い。auth.login と同じ画面で
            # 表示が揃うよう、セッションの role / user_id から氏名を引いて明示的に渡す。
            audit("auth.logout", shop_id=session.get("shop_id"),
                  actor_role=session["role"], actor_id=session["user_id"],
                  actor_name=_session_actor_name(session))
        execute("DELETE FROM sessions WHERE token=?", (token,))
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    role, user, _ = require_auth(["admin", "shop", "staff"])
    result = {"role": role, "user": user}
    # shop ロールの場合は manager ロールかどうか、staff 情報も併せて返す
    # （UI で「店舗管理者」vs「旧仕様店主」を正確に区別するため）
    if role == "shop":
        try:
            staff = _resolve_my_staff()
        except Exception:
            staff = None
        if staff:
            result["staff_info"] = {
                "id": staff["id"], "name": staff["name"], "role": staff["role"],
                "staff_code": staff["staff_code"],
            }
            result["is_manager"] = staff.get("role") == "manager"
        else:
            result["staff_info"] = None
            result["is_manager"] = False
    return jsonify(result)


# ===========================================================
# 店舗
# ===========================================================
def _shop_ctx():
    require_auth(["shop"])
    return g.user, g.user["id"], parse_settings(g.user.get("settings"))


def _assert_staff_in_shop(staff_id, shop_id):
    """staff_id が自店舗に属することを検証する（他店舗・存在しないIDは404）。"""
    row = query_one("SELECT id FROM staffs WHERE id=? AND shop_id=?", (staff_id, shop_id))
    if row is None:
        abort(404, description="スタッフが見つかりません")


def _get_shop_shift_end_time(shop_id):
    """店舗のシフト終了時刻を取得（shift_hours優先 → shift_patterns → 22:00）。

    「いつでも」希望のデフォルト終了時刻等で使う。
    """
    try:
        shop = query_one("SELECT settings FROM shops WHERE id=?", (shop_id,))
        if shop:
            settings = parse_settings(shop["settings"])
            sh = settings.get("shift_hours") or {}
            bulk = sh.get("bulk") or {}
            if bulk.get("end_time"):
                return bulk["end_time"]
            # 曜日別設定の場合は最大を探す
            days = sh.get("days") or {}
            end_times = [d.get("end_time") for d in days.values() if d.get("end_time")]
            if end_times:
                return max(end_times)
    except Exception:
        pass
    # shift_patterns の最遅終了時刻
    try:
        rows = query_all("SELECT end_time FROM shift_patterns WHERE shop_id=?", (shop_id,))
        if rows:
            return max(r["end_time"] for r in rows if r.get("end_time"))
    except Exception:
        pass
    return "22:00"


def _get_shop_shift_start_time(shop_id):
    """店舗のシフト開始時刻を取得（shift_hours優先 → shift_patterns → 09:00）。"""
    try:
        shop = query_one("SELECT settings FROM shops WHERE id=?", (shop_id,))
        if shop:
            settings = parse_settings(shop["settings"])
            sh = settings.get("shift_hours") or {}
            bulk = sh.get("bulk") or {}
            if bulk.get("start_time"):
                return bulk["start_time"]
            days = sh.get("days") or {}
            start_times = [d.get("start_time") for d in days.values() if d.get("start_time")]
            if start_times:
                return min(start_times)
    except Exception:
        pass
    try:
        rows = query_all("SELECT start_time FROM shift_patterns WHERE shop_id=?", (shop_id,))
        if rows:
            return min(r["start_time"] for r in rows if r.get("start_time"))
    except Exception:
        pass
    return "09:00"


def _check_student_only_shift(shop_id, staff_id, start_iso, exclude_id=None):
    """学生アルバイトのみで構成されるシフトになるかをチェック。

    指定 staff を追加した場合、当日の当該時間帯に勤務するスタッフが
    全員 student ロールになる場合は（社会人不在で）NG。
    戻り値: (is_ng: bool, message: str or None)
    """
    try:
        target = query_one("SELECT role FROM staffs WHERE id=?", (staff_id,))
    except Exception:
        target = None
    if not target:
        return (False, None)
    # 学生以外のロールを追加する場合は問題なし
    if target["role"] != "student":
        return (False, None)
    # 当該日の confirmed シフトと重なるスタッフを抽出
    day = (start_iso or "")[:10]
    if not day:
        return (False, None)
    rows = query_all(
        "SELECT sh.id, sh.staff_id, sh.start_datetime, sh.end_datetime, s.role "
        "FROM shifts sh JOIN staffs s ON sh.staff_id=s.id "
        "WHERE sh.shop_id=? AND sh.status='confirmed' "
        "AND sh.start_datetime>=? AND sh.start_datetime<=?",
        (shop_id, day + "T00:00:00", day + "T23:59:59"))
    try:
        s_new = parse_iso(start_iso)
        # 終了時刻が無い場合はとりあえず開始+1hと仮定（簡易チェック）
        from datetime import timedelta as _td
        e_new = s_new + _td(hours=1)
    except Exception:
        return (False, None)
    overlapping_roles = set()
    for r in rows:
        if exclude_id and str(r["id"]) == str(exclude_id):
            continue
        try:
            s = parse_iso(r["start_datetime"]); e = parse_iso(r["end_datetime"])
        except Exception:
            continue
        # 時間帯が重なるか
        if s_new < e and s < e_new:
            overlapping_roles.add(r["role"])
    # 追加対象（student）を加えた構成
    overlapping_roles.add("student")
    # 学生しかいない状態（社会人ロール employee/manager/part_time がいない）
    non_student_roles = overlapping_roles - {"student"}
    if not non_student_roles:
        return (True, "学生アルバイトのみで構成されるシフトは作成できません（社会人スタッフを少なくとも1名配置してください）")
    return (False, None)


@app.get("/api/shop/dashboard")
def shop_dashboard():
    """ダッシュボード用の統計データを一括取得。"""
    shop, shop_id, settings = _shop_ctx()
    today = jst_today().strftime("%Y-%m-%d")
    today_shifts = query_all(
        "SELECT sh.*, s.name as staff_name, s.role as staff_role FROM shifts sh JOIN staffs s ON sh.staff_id=s.id "
        "WHERE sh.shop_id=? AND sh.status='confirmed' AND sh.start_datetime>=? AND sh.start_datetime<=?",
        (shop_id, today + "T00:00:00", today + "T23:59:59"))
    # 月間データ
    month_start = today[:8] + "01"
    month_end = today[:8] + "31"
    month_shifts = query_all("SELECT * FROM shifts WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=?",
                             (shop_id, month_start + "T00:00:00", month_end + "T23:59:59"))
    staffs = query_all("SELECT id, name, role, hourly_wage, is_resigned FROM staffs WHERE shop_id=?", (shop_id,))
    active_staff = [s for s in staffs if not s.get("is_resigned")]
    patterns = query_all("SELECT * FROM shift_patterns WHERE shop_id=?", (shop_id,))
    creq = query_all("SELECT * FROM change_requests WHERE shop_id=? AND status='pending'", (shop_id,))
    req_shifts = query_all("SELECT * FROM shifts WHERE shop_id=? AND status='requested'", (shop_id,))
    notif = query_all("SELECT * FROM notifications WHERE shop_id=? AND staff_id IS NULL AND is_read=0", (shop_id,))

    # 時間帯別の今日の出勤人数
    hourly = {}
    for sh in today_shifts:
        for hr in range(int(sh["start_datetime"][11:13]), int(sh["end_datetime"][11:13])):
            hourly[hr] = hourly.get(hr, 0) + 1

    # 人件費計算
    wage_map = {s["id"]: s["hourly_wage"] for s in staffs}
    total_cost = 0
    total_hours = 0
    daily_cost = {}  # date -> cost
    for sh in month_shifts:
        work = minutes_between(sh["start_datetime"], sh["end_datetime"]) - (sh.get("break_time_minutes") or 0)
        work = max(0, work)
        wage = wage_map.get(sh["staff_id"], 0)
        cost = int(work / 60 * wage)
        total_cost += cost
        total_hours += work / 60
        d = sh["start_datetime"][:10]
        daily_cost[d] = daily_cost.get(d, 0) + cost

    # 不足計算
    overrides = shift_engine.load_weekday_overrides(shop_id)
    shortage = shift_engine.compute_shortage(month_shifts, patterns, month_start, month_end, overrides)
    # ★【重なりパターン補正】複数パターンが時間帯を重ねる場合、パターン別集計だと
    # 「同じ時間帯がN回カウントされる」過大表示になる（インシデント）。
    # 時間帯別の一意不足で「今日の不足枠数」「月間不足枠数」を算出する。
    unique_shortage = shift_engine.compute_shortage_unique_hours(
        month_shifts, patterns, month_start, month_end, overrides)
    today_unique = [s for s in unique_shortage if s["date"] == today]

    return jsonify({
        "today_attendance": len(today_shifts),
        "today_shifts": [{"name": s["staff_name"], "start": s["start_datetime"][11:16], "end": s["end_datetime"][11:16], "role": s["staff_role"]} for s in today_shifts],
        "today_hourly": [{"hour": h, "count": c} for h, c in sorted(hourly.items())],
        # 表示用の「枠数」は時間帯別一意（重なりをマージ）
        "today_shortage": len(today_unique),
        "today_shortage_breakdown": today_unique,
        "month_cost": total_cost,
        "month_hours": round(total_hours, 1),
        "staff_count": len(active_staff),
        "employee_count": sum(1 for s in active_staff if s["role"] in ("employee", "manager")),
        "part_time_count": sum(1 for s in active_staff if s["role"] == "part_time"),
        "manager_count": sum(1 for s in active_staff if s["role"] == "manager"),
        "pending_requests": len(req_shifts),
        "pending_approvals": len(creq),
        "unread_notifications": len(notif),
        "daily_cost_series": [{"date": d, "cost": c} for d, c in sorted(daily_cost.items())][-30:],
        "shortage_total": len(unique_shortage),
        "patterns": [{"name": p["pattern_name"], "start": p["start_time"], "end": p["end_time"], "required": p["required_staff"]} for p in patterns],
    })


@app.get("/api/shop/notifications")
def shop_notifs():
    shop, shop_id, _ = _shop_ctx()
    rows = query_all("SELECT id, type, title, body, is_read, created_at FROM notifications WHERE shop_id=? AND staff_id IS NULL ORDER BY id DESC LIMIT 50", (shop_id,))
    unread = sum(1 for r in rows if not r.get("is_read"))
    return jsonify({"notifications": rows, "unread": unread})


@app.put("/api/shop/notifications/read-all")
def shop_notifs_readall():
    shop, shop_id, _ = _shop_ctx()
    execute("UPDATE notifications SET is_read=1 WHERE shop_id=? AND staff_id IS NULL", (shop_id,))
    return jsonify({"ok": True})


@app.get("/api/shop/settings")
def shop_settings_get():
    shop, shop_id, settings = _shop_ctx()
    return jsonify({"id": shop_id, "shop_code": shop["shop_code"], "shop_name": shop["shop_name"],
                    "is_active": shop["is_active"], "settings": settings})


@app.put("/api/shop/settings")
def shop_settings_put():
    shop, shop_id, settings = _shop_ctx()
    body = request.get_json(silent=True) or {}
    cur = dict(settings)
    if body.get("settings"):
        cur.update(body["settings"])
    execute("UPDATE shops SET shop_name=?, settings=? WHERE id=?",
            (body.get("shop_name", shop["shop_name"]), json.dumps(cur, ensure_ascii=False), shop_id))
    return jsonify({"ok": True})


@app.put("/api/shop/password")
def shop_password():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    full = query_one("SELECT password_hash FROM shops WHERE id=?", (shop_id,))
    if not verify_password(body.get("current_password", ""), full["password_hash"]):
        abort(400, description="現在のパスワードが正しくありません")
    err = validate_password(body.get("new_password", ""))
    if err:
        abort(400, description=err)
    execute("UPDATE shops SET password_hash=? WHERE id=?", (hash_password(body["new_password"]), shop_id))
    return jsonify({"ok": True})


# ===========================================================
# 店舗管理者自身の希望・シフト（manager も勤務者として希望を出せる）
# ===========================================================
def _resolve_my_staff():
    """shop ロールでログイン中の manager の staffs 行を返す。
    旧店主ログイン（shops.id を user_id とする後方互換）の場合は None を返す。"""
    auth_h = request.headers.get("Authorization", "")
    token = auth_h[7:] if auth_h.startswith("Bearer ") else ""
    session = query_one("SELECT * FROM sessions WHERE token=?", (token,))
    if not session:
        return None
    user_id = session.get("user_id")
    shop_id = session.get("shop_id")
    if not user_id or not shop_id:
        return None
    # user_id が staffs.id として存在するか確認（旧店主ログイン対策）
    return query_one("SELECT * FROM staffs WHERE id=? AND shop_id=?", (user_id, shop_id))


@app.get("/api/shop/me")
def shop_me():
    """shop ロールでログイン中のユーザー自身のスタッフ情報を返す。
    manager ロールでログインしている場合はそのスタッフ情報、
    旧店主ログインの場合は staff=null を返す。"""
    require_auth(["shop"])
    staff = _resolve_my_staff()
    if not staff:
        return jsonify({"staff": None})
    return jsonify({"staff": {
        "id": staff["id"], "shop_id": staff["shop_id"],
        "staff_code": staff["staff_code"], "name": staff["name"],
        "role": staff["role"], "hourly_wage": staff["hourly_wage"],
    }})


@app.post("/api/shop/my-requests")
def shop_my_requests_post():
    """店舗管理者が自分自身の希望シフトを提出。

    staff/requests と同様に wish_history にも保存され、
    AI自動生成入力の対象となる。
    """
    require_auth(["shop"])
    staff = _resolve_my_staff()
    if not staff:
        abort(400, description="このアカウントでは希望提出ができません（manager ロールでログインしてください）")
    body = request.get_json(silent=True) or {}
    items = body.get("shifts") or []
    if not items:
        abort(400, description="希望がありません")
    shop_id = staff["shop_id"]
    first_day = items[0]["start_datetime"][:10]
    period = query_one(
        "SELECT * FROM shift_request_periods WHERE shop_id=? AND is_active=1 AND start_date<=? AND end_date>=? ORDER BY deadline DESC LIMIT 1",
        (shop_id, first_day, first_day))
    if not period:
        abort(400, description="この日程は募集期間外です")
    if period["deadline"] < jst_today().strftime("%Y-%m-%d"):
        abort(400, description=f"締切（{period['deadline']}）を過ぎています")
    count = 0
    skipped_overlap = 0
    for sh in items:
        avail = sh.get("availability")
        start_dt = normalize_iso(sh["start_datetime"])
        if avail == "rest":
            # 休希望: 終日扱い（00:00〜23:59）
            end_dt = normalize_iso(sh.get("end_datetime")) or (start_dt[:10] + "T23:59:59")
        elif avail:
            # 「いつでも/早番/遅番」: 終了時刻が未指定なら店舗のシフト時間設定から取得
            shop_end = _get_shop_shift_end_time(staff["shop_id"])
            end_dt = normalize_iso(sh.get("end_datetime")) or (start_dt[:10] + f"T{shop_end}:00")
        else:
            end_dt = normalize_iso(sh["end_datetime"])
        # 重複チェック（自身の confirmed + 既存希望）。ただし rest 希望は重複OK
        if avail != "rest":
            overlap, _conflict = _check_staff_overlap(
                shop_id, staff["id"], start_dt, end_dt, include_requested=True)
            if overlap:
                skipped_overlap += 1
                continue
        if avail == "rest":
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason, availability) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, staff["id"], start_dt, end_dt, "requested", "管理者:休希望", avail))
        elif avail:
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason, availability) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, staff["id"], start_dt, end_dt, "requested", "管理者希望(柔軟)", avail))
        else:
            work = minutes_between(start_dt, end_dt)
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, staff["id"], start_dt, end_dt, compute_break_minutes(work), "requested", "管理者希望提出"))
        # wish_history にも保存
        existing = None
        try:
            existing = query_one(
                "SELECT id FROM wish_history WHERE staff_id=? AND start_datetime=? AND end_datetime=?",
                (staff["id"], start_dt, end_dt))
        except Exception:
            pass
        if existing is None:
            try:
                execute("INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, availability, note) VALUES (?,?,?,?,?,?)",
                        (shop_id, staff["id"], start_dt, end_dt, avail, "管理者希望提出"))
            except Exception:
                pass
        count += 1
    msg = f"{count}件の希望を提出しました"
    if skipped_overlap:
        msg += f"（{skipped_overlap}件は同日時間重複でスキップ）"
    return jsonify({"ok": True, "submitted": count, "skipped_overlap": skipped_overlap, "message": msg})


@app.get("/api/shop/my-requests")
def shop_my_requests_list():
    """自分の（pending中の）希望一覧。"""
    require_auth(["shop"])
    staff = _resolve_my_staff()
    if not staff:
        return jsonify({"requests": []})
    rows = query_all(
        "SELECT id, start_datetime, end_datetime, status, reason, availability "
        "FROM shifts WHERE staff_id=? AND status='requested' ORDER BY start_datetime",
        (staff["id"],))
    return jsonify({"requests": rows})


@app.delete("/api/shop/my-requests/<int:rid>")
def shop_my_requests_del(rid):
    """自分の希望を削除。"""
    require_auth(["shop"])
    staff = _resolve_my_staff()
    if not staff:
        abort(404, description="このアカウントでは希望削除ができません")
    sh = query_one(
        "SELECT start_datetime, end_datetime FROM shifts "
        "WHERE id=? AND staff_id=? AND status='requested'",
        (rid, staff["id"]))
    if sh:
        execute("DELETE FROM shifts WHERE id=? AND staff_id=? AND status='requested'",
                (rid, staff["id"]))
        try:
            execute("DELETE FROM wish_history WHERE staff_id=? AND start_datetime=? AND end_datetime=?",
                    (staff["id"], sh["start_datetime"], sh["end_datetime"]))
        except Exception:
            pass
    return jsonify({"ok": True})


@app.get("/api/shop/my-wishes")
def shop_my_wishes():
    """自分の希望履歴（確定/却下問わず全て）。"""
    require_auth(["shop"])
    staff = _resolve_my_staff()
    if not staff:
        return jsonify({"wishes": []})
    try:
        rows = query_all(
            "SELECT id, start_datetime, end_datetime, availability, submitted_at, note "
            "FROM wish_history WHERE staff_id=? "
            "ORDER BY start_datetime DESC LIMIT 200",
            (staff["id"],))
    except Exception:
        rows = []
    return jsonify({"wishes": rows})


@app.get("/api/shop/my-shifts")
def shop_my_shifts():
    """自分の確定シフトを取得（今月〜来月など期間指定可）。"""
    require_auth(["shop"])
    staff = _resolve_my_staff()
    if not staff:
        return jsonify({"shifts": []})
    start_d = request.args.get("start")
    end_d = request.args.get("end")
    sql = ("SELECT id, shop_id, staff_id, start_datetime, end_datetime, "
           "break_time_minutes, status, reason FROM shifts WHERE staff_id=?")
    params = [staff["id"]]
    if start_d:
        sql += " AND start_datetime>=?"
        params.append(start_d + "T00:00:00")
    if end_d:
        sql += " AND start_datetime<=?"
        params.append(end_d + "T23:59:59")
    sql += " ORDER BY start_datetime"
    rows = query_all(sql, tuple(params))
    return jsonify({"shifts": rows})


# ===========================================================
# シフト時間設定（シフト作成可能時間）
# ===========================================================
# 設定スキーマ (shops.settings.shift_hours):
#   {
#     "bulk_mode": bool,                # True=一括設定、False=曜日別
#     "bulk": {"start_time": "09:00", "end_time": "22:00", "is_closed": false},
#     "days": {                          # 曜日別設定（祝日含む）
#       "0": {"start_time": "...", "end_time": "...", "is_closed": false},  # 日
#       "1": {...},  # 月
#       ...
#       "6": {...},  # 土
#       "holiday": {...}  # 祝日
#     }
#   }
_DEFAULT_SHIFT_HOURS = {
    "bulk_mode": True,
    "bulk": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
    "days": {
        "0": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
        "1": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
        "2": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
        "3": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
        "4": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
        "5": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
        "6": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
        "holiday": {"start_time": "09:00", "end_time": "22:00", "is_closed": False},
    },
}

_SHIFT_HOURS_DAY_KEYS = ("0", "1", "2", "3", "4", "5", "6", "holiday")


def _normalize_shift_hours(sh):
    """フロントからの入力を正規化。不正値はデフォルトにフォールバック。"""
    if not isinstance(sh, dict):
        return dict(_DEFAULT_SHIFT_HOURS)
    out = {"bulk_mode": bool(sh.get("bulk_mode", True))}
    # bulk
    bulk_in = sh.get("bulk") or {}
    out["bulk"] = {
        "start_time": _validate_hhmm(bulk_in.get("start_time"), "09:00"),
        "end_time": _validate_hhmm(bulk_in.get("end_time"), "22:00"),
        "is_closed": bool(bulk_in.get("is_closed", False)),
    }
    # days
    days_in = sh.get("days") or {}
    days_out = {}
    for k in _SHIFT_HOURS_DAY_KEYS:
        d = days_in.get(k) or {}
        days_out[k] = {
            "start_time": _validate_hhmm(d.get("start_time"), "09:00"),
            "end_time": _validate_hhmm(d.get("end_time"), "22:00"),
            "is_closed": bool(d.get("is_closed", False)),
        }
    out["days"] = days_out
    return out


def _validate_hhmm(v, default):
    """HH:MM 形式の簡易バリデーション。"""
    if not isinstance(v, str):
        return default
    parts = v.split(":")
    if len(parts) != 2:
        return default
    try:
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 47 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    return default


@app.get("/api/shop/shift-hours")
def shop_shift_hours_get():
    """シフト時間設定を取得。"""
    shop, shop_id, settings = _shop_ctx()
    sh = settings.get("shift_hours")
    if not sh:
        # 従来の business_hours 設定があればそれを流用（後方互換）
        sh = dict(_DEFAULT_SHIFT_HOURS)
    result = _normalize_shift_hours(sh)
    # 祝日日付リストも併せて返す
    try:
        holidays = query_all(
            "SELECT holiday_date, note FROM shop_holidays WHERE shop_id=? ORDER BY holiday_date",
            (shop_id,))
    except Exception:
        holidays = []
    result["holidays"] = holidays
    return jsonify(result)


@app.put("/api/shop/shift-hours")
def shop_shift_hours_put():
    """シフト時間設定を保存。

    body: shift_hours オブジェクト（bulk_mode, bulk, days）
    body.sync_patterns: true の場合、shift_patterns テーブルにも
    時間を反映する（AI生成エンジンは shift_patterns を使うため重要）。
    """
    shop, shop_id, settings = _shop_ctx()
    body = request.get_json(silent=True) or {}
    sh = body.get("shift_hours") or body  # トップレベルでも shift_hours キーでも許容
    normalized = _normalize_shift_hours(sh)
    cur = dict(settings)
    cur["shift_hours"] = normalized
    execute("UPDATE shops SET settings=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), shop_id))

    # オプション: shift_patterns テーブルへ時間を同期
    sync_log = []
    if body.get("sync_patterns"):
        existing = query_all(
            "SELECT id, pattern_name, required_staff FROM shift_patterns WHERE shop_id=?",
            (shop_id,))
        # bulk_mode=True: 単一時間で全パターン更新
        # bulk_mode=False: 曜日別モード → 既存パターンは代表時間（月曜）で更新、
        #                   パターン無ければ月曜の時間で新規作成
        if normalized["bulk_mode"]:
            ref = normalized["bulk"]
        else:
            ref = normalized["days"].get("1") or normalized["bulk"]
        ref_st = ref["start_time"]
        ref_et = ref["end_time"]
        if existing:
            for pat in existing:
                execute(
                    "UPDATE shift_patterns SET start_time=?, end_time=? WHERE id=? AND shop_id=?",
                    (ref_st, ref_et, pat["id"], shop_id))
            sync_log.append(
                f"{len(existing)} 個の既存パターンを {ref_st}-{ref_et} に更新"
                + ("（※曜日別設定時は代表時間=月曜で全パターン統一）" if not normalized["bulk_mode"] else "")
            )
        else:
            execute(
                "INSERT INTO shift_patterns (shop_id, pattern_name, start_time, end_time, required_staff) "
                "VALUES (?,?,?,?,?)",
                (shop_id, "通し", ref_st, ref_et, 2))
            sync_log.append(f"新規パターン「通し」を {ref_st}-{ref_et} で作成（必要人数2）")
    # 祝日リストの差分更新（オプション）
    if "holidays" in body:
        new_dates = set()
        for h in (body.get("holidays") or []):
            d = h.get("holiday_date") if isinstance(h, dict) else h
            if d:
                new_dates.add(d)
        try:
            existing = query_all("SELECT holiday_date FROM shop_holidays WHERE shop_id=?", (shop_id,))
            existing_dates = {r["holiday_date"] for r in existing}
            for d in new_dates - existing_dates:
                execute("INSERT OR IGNORE INTO shop_holidays (shop_id, holiday_date) VALUES (?,?)", (shop_id, d))
            for d in existing_dates - new_dates:
                execute("DELETE FROM shop_holidays WHERE shop_id=? AND holiday_date=?", (shop_id, d))
        except Exception:
            pass
    return jsonify({"ok": True, "shift_hours": normalized, "sync_log": sync_log})


@app.get("/api/shop/holidays")
def shop_holidays_get():
    """店舗の祝日・特別休業日リストを取得。"""
    shop, shop_id, _ = _shop_ctx()
    try:
        rows = query_all(
            "SELECT id, holiday_date, note FROM shop_holidays WHERE shop_id=? ORDER BY holiday_date",
            (shop_id,))
    except Exception:
        rows = []
    return jsonify({"holidays": rows})


@app.post("/api/shop/holidays")
def shop_holidays_post():
    """祝日・特別休業日を1件追加。"""
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    d = body.get("holiday_date")
    if not d:
        abort(400, description="holiday_date が必要です")
    try:
        execute("INSERT OR IGNORE INTO shop_holidays (shop_id, holiday_date, note) VALUES (?,?,?)",
                (shop_id, d, body.get("note") or ""))
    except Exception as e:
        abort(400, description="祝日の追加に失敗しました: " + str(e))
    return jsonify({"ok": True})


@app.delete("/api/shop/holidays/<path:d>")
def shop_holidays_del(d):
    """祝日・特別休業日を1件削除。"""
    shop, shop_id, _ = _shop_ctx()
    execute("DELETE FROM shop_holidays WHERE shop_id=? AND holiday_date=?", (shop_id, d))
    return jsonify({"ok": True})


@app.post("/api/shop/holidays/import-japanese")
def shop_holidays_import_japanese():
    """日本の祝日を一括インポート。

    body:
      - years: [2026, 2027, ...] 指定無し時は今年 + 翌年 + 翌々年（3年分）
      - overwrite: bool 既存の祝日を上書きするか（デフォルト false）

    戻り値: {ok, imported: int, skipped: int, holidays: [{date, name}]}
    """
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    overwrite = bool(body.get("overwrite", False))
    # 対象年
    years = body.get("years")
    if not years or not isinstance(years, list):
        from utils import jst_today
        y0 = jst_today().year
        years = [y0, y0 + 1, y0 + 2]
    # 計算
    all_holidays = []
    for y in years:
        try:
            for h in holidays_jp.japanese_holidays(int(y)):
                all_holidays.append(h)
        except Exception:
            continue
    # 重複排除
    seen = set()
    unique = []
    for h in all_holidays:
        if h["date"] not in seen:
            seen.add(h["date"])
            unique.append(h)
    # 既存取得
    try:
        existing = query_all("SELECT holiday_date FROM shop_holidays WHERE shop_id=?", (shop_id,))
        existing_dates = {r["holiday_date"] for r in existing}
    except Exception:
        existing_dates = set()
    # 登録
    imported = 0
    skipped = 0
    for h in unique:
        if h["date"] in existing_dates and not overwrite:
            skipped += 1
            continue
        try:
            if overwrite and h["date"] in existing_dates:
                execute("UPDATE shop_holidays SET note=? WHERE shop_id=? AND holiday_date=?",
                        (h["name"], shop_id, h["date"]))
            else:
                execute("INSERT OR IGNORE INTO shop_holidays (shop_id, holiday_date, note) VALUES (?,?,?)",
                        (shop_id, h["date"], h["name"]))
            imported += 1
        except Exception:
            skipped += 1
    return jsonify({"ok": True, "imported": imported, "skipped": skipped, "holidays": unique,
                    "years": years})


@app.get("/api/shop/holidays/japanese-preview")
def shop_holidays_japanese_preview():
    """日本の祝日プレビュー（保存せず計算結果だけ返す）。
    クエリ: ?years=2026,2027,2028
    """
    shop, shop_id, _ = _shop_ctx()
    years_str = request.args.get("years", "")
    if years_str:
        try:
            years = [int(y) for y in years_str.split(",") if y.strip()]
        except ValueError:
            years = []
    else:
        from utils import jst_today
        y0 = jst_today().year
        years = [y0, y0 + 1, y0 + 2]
    result = []
    for y in years:
        try:
            result.extend(holidays_jp.japanese_holidays(y))
        except Exception:
            continue
    # 重複排除
    seen = set()
    unique = []
    for h in result:
        if h["date"] not in seen:
            seen.add(h["date"])
            unique.append(h)
    unique.sort(key=lambda x: x["date"])
    return jsonify({"holidays": unique, "years": years})


# --- スタッフ ---
@app.get("/api/shop/staffs")
def shop_staffs():
    shop, shop_id, _ = _shop_ctx()
    rows = query_all("SELECT id, staff_code, name, role, hourly_wage, min_hours_per_month, max_hours_per_month, is_resigned FROM staffs WHERE shop_id=? ORDER BY role DESC, id", (shop_id,))
    return jsonify({"staffs": rows})


@app.post("/api/shop/staffs")
def shop_staffs_post():
    shop, shop_id, settings = _shop_ctx()
    body = request.get_json(silent=True) or {}
    if not body.get("staff_code"):
        abort(400, description="コードを入力してください")
    if not body.get("name"):
        abort(400, description="氏名を入力してください")
    pw = body.get("password") or "password"
    err = validate_password(pw)
    if err:
        abort(400, description=err)
    # 重複チェック（UNIQUE制約を分かりやすいメッセージで事前検知）
    dup = query_one("SELECT id FROM staffs WHERE shop_id=? AND staff_code=?", (shop_id, body["staff_code"]))
    if dup:
        abort(400, description=f"コード '{body['staff_code']}' は既に存在します。別のコードを指定してください。")
    # role のバリデーション（'employee' / 'part_time' / 'manager' / 'student' 以外は拒否）
    role = body.get("role") or "part_time"
    if role not in ("employee", "part_time", "manager", "student"):
        abort(400, description="ロールは employee / part_time / manager / student のいずれかを指定してください")
    # 学生アルバイト: 月80h上限を強制（ロール固有ルール）
    max_hours = body.get("max_hours_per_month")
    if role == "student":
        try:
            mh = int(max_hours) if max_hours is not None else 80
        except (ValueError, TypeError):
            mh = 80
        if mh > 80:
            abort(400, description="学生アルバイトの月間上限は80時間です（80時間を超える設定はできません）")
        max_hours = min(mh, 80)
    else:
        try:
            max_hours = int(max_hours) if max_hours is not None else 160
        except (ValueError, TypeError):
            max_hours = 160
    meta = execute("INSERT INTO staffs (shop_id, staff_code, password_hash, name, role, hourly_wage, min_hours_per_month, max_hours_per_month) VALUES (?,?,?,?,?,?,?,?)",
                   (shop_id, body["staff_code"], hash_password(pw), body["name"], role,
                    body.get("hourly_wage") or settings.get("default_hourly_wage") or 1000,
                    body.get("min_hours_per_month") or 0, max_hours))
    return jsonify({"ok": True, "id": meta["last_row_id"]})


@app.put("/api/shop/staffs/<int:sid>")
def shop_staffs_put(sid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    # 学生アルバイト上限のバリデーション
    cur_staff = query_one("SELECT role FROM staffs WHERE id=? AND shop_id=?", (sid, shop_id))
    role = cur_staff["role"] if cur_staff else "part_time"
    max_hours = body.get("max_hours_per_month")
    try:
        mh = int(max_hours) if max_hours is not None else None
    except (ValueError, TypeError):
        mh = None
    if role == "student" and mh is not None and mh > 80:
        abort(400, description="学生アルバイトの月間上限は80時間です（80時間を超える設定はできません）")
    if mh is None:
        mh = 80 if role == "student" else 160
    execute("UPDATE staffs SET name=?, hourly_wage=?, min_hours_per_month=?, max_hours_per_month=?, is_resigned=? WHERE id=? AND shop_id=?",
            (body["name"], body["hourly_wage"], body["min_hours_per_month"], mh,
             1 if body.get("is_resigned") else 0, sid, shop_id))
    if body.get("password"):
        err = validate_password(body["password"])
        if err:
            abort(400, description=err)
        execute("UPDATE staffs SET password_hash=? WHERE id=? AND shop_id=?", (hash_password(body["password"]), sid, shop_id))
    return jsonify({"ok": True})


@app.delete("/api/shop/staffs/<int:sid>")
def shop_staffs_del(sid):
    """スタッフ削除（ハード削除・カスケード）。

    関連データも全て削除し、参照整合性を保つ:
      - fixed_shifts / shifts / change_requests / wish_history / notifications
      - 当該スタッフのセッション（ログイン無効化）
    shop_id で絞り込むことで他店舗スタッフの IDOR も防ぐ。
    存在しない / 他店舗の場合は 404 を返す。
    """
    shop, shop_id, _ = _shop_ctx()
    row = query_one("SELECT id FROM staffs WHERE id=? AND shop_id=?", (sid, shop_id))
    if not row:
        abort(404, description="スタッフが見つかりません")
    execute("DELETE FROM fixed_shifts WHERE staff_id=?", (sid,))
    execute("DELETE FROM shifts WHERE staff_id=?", (sid,))
    execute("DELETE FROM change_requests WHERE staff_id=?", (sid,))
    execute("DELETE FROM wish_history WHERE staff_id=?", (sid,))
    execute("DELETE FROM notifications WHERE staff_id=?", (sid,))
    execute("DELETE FROM sessions WHERE role='staff' AND user_id=?", (sid,))
    execute("DELETE FROM staffs WHERE id=? AND shop_id=?", (sid, shop_id))
    return jsonify({"ok": True})


# --- シフトパターン ---
@app.get("/api/shop/patterns")
def shop_patterns():
    shop, shop_id, _ = _shop_ctx()
    patterns = query_all("SELECT * FROM shift_patterns WHERE shop_id=? ORDER BY id", (shop_id,))
    overrides = shift_engine.load_weekday_overrides(shop_id)
    for pat in patterns:
        wd = {}
        for w in range(7):
            v = overrides.get((pat["id"], w))
            if v is not None:
                wd[str(w)] = v
        pat["weekday_required"] = wd
    return jsonify({"patterns": patterns})


def _validate_pattern_hours(start_time, end_time):
    """パターン時間長のバリデーション（労基法コンプライアンス）。

    戻り値: (ok, warning_message)
      - ok=False: 登録拒否（労基法上明らかに違法）
      - ok=True, warning: 登録可だが警告表示推奨
      - ok=True, warning=None: 完全にクリア
    """
    try:
        ps = int((start_time or "").split(":")[0]) * 60 + int((start_time or "").split(":")[1])
        pe = int((end_time or "").split(":")[0]) * 60 + int((end_time or "").split(":")[1])
    except (ValueError, IndexError):
        return False, "時刻形式が不正です（HH:MM形式で指定してください）"
    if pe <= ps:
        pe += 24 * 60  # overnight
    hours = (pe - ps) / 60
    if hours > 15:
        return False, (
            f"パターン時間が{hours:.1f}hに及びます。"
            f"1人ではカバーできず労基法32条違反(1日8h原則)となるため、"
            f"パターンを分割してください（例: 朝/昼/夜）。")
    if hours > 13:
        return True, (
            f"パターン時間が{hours:.1f}hと長すぎます。"
            f"社員でカバーしても13hが上限のため、シフト生成時に不足が出ます。"
            f"パターン分割を推奨します。")
    if hours > 9:
        return True, (
            f"パターン時間が{hours:.1f}hです。"
            f"アルバイト(max_daily_hours)ではカバーできず社員限定になります。")
    return True, None


@app.post("/api/shop/patterns")
def shop_patterns_post():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    ok, warning = _validate_pattern_hours(body.get("start_time"), body.get("end_time"))
    if not ok:
        abort(400, description=warning)
    meta = execute("INSERT INTO shift_patterns (shop_id, pattern_name, start_time, end_time, required_staff) VALUES (?,?,?,?,?)",
                   (shop_id, body["pattern_name"], body["start_time"], body["end_time"], body.get("required_staff") or 1))
    return jsonify({"ok": True, "id": meta["last_row_id"], "warning": warning})


@app.put("/api/shop/patterns/<int:pid>")
def shop_patterns_put(pid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    ok, warning = _validate_pattern_hours(body.get("start_time"), body.get("end_time"))
    if not ok:
        abort(400, description=warning)
    execute("UPDATE shift_patterns SET pattern_name=?, start_time=?, end_time=?, required_staff=? WHERE id=? AND shop_id=?",
            (body["pattern_name"], body["start_time"], body["end_time"], body.get("required_staff") or 1, pid, shop_id))
    return jsonify({"ok": True, "warning": warning})


@app.delete("/api/shop/patterns/<int:pid>")
def shop_patterns_del(pid):
    shop, shop_id, _ = _shop_ctx()
    execute("DELETE FROM shift_pattern_weekday_required WHERE pattern_id=? AND shop_id=?", (pid, shop_id))
    execute("DELETE FROM shift_patterns WHERE id=? AND shop_id=?", (pid, shop_id))
    return jsonify({"ok": True})


# --- 曜日別必要人数（パターンの曜日別オーバーライド） ---
@app.put("/api/shop/patterns/<int:pid>/weekday-required")
def shop_pattern_weekday_required(pid):
    shop, shop_id, _ = _shop_ctx()
    pat = query_one("SELECT id FROM shift_patterns WHERE id=? AND shop_id=?", (pid, shop_id))
    if not pat:
        abort(404, description="パターンが見つかりません")
    body = request.get_json(silent=True) or {}
    # body.weekday_required: {"0": 3, "6": 4} のようなマップ（NULL=削除/デフォルトに戻す）
    wr = body.get("weekday_required") or {}
    if not isinstance(wr, dict):
        abort(400, description="weekday_required は {weekday: count} 形式で指定してください")
    execute("DELETE FROM shift_pattern_weekday_required WHERE pattern_id=? AND shop_id=?", (pid, shop_id))
    for k, v in wr.items():
        try:
            wd = int(k)
            cnt = int(v)
        except (ValueError, TypeError):
            continue
        if not (0 <= wd <= 6) or cnt < 0:
            continue
        execute("INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
                (pid, shop_id, wd, cnt))
    return jsonify({"ok": True})


# --- 固定シフト ---
@app.get("/api/shop/fixed-shifts")
def shop_fixed():
    shop, shop_id, _ = _shop_ctx()
    rows = query_all("SELECT fs.*, s.name as staff_name FROM fixed_shifts fs JOIN staffs s ON fs.staff_id=s.id WHERE s.shop_id=? ORDER BY fs.staff_id, fs.weekday", (shop_id,))
    return jsonify({"fixed_shifts": rows})


def _assert_fixed_shift_in_shop(fid, shop_id):
    """固定シフトが自店舗スタッフのものであることを検証する。

    fixed_shifts には shop_id 列が無く staff_id しか持たないため、staffs 経由で
    JOIN して所属を判定する。他店舗のものは 404（存在を秘匿する既存方針に合わせる）。
    """
    row = query_one(
        "SELECT fs.id FROM fixed_shifts fs JOIN staffs s ON fs.staff_id=s.id "
        "WHERE fs.id=? AND s.shop_id=?", (fid, shop_id))
    if row is None:
        abort(404, description="固定シフトが見つかりません")


@app.post("/api/shop/fixed-shifts")
def shop_fixed_post():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    _assert_staff_in_shop(body["staff_id"], shop_id)
    meta = execute("INSERT INTO fixed_shifts (staff_id, weekday, start_time, end_time) VALUES (?,?,?,?)",
                   (body["staff_id"], body["weekday"], body["start_time"], body["end_time"]))
    return jsonify({"ok": True, "id": meta["last_row_id"]})


@app.put("/api/shop/fixed-shifts/<int:fid>")
def shop_fixed_put(fid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    _assert_fixed_shift_in_shop(fid, shop_id)
    execute("UPDATE fixed_shifts SET weekday=?, start_time=?, end_time=? WHERE id=?",
            (body["weekday"], body["start_time"], body["end_time"], fid))
    return jsonify({"ok": True})


@app.delete("/api/shop/fixed-shifts/<int:fid>")
def shop_fixed_del(fid):
    shop, shop_id, _ = _shop_ctx()
    _assert_fixed_shift_in_shop(fid, shop_id)
    execute("DELETE FROM fixed_shifts WHERE id=?", (fid,))
    return jsonify({"ok": True})


# --- 募集期間 ---
@app.get("/api/shop/periods")
def shop_periods():
    shop, shop_id, _ = _shop_ctx()
    return jsonify({"periods": query_all("SELECT * FROM shift_request_periods WHERE shop_id=? ORDER BY start_date DESC", (shop_id,))})


@app.get("/api/shop/periods/next")
def shop_periods_next():
    shop, shop_id, settings = _shop_ctx()
    return jsonify(calc_next_period(mode=settings.get("period_mode") or "half"))


@app.post("/api/shop/periods")
def shop_periods_post():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    meta = execute("INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) VALUES (?,?,?,?,?)",
                   (shop_id, body["start_date"], body["end_date"], body["deadline"], 0 if body.get("is_active") is False else 1))
    notify(shop_id, None, "info", "募集期間を作成", f"{body['start_date']}〜{body['end_date']}（締切{body['deadline']}）")
    return jsonify({"ok": True, "id": meta["last_row_id"]})


@app.put("/api/shop/periods/<int:pid>")
def shop_periods_put(pid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    execute("UPDATE shift_request_periods SET is_active=?, deadline=? WHERE id=? AND shop_id=?",
            (1 if body.get("is_active") else 0, body.get("deadline"), pid, shop_id))
    return jsonify({"ok": True})


@app.delete("/api/shop/periods/<int:pid>")
def shop_periods_del(pid):
    shop, shop_id, _ = _shop_ctx()
    execute("DELETE FROM shift_request_periods WHERE id=? AND shop_id=?", (pid, shop_id))
    return jsonify({"ok": True})


# --- シフト自動作成（dry_run対応） ---
# ★ 希望の保持は wish_history テーブルに一本化（reason ベースの保存は廃止）:
#    - staff が希望提出時 → wish_history に永久保存（shifts.requested にも書く）
#    - AI自動生成の入力 → wish_history を参照（再生成時にも希望が残る）
#    - shop_shifts_auto は preserved_wishes の複雑な reason 别判定を持たない
#      （過去の「統合/短縮で元時間消失 → 再生成で希望消失」バグの恒久解決）


@app.post("/api/shop/shifts/auto")
def shop_shifts_auto():
    """AI自動生成。

    body:
      - dry_run: true ならプレビュー（保存しない）
      - draft: true ならドラフト保存（status='requested', reason='AIドラフト'）
              ※ デフォルト: true（即確定しない）
              ※ false なら即 confirmed で保存（従来動作）
    """
    shop, shop_id, settings = _shop_ctx()
    body = request.get_json(silent=True) or {}
    start_d, end_d = body.get("start_date"), body.get("end_date")
    dry = bool(body.get("dry_run"))
    # draft オプション（デフォルト False = 後方互換・即確定）
    # UI の「ドラフト保存」ボタンから draft=true が明示的に渡される
    draft = bool(body.get("draft", False))
    if not start_d or not end_d:
        abort(400, description="start_date, end_date が必要です")
    result = shift_engine.auto_generate(shop_id, settings, start_d, end_d)
    if dry:
        return jsonify({"ok": True, "dry_run": True, "confirmed_count": len(result["confirmed"]),
                        "pending_count": len(result["pending"]), "minutes_by_staff": result["minutes_by_staff"],
                        "shortage": result.get("shortage", []),
                        "shortage_unique_count": len(result.get("shortage_unique", [])),
                        "shortage_count": len(result.get("shortage_unique", [])),
                        "warnings": result.get("warnings", []),
                        "explanations": result.get("explanations", []),
                        "preview": [{"staff_id": c["staff_id"], "start": c["start"], "end": c["end"], "break": c["break"], "reason": c["reason"]} for c in result["confirmed"]]})

    # AI生成前に、手動配置の confirmed のみを記録して保持
    # ★【インシデント対策】ホワイトリスト（auto_reasons）方式は、新しい reason を
    #    追加するたびにリストをメンテする必要があり、漏れが発生すると重複バグに
    #    なる（過去3回発生：'自動調整(統合)'漏れ、'固定シフト（社員・候補）'漏れ等）。
    #    ブラックリスト方式に転換：明示的に「手動」の reason のみ保持し、
    #    それ以外（エンジン/自動調整/社員候補/希望/wish等）はすべて再生成対象。
    MANUAL_REASONS = (
        '手動追加', '手動調整',
        '変更申請承認', '追加申請承認',
        'コピー',
    )
    manual_confirmed = query_all(
        "SELECT staff_id, start_datetime, end_datetime, break_time_minutes, reason FROM shifts "
        "WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=? "
        "AND reason IN ({})".format(",".join(["?"] * len(MANUAL_REASONS))),
        (shop_id, start_d + "T00:00:00", end_d + "T23:59:59", *MANUAL_REASONS))
    # ★【生成前クリア】ドラフト・即確定いずれも、期間内の confirmed(手動除く)/
    # modifying/requested を全削除してから配置し直す。
    #
    # 【重要】スタッフ希望の元 requested 行も削除する。
    # 希望表管理は wish_history（永久履歴）を参照する設計のため、shifts.requested を
    # 消しても希望表からは消えない。エンジンは既に wish_history を入力として消費済み
    # （auto_generate は本削除より前に実行）。
    # 旧実装は draft 時にスタッフ希望 requested を保持していたため、エンジンが同じ希望を
    # 'AIドラフト: 希望シフト' として再配置すると同一時間が2件になり、タイムラインで
    # 過剰配置に見えるバグがあった（元希望 + ドラフトの二重）。ここで統一して全削除する。
    # 手動配置の confirmed は manual_confirmed に退避済みで、後段で再INSERTする。
    execute(
        "DELETE FROM shifts WHERE shop_id=? AND status IN ('confirmed','modifying','requested') "
        "AND start_datetime>=? AND start_datetime<=?",
        (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    # draft モード: confirmed を requested + reason='AIドラフト' で保存（確定通知しない）
    # 即確定モード: confirmed をそのまま保存（従来通り）
    insert_status = "requested" if draft else "confirmed"
    insert_reason_suffix = "" if draft else ""
    placed = set()
    for s in result["confirmed"]:
        reason = ("AIドラフト: " + s["reason"]) if draft else s["reason"]
        execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                (s["shop_id"], s["staff_id"], s["start"], s["end"], s["break"], insert_status, reason))
        placed.add(s["staff_id"])
    # 手動配置の confirmed を再INSERT（auto_generateが再配置したものと重複しないもののみ）
    auto_keys = set((s["staff_id"], s["start"]) for s in result["confirmed"])
    for m in manual_confirmed:
        if (m["staff_id"], m["start_datetime"]) not in auto_keys:
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, m["staff_id"], m["start_datetime"], m["end_datetime"], m["break_time_minutes"], "confirmed", m["reason"]))
    pending_count = 0
    for p in result["pending"]:
        if not query_one("SELECT id FROM shifts WHERE staff_id=? AND start_datetime=? AND status=?", (p["staff_id"], p["start"], p["status"])):
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason, availability) VALUES (?,?,?,?,?,?,?,?)",
                    (p["shop_id"], p["staff_id"], p["start"], p["end"], p["break"], p["status"], p["reason"], p.get("availability")))
            pending_count += 1
    # 通知は確定時のみ（draft モードでは送らない）
    if not draft:
        for sid in placed:
            notify(shop_id, sid, "confirmed", "シフトが確定しました", f"{start_d}〜{end_d}のシフトが確定しました。")
    draft_msg = "（ドラフト保存・確定前）" if draft else ""
    return jsonify({"ok": True, "draft": draft,
                    "confirmed_count": len(result["confirmed"]), "pending_count": pending_count,
                    "minutes_by_staff": result["minutes_by_staff"], "shortage": result.get("shortage", []),
                    "shortage_unique_count": len(result.get("shortage_unique", [])),
                    "warnings": result.get("warnings", []),
                    "explanations": result.get("explanations", []),
                    "message": f"AI生成完了{draft_msg}。{'確定ボタンで通知が飛びます。' if draft else 'スタッフに確定通知を送信しました。'}"})


@app.post("/api/shop/shifts/finalize")
def shop_shifts_finalize():
    """ドラフト状態のシフト（reason LIKE 'AIドラフト%'）を一括確定。

    また、期間内のスタッフ希望（status='requested'）も confirmed に変換する。
    これにより「確定」ボタンを押すと希望表カードが消える（シフトが完全確定）。

    body:
      - start_date, end_date: 期間指定
    戻り値: {ok, finalized, notified_staff, message}
    """
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    start_d = body.get("start_date")
    end_d = body.get("end_date")
    if not start_d or not end_d:
        abort(400, description="start_date, end_date が必要です")
    # 期間内の全 requested を取得（AIドラフト + スタッフ希望 両方）
    targets = query_all(
        "SELECT id, staff_id, start_datetime, end_datetime, reason FROM shifts "
        "WHERE shop_id=? AND status='requested' "
        "AND start_datetime>=? AND start_datetime<=?",
        (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    if not targets:
        return jsonify({"ok": True, "finalized": 0, "notified_staff": 0, "over_cap": 0,
                        "message": "確定対象のシフトがありません。AI生成（ドラフト保存）を実行してください。"})
    # 全て confirmed に変換
    finalized_staff = set()
    finalized_count = 0
    for t in targets:
        # AIドラフトの reason は「AIドラフト: ...」なので、確定時に整理
        new_reason = t["reason"]
        if t["reason"] and t["reason"].startswith("AIドラフト: "):
            new_reason = t["reason"][len("AIドラフト: "):]  # プレフィックス除去
        elif t["reason"] and t["reason"].startswith("AIドラフト"):
            new_reason = "AI自動生成"
        execute("UPDATE shifts SET status='confirmed', reason=? WHERE id=? AND shop_id=?",
                (new_reason, t["id"], shop_id))
        finalized_staff.add(t["staff_id"])
        finalized_count += 1
    # 必要人数超過の枠に重なる確定シフトへフラグを付与
    over_cap = _flag_over_cap_shifts(shop_id, start_d + "T00:00:00", end_d + "T23:59:59")
    # スタッフに通知
    for sid in finalized_staff:
        notify(shop_id, sid, "confirmed", "シフトが確定しました", f"{start_d}〜{end_d}のシフトが確定しました。")
    # 店舗にも通知
    notify(shop_id, None, "info", "シフト確定完了",
           f"{start_d}〜{end_d}のシフトを {finalized_count} 件確定し、{len(finalized_staff)} 名に通知しました。")
    audit("shift.finalize", target_type="shop", target_id=shop_id, shop_id=shop_id,
          detail=f"{start_d}〜{end_d} finalized={finalized_count} over_cap={over_cap}")
    msg = f"{finalized_count} 件のシフトを確定し、{len(finalized_staff)} 名のスタッフに通知しました。"
    if over_cap:
        msg += f"（うち {over_cap} 件が必要人数超過です）"
    return jsonify({"ok": True, "finalized": finalized_count,
                    "notified_staff": len(finalized_staff), "over_cap": over_cap,
                    "message": msg})


@app.patch("/api/shop/shifts/<int:sid>/note")
def shop_shift_note_patch(sid):
    """シフトに店長メモを設定/クリアする（店長画面のみ表示）。空文字は NULL 扱い。"""
    _, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    note = (body.get("note") or "").strip() or None
    existing = query_one("SELECT id FROM shifts WHERE id=? AND shop_id=?", (sid, shop_id))
    if not existing:
        abort(404, description="シフトが見つかりません")
    execute("UPDATE shifts SET note=? WHERE id=? AND shop_id=?", (note, sid, shop_id))
    return jsonify({"ok": True, "note": note})


# --- シフト コピー ---
@app.post("/api/shop/shifts/copy")
def shop_shifts_copy():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    fs, fe, ts = body.get("from_start"), body.get("from_end"), body.get("to_start")
    if not fs or not fe or not ts:
        abort(400, description="from_start, from_end, to_start が必要")
    offset_days = (datetime.strptime(ts, "%Y-%m-%d") - datetime.strptime(fs, "%Y-%m-%d")).days
    rows = query_all("SELECT staff_id, start_datetime, end_datetime, break_time_minutes, reason FROM shifts WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=?",
                     (shop_id, fs + "T00:00:00", fe + "T23:59:59"))
    cnt = 0
    skipped_overlap = 0
    for r in rows:
        ns_date = (parse_iso(r["start_datetime"]).date() + timedelta(days=offset_days))
        new_start = f"{ns_date.strftime('%Y-%m-%d')}{r['start_datetime'][10:]}"
        new_end = (parse_iso(r["end_datetime"]) + timedelta(days=offset_days)).strftime("%Y-%m-%dT%H:%M:%S")
        # コピー先で同スタッフの同日シフトと重複する場合はスキップ
        overlap, _c = _check_staff_overlap(shop_id, r["staff_id"], new_start, new_end)
        if overlap:
            skipped_overlap += 1
            continue
        execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                (shop_id, r["staff_id"], new_start, new_end, r["break_time_minutes"], "confirmed", (r.get("reason") or "コピー")))
        cnt += 1
    return jsonify({"ok": True, "copied": cnt, "skipped_overlap": skipped_overlap})


def _try_confirm_with_adjust(shop_id, shift_id, staff_id, start_iso, end_iso):
    """1件の requested を自動調整で確定。
    戻り値: {ok, reason, adjustments, action}
      action: 'confirmed' / 'merged' / 'skipped'
    """
    # A) 同一スタッフの同日 confirmed があるか（同日内重複）
    day = start_iso[:10]
    own = query_one(
        "SELECT id, start_datetime, end_datetime FROM shifts "
        "WHERE staff_id=? AND shop_id=? AND status='confirmed' "
        "AND start_datetime>=? AND start_datetime<=?",
        (staff_id, shop_id, day + "T00:00:00", day + "T23:59:59"))
    if own:
        # 既存 confirmed を延長して統合（min開始-max終了 の1シフトに）
        new_s = min(start_iso, own["start_datetime"])
        new_e = max(end_iso, own["end_datetime"])
        work = minutes_between(new_s, new_e)
        if work > 14 * 60:
            # 14h超は現実的でない → スキップ
            return {"ok": False, "action": "skipped", "reason": "統合すると14h超になるためスキップ"}
        # ★ 上限人数チェック: 統合後の時間帯が cap を超える場合、
        # 統合対象の自分自身を除外して他スタッフの配置状況を確認。
        over_after_merge, req_m, cur_m = _check_slot_cap(
            shop_id, new_s, new_e, exclude_id=own["id"])
        if over_after_merge:
            # ★ 統合後の時間帯で cap 超過 → 社員のシフトを短縮して調整を試みる
            # （ユーザー要望: 「社員の時間を減らして調整すべき」）
            print(f"[AUTO-CONFIRM] merge cap超過(必要{req_m}/配置{cur_m}) → "
                  f"社員シフト短縮で調整を試みます: {new_s[11:16]}-{new_e[11:16]}", flush=True)
            adjustments = _auto_adjust_for_overlap(
                shop_id, staff_id, new_s, new_e, exclude_id=own["id"])
            # 調整後再度 cap チェック
            over2, req2, cur2 = _check_slot_cap(
                shop_id, new_s, new_e, exclude_id=own["id"])
            if over2:
                # それでも cap 超過 → target 自体を cap 内に短縮も試す
                shortened = _shorten_to_cap(shop_id, staff_id, new_s, new_e, exclude_id=own["id"])
                if shortened:
                    ns, ne = shortened
                    adjustments.append({
                        "staff_id": staff_id, "name": "(対象シフト)",
                        "old_start": new_s, "old_end": new_e,
                        "new_start": ns, "new_end": ne,
                        "message": f"配置可能な時間帯に短縮: {new_s[11:16]}-{new_e[11:16]} → {ns[11:16]}-{ne[11:16]}",
                    })
                    new_s, new_e = ns, ne
                    print(f"[AUTO-CONFIRM] merge target shortened: {new_s}〜{new_e}", flush=True)
                else:
                    print(f"[AUTO-CONFIRM] merge 諦め: cap超過解消不可 "
                          f"(必要{req_m}/配置{cur_m} → 調整後も必要{req2}/配置{cur2})", flush=True)
                    return {
                        "ok": False, "action": "skipped",
                        "reason": (
                            f"既存シフト({own['start_datetime'][11:16]}-{own['end_datetime'][11:16]})と"
                            f"希望({start_iso[11:16]}-{end_iso[11:16]})を統合すると"
                            f"必要人数({req_m}名)を超え、社員シフトの短縮でも解消できなかったためスキップしました。"
                        ),
                        "adjustments": adjustments,
                    }
            else:
                # ★ cap 内になった → merge 続行
                print(f"[AUTO-CONFIRM] merge OK via auto_adjust: "
                      f"{len(adjustments)}件の社員シフトを短縮", flush=True)
            # merge 実行
            work = minutes_between(new_s, new_e)
            brk = compute_break_minutes(work)
            execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, reason='自動調整(統合)' WHERE id=?",
                    (new_s, new_e, brk, own["id"]))
            # requested を削除（統合したため）
            execute("DELETE FROM shifts WHERE id=? AND shop_id=?", (shift_id, shop_id))
            return {
                "ok": True, "action": "merged",
                "adjustments": adjustments + [{
                    "shift_id": own["id"], "staff_id": staff_id,
                    "old_start": own["start_datetime"], "old_end": own["end_datetime"],
                    "new_start": new_s, "new_end": new_e,
                    "message": f"既存シフト({own['start_datetime'][11:16]}-{own['end_datetime'][11:16]})と希望({start_iso[11:16]}-{end_iso[11:16]})を統合し {new_s[11:16]}-{new_e[11:16]} で確定しました。",
                }],
            }
        brk = compute_break_minutes(work)
        execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, reason='自動調整(統合)' WHERE id=?",
                (new_s, new_e, brk, own["id"]))
        # requested を削除（統合したため）
        execute("DELETE FROM shifts WHERE id=? AND shop_id=?", (shift_id, shop_id))
        return {
            "ok": True, "action": "merged",
            "adjustments": [{
                "shift_id": own["id"], "staff_id": staff_id, "old_start": own["start_datetime"], "old_end": own["end_datetime"],
                "new_start": new_s, "new_end": new_e,
                "message": f"既存シフト({own['start_datetime'][11:16]}-{own['end_datetime'][11:16]})と希望({start_iso[11:16]}-{end_iso[11:16]})を統合し {new_s[11:16]}-{new_e[11:16]} で確定しました。",
            }],
        }
    # B) cap 超過？
    over, req, cur = _check_slot_cap(shop_id, start_iso, end_iso, exclude_id=shift_id)
    if not over:
        # cap 内 → 単純に確定
        work = minutes_between(start_iso, end_iso)
        execute("UPDATE shifts SET status='confirmed', break_time_minutes=?, reason='自動確定' WHERE id=? AND shop_id=?",
                (compute_break_minutes(work), shift_id, shop_id))
        return {"ok": True, "action": "confirmed", "adjustments": []}
    # C) cap 超過 → 自動調整で他を短縮
    adjustments = _auto_adjust_for_overlap(shop_id, staff_id, start_iso, end_iso, exclude_id=shift_id)
    # 調整後に再度 cap チェック
    over2, _r, _c = _check_slot_cap(shop_id, start_iso, end_iso, exclude_id=shift_id)
    if over2:
        # 他シフトの短縮で解消できない → target自体をcap内に短縮
        shortened = _shorten_to_cap(shop_id, staff_id, start_iso, end_iso, exclude_id=shift_id)
        if shortened:
            new_s, new_e = shortened
            adjustments.append({
                "staff_id": staff_id, "name": "(対象シフト)",
                "old_start": start_iso, "old_end": end_iso,
                "new_start": new_s, "new_end": new_e,
                "message": f"配置可能な時間帯に短縮: {start_iso[11:16]}-{end_iso[11:16]} → {new_s[11:16]}-{new_e[11:16]}",
            })
            start_iso = new_s
            end_iso = new_e
            print(f"[AUTO-CONFIRM] target shortened: {start_iso}〜{end_iso}", flush=True)
        else:
            return {"ok": False, "action": "skipped", "reason": f"cap超過が解消できず短縮も不可のためスキップ", "adjustments": adjustments}
    work = minutes_between(start_iso, end_iso)
    execute("UPDATE shifts SET status='confirmed', start_datetime=?, end_datetime=?, break_time_minutes=?, reason='自動確定(cap内短縮)' WHERE id=? AND shop_id=?",
            (start_iso, end_iso, compute_break_minutes(work), shift_id, shop_id))
    return {"ok": True, "action": "confirmed", "adjustments": adjustments}


@app.post("/api/shop/shifts/auto-confirm")
def shop_shifts_auto_confirm():
    """期間内の全 requested（調整待ち）を一括で自動調整して確定。

    各 requested について：
      - 同一スタッフの同日 confirmed がある → 統合（1シフトに）
      - cap 超過 → 他の confirmed を短縮して配置
      - どうしても無理 → スキップ
    """
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    start_d = body.get("start_date")
    end_d = body.get("end_date")
    if not start_d or not end_d:
        abort(400, description="start_date, end_date が必要")
    reqs = query_all(
        "SELECT id, staff_id, start_datetime, end_datetime FROM shifts "
        "WHERE shop_id=? AND status='requested' "
        "AND start_datetime>=? AND start_datetime<=? ORDER BY start_datetime",
        (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    results = []
    confirmed_n = merged_n = skipped_n = 0
    all_adjustments = []
    for r in reqs:
        res = _try_confirm_with_adjust(shop_id, r["id"], r["staff_id"], r["start_datetime"], r["end_datetime"])
        results.append({"id": r["id"], "start": r["start_datetime"], **res})
        if res["action"] == "confirmed":
            confirmed_n += 1
        elif res["action"] == "merged":
            merged_n += 1
        else:
            skipped_n += 1
        if res.get("adjustments"):
            all_adjustments.extend(res["adjustments"])
    print(f"[AUTO-CONFIRM] {start_d}〜{end_d}: requested={len(reqs)} 確定={confirmed_n} 統合={merged_n} スキップ={skipped_n} 調整={len(all_adjustments)}", flush=True)
    return jsonify({
        "ok": True, "total": len(reqs),
        "confirmed": confirmed_n, "merged": merged_n, "skipped": skipped_n,
        "results": results, "adjustments": all_adjustments,
    })


# --- シフト一覧/CRUD ---
@app.get("/api/shop/shifts")
def shop_shifts_list():
    shop, shop_id, _ = _shop_ctx()
    start_d, end_d = request.args.get("start"), request.args.get("end")
    if not start_d or not end_d:
        abort(400, description="start, end クエリが必要")
    rows = query_all("SELECT sh.*, s.name as staff_name, s.role as staff_role FROM shifts sh JOIN staffs s ON sh.staff_id=s.id WHERE sh.shop_id=? AND sh.start_datetime>=? AND sh.start_datetime<=? ORDER BY sh.start_datetime",
                     (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    return jsonify({"shifts": rows})


@app.post("/api/shop/shifts")
def shop_shifts_post():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    auto_adjust = bool(body.get("auto_adjust"))
    staff_id = body["staff_id"]
    # 自店舗の shop_id を持ちながら他店舗スタッフを指す行を作らせない。
    # /api/shop/wishes/bulk (src/app.py:3711-3720 付近) と同じ防御。
    _assert_staff_in_shop(staff_id, shop_id)
    start_dt = body["start_datetime"]
    end_dt = body["end_datetime"]
    # 隣接する同一スタッフの confirmed があれば自動的に統合（17-18 + 18-22 → 17-22）
    merged, merged_id = _try_merge_adjacent(shop_id, staff_id, start_dt, end_dt)
    if merged:
        # 統合後の時間帯で cap/overlap を再チェック
        merged_shift = query_one("SELECT start_datetime, end_datetime FROM shifts WHERE id=?", (merged_id,))
        ms, me = merged_shift["start_datetime"], merged_shift["end_datetime"]
        over, req, cur = _check_slot_cap(shop_id, ms, me, exclude_id=merged_id)
        if over and not auto_adjust:
            # 統合で cap 超過になった → ロールバック
            execute("UPDATE shifts SET start_datetime=?, end_datetime=?, reason=? WHERE id=?",
                    (start_dt, end_dt, "手動追加", merged_id))
            msg = f"統合すると必要人数{req}名を超えるため、別シフトとして追加しました。"
            print(f"[SHIFT POST] merge rollback: {msg}", flush=True)
        elif over and auto_adjust:
            _auto_adjust_for_overlap(shop_id, staff_id, ms, me, exclude_id=merged_id)
            print(f"[SHIFT POST] merge+auto_adjust: id={merged_id} {ms}〜{me}", flush=True)
        else:
            print(f"[SHIFT POST] merge: id={merged_id} {ms}〜{me} (隣接統合)", flush=True)
        return jsonify({"ok": True, "id": merged_id, "merged": True})
    # 通常の cap/overlap チェック
    over, req, cur = _check_slot_cap(shop_id, start_dt, end_dt)
    if over and not auto_adjust:
        msg = f"この時間帯の必要人数は{req}名です（既に{cur}名配置済）。これ以上は配置できません。"
        print(f"[SHIFT POST] over_cap: {msg} staff_id={staff_id} {start_dt}〜{end_dt}", flush=True)
        return jsonify({"error": msg, "over_cap": True}), 400
    overlap, conflict = _check_staff_overlap(shop_id, staff_id, start_dt, end_dt)
    if overlap:
        c = conflict or {}
        msg = f"このスタッフは同日に既にシフトがあります（{c.get('start_datetime','')[11:16]}-{c.get('end_datetime','')[11:16]}）。重複・中抜けはできません。"
        print(f"[SHIFT POST] overlap: {msg} staff_id={staff_id} conflict_id={c.get('id')}", flush=True)
        return jsonify({"error": msg, "overlap": True}), 400
    # 自動調整
    adjustments = []
    if over and auto_adjust:
        adjustments = _auto_adjust_for_overlap(shop_id, staff_id, start_dt, end_dt)
        # 調整後もまだcap超過？
        over2, req2, cur2 = _check_slot_cap(shop_id, start_dt, end_dt)
        if over2:
            # 自動調整でも解消できない → targetをcap内に短縮して配置
            # targetの時間帯のうち、cap内に収まる部分のみを配置
            pats = query_all("SELECT id, start_time, end_time, required_staff FROM shift_patterns WHERE shop_id=?", (shop_id,))
            weekday_overrides = shift_engine.load_weekday_overrides(shop_id)
            wd = (datetime.strptime(start_dt[:10], "%Y-%m-%d").weekday() + 1) % 7
            applied = []
            for pat in pats:
                ov = weekday_overrides.get((pat.get("id"), wd))
                p = dict(pat)
                if ov is not None:
                    p["required_staff"] = ov
                applied.append(p)
            req_map = shift_engine._day_requirements(applied, shift_engine.GRAN, wd, weekday_overrides)
            # targetの各スロットで配置可能な部分を探す
            best_start = None
            best_end = None
            best_len = 0
            target_slots = shift_engine._shift_slots(start_dt, end_dt, shift_engine.GRAN)
            # 既存coverageを計算（auto_adjust後）
            day = start_dt[:10]
            existing = query_all("SELECT start_datetime, end_datetime FROM shifts WHERE shop_id=? AND status='confirmed' AND start_datetime>=? AND start_datetime<=? AND staff_id!=?",
                                 (shop_id, day + "T00:00:00", day + "T23:59:59", staff_id))
            coverage = {}
            for ex in existing:
                for sl in shift_engine._shift_slots(ex["start_datetime"], ex["end_datetime"], shift_engine.GRAN):
                    coverage[sl] = coverage.get(sl, 0) + 1
            # 連続して配置可能な区間を探す
            cur_start = None
            cur_len = 0
            for sl in sorted(target_slots):
                req_s = req_map.get(sl, 0)
                if req_s > 0 and coverage.get(sl, 0) + 1 <= req_s:
                    if cur_start is None:
                        cur_start = sl
                    cur_len += shift_engine.GRAN
                else:
                    if cur_len > best_len:
                        best_start = cur_start
                        best_len = cur_len
                    cur_start = None
                    cur_len = 0
            if cur_len > best_len:
                best_start = cur_start
                best_len = cur_len
            if best_start is not None and best_len >= 60:
                # cap内の区間に短縮して配置
                new_end_min = best_start + best_len
                new_start_iso = f"{start_dt[:10]}T{best_start // 60:02d}:{best_start % 60:02d}:00"
                new_end_iso = f"{start_dt[:10]}T{new_end_min // 60:02d}:{new_end_min % 60:02d}:00"
                adjustments.append({
                    "staff_id": staff_id, "name": "(対象シフト)",
                    "old_start": start_dt, "old_end": end_dt,
                    "new_start": new_start_iso, "new_end": new_end_iso,
                    "message": f"配置可能な時間帯に短縮しました: {start_dt[11:16]}-{end_dt[11:16]} → {new_start_iso[11:16]}-{new_end_iso[11:16]}",
                })
                start_dt = new_start_iso
                end_dt = new_end_iso
                print(f"[SHIFT POST] target shortened to {start_dt}〜{end_dt}", flush=True)
            else:
                msg = f"この時間帯は必要人数を超過するため配置できません（自動調整でも解消不可）。別の時間帯を選んでください。"
                print(f"[SHIFT POST] unresolvable cap: {msg}", flush=True)
                return jsonify({"error": msg, "over_cap": True, "adjustments": adjustments}), 400
    work = minutes_between(start_dt, end_dt)
    brk = body.get("break_time_minutes")
    if brk is None:
        brk = compute_break_minutes(work)
    # 学生アルバイトのみ構成チェック（追加前）
    student_ng, student_msg = _check_student_only_shift(shop_id, staff_id, start_dt)
    if student_ng:
        print(f"[SHIFT POST] student_only: {student_msg} staff_id={staff_id} {start_dt}〜{end_dt}", flush=True)
        return jsonify({"error": student_msg, "student_only": True}), 400
    meta = execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason, availability) VALUES (?,?,?,?,?,?,?,?)",
                   (shop_id, staff_id, start_dt, end_dt, brk, body.get("status") or "confirmed", body.get("reason") or "手動追加", body.get("availability")))
    print(f"[SHIFT POST id={meta['last_row_id']}] OK: staff_id={staff_id} {start_dt}〜{end_dt}", flush=True)
    result = {"ok": True, "id": meta["last_row_id"]}
    if adjustments:
        result["adjustments"] = adjustments
    return jsonify(result)


@app.put("/api/shop/shifts/<int:sid>")
def shop_shifts_put(sid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    # 既存シフトを取得（staff_id のフォールバック兼、存在確認）
    existing = query_one("SELECT staff_id, start_datetime, end_datetime, status FROM shifts WHERE id=? AND shop_id=?", (sid, shop_id))
    if not existing:
        abort(404, description="シフトが見つかりません")
    staff_id = body.get("staff_id") or existing["staff_id"]
    # 自店舗の shop_id を持ちながら他店舗スタッフへ付け替えられないようにする。
    # staff_id 省略時は既存値を維持するだけなので検証不要（値があるときだけ検証）。
    # /api/shop/wishes/bulk (src/app.py:3711-3720 付近) と同じ防御。
    if body.get("staff_id") is not None:
        _assert_staff_in_shop(body.get("staff_id"), shop_id)
    # 確定シフトのロック：確定済みシフトの時間・担当変更は直接編集できない。
    # 変更はスタッフの「変更申請」を承認して反映する（時刻・担当が変わらない再保存や
    # requested/modifying からの確定は許可）。UI もメモ以外を編集不可にしている。
    if existing["status"] == "confirmed":
        changed = (
            normalize_iso(body.get("start_datetime")) != existing["start_datetime"]
            or normalize_iso(body.get("end_datetime")) != existing["end_datetime"]
            or int(staff_id) != int(existing["staff_id"])
        )
        if changed and not bool(body.get("allow_confirmed_edit")):
            return jsonify({
                "error": "確定シフトは直接変更できません。時間変更・取消はスタッフの変更申請を承認して反映してください。",
                "locked": True,
            }), 409
    auto_adjust = bool(body.get("auto_adjust"))
    force = bool(body.get("force"))
    # cap判定はforceに関わらず実施（force/auto_adjustで後で許可判定）
    over, req, cur = _check_slot_cap(shop_id, body["start_datetime"], body["end_datetime"], exclude_id=sid)
    if over and not (force or auto_adjust):
        msg = f"この時間帯の必要人数は{req}名です（既に{cur}名配置済）。これ以上は配置できません。"
        print(f"[SHIFT PUT sid={sid}] over_cap: {msg} staff_id={staff_id} {body['start_datetime']}〜{body['end_datetime']}", flush=True)
        return jsonify({"error": msg, "over_cap": True}), 400
    overlap, conflict = _check_staff_overlap(shop_id, staff_id, body["start_datetime"], body["end_datetime"], exclude_id=sid)
    if overlap and not auto_adjust:
        c = conflict or {}
        msg = f"このスタッフは同日に別のシフトがあります（{c.get('start_datetime','')[11:16]}-{c.get('end_datetime','')[11:16]}）。重複・中抜けはできません。"
        print(f"[SHIFT PUT sid={sid}] overlap: staff_id={staff_id} conflict_id={c.get('id')} {body['start_datetime']}〜{body['end_datetime']}", flush=True)
        return jsonify({"error": msg, "overlap": True}), 400
    # overlap + auto_adjust → 統合して確定（targetを他シフトに統合）
    if overlap and auto_adjust:
        res = _try_confirm_with_adjust(shop_id, sid, staff_id, body["start_datetime"], body["end_datetime"])
        if not res["ok"]:
            return jsonify({"error": res.get("reason", "統合できませんでした"), "overlap": True}), 400
        print(f"[SHIFT PUT sid={sid}] overlap auto_adjust: action={res['action']} adjustments={len(res.get('adjustments', []))}", flush=True)
        return jsonify({"ok": True, "adjustments": res.get("adjustments", []), "action": res["action"]})
    # 自動調整モード: cap 超過を解消するため、他のシフト（社員優先）を短縮
    adjustments = []
    if over and auto_adjust:
        adjustments = _auto_adjust_for_overlap(shop_id, staff_id, body["start_datetime"], body["end_datetime"], exclude_id=sid)
        print(f"[SHIFT PUT sid={sid}] auto_adjust: {len(adjustments)}件を短縮 - {[a['message'] for a in adjustments]}", flush=True)
    work = minutes_between(body["start_datetime"], body["end_datetime"])
    brk = body.get("break_time_minutes")
    if brk is None:
        brk = compute_break_minutes(work)
    # 学生アルバイトのみ構成チェック（更新後の時間帯で）
    student_ng, student_msg = _check_student_only_shift(shop_id, staff_id, body["start_datetime"], exclude_id=sid)
    if student_ng:
        print(f"[SHIFT PUT sid={sid}] student_only: {student_msg} staff_id={staff_id} {body['start_datetime']}〜{body['end_datetime']}", flush=True)
        return jsonify({"error": student_msg, "student_only": True}), 400
    execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, status=?, reason=? WHERE id=? AND shop_id=?",
            (body["start_datetime"], body["end_datetime"], brk, body.get("status") or "confirmed", body.get("reason") or "手動調整", sid, shop_id))
    print(f"[SHIFT PUT sid={sid}] OK: staff_id={staff_id} {body['start_datetime']}〜{body['end_datetime']} status={body.get('status')} auto_adjust={auto_adjust}", flush=True)
    result = {"ok": True}
    if adjustments:
        result["adjustments"] = adjustments
    return jsonify(result)


@app.patch("/api/shop/shifts/<int:sid>/draft-time")
def shop_shift_draft_time_patch(sid):
    """AIドラフトだけを15分単位で直接調整する。"""
    _, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    draft = query_one("SELECT * FROM shifts WHERE id=? AND shop_id=?", (sid, shop_id))
    if not draft:
        abort(404, description="シフトが見つかりません")
    if draft.get("status") != "requested" or not (draft.get("reason") or "").startswith("AIドラフト"):
        return jsonify({"error": "AIドラフトだけを直接調整できます"}), 409

    expected_updated_at = body.get("updated_at")
    current_updated_at = draft.get("updated_at") or draft.get("created_at")
    if not expected_updated_at:
        abort(400, description="ドラフトの更新時刻が必要です")
    if expected_updated_at != current_updated_at:
        return jsonify({"error": "ほかの調整内容があります。ドラフトを再読み込みしてください", "shift": draft}), 409

    start_datetime = normalize_iso(body.get("start_datetime"))
    end_datetime = normalize_iso(body.get("end_datetime"))
    try:
        start_at = parse_iso(start_datetime)
        end_at = parse_iso(end_datetime)
    except (TypeError, ValueError):
        abort(400, description="開始・終了時刻の形式が不正です")
    if start_at.second != 0 or end_at.second != 0 or start_at.minute % 15 or end_at.minute % 15:
        abort(400, description="時刻は15分単位で指定してください")
    if end_at <= start_at or minutes_between(start_datetime, end_datetime) < 15:
        abort(400, description="終了は開始の15分後以降にしてください")

    over, required, current = _check_slot_cap(shop_id, start_datetime, end_datetime, exclude_id=sid)
    if over:
        return jsonify({"error": f"この時間帯の必要人数は{required}名です（既に{current}名配置済）", "over_cap": True}), 409

    overlap = query_one(
        "SELECT id, start_datetime, end_datetime FROM shifts "
        "WHERE shop_id=? AND staff_id=? AND id!=? "
        "AND (status IN ('confirmed','modifying') OR (status='requested' AND reason LIKE 'AIドラフト%')) "
        "AND start_datetime < ? AND end_datetime > ? LIMIT 1",
        (shop_id, draft["staff_id"], sid, end_datetime, start_datetime),
    )
    if overlap:
        return jsonify({"error": "同じスタッフの別シフトと重複します", "overlap": True}), 409

    student_ng, student_msg = _check_student_only_shift(
        shop_id, draft["staff_id"], start_datetime, exclude_id=sid
    )
    if student_ng:
        return jsonify({"error": student_msg, "student_only": True}), 409

    next_updated_at = jst_now().strftime("%Y-%m-%dT%H:%M:%S.%f")
    execute(
        "UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, updated_at=? "
        "WHERE id=? AND shop_id=? AND COALESCE(updated_at, created_at)=?",
        (start_datetime, end_datetime, compute_break_minutes(minutes_between(start_datetime, end_datetime)),
         next_updated_at, sid, shop_id, expected_updated_at),
    )
    updated = query_one("SELECT * FROM shifts WHERE id=? AND shop_id=?", (sid, shop_id))
    if not updated or updated.get("updated_at") != next_updated_at:
        return jsonify({"error": "ほかの調整内容があります。ドラフトを再読み込みしてください", "shift": updated}), 409
    return jsonify({"ok": True, "shift": updated})


@app.delete("/api/shop/shifts/<int:sid>")
def shop_shifts_del(sid):
    shop, shop_id, _ = _shop_ctx()
    execute("DELETE FROM shifts WHERE id=? AND shop_id=?", (sid, shop_id))
    return jsonify({"ok": True})


# --- 集計 / 不足 / CSV ---
@app.get("/api/shop/summary")
def shop_summary():
    shop, shop_id, settings = _shop_ctx()
    start_d, end_d = request.args.get("start"), request.args.get("end")
    if not start_d or not end_d:
        abort(400, description="start, end が必要")
    shifts = query_all("SELECT sh.*, s.name as staff_name FROM shifts sh JOIN staffs s ON sh.staff_id=s.id WHERE sh.shop_id=? AND sh.start_datetime>=? AND sh.start_datetime<=?",
                       (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    staffs = query_all("SELECT id, name, role, hourly_wage FROM staffs WHERE shop_id=? AND is_resigned=0", (shop_id,))
    return jsonify(summarize_shifts(shifts, {s["id"]: s for s in staffs}, settings))


@app.get("/api/shop/shortage")
def shop_shortage():
    shop, shop_id, _ = _shop_ctx()
    start_d, end_d = request.args.get("start"), request.args.get("end")
    if not start_d or not end_d:
        abort(400, description="start, end が必要")
    shifts = query_all("SELECT * FROM shifts WHERE shop_id=? AND start_datetime>=? AND start_datetime<=?", (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    pats = query_all("SELECT * FROM shift_patterns WHERE shop_id=?", (shop_id,))
    overrides = shift_engine.load_weekday_overrides(shop_id)
    # ★ パターン別（詳細表示用）と時間帯別一意（カウント用）の両方を返す
    shortage_by_pattern = shift_engine.compute_shortage(shifts, pats, start_d, end_d, overrides)
    shortage_unique = shift_engine.compute_shortage_unique_hours(shifts, pats, start_d, end_d, overrides)
    return jsonify({
        "shortage": shortage_by_pattern,
        "shortage_unique": shortage_unique,
        "shortage_count": len(shortage_unique),
    })


@app.get("/api/shop/shifts/export")
def shop_shifts_export():
    shop, shop_id, _ = _shop_ctx()
    start_d, end_d = request.args.get("start"), request.args.get("end")
    if not start_d or not end_d:
        abort(400, description="start, end が必要")
    rows = query_all("SELECT sh.*, s.name as staff_name, s.role as staff_role, s.staff_code FROM shifts sh JOIN staffs s ON sh.staff_id=s.id WHERE sh.shop_id=? AND sh.start_datetime>=? AND sh.start_datetime<=? ORDER BY sh.start_datetime",
                     (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    wd = ["日", "月", "火", "水", "木", "金", "土"]
    lines = ["日付,曜日,開始,終了,休憩(分),実働(分),深夜(分),スタッフコード,氏名,ロール,ステータス,超過,メモ"]
    for r in rows:
        d = r["start_datetime"][:10]
        w = wd[(datetime.strptime(d, "%Y-%m-%d").weekday() + 1) % 7]
        work = minutes_between(r["start_datetime"], r["end_datetime"])
        nm = night_minutes(r["start_datetime"], r["end_datetime"])
        cells = [
            d, w, r["start_datetime"][11:16], r["end_datetime"][11:16],
            r.get("break_time_minutes") or 0, work, nm,
            r.get("staff_code", ""), r.get("staff_name", ""),
            "社員" if r.get("staff_role") == "employee" else "バイト",
            r.get("status", ""),
            "超過" if (r.get("over_cap_flag") or 0) else "",
            r.get("note") or "",
        ]
        lines.append(",".join(_csv_safe(c) for c in cells))
    csv = "\ufeff" + "\n".join(lines)
    return Response(csv, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="shift_{start_d}_{end_d}.csv"'})


# --- 変更申請 ---
@app.get("/api/shop/change-requests")
def shop_creq_list():
    shop, shop_id, _ = _shop_ctx()
    rows = query_all("SELECT cr.*, s.name as staff_name FROM change_requests cr JOIN staffs s ON cr.staff_id=s.id WHERE cr.shop_id=? ORDER BY cr.status='pending' DESC, cr.id DESC", (shop_id,))
    return jsonify({"change_requests": rows})


@app.put("/api/shop/change-requests/<int:crid>")
def shop_creq_resolve(crid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    cr = query_one("SELECT * FROM change_requests WHERE id=? AND shop_id=?", (crid, shop_id))
    if not cr:
        abort(404, description="申請が見つかりません")
    if cr["status"] != "pending":
        abort(400, description="既に処理済みです")
    now = jst_now().strftime("%Y-%m-%d %H:%M:%S")
    if body.get("action") == "reject":
        execute("UPDATE change_requests SET status='rejected', resolved_at=? WHERE id=?", (now, crid))
        notify(shop_id, cr["staff_id"], "info", "変更申請が却下されました",
               "ご申請は却下されました。詳細は店舗にご確認ください。")
        audit("creq.reject", target_type="change_request", target_id=crid, shop_id=shop_id,
              detail=cr["request_type"])
    else:
        if cr["request_type"] == "cancel" and cr.get("shift_id"):
            execute("DELETE FROM shifts WHERE id=? AND shop_id=?", (cr["shift_id"], shop_id))
        elif cr["request_type"] == "change" and cr.get("shift_id"):
            # 変更後時間が同スタッフの別シフトと重ならないか（自身は除外）
            overlap, _c = _check_staff_overlap(shop_id, cr["staff_id"], cr["desired_start"], cr["desired_end"], exclude_id=cr["shift_id"])
            if overlap:
                return jsonify({"error": "変更後の時間が同スタッフの別シフトと重複するため承認できません。", "overlap": True}), 400
            work = minutes_between(cr["desired_start"], cr["desired_end"])
            execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, status='confirmed', reason='変更申請承認' WHERE id=? AND shop_id=?",
                    (cr["desired_start"], cr["desired_end"], compute_break_minutes(work), cr["shift_id"], shop_id))
        elif cr["request_type"] == "add":
            # 追加申請：同スタッフの同日シフトと重ならないか
            overlap, _c = _check_staff_overlap(shop_id, cr["staff_id"], cr["desired_start"], cr["desired_end"])
            if overlap:
                return jsonify({"error": "同スタッフの同日シフトと重複するため承認できません。", "overlap": True}), 400
            work = minutes_between(cr["desired_start"], cr["desired_end"])
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, cr["staff_id"], cr["desired_start"], cr["desired_end"], compute_break_minutes(work), "confirmed", "追加申請承認"))
        execute("UPDATE change_requests SET status='approved', resolved_at=? WHERE id=?", (now, crid))
        # 承認で必要人数超過が生じた場合は該当日の確定シフトへフラグを付与（ブロックしない）
        day = (cr.get("desired_start") or "")[:10]
        if day:
            _flag_over_cap_shifts(shop_id, day + "T00:00:00", day + "T23:59:59")
        notify(shop_id, cr["staff_id"], "info", "変更申請が承認されました", "ご申請の変更を反映しました。")
        audit("creq.approve", target_type="change_request", target_id=crid, shop_id=shop_id,
              detail=cr["request_type"])
    return jsonify({"ok": True})


# --- AI ---
@app.post("/api/shop/ai/help-message")
def shop_ai_help():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    msg = ai.generate_help_message(body.get("date_label") or "近日中", body.get("time_label") or "終日", body.get("shortage") or 1, shop.get("shop_name") or "店舗")
    return jsonify({"message": msg})


@app.post("/api/shop/ai/review")
def shop_ai_review():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    start_d, end_d = body.get("start"), body.get("end")
    shifts = query_all("SELECT sh.*, s.name as staff_name FROM shifts sh JOIN staffs s ON sh.staff_id=s.id WHERE sh.shop_id=? AND sh.status='confirmed' AND sh.start_datetime>=? AND sh.start_datetime<=?",
                       (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    return jsonify(ai.review_shift_balance(shifts))


# --- AI 会話チャット（店長アシスタント） ---
@app.post("/api/shop/ai/chat")
def shop_ai_chat():
    shop, shop_id, settings = _shop_ctx()
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not message:
        abort(400, description="message が必要です")
    # 店舗コンテキストを構築（実データでAI/ルールベース回答に活用）
    today = jst_now().strftime("%Y-%m-%d")
    month_start = today[:8] + "01"
    month_end = today[:8] + "31"
    staffs = query_all("SELECT id, name, role, hourly_wage, max_hours_per_month, is_resigned FROM staffs WHERE shop_id=?", (shop_id,))
    active_staff = [s for s in staffs if not s.get("is_resigned")]
    patterns = query_all("SELECT id, pattern_name, start_time, end_time, required_staff FROM shift_patterns WHERE shop_id=?", (shop_id,))
    overrides = shift_engine.load_weekday_overrides(shop_id)
    # 今月の確定シフトで人件費・時間を計算
    month_shifts = query_all("SELECT sh.*, s.name as staff_name, s.role, s.hourly_wage FROM shifts sh JOIN staffs s ON sh.staff_id=s.id WHERE sh.shop_id=? AND sh.status='confirmed' AND sh.start_datetime>=? AND sh.start_datetime<=?",
                             (shop_id, month_start + "T00:00:00", month_end + "T23:59:59"))
    wage_map = {s["id"]: s["hourly_wage"] for s in staffs}
    total_cost = 0; total_hours = 0
    staff_hours = {}
    for sh in month_shifts:
        work = max(0, minutes_between(sh["start_datetime"], sh["end_datetime"]) - (sh.get("break_time_minutes") or 0))
        cost = int(work / 60 * wage_map.get(sh["staff_id"], 0))
        total_cost += cost
        total_hours += work / 60
        staff_hours[sh["staff_name"]] = staff_hours.get(sh["staff_name"], 0) + work / 60
    # 不足状況（パターン別詳細＋時間帯別一意カウント）
    shortage = shift_engine.compute_shortage(month_shifts, patterns, month_start, month_end, overrides)
    unique_shortage = shift_engine.compute_shortage_unique_hours(
        month_shifts, patterns, month_start, month_end, overrides)
    # 今日の出勤
    today_shifts = [s for s in month_shifts if s["start_datetime"][:10] == today]
    today_names = [s["staff_name"] for s in today_shifts]
    # 未処理の申請・希望
    creq_pending = query_all("SELECT * FROM change_requests WHERE shop_id=? AND status='pending'", (shop_id,))
    req_pending = query_all("SELECT * FROM shifts WHERE shop_id=? AND status='requested'", (shop_id,))
    notif_unread = query_all("SELECT * FROM notifications WHERE shop_id=? AND staff_id IS NULL AND is_read=0", (shop_id,))
    # 募集期間
    periods = query_all("SELECT * FROM shift_request_periods WHERE shop_id=? AND is_active=1 ORDER BY end_date DESC LIMIT 1", (shop_id,))
    active_period = periods[0] if periods else None

    ctx = {
        "shop_name": shop.get("shop_name") or "店舗",
        "today": today,
        "staff_count": len(active_staff),
        "employee_count": sum(1 for s in active_staff if s["role"] in ("employee", "manager")),
        "part_time_count": sum(1 for s in active_staff if s["role"] == "part_time"),
        "manager_count": sum(1 for s in active_staff if s["role"] == "manager"),
        "staff_names": [s["name"] for s in active_staff],
        "patterns": [{"name": p["pattern_name"], "time": f"{p['start_time']}-{p['end_time']}", "required": p["required_staff"]} for p in patterns],
        "has_weekday_overrides": len(overrides) > 0,
        "upcoming_confirmed": len(month_shifts),
        "today_attendance": len(today_names),
        "today_staff_names": today_names,
        "month_cost": total_cost,
        "month_hours": round(total_hours, 1),
        "staff_hours": staff_hours,
        "shortage_count": len(unique_shortage),
        "shortage_details": shortage[:8],
        "pending_requests": len(req_pending),
        "pending_approvals": len(creq_pending),
        "unread_notifications": len(notif_unread),
        "active_period": {"start": active_period["start_date"], "end": active_period["end_date"], "deadline": active_period["deadline"]} if active_period else None,
        "business_hours": settings.get("business_hours"),
        "default_wage": settings.get("default_hourly_wage"),
        "min_daily_hours": settings.get("min_daily_hours"),
        "max_consecutive_days": settings.get("max_consecutive_days"),
    }
    return jsonify(ai.chat(message, history, ctx))


# ===========================================================
# スタッフ
# ===========================================================
@app.get("/api/staff/periods")
def staff_periods():
    require_auth(["staff"]); staff = g.user
    rows = query_all("SELECT id, start_date, end_date, deadline, is_active FROM shift_request_periods WHERE shop_id=? ORDER BY start_date DESC", (staff["shop_id"],))
    return jsonify({"periods": rows})


@app.get("/api/staff/shifts")
def staff_shifts():
    """スタッフのシフト一覧。

    自分のシフト（全ステータス）＋同店舗の他スタッフの確定シフトを返す。
    他スタッフのシフトは「谁が出勤するか」の確認用（個人情報は含まない）。
    """
    require_auth(["staff"]); staff = g.user
    start_d, end_d = request.args.get("start"), request.args.get("end")
    date_clause = ""
    date_params = []
    if start_d and end_d:
        date_clause = " AND start_datetime>=? AND start_datetime<=?"
        date_params = [start_d + "T00:00:00", end_d + "T23:59:59"]
    # SQL の ? の順序: shop_id → date_range → staff_id
    sql = (
        "SELECT sh.id, sh.shop_id, sh.staff_id, sh.start_datetime, sh.end_datetime, "
        "sh.break_time_minutes, sh.status, sh.reason, s.name as staff_name, s.role as staff_role "
        "FROM shifts sh JOIN staffs s ON sh.staff_id=s.id "
        "WHERE sh.shop_id=? "
        f"{date_clause} "
        "AND (sh.staff_id=? OR sh.status='confirmed') "
        "ORDER BY sh.start_datetime"
    )
    all_params = [staff["shop_id"]] + date_params + [staff["id"]]
    return jsonify({"shifts": query_all(sql, tuple(all_params))})


@app.get("/api/staff/notifications")
def staff_notifs():
    require_auth(["staff"]); staff = g.user
    rows = query_all("SELECT id, type, title, body, is_read, created_at FROM notifications WHERE staff_id=? ORDER BY id DESC LIMIT 50", (staff["id"],))
    unread = sum(1 for r in rows if not r.get("is_read"))
    return jsonify({"notifications": rows, "unread": unread})


@app.put("/api/staff/notifications/read-all")
def staff_notifs_readall():
    require_auth(["staff"]); staff = g.user
    execute("UPDATE notifications SET is_read=1 WHERE staff_id=?", (staff["id"],))
    return jsonify({"ok": True})


@app.get("/api/staff/requests")
def staff_requests_list():
    require_auth(["staff"]); staff = g.user
    rows = query_all("SELECT id, start_datetime, end_datetime, status, reason FROM shifts WHERE staff_id=? AND status='requested' ORDER BY start_datetime", (staff["id"],))
    return jsonify({"requests": rows})


@app.post("/api/staff/requests")
def staff_requests_post():
    require_auth(["staff"]); staff = g.user
    body = request.get_json(silent=True) or {}
    items = body.get("shifts") or []
    if not items:
        abort(400, description="希望がありません")
    first_day = items[0]["start_datetime"][:10]
    period = query_one("SELECT * FROM shift_request_periods WHERE shop_id=? AND is_active=1 AND start_date<=? AND end_date>=? ORDER BY deadline DESC LIMIT 1", (staff["shop_id"], first_day, first_day))
    if not period:
        abort(400, description="この日程は募集期間外です")
    if period["deadline"] < jst_today().strftime("%Y-%m-%d"):
        abort(400, description=f"締切（{period['deadline']}）を過ぎています")
    count = 0
    skipped_overlap = 0
    for sh in items:
        avail = sh.get("availability")
        # 秒なし datetime を正規化（"YYYY-MM-DDTHH:MM" → "...HH:MM:00"）
        start_dt = normalize_iso(sh["start_datetime"])
        if avail:
            # 店舗のシフト時間設定から終了時刻デフォルトを取得
            shop_end = _get_shop_shift_end_time(staff["shop_id"])
            end_dt = normalize_iso(sh.get("end_datetime")) or (start_dt[:10] + f"T{shop_end}:00")
        else:
            end_dt = normalize_iso(sh["end_datetime"])
        # 同一スタッフの同日内で、確定シフト OR 既に出している希望と時間帯が重なる場合はスキップ（重複防止）
        overlap, _conflict = _check_staff_overlap(
            staff["shop_id"], staff["id"], start_dt, end_dt, include_requested=True)
        if overlap:
            skipped_overlap += 1
            continue
        if avail:
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason, availability) VALUES (?,?,?,?,?,?,?)",
                    (staff["shop_id"], staff["id"], start_dt, end_dt, "requested", "スタッフ希望(柔軟)", avail))
        else:
            work = minutes_between(start_dt, end_dt)
            execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                    (staff["shop_id"], staff["id"], start_dt, end_dt, compute_break_minutes(work), "requested", "スタッフ希望提出"))
        # ★ wish_history に永久保存（AI再生成時の入力 + スタッフへの履歴参照）
        # 同じ (staff_id, start, end) が既存ならスキップ（二重提出防止）
        existing = None
        try:
            existing = query_one(
                "SELECT id FROM wish_history WHERE staff_id=? AND start_datetime=? AND end_datetime=?",
                (staff["id"], start_dt, end_dt))
        except Exception:
            pass  # テーブル未作成時は無害（ensure_db で自動作成される）
        if existing is None:
            try:
                execute("INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, availability, note) VALUES (?,?,?,?,?,?)",
                        (staff["shop_id"], staff["id"], start_dt, end_dt, avail, "スタッフ希望提出"))
            except Exception:
                pass  # wish_history 未作成時は無害
        count += 1
    msg = f"{count}件の希望を提出しました"
    if skipped_overlap:
        msg += f"（{skipped_overlap}件は同日時間重複でスキップ）"
    return jsonify({"ok": True, "submitted": count, "skipped_overlap": skipped_overlap, "message": msg})


@app.delete("/api/staff/requests/<int:rid>")
def staff_requests_del(rid):
    require_auth(["staff"]); staff = g.user
    # 該当 shift を取得して wish_history のマッチング情報も得る
    sh = query_one(
        "SELECT start_datetime, end_datetime FROM shifts "
        "WHERE id=? AND staff_id=? AND status='requested'",
        (rid, staff["id"]))
    if sh:
        execute("DELETE FROM shifts WHERE id=? AND staff_id=? AND status='requested'",
                (rid, staff["id"]))
        # ★ wish_history からも削除（スタッフが明示的にキャンセルした希望）
        # 該当時間の希望のみ削除（他の希望は残す）
        try:
            execute("DELETE FROM wish_history WHERE staff_id=? AND start_datetime=? AND end_datetime=?",
                    (staff["id"], sh["start_datetime"], sh["end_datetime"]))
        except Exception:
            pass  # wish_history テーブル未作成時は無害
    return jsonify({"ok": True})


@app.get("/api/staff/wishes")
def staff_wishes():
    """スタッフ自身の希望履歴を取得（永久保存・AI再生成で消失しない）。

    スタッフから「わたしこういう希望出していたはず」と問い合わせがあった際の
    参照用。また AI自動生成の入力（shift_engine.auto_generate）と同ソース。
    """
    require_auth(["staff"]); staff = g.user
    try:
        rows = query_all(
            "SELECT id, start_datetime, end_datetime, availability, submitted_at, note "
            "FROM wish_history WHERE staff_id=? "
            "ORDER BY start_datetime DESC LIMIT 200",
            (staff["id"],))
    except Exception:
        rows = []  # テーブル未作成時は空
    return jsonify({"wishes": rows})


@app.get("/api/shop/wishes")
def shop_wishes():
    """店舗の全スタッフ希望履歴を取得（店長が確認用）。

    `staff_id` を渡すとそのスタッフだけに絞り込む（省略時は従来どおり店舗全体）。
    絞り込みが必要な理由: 末尾の LIMIT 500 は ORDER BY start_datetime DESC と
    組み合わさるため、上限に達したときに黙って落ちるのは「古い日付側」＝
    取り込みプレビューが確認したい対象月そのものになる。wish_history は永久履歴で
    上書き分も蓄積するため、11名運用でも数ヶ月で到達する。到達すると「既存あり」の
    印が出ず、サーバ側は重複としてスキップするのに画面は新規登録できるように見え、
    created がプレビュー件数と乖離する。対象スタッフが分かっている呼び出し元は
    staff_id を渡すことで、必要な行が黙って欠けないようにできる。
    指定形式: ?staff_id=3&staff_id=5 / ?staff_id=3,5 の両方を受け付ける。
    """
    shop, shop_id, _ = _shop_ctx()
    start_d = request.args.get("start")
    end_d = request.args.get("end")
    raw_staff_args = request.args.getlist("staff_id")
    staff_ids = []
    for raw_v in raw_staff_args:
        for part in str(raw_v).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                sid = int(part)
            except ValueError:
                continue
            if sid not in staff_ids:
                staff_ids.append(sid)
    if raw_staff_args and not staff_ids:
        # 絞り込みを指定されたが有効なIDが1つも無い。店舗全体を返すと
        # 「頼んだ範囲」と違うものを返すことになるため、空で返す。
        return jsonify({"wishes": []})
    sql = ("SELECT wh.id, wh.staff_id, s.name as staff_name, s.staff_code, "
           "wh.start_datetime, wh.end_datetime, wh.availability, wh.submitted_at, wh.note "
           "FROM wish_history wh "
           "JOIN staffs s ON wh.staff_id=s.id WHERE wh.shop_id=?")
    params = [shop_id]
    if staff_ids:
        sql += " AND wh.staff_id IN (" + ",".join(["?"] * len(staff_ids)) + ")"
        params.extend(staff_ids)
    if start_d:
        sql += " AND wh.start_datetime>=?"
        params.append(start_d + "T00:00:00")
    if end_d:
        sql += " AND wh.start_datetime<=?"
        params.append(end_d + "T23:59:59")
    sql += " ORDER BY wh.start_datetime DESC LIMIT 500"
    try:
        rows = query_all(sql, tuple(params))
    except Exception:
        rows = []
    return jsonify({"wishes": rows})


_YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# 「元の文」照合時に無視する引用符（LLM が 「…」 や "…" で括って返すことがある）
_RAW_QUOTE_CHARS = "「」『』“”‘’\"'`"


def _wish_raw_norm(s):
    """raw と貼り付けテキストを照合するための「緩い」正規化。

    全角/半角（NFKC）・大文字小文字・空白・引用符の差だけを吸収する。
    意味のある文字は落とさない（落とすと幻覚された raw まで一致してしまう）。
    厳しすぎると常に不一致になり、警告そのものが無意味になるためこの粒度にする。
    """
    if not isinstance(s, str):
        return ""
    t = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in t if not ch.isspace() and ch not in _RAW_QUOTE_CHARS)


def _wish_raw_verified(raw, text_norm):
    """entry の raw が貼り付けテキストに実在するか（設計書 §6 の関門を守る）。

    raw は LLM が生成した文字列で、入力テキストの部分文字列である保証がどこにも
    無い。要約・言い換え・幻覚された raw を「元の文」として見せると、店長は
    捏造された文と照合することになり、「元の文を必ず見せる＝誤読を発見する唯一の
    手段」（設計書 §6）が機能しなくなる。プロンプトインジェクションと組み合わせ
    れば意図的にも悪用できる。ここで照合し、確認できないものには
    raw_verified=false を立てて UI に警告させる。

    raw が複数の断片を連結したもの（フォールバックは "\\n" や " / " で連結する）の
    場合は、全ての断片が入力に含まれるときだけ verified とする。
    """
    if not isinstance(raw, str) or not raw.strip():
        return False  # 照合するものが無い＝店長は確認できない
    parts = []
    for line in raw.split("\n"):
        parts.extend(line.split(" / "))
    checked = 0
    for p in parts:
        n = _wish_raw_norm(p)
        if not n:
            continue
        checked += 1
        if n not in text_norm:
            return False
    return checked > 0


@app.post("/api/shop/wishes/parse")
def shop_wishes_parse():
    """希望テキストを解析する。保存はしない（何度でも試せる）。"""
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    raw_text = body.get("text")
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    if not text:
        abort(400, description="text が必要です")
    # year_month を検証してから使う（不正値のまま解析に渡すと int() の ValueError が
    # そのまま 400 のメッセージになり、Python の内部メッセージが利用者に露出する）
    raw_ym = body.get("year_month")
    year_month = raw_ym.strip() if isinstance(raw_ym, str) else ""
    if not year_month:
        year_month = jst_today().strftime("%Y-%m")
    if not _YEAR_MONTH_RE.match(year_month):
        abort(400, description="year_month は YYYY-MM 形式で指定してください（例: 2026-08）")
    # ★ 明示指定された staff_id も店舗所属を検証する。bulk 側でも弾いてはいるが、
    # 未検証のままエコーバックすると parse のレスポンスだけを信じる呼び出し元
    # （将来の画面・外部連携）に他店舗の staff_id が渡ってしまう。
    raw_staff_id = body.get("staff_id")
    staff_id = None
    if raw_staff_id not in (None, ""):
        try:
            sid = int(raw_staff_id)
        except (TypeError, ValueError):
            abort(400, description="staff_id が不正です")
        row = query_one("SELECT id FROM staffs WHERE id=? AND shop_id=? AND is_resigned=0",
                        (sid, shop_id))
        if not row:
            abort(400, description="指定されたスタッフが見つかりません")
        staff_id = row["id"]
    staffs = query_all("SELECT id, name FROM staffs WHERE shop_id=? AND is_resigned=0", (shop_id,))
    result = ai.parse_wish_text(text, year_month, [s["name"] for s in staffs])
    # staff_hint をスタッフIDに解決する（一致しなければ None のまま＝未割り当て。推測はしない）
    by_name = {s["name"]: s["id"] for s in staffs}
    entries = [e for e in (result.get("entries") or []) if isinstance(e, dict)]
    result["entries"] = entries
    text_norm = _wish_raw_norm(text)
    for e in entries:
        # ★ 「元の文」が本当に入力に在るかを毎回検証して返す（UI が警告を出す）
        e["raw_verified"] = _wish_raw_verified(e.get("raw"), text_norm)
        if staff_id is not None:
            e["staff_id"] = staff_id  # 明示指定が最優先。staff_hint は無視する
        else:
            hint = e.get("staff_hint")
            # LLM が list/dict を返しても落ちないこと（dict.get に非ハッシュ可能な
            # 値を渡すと TypeError で 500 になる）
            e["staff_id"] = by_name.get(hint) if isinstance(hint, str) and hint else None
    return jsonify(result)


def _wish_times(date, availability, start, end, shop_end):
    """希望の availability から start_datetime/end_datetime を決める（設計書 §3 の表）。

    - rest        : {date}T00:00:00 〜 {date}T23:59:59（終日休み）
    - any/morning/evening : {date}T09:00:00 〜 {date}T{shop_end}:00
      （3種とも時刻は同じ。既存の /api/staff/requests と同じ挙動で、区別は
      availability の値そのものが担う。ここを変えると既存データとの整合が崩れる）
    - time        : 指定された start〜end。end<=start なら翌日扱い
      （combine_dt_overnight は shift_patterns/fixed_shifts と同じ日またぎ判定を使う）
    """
    if availability == "rest":
        return f"{date}T00:00:00", f"{date}T23:59:59"
    if availability == "time":
        return combine_dt_overnight(date, start, end)
    return f"{date}T09:00:00", f"{date}T{shop_end}:00"


# 希望表管理画面の .wmark が理解する語彙のみを許可する（設計書 §3）。
# ここに無い値（タイプミス等）は any/morning/evening 扱いにフォールバックさせず、
# 明示的にスキップする（未知トークンによる画面表示崩れを防ぐ）。
_WISH_AVAILABILITY_VALUES = ("rest", "any", "morning", "evening", "time")

# availability='time' の start/end はこの形式のみ受け付ける（00:00〜23:59）。
# 検証しないと utils.norm_hhmm が "17時" を黙って "00:00" に潰し、24時間の希望
# として保存される。"25:00" は不正な datetime を組み立てて後続処理を例外にする。
_WISH_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_WISH_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_wish_date(s):
    """'YYYY-MM-DD' 形式かつ暦上有効な日付か。"""
    if not isinstance(s, str) or not _WISH_DATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _wish_history_exists(staff_id, start_dt, end_dt):
    """wish_history に同一 (staff_id, start_datetime, end_datetime) の行が既存か。

    wish_history はスキーマ初期化時に必ず作成される（schema.sql）。
    「no such table」以外の DB エラーはここで握りつぶさず呼び出し元に伝える。
    黙って握りつぶすと「実際には確認できていないのに既存なしとして登録を進めた」
    という事故につながるため。呼び出し元（bulk）は item 単位で捕捉し、その1件だけを
    スキップして続行する（1件のDB不調でバッチ全体を 500 にしない）。
    """
    try:
        return query_one(
            "SELECT id FROM wish_history WHERE staff_id=? AND start_datetime=? AND end_datetime=?",
            (staff_id, start_dt, end_dt)) is not None
    except Exception as e:
        if "no such table" in str(e).lower():
            return False
        raise


@app.post("/api/shop/wishes/bulk")
def shop_wishes_bulk():
    """プレビューで確定した希望を一括登録する。

    既存の希望提出（/api/staff/requests）と同じく shifts(status='requested') と
    wish_history の両方に書く。片方だけでは機能しない
    （前者はAI生成の入力、後者は希望表管理画面が読む永久履歴）。
    `created` は「両テーブルに実際に書けた」件数だけを数える
    （どちらか一方にしか書けない状態を作らない）。

    店長の代理入力なので、スタッフ提出時の募集期間・締切の検証は行わない
    （締切はスタッフに対する期限であり店長を縛らない。募集期間が未設定の
    店舗でも取り込めるようにする）。

    レスポンスの `skipped_detail` は skipped の内訳（合計は必ず skipped と一致）。
      - duplicate : 既に同じ希望がある（_check_staff_overlap / _wish_history_exists）
      - invalid   : 入力が不正（他店舗/退職スタッフ、enum外 availability、
                    不正な date/start/end）
      - rollback  : 書き込みに失敗して取り消した（DB不調で書けなかった分を含む）
    内訳を分けずに「重複のためスキップ」とだけ返すと、書き込みエラーまで
    「重複」と偽って店長に伝えることになる。
    """
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    wishes = body.get("wishes") or []
    overwrite = bool(body.get("overwrite"))
    if not wishes:
        abort(400, description="wishes が必要です")
    shop_end = _get_shop_shift_end_time(shop_id)
    created = 0
    detail = {"duplicate": 0, "invalid": 0, "rollback": 0}
    for w in wishes:
        if not isinstance(w, dict):
            detail["invalid"] += 1
            continue
        staff_id = w.get("staff_id")
        date = w.get("date")
        avail = w.get("availability")
        if not staff_id or avail not in _WISH_AVAILABILITY_VALUES or not _is_wish_date(date):
            detail["invalid"] += 1
            continue
        try:
            staff_id = int(staff_id)
        except (TypeError, ValueError):
            detail["invalid"] += 1
            continue
        # ★ time は start/end の「形式」まで検証する（存在チェックだけでは不足）。
        # "17時" は norm_hhmm が黙って "00:00" に潰し 00:00〜24:00 の終日希望として
        # 保存され、"25:00" は不正な datetime を組み立てて破壊的な例外経路に入る。
        # パーサ側でも検証するが、bulk はサーバ側の最終防衛線なのでここでも弾く。
        if avail == "time" and not (
                _WISH_TIME_RE.match(str(w.get("start") or "")) and
                _WISH_TIME_RE.match(str(w.get("end") or ""))):
            detail["invalid"] += 1
            continue
        # ★ 他店舗/退職者の staff_id を弾く（parse 側では未検証。保存側が最終防衛線）
        try:
            staff = query_one("SELECT id FROM staffs WHERE id=? AND shop_id=?", (staff_id, shop_id))
        except Exception as e:
            detail["rollback"] += 1
            print(f"[wishes/bulk] staffs 照会に失敗（この1件のみスキップ・データは無傷） "
                  f"staff_id={staff_id} date={date}: {e}", flush=True)
            continue
        if not staff:
            detail["invalid"] += 1
            continue
        try:
            start_dt, end_dt = _wish_times(date, avail, w.get("start"), w.get("end"), shop_end)
        except Exception as e:
            detail["invalid"] += 1
            print(f"[wishes/bulk] 日時の組み立てに失敗 staff_id={staff_id} date={date}: {e}", flush=True)
            continue
        # ★ overwrite でも「先に DELETE」はしない。
        # 先に消すと、後続の INSERT が失敗したときに既存の希望が消えたまま戻らない
        # （HTTP 200・ok:true・「重複のためスキップ」と報告しながら、店長やスタッフ
        # 本人が持っていた希望だけが消える）。再送すればまた DELETE が走り、また失敗し、
        # データは戻らない。本番は D1（REST API 越し）でタイムアウト・レート制限が
        # 現実に起きるため、消す前に対象を id で退避し、両テーブルへの INSERT が
        # 成功したときだけ退避行を削除する（失敗時は1行も消えない）。
        stale_wh, stale_sh = [], []
        if overwrite:
            try:
                stale_wh = [r["id"] for r in query_all(
                    "SELECT id FROM wish_history WHERE shop_id=? AND staff_id=? AND start_datetime LIKE ?",
                    (shop_id, staff_id, date + "%"))]
                stale_sh = [r["id"] for r in query_all(
                    "SELECT id FROM shifts WHERE shop_id=? AND staff_id=? AND status='requested' "
                    "AND start_datetime LIKE ?",
                    (shop_id, staff_id, date + "%"))]
            except Exception as e:
                detail["rollback"] += 1
                print(f"[wishes/bulk] 上書き対象の照会に失敗（既存希望は無傷のまま中止） "
                      f"staff_id={staff_id} date={date}: {e}", flush=True)
                continue
        else:
            # ★ shifts と wish_history はスキップ判定の基準が違う
            #   （shifts=時間帯の重なり判定 / wish_history=完全一致判定）。
            # どちらか一方だけを見て INSERT すると「重ならないので shifts には新規
            # 行ができるが、wish_history は完全一致の既存行があるため書けない」という
            # 非対称が起き、created が実態と乖離する。両方をここでまとめて判定し、
            # どちらかに該当すれば INSERT 自体を行わない。
            try:
                overlap, _conflict = _check_staff_overlap(
                    shop_id, staff_id, start_dt, end_dt, include_requested=True)
                is_dup = bool(overlap) or _wish_history_exists(staff_id, start_dt, end_dt)
            except Exception as e:
                # 重複判定そのものが失敗した。「確認できていないのに登録する」ことは
                # しない。かつ 1件のDB不調でバッチ全体を 500 にもしない（item 単位で
                # スキップして続行する）。重複ではないので rollback に計上する。
                detail["rollback"] += 1
                print(f"[wishes/bulk] 重複判定に失敗（この1件のみスキップ・データは無傷） "
                      f"staff_id={staff_id} date={date}: {e}", flush=True)
                continue
            if is_dup:
                detail["duplicate"] += 1
                continue
        note = "店長が取り込み"
        raw = str(w.get("raw") or "").strip()
        if raw:
            # プレビューは raw を全文見せる。切り詰めた記録だけが残ると「見た文」と
            # 「残る記録」がずれるため、切り詰めた事実を記録側にも残す。
            note += f": {raw[:500]}" + ("…（元の文はここで省略）" if len(raw) > 500 else "")
        try:
            if avail == "time":
                work = minutes_between(start_dt, end_dt)
                shift_meta = execute(
                    "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, staff_id, start_dt, end_dt, compute_break_minutes(work), "requested", note))
            else:
                shift_meta = execute(
                    "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason, availability) VALUES (?,?,?,?,?,?,?)",
                    (shop_id, staff_id, start_dt, end_dt, "requested", note, avail))
        except Exception as e:
            detail["rollback"] += 1
            print(f"[wishes/bulk] shifts INSERT失敗（既存希望は削除していないため無傷） "
                  f"staff_id={staff_id} date={date}: {e}", flush=True)
            continue
        # ★ wish_history にも永久保存（AI再生成の入力 + 希望表管理画面の参照元）。
        # availability は shifts 側と完全に同じ値にする。時間指定希望は shifts に
        # availability を書かない（NULL）ので、ここも None を書く。既存の唯一の
        # 書き手 /api/staff/requests も時間指定では両テーブル NULL であり、
        # 'time' という値は本ブランチ以前 DB に一度も存在しなかった。
        # 'time' を書くと壊れるもの:
        #   1. 希望表管理画面が「時間指定」を「柔軟 17:00-22:00（目安）」と
        #      偽って表示する（.wmark の語彙に 'time' が無く「柔軟」に落ちる）
        #   2. shift_engine Step2a の UNION が畳めず同じ希望が2行に増え、片方は
        #      flex 扱いになるが _slot_matches('time', ...) が常に False なので
        #      永久に配置されない幽霊希望として残る
        #   3. 希望反映率の分母（req_count）が水増しされ、「調整待ち」の件数が嘘になる
        # ここで失敗すると shifts 側だけ書けた状態（片方だけ入る）になってしまうため、
        # 直前に作った shifts 行を取り消し、created ではなく rollback として扱う。
        # 例外は握りつぶさず print で残す（「登録できていないのに登録した」と
        # 表示する事故を防ぐため、原因は追えるようにする）。
        wh_avail = None if avail == "time" else avail
        try:
            execute(
                "INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, availability, note) VALUES (?,?,?,?,?,?)",
                (shop_id, staff_id, start_dt, end_dt, wh_avail, note))
        except Exception as e:
            new_shift_id = (shift_meta or {}).get("last_row_id")
            if new_shift_id:
                try:
                    execute("DELETE FROM shifts WHERE id=? AND shop_id=?", (new_shift_id, shop_id))
                except Exception as de:
                    print(f"[wishes/bulk] ★補償失敗: shifts行 id={new_shift_id} を取り消せなかった "
                          f"staff_id={staff_id} date={date}: {de}", flush=True)
            else:
                # D1 では meta.last_row_id が取れないと 0 が返る（src/db.py）。
                # id=0 で DELETE を撃っても無言で空振りするだけなので、撃たずに
                # 「補償できなかった」と記録する（孤立行が残った可能性がある）。
                print(f"[wishes/bulk] ★補償失敗: last_row_id を取得できず shifts行を取り消せない "
                      f"staff_id={staff_id} date={date}", flush=True)
            detail["rollback"] += 1
            print(f"[wishes/bulk] wish_history INSERT失敗のため shifts行を取消 "
                  f"staff_id={staff_id} date={date}: {e}", flush=True)
            continue
        # 両テーブルに書けたので、ここで初めて既存行を消す（overwrite 指定時のみ）。
        # 退避した id を消すので、直前に INSERT した新しい行は対象にならない。
        for table, stale_ids in (("wish_history", stale_wh), ("shifts", stale_sh)):
            for old_id in stale_ids:
                try:
                    execute(f"DELETE FROM {table} WHERE id=? AND shop_id=?", (old_id, shop_id))
                except Exception as e:
                    print(f"[wishes/bulk] 上書き対象の {table} 行 id={old_id} を削除できず "
                          f"（新しい希望は登録済み・古い希望が残っている） "
                          f"staff_id={staff_id} date={date}: {e}", flush=True)
        created += 1
    skipped = detail["duplicate"] + detail["invalid"] + detail["rollback"]
    msg = f"{created}件の希望を登録しました"
    reasons = []
    if detail["duplicate"]:
        reasons.append(f"{detail['duplicate']}件は既存の希望と重複")
    if detail["invalid"]:
        reasons.append(f"{detail['invalid']}件は入力が不正")
    if detail["rollback"]:
        reasons.append(f"{detail['rollback']}件は登録に失敗（取り消し済み）")
    if reasons:
        msg += "（" + "、".join(reasons) + "のためスキップ）"
    return jsonify({"ok": True, "created": created, "skipped": skipped,
                    "skipped_detail": detail, "message": msg})


@app.get("/api/shop/staff-tendencies")
def shop_staff_tendencies():
    """スタッフ別の勤務傾向スコアを取得（AI学習データの透明化）。

    過去90日分の確定シフト + 希望から計算した時間帯ヒストグラムを返す。
    店長が「なぜこの人をこのシフトにしたか」を理解するための参照用。
    """
    shop, shop_id, _ = _shop_ctx()
    try:
        past_confirmed = query_all(
            "SELECT staff_id, start_datetime, end_datetime FROM shifts "
            "WHERE shop_id=? AND status='confirmed' "
            "AND start_datetime >= datetime('now', '-90 days')",
            (shop_id,))
        past_wishes = query_all(
            "SELECT staff_id, start_datetime, end_datetime FROM wish_history "
            "WHERE shop_id=? AND start_datetime >= datetime('now', '-90 days')",
            (shop_id,))
        tendency_map = build_staff_tendency(past_confirmed, past_wishes)
    except Exception:
        tendency_map = {}
    staffs = query_all("SELECT id, name, staff_code, role FROM staffs WHERE shop_id=? AND is_resigned=0", (shop_id,))
    result = []
    for s in staffs:
        hist = tendency_map.get(s["id"])
        if not hist:
            continue
        # 上位3時間帯を抽出
        ranked = sorted(enumerate(hist), key=lambda x: -x[1])[:5]
        top_hours = [{"hour": h, "score": round(v, 3)} for h, v in ranked if v > 0]
        result.append({
            "staff_id": s["id"],
            "name": s["name"],
            "staff_code": s["staff_code"],
            "role": s["role"],
            "top_hours": top_hours,
            "sample_count_confirmed": sum(1 for x in past_confirmed if x["staff_id"] == s["id"]),
            "sample_count_wish": sum(1 for x in past_wishes if x["staff_id"] == s["id"]),
        })
    result.sort(key=lambda x: -x["sample_count_confirmed"])
    return jsonify({"tendencies": result, "total_staff": len(result)})


@app.post("/api/staff/change-requests")
def staff_creq_post():
    require_auth(["staff"]); staff = g.user
    body = request.get_json(silent=True) or {}
    rtype = body.get("request_type")
    if rtype not in ("change", "cancel", "add"):
        abort(400, description="request_type が不正です")
    shift_id = body.get("shift_id")
    if shift_id:
        sh = query_one("SELECT id FROM shifts WHERE id=? AND staff_id=?", (shift_id, staff["id"]))
        if not sh:
            abort(404, description="対象シフトが見つかりません")
    insert_row("change_requests", {"shop_id": staff["shop_id"], "staff_id": staff["id"], "shift_id": shift_id,
                                   "request_type": rtype, "desired_start": body.get("desired_start"),
                                   "desired_end": body.get("desired_end"), "reason": body.get("reason")})
    notify(staff["shop_id"], None, "info", "変更申請が届きました", f"{staff.get('name','スタッフ')}さんから{rtype}の申請があります。")
    return jsonify({"ok": True})


@app.get("/api/staff/change-requests")
def staff_creq_list():
    require_auth(["staff"]); staff = g.user
    rows = query_all("SELECT id, request_type, desired_start, desired_end, reason, status, created_at, resolved_at FROM change_requests WHERE staff_id=? ORDER BY id DESC LIMIT 50", (staff["id"],))
    return jsonify({"change_requests": rows})


@app.get("/api/staff/shifts/ics")
def staff_ics():
    # カレンダーアプリ向けにクエリトークンで認証
    token = request.args.get("t") or ""
    sess = query_one("SELECT * FROM sessions WHERE token=?", (token,))
    if not sess or sess["role"] != "staff":
        abort(401, description="無効なURLです")
    sid = sess["user_id"]
    st = query_one("SELECT * FROM staffs WHERE id=?", (sid,))
    shop = query_one("SELECT shop_name FROM shops WHERE id=?", (st["shop_id"],))
    shifts = query_all("SELECT * FROM shifts WHERE staff_id=? AND status='confirmed' ORDER BY start_datetime", (sid,))
    ics = build_ics(shifts, st["name"], shop["shop_name"] if shop else "ShiftAI")
    return Response(ics, mimetype="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=\"my_shift.ics\""})


@app.get("/api/staff/dashboard")
def staff_dashboard():
    require_auth(["staff"]); staff = g.user
    pend_req = query_one("SELECT count(*) as c FROM shifts WHERE staff_id=? AND status='requested'", (staff["id"],))
    pend_app = query_one("SELECT count(*) as c FROM change_requests WHERE staff_id=? AND status='pending'", (staff["id"],))
    next_shift = query_one("SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='confirmed' AND start_datetime>=? ORDER BY start_datetime LIMIT 1", (staff["id"], jst_now().strftime("%Y-%m-%dT%H:%M:%S")))
    return jsonify({"pending_requests": pend_req["c"], "pending_approvals": pend_app["c"], "next_shift": next_shift})


@app.get("/api/staff/summary")
def staff_summary():
    require_auth(["staff"]); staff = g.user
    start_d, end_d = request.args.get("start"), request.args.get("end")
    if not start_d or not end_d:
        abort(400, description="start, end が必要")
    shifts = query_all("SELECT * FROM shifts WHERE staff_id=? AND start_datetime>=? AND start_datetime<=?", (staff["id"], start_d + "T00:00:00", end_d + "T23:59:59"))
    shop = query_one("SELECT settings FROM shops WHERE id=?", (staff["shop_id"],))
    return jsonify(summarize_shifts(shifts, {staff["id"]: staff}, parse_settings(shop["settings"])))


@app.put("/api/staff/password")
def staff_password():
    require_auth(["staff"]); staff = g.user
    body = request.get_json(silent=True) or {}
    full = query_one("SELECT password_hash FROM staffs WHERE id=?", (staff["id"],))
    if not verify_password(body.get("current_password", ""), full["password_hash"]):
        abort(400, description="現在のパスワードが正しくありません")
    err = validate_password(body.get("new_password", ""))
    if err:
        abort(400, description=err)
    execute("UPDATE staffs SET password_hash=? WHERE id=?", (hash_password(body["new_password"]), staff["id"]))
    return jsonify({"ok": True})


@app.post("/api/staff/ai/parse")
def staff_ai_parse():
    require_auth(["staff"]); staff = g.user
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not text:
        abort(400, description="text が必要です")
    return jsonify(ai.parse_shift_request(text, staff.get("hourly_wage") or 1000, body.get("period_days") or 15))


@app.post("/api/staff/ai/chat")
def staff_ai_chat():
    require_auth(["staff"]); staff = g.user
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not message:
        abort(400, description="message が必要です")
    today = jst_now().strftime("%Y-%m-%d")
    shifts = query_all("SELECT start_datetime, end_datetime, status, break_time_minutes FROM shifts WHERE staff_id=? AND start_datetime>=? ORDER BY start_datetime",
                       (staff["id"], today + "T00:00:00"))
    wage = staff.get("hourly_wage") or 1000
    ctx = {
        "staff_name": staff.get("name") or "スタッフ",
        "hourly_wage": wage,
        "today": today,
        "upcoming_shifts": [{"start": s["start_datetime"], "end": s["end_datetime"], "status": s["status"]} for s in shifts[:30]],
        "role": "社員" if staff.get("role") == "employee" else "アルバイト",
    }
    return jsonify(ai.chat_staff(message, history, ctx))


# ===========================================================
# 静的アセット配信（SPAフォールバック）
# ===========================================================
def _index_html_with_asset_version():
    """index.html を返す際、app.js と style.css に mtime ベースの ?v= を付与し、
    ブラウザキャッシュによる古いJS/新HTMLの不整合（TypeError: null.addEventListener 等）を防ぐ。"""
    with open(os.path.join(PUBLIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    try:
        js_mtime = int(os.path.getmtime(os.path.join(PUBLIC_DIR, "app.js")))
        admin_mtime = int(os.path.getmtime(os.path.join(PUBLIC_DIR, "admin.js")))
        css_mtime = int(os.path.getmtime(os.path.join(PUBLIC_DIR, "style.css")))
        html = html.replace('src="app.js"', f'src="app.js?v={js_mtime}"')
        html = html.replace('src="admin.js"', f'src="admin.js?v={admin_mtime}"')
        html = html.replace('href="style.css"', f'href="style.css?v={css_mtime}"')
    except Exception:
        pass
    return html


@app.get("/")
def index():
    html = _index_html_with_asset_version()
    resp = Response(html, content_type="text/html; charset=utf-8")
    # HTML自体もキャッシュさせない（常に最新を取得させ、app.js/style.cssの?v=も最新化）
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/<path:path>")
def static_files(path):
    if path.startswith("api/"):
        abort(404, description="Not Found")
    full = safe_join(PUBLIC_DIR, path)
    if full is None:
        abort(404, description="Not Found")
    if os.path.isfile(full):
        # app.js / admin.js / style.css は短時間キャッシュ（常に最新を取得させる）
        if path in ("app.js", "admin.js", "style.css"):
            resp = send_file(full)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp
        return send_file(full)
    return _index_html_with_asset_version()


# 管理者API（/api/admin/*）は src/admin_api.py に切り出してある。
# require_auth / audit / summarize_shifts は app.py 側にしか無いため引数で渡す。
import admin_api
admin_api.register_admin_routes(
    app, require_auth=require_auth, audit=audit, summarize_shifts=summarize_shifts)


# ===========================================================
# 起動
# ===========================================================
def ensure_db():
    """起動時にスキーマを整備。本番(Railway等)では失敗するとコンテナが死ぬので、
    エラーは握り潰さずにログ出力して上位に伝播させる。"""
    if not os.path.exists(SCHEMA_PATH):
        print(f"[ensure_db] WARN: schema.sql not found at {SCHEMA_PATH}", flush=True)
        return
    try:
        init_schema(SCHEMA_PATH)
        print(f"[ensure_db] OK: schema initialized from {SCHEMA_PATH}", flush=True)
    except Exception as e:
        # スキーマ初期化失敗は致命的 → ログを出して伝播
        print(f"[ensure_db] FAIL: {e}", flush=True)
        raise
    try:
        shift_columns = {row["name"] for row in query_all("PRAGMA table_info(shifts)")}
        if "updated_at" not in shift_columns:
            execute("ALTER TABLE shifts ADD COLUMN updated_at TEXT")
            print("[ensure_db] OK: shifts.updated_at added", flush=True)
    except Exception as e:
        print(f"[ensure_db] FAIL: shifts.updated_at migration failed: {e}", flush=True)
        raise
    # over_cap_flag / note（超過確定の可視化・店長メモ）
    try:
        shift_columns = {row["name"] for row in query_all("PRAGMA table_info(shifts)")}
        if "over_cap_flag" not in shift_columns:
            execute("ALTER TABLE shifts ADD COLUMN over_cap_flag INTEGER DEFAULT 0")
            print("[ensure_db] OK: shifts.over_cap_flag added", flush=True)
        if "note" not in shift_columns:
            execute("ALTER TABLE shifts ADD COLUMN note TEXT")
            print("[ensure_db] OK: shifts.note added", flush=True)
    except Exception as e:
        print(f"[ensure_db] FAIL: shifts over_cap/note migration failed: {e}", flush=True)
        raise
    # ★ データ正規化（インシデント対策）
    # 過去バージョンで "2026-08-01T7:00:00" のような非ゼロ埋め時刻が
    # shifts / wish_history に保存されていた問題を修復する。
    # また、wish_history で end <= start（同日）の翌日またぎ希望も修復する。
    try:
        _normalize_datetime_data()
    except Exception as e:
        print(f"[ensure_db] WARN: data normalization failed (skipped): {e}", flush=True)
    # 監査ログテーブル（作成失敗しても業務は止めない）
    try:
        execute(
            "CREATE TABLE IF NOT EXISTS audit_logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, actor_role TEXT, actor_id INTEGER, "
            "actor_name TEXT, action TEXT NOT NULL, target_type TEXT, target_id INTEGER, "
            "shop_id INTEGER, detail TEXT, created_at TEXT DEFAULT (datetime('now')))")
        execute("CREATE INDEX IF NOT EXISTS idx_audit_shop ON audit_logs(shop_id, created_at)")
        execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action, created_at)")
    except Exception as e:
        print(f"[ensure_db] WARN: audit_logs setup failed (skipped): {e}", flush=True)
    # ログイン試行のレート制限テーブル。
    # ここで握り潰しても _verify_critical_tables() が起動を止めるので、
    # 「テーブルが無いのに healthy」という状態にはならない。
    try:
        execute(
            "CREATE TABLE IF NOT EXISTS login_attempts ("
            "attempt_key TEXT PRIMARY KEY, fail_count INTEGER NOT NULL DEFAULT 0, "
            "locked_until TEXT, updated_at TEXT, "
            "blocked_logged INTEGER NOT NULL DEFAULT 0)")
        # 既存DB（blocked_logged 導入前）への追加。CREATE TABLE IF NOT EXISTS は
        # 既存テーブルの列を増やさないため、ALTER で追う。
        cols = {row["name"] for row in query_all("PRAGMA table_info(login_attempts)")}
        if "blocked_logged" not in cols:
            execute("ALTER TABLE login_attempts ADD COLUMN blocked_logged INTEGER NOT NULL DEFAULT 0")
            print("[ensure_db] OK: login_attempts.blocked_logged added", flush=True)
    except Exception as e:
        print(f"[ensure_db] WARN: login_attempts setup failed (skipped): {e}", flush=True)


def _verify_critical_tables():
    """ensure_db() の後に、無いと機能が黙って壊れるテーブル・列の実在を検証する。

    【なぜ起動を落とすのか】
      login_attempts が無いと _check_login_lock の SELECT が失敗し、
      未認証クライアントに 500（生の SQL エラー付き）が返る。つまり誰もログイン
      できないのに /api/health は 200 を返し続け、Railway のヘルスチェックは通る。
      「レート制限をフェイルオープンさせる」案はブルートフォース防御が静かに
      無効化されるため採らない。起動時に落として再起動させる方が安全。

      ensure_db() は途中の PRAGMA/ALTER で raise し得るため、そこで中断すると
      login_attempts の CREATE に到達しない。また D1 モードの init_schema は
      個別ステートメントの失敗を握り潰す。どちらの経路も「作られなかった」を
      検知できるよう、CREATE の成否ではなく実在を最後に確認する。

    【なぜテーブルだけでなく列まで見るのか】
      login_attempts.blocked_logged は CREATE TABLE IF NOT EXISTS では追加され
      ず、既存DBには ensure_db() 内の ALTER TABLE ADD COLUMN で追う。この
      ALTER が（本番D1で実際に起きた migrations/0004 のスキーマ変更失敗のよ
      うに）何らかの理由で失敗すると、テーブルは存在するのに列だけが欠けた
      状態になる。「SELECT 1 FROM table」はテーブルの実在しか見ないため、この
      状態を素通りさせてしまい、結局ロック中の /api/login が
      「no such column: blocked_logged」の 500 を未認証クライアントに返す。
      これは本関数を導入した動機（テーブル欠如の見逃し）と同じ失敗モードが
      列単位で再発しているだけなので、各テーブルで実際に使う列を明示して
      SELECT することで、テーブル・列どちらの欠落も検知する。
    """
    # テーブル名 -> そのテーブルで実際に使う列（一貫性のため audit_logs /
    # sessions も同じ形で検証する）。
    checks = {
        "login_attempts": "blocked_logged",
        "audit_logs": "action",
        "sessions": "role",
    }
    for table, column in checks.items():
        try:
            query_all(f"SELECT {column} FROM {table} LIMIT 1")
        except Exception as e:
            raise RuntimeError(
                f"必須テーブル {table} の列 {column} が見つかりません"
                f"（テーブルが無いか、列だけが欠けています。DB初期化が完了していません）。"
                f"この状態で起動を続けるとログインが機能しないため停止します: {e}")


def _normalize_datetime_data():
    """起動時データ正規化。

    1. shifts / wish_history の start_datetime, end_datetime を
       "YYYY-MM-DDTHH:MM:SS" 形式にゼロ埋め（"T7:00:00" → "T07:00:00"）。
    2. wish_history で end_datetime <= start_datetime（同日）のレコードは
       翌日またぎ希望が誤って同日保存されていたインシデントの修復。
       end_datetime を start と同じ日付の翌日の同時刻に補正。
    3. shift_patterns / fixed_shifts の start_time, end_time を "HH:MM" にゼロ埋め。
    """
    # --- 1. shifts / wish_history の datetime ゼロ埋め ---
    for table in ("shifts", "wish_history"):
        try:
            rows = query_all(f"SELECT id, start_datetime, end_datetime FROM {table}")
        except Exception:
            continue
        for r in rows:
            sid = r["id"]
            s_old = r.get("start_datetime") or ""
            e_old = r.get("end_datetime") or ""
            s_new = norm_dt_iso(s_old)
            e_new = norm_dt_iso(e_old)
            # --- 2. wish_history の翌日またぎ誤保存修復 ---
            # end <= start かつ same day なら end を翌日に
            if table == "wish_history" and s_new and e_new:
                if e_new <= s_new:
                    # end を翌日に繰り越す（HH:MM:SS 部分は保持）
                    try:
                        e_new = f"{add_days(s_new[:10], 1)}T{e_new[11:]}"
                    except Exception:
                        pass
            if s_new != s_old or e_new != e_old:
                try:
                    execute(
                        f"UPDATE {table} SET start_datetime=?, end_datetime=? WHERE id=?",
                        (s_new, e_new, sid))
                except Exception as e:
                    print(f"[ensure_db] WARN: update {table} id={sid} failed: {e}", flush=True)

    # --- 3. shift_patterns / fixed_shifts の time ゼロ埋め ---
    for table in ("shift_patterns", "fixed_shifts"):
        try:
            rows = query_all(f"SELECT id, start_time, end_time FROM {table}")
        except Exception:
            continue
        for r in rows:
            rid = r["id"]
            s_old = r.get("start_time") or ""
            e_old = r.get("end_time") or ""
            s_new = norm_hhmm(s_old)
            e_new = norm_hhmm(e_old)
            if s_new != s_old or e_new != e_old:
                try:
                    execute(
                        f"UPDATE {table} SET start_time=?, end_time=? WHERE id=?",
                        (s_new, e_new, rid))
                except Exception as e:
                    print(f"[ensure_db] WARN: update {table} id={rid} failed: {e}", flush=True)


# gunicorn 等でインポートされた場合もスキーマを整備（起動時に1回）
try:
    ensure_db()
    _verify_critical_tables()
except Exception as _e:
    # ログを出したうえで必ず伝播させる（gunicorn 起動時は ImportError になる）。
    # ここで握り潰すと「/api/health は 200 なのに誰もログインできない」状態のまま
    # 起動が完了してしまい、Railway のヘルスチェックが異常を検知できない。
    print(f"[startup] FATAL: DB initialization failed: {_e}", flush=True)
    raise


if __name__ == "__main__":
    ensure_db()
    _verify_critical_tables()
    # ポート5000はmacOSのAirPlay Receiverが使用するため、デフォルトは8000
    port = int(os.getenv("PORT", "8000"))
    # debug=True は開発時のみ。本番環境変数 FLASK_DEBUG=0 で明示的に無効化可能。
    # （debug=True のまま本番運用すると Werkzeug debugger で RCE 可能になるため）
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
