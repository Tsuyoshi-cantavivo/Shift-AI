"""app.py - ShiftAI Flask アプリ（メイン）。

ルーティング・認証・全APIエンドポイントを提供。
起動: python src/app.py  (または flask --app src.app run)
"""
import os
import json
from datetime import datetime, timedelta
from flask import (Flask, request, jsonify, abort, Response, send_file, g)
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # src/ をモジュールパスに追加

from db import query_all, query_one, execute, insert_row, init_schema
from auth import hash_password, verify_password, gen_token, strip_password
from utils import (
    calc_next_period, jst_now, jst_today, minutes_between, compute_break_minutes,
    night_minutes, validate_password, parse_settings, build_ics, parse_iso, normalize_iso,
)
import shift_engine
import ai

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # プロジェクトルート
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False  # 日本語をそのまま返す


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
    if role == "admin":
        user = query_one("SELECT id, admin_id, name FROM system_admins WHERE id=?", (session["user_id"],))
    elif role == "shop":
        # user_id は従来 shops.id（旧店主）または staffs.id（manager ロール）。
        # shop_id を使って店舗情報を取得する方がロバスト。
        shop = query_one("SELECT * FROM shops WHERE id=?", (session.get("shop_id"),))
        if shop is None:
            # フォールバック: user_id を shops.id とみなす（後方互換）
            shop = query_one("SELECT * FROM shops WHERE id=?", (session["user_id"],))
        user = shop
    else:
        user = query_one("SELECT * FROM staffs WHERE id=?", (session["user_id"],))
    g.role = role
    g.user = strip_password(user)
    g.shop_id = session.get("shop_id")
    return role, g.user, session.get("shop_id")


def notify(shop_id, staff_id, ntype, title, body):
    """アプリ内通知を1件作成。"""
    insert_row("notifications", {"shop_id": shop_id, "staff_id": staff_id, "type": ntype,
                                "title": title, "body": body})


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
    """初回セットアップ: 管理者が未登録の場合のみ、初期管理者(admin/admin123)を作成。
    ※ 認証不要だが、管理者が既に存在する場合は何もしない（安全性）。
    ※ 本番運用開始後は必ず admin のパスワードを変更すること。
    """
    msg = {"admin": "", "shop": "", "logins": {}}
    if not query_one("SELECT id FROM system_admins LIMIT 1"):
        execute("INSERT INTO system_admins (admin_id, password_hash, name) VALUES (?,?,?)",
                ("admin", hash_password("admin123"), "システム管理者"))
        msg["admin"] = "管理者作成: admin / admin123（※必ずパスワードを変更してください）"
        msg["logins"] = {"admin": {"id": "admin", "password": "admin123"}}
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
      - 店舗管理者: staffs.role='manager' のスタッフ → role='shop' セッション。
      - 一般スタッフ: staffs.role='employee'/'part_time' → role='staff' セッション。
      - 後方互換: user_code == shop_code の場合、shops テーブルでの旧店主ログイン可。

    【背景】
      かつて staff_code 単独で検索したため別店舗同コードで誤ログインする致命的
      バグがあった。本仕様では (shop_code, staff_code) の複合キーで一意特定し、
      さらに 'manager' ロールで店舗権限も一本化する。
    """
    body = request.get_json(silent=True) or {}
    shop_code = (body.get("shop_code") or body.get("id") or "").strip()
    user_code = (body.get("user_code") or body.get("staff_code") or "").strip()
    pw = body.get("password") or ""
    if not pw:
        raise ValueError("パスワードを入力してください")

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
            return jsonify(_create_session("admin", admin["id"], None, admin))
        raise ValueError("管理者IDまたはパスワードが正しくありません")

    if not shop_code or not user_code:
        raise ValueError("店舗コードとユーザーコードを入力してください")

    # ---- 店舗管理者 / スタッフ: (shop_code, user_code) で一意検索 ----
    staff = query_one(
        "SELECT s.* FROM staffs s JOIN shops sh ON s.shop_id=sh.id "
        "WHERE sh.shop_code=? AND s.staff_code=? AND s.is_resigned=0 AND sh.is_active=1",
        (shop_code, user_code))
    if staff and verify_password(pw, staff["password_hash"]):
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
            return jsonify(_create_session("shop", shop["id"], shop["id"], shop))

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


@app.post("/api/logout")
def logout():
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if token:
        execute("DELETE FROM sessions WHERE token=?", (token,))
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    role, user, _ = require_auth(["admin", "shop", "staff"])
    return jsonify({"role": role, "user": user})


# ===========================================================
# 管理者
# ===========================================================
@app.get("/api/admin/shops")
def admin_shops():
    require_auth(["admin"])
    rows = query_all("SELECT id, shop_code, shop_name, is_active, settings, created_at FROM shops ORDER BY id")
    return jsonify({"shops": rows})


@app.post("/api/admin/shops")
def admin_create_shop():
    require_auth(["admin"])
    body = request.get_json(silent=True) or {}
    meta = execute("INSERT INTO shops (shop_code, shop_name, password_hash, settings) VALUES (?,?,?,?)",
                   (body["shop_code"], body["shop_name"], hash_password(body["password"]), json.dumps(body.get("settings") or {})))
    return jsonify({"ok": True, "id": meta["last_row_id"]})


@app.put("/api/admin/shops/<int:sid>")
def admin_update_shop(sid):
    require_auth(["admin"])
    body = request.get_json(silent=True) or {}
    execute("UPDATE shops SET shop_name=?, is_active=? WHERE id=?",
            (body.get("shop_name"), 1 if body.get("is_active") else 0, sid))
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


# ===========================================================
# 店舗
# ===========================================================
def _shop_ctx():
    require_auth(["shop"])
    return g.user, g.user["id"], parse_settings(g.user.get("settings"))


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
    today_shortage = [s for s in shortage if s["date"] == today]

    return jsonify({
        "today_attendance": len(today_shifts),
        "today_shifts": [{"name": s["staff_name"], "start": s["start_datetime"][11:16], "end": s["end_datetime"][11:16], "role": s["staff_role"]} for s in today_shifts],
        "today_hourly": [{"hour": h, "count": c} for h, c in sorted(hourly.items())],
        "today_shortage": len(today_shortage),
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
        "shortage_total": len(shortage),
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


@app.get("/api/admin/notifications")
def admin_notifs():
    # システム管理者向け通知は現状なし（空リストを返す）
    return jsonify({"notifications": [], "unread": 0})


@app.put("/api/admin/notifications/read-all")
def admin_notifs_readall():
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
    # role のバリデーション（'employee' / 'part_time' / 'manager' 以外は拒否）
    role = body.get("role") or "part_time"
    if role not in ("employee", "part_time", "manager"):
        abort(400, description="ロールは employee / part_time / manager のいずれかを指定してください")
    meta = execute("INSERT INTO staffs (shop_id, staff_code, password_hash, name, role, hourly_wage, min_hours_per_month, max_hours_per_month) VALUES (?,?,?,?,?,?,?,?)",
                   (shop_id, body["staff_code"], hash_password(pw), body["name"], role,
                    body.get("hourly_wage") or settings.get("default_hourly_wage") or 1000,
                    body.get("min_hours_per_month") or 0, body.get("max_hours_per_month") or 160))
    return jsonify({"ok": True, "id": meta["last_row_id"]})


@app.put("/api/shop/staffs/<int:sid>")
def shop_staffs_put(sid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    execute("UPDATE staffs SET name=?, hourly_wage=?, min_hours_per_month=?, max_hours_per_month=?, is_resigned=? WHERE id=? AND shop_id=?",
            (body["name"], body["hourly_wage"], body["min_hours_per_month"], body["max_hours_per_month"],
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


@app.post("/api/shop/patterns")
def shop_patterns_post():
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    meta = execute("INSERT INTO shift_patterns (shop_id, pattern_name, start_time, end_time, required_staff) VALUES (?,?,?,?,?)",
                   (shop_id, body["pattern_name"], body["start_time"], body["end_time"], body.get("required_staff") or 1))
    return jsonify({"ok": True, "id": meta["last_row_id"]})


@app.put("/api/shop/patterns/<int:pid>")
def shop_patterns_put(pid):
    shop, shop_id, _ = _shop_ctx()
    body = request.get_json(silent=True) or {}
    execute("UPDATE shift_patterns SET pattern_name=?, start_time=?, end_time=?, required_staff=? WHERE id=? AND shop_id=?",
            (body["pattern_name"], body["start_time"], body["end_time"], body.get("required_staff") or 1, pid, shop_id))
    return jsonify({"ok": True})


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


@app.post("/api/shop/fixed-shifts")
def shop_fixed_post():
    body = request.get_json(silent=True) or {}
    meta = execute("INSERT INTO fixed_shifts (staff_id, weekday, start_time, end_time) VALUES (?,?,?,?)",
                   (body["staff_id"], body["weekday"], body["start_time"], body["end_time"]))
    return jsonify({"ok": True, "id": meta["last_row_id"]})


@app.put("/api/shop/fixed-shifts/<int:fid>")
def shop_fixed_put(fid):
    body = request.get_json(silent=True) or {}
    execute("UPDATE fixed_shifts SET weekday=?, start_time=?, end_time=? WHERE id=?", (body["weekday"], body["start_time"], body["end_time"], fid))
    return jsonify({"ok": True})


@app.delete("/api/shop/fixed-shifts/<int:fid>")
def shop_fixed_del(fid):
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
    shop, shop_id, settings = _shop_ctx()
    body = request.get_json(silent=True) or {}
    start_d, end_d = body.get("start_date"), body.get("end_date")
    dry = bool(body.get("dry_run"))
    if not start_d or not end_d:
        abort(400, description="start_date, end_date が必要です")
    result = shift_engine.auto_generate(shop_id, settings, start_d, end_d)
    if dry:
        return jsonify({"ok": True, "dry_run": True, "confirmed_count": len(result["confirmed"]),
                        "pending_count": len(result["pending"]), "minutes_by_staff": result["minutes_by_staff"],
                        "shortage": result.get("shortage", []), "warnings": result.get("warnings", []),
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
    # confirmed/modifying/requested を全て削除して再配置
    execute("DELETE FROM shifts WHERE shop_id=? AND status IN ('confirmed','modifying','requested') AND start_datetime>=? AND start_datetime<=?",
            (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    placed = set()
    for s in result["confirmed"]:
        execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                (s["shop_id"], s["staff_id"], s["start"], s["end"], s["break"], s["status"], s["reason"]))
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
    for sid in placed:
        notify(shop_id, sid, "confirmed", "シフトが確定しました", f"{start_d}〜{end_d}のシフトが確定しました。")
    return jsonify({"ok": True, "confirmed_count": len(result["confirmed"]), "pending_count": pending_count,
                    "minutes_by_staff": result["minutes_by_staff"], "shortage": result.get("shortage", []),
                    "warnings": result.get("warnings", []),
                    "explanations": result.get("explanations", [])})


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
    meta = execute("INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, break_time_minutes, status, reason) VALUES (?,?,?,?,?,?,?)",
                   (shop_id, staff_id, start_dt, end_dt, brk, body.get("status") or "confirmed", body.get("reason") or "手動追加"))
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
    existing = query_one("SELECT staff_id, start_datetime, end_datetime FROM shifts WHERE id=? AND shop_id=?", (sid, shop_id))
    if not existing:
        abort(404, description="シフトが見つかりません")
    staff_id = body.get("staff_id") or existing["staff_id"]
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
    execute("UPDATE shifts SET start_datetime=?, end_datetime=?, break_time_minutes=?, status=?, reason=? WHERE id=? AND shop_id=?",
            (body["start_datetime"], body["end_datetime"], brk, body.get("status") or "confirmed", body.get("reason") or "手動調整", sid, shop_id))
    print(f"[SHIFT PUT sid={sid}] OK: staff_id={staff_id} {body['start_datetime']}〜{body['end_datetime']} status={body.get('status')} auto_adjust={auto_adjust}", flush=True)
    result = {"ok": True}
    if adjustments:
        result["adjustments"] = adjustments
    return jsonify(result)


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
    return jsonify({"shortage": shift_engine.compute_shortage(shifts, pats, start_d, end_d, overrides)})


@app.get("/api/shop/shifts/export")
def shop_shifts_export():
    shop, shop_id, _ = _shop_ctx()
    start_d, end_d = request.args.get("start"), request.args.get("end")
    if not start_d or not end_d:
        abort(400, description="start, end が必要")
    rows = query_all("SELECT sh.*, s.name as staff_name, s.role as staff_role, s.staff_code FROM shifts sh JOIN staffs s ON sh.staff_id=s.id WHERE sh.shop_id=? AND sh.start_datetime>=? AND sh.start_datetime<=? ORDER BY sh.start_datetime",
                     (shop_id, start_d + "T00:00:00", end_d + "T23:59:59"))
    wd = ["日", "月", "火", "水", "木", "金", "土"]
    lines = ["日付,曜日,開始,終了,休憩(分),実働(分),深夜(分),スタッフコード,氏名,ロール,ステータス"]
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
        notify(shop_id, cr["staff_id"], "info", "変更申請が承認されました", "ご申請の変更を反映しました。")
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
    # 不足状況
    shortage = shift_engine.compute_shortage(month_shifts, patterns, month_start, month_end, overrides)
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
        "shortage_count": len(shortage),
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
    require_auth(["staff"]); staff = g.user
    start_d, end_d = request.args.get("start"), request.args.get("end")
    sql = "SELECT id, shop_id, start_datetime, end_datetime, break_time_minutes, status, reason FROM shifts WHERE staff_id=?"
    params = [staff["id"]]
    if start_d and end_d:
        sql += " AND start_datetime>=? AND start_datetime<=?"; params += [start_d + "T00:00:00", end_d + "T23:59:59"]
    sql += " ORDER BY start_datetime"
    return jsonify({"shifts": query_all(sql, tuple(params))})


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
            end_dt = normalize_iso(sh.get("end_datetime")) or (start_dt[:10] + "T22:00:00")
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
    """店舗の全スタッフ希望履歴を取得（店長が確認用）。"""
    shop, shop_id, _ = _shop_ctx()
    start_d = request.args.get("start")
    end_d = request.args.get("end")
    sql = ("SELECT wh.id, wh.staff_id, s.name as staff_name, s.staff_code, "
           "wh.start_datetime, wh.end_datetime, wh.availability, wh.submitted_at, wh.note "
           "FROM wish_history wh "
           "JOIN staffs s ON wh.staff_id=s.id WHERE wh.shop_id=?")
    params = [shop_id]
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
        css_mtime = int(os.path.getmtime(os.path.join(PUBLIC_DIR, "style.css")))
        html = html.replace('src="app.js"', f'src="app.js?v={js_mtime}"')
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
    full = os.path.join(PUBLIC_DIR, path)
    if os.path.isfile(full):
        # app.js / style.css は短時間キャッシュ（常に最新を取得させる）
        if path in ("app.js", "style.css"):
            resp = send_file(full)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp
        return send_file(full)
    return _index_html_with_asset_version()


# ===========================================================
# 起動
# ===========================================================
def ensure_db():
    if not os.path.exists(SCHEMA_PATH):
        return
    init_schema(SCHEMA_PATH)


if __name__ == "__main__":
    ensure_db()
    # ポート5000はmacOSのAirPlay Receiverが使用するため、デフォルトは8000
    port = int(os.getenv("PORT", "8000"))
    # debug=True は開発時のみ。本番環境変数 FLASK_DEBUG=0 で明示的に無効化可能。
    # （debug=True のまま本番運用すると Werkzeug debugger で RCE 可能になるため）
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
else:
    # `flask run` 等でインポートされた場合もスキーマを整備
    try:
        ensure_db()
    except Exception as _e:
        pass
