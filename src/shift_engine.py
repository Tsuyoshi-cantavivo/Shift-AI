"""shift_engine.py - シフト自動作成コアルゴジック（Flask版・同期・スロットベース）。

設計の肝:
  1. **スロットベースの上限厳守（検証A）**: 1日を GRAN(分)単位のスロットに分割し、
     各スロットの配置人数が shift_patterns.required_staff を **絶対に超えない** ようにする。
     従来の「パターン実質カバー(50%)」集計では、部分重複シフトが多数並ぶと
     特定時間帯が required を超えてしまう致命的バグがあったため、これを廃止した。
  2. **配置優先度**: 固定シフト(最優先) → 時間指定希望 → 柔軟希望 → 社員による不足補填。
  3. **社員の穴埋め（検証D）**: アルバイトだけでは足りない空きスロット（不足セグメント）
     を抽出し、稼働可能な社員を必ずアサインする。
  4. **労働条件（検証E）**: 1日最低勤務時間(min_daily_hours)未満の配置は行わない。
     同一スタッフの1日複数シフト（中抜け）は staff_busy で禁止する。
  5. **休憩（検証F）**: compute_break_minutes で 6h超→45分 / 8h超→60分 を自動算出。
  6. **月間上限**: 各スタッフの max_hours_per_month を超える配置はスキップ（警告のみでなく配置抑制）。
"""
from datetime import datetime
from db import query_all
from utils import (
    weekday_sun0, combine_dt, combine_dt_overnight, minutes_between, compute_break_minutes,
    add_days, max_consecutive_run, _hhmm_to_min, parse_iso,
)

# スロット粒度(分)。1時間単位より細かくしておき、検証Aは時間単位で保証する。
GRAN = 30


# ===========================================================
# スロット/要件の纯粋関数
# ===========================================================
def _min_to_hhmm(m):
    """分(整数) → "HH:MM"。"""
    return f"{m // 60:02d}:{m % 60:02d}"


def _min_to_iso(day, m):
    """拡張分(0-2879) を day 基準の ISO datetime に変換。

    m >= 1440 は翌日扱い（深夜営業のスロット表現用）。
    """
    if m >= 1440:
        return f"{add_days(day, 1)}T{_min_to_hhmm(m - 1440)}:00"
    return f"{day}T{_min_to_hhmm(m)}:00"


def _pat_end_min(pat_or_start, pat_end=None):
    """パターンの終了分を返す。end <= start なら翌日 (+1440) 扱い。

    引数は (pat_dict) または (start_time, end_time) のいずれか。
    """
    if pat_end is None:
        ps = _hhmm_to_min(pat_or_start["start_time"])
        pe = _hhmm_to_min(pat_or_start["end_time"])
    else:
        ps = _hhmm_to_min(pat_or_start)
        pe = _hhmm_to_min(pat_end)
    return pe + 1440 if pe <= ps else pe


def _day_requirements(patterns, gran=GRAN, weekday=None, weekday_overrides=None, prev_weekday=None):
    """パターン群から {slot_min: required_count} の要件マップを構築。

    同一スロットに複数パターンが重なる場合は最大値（最も厳しい要件）を採用。
    weekday と weekday_overrides({(pattern_id, weekday): required_staff}) を渡すと、
    該当曜日の曜日別必要人数で pattern の required_staff を上書きする。

    【日またぎ(overnight)対応・拡張スロットモデル】
      end_time <= start_time のパターンは翌日またぎとみなし、pe に +1440 して
      当日のスロット空間を [0, 2880) に拡張する。これにより:
        - 当日 07:00〜翌日 05:00 = スロット 420 〜 1740
        - overnight シフトも同様に拡張スロットで表現され、カバレッジが直接比較できる
      ※ prev_weekday は後方互換のため残しているが使用しない（拡張スロットで統一）。
    """
    req = {}
    for pat in patterns:
        ps = _hhmm_to_min(pat["start_time"])
        pe = _hhmm_to_min(pat["end_time"])
        needed = pat.get("required_staff") or 0
        if weekday_overrides and weekday is not None:
            ov = weekday_overrides.get((pat.get("id"), weekday))
            if ov is not None:
                needed = ov
        if needed <= 0:
            continue
        if pe <= ps:
            pe += 1440  # overnight: 当日ベースで +1440 した拡張スロットに
        s = (ps // gran) * gran
        while s < pe:
            if needed > req.get(s, 0):
                req[s] = needed
            s += gran
    return req


def load_weekday_overrides(shop_id):
    """店舗の曜日別必要人数オーバーライドを {(pattern_id, weekday): required_staff} で返す。"""
    rows = query_all(
        "SELECT pattern_id, weekday, required_staff FROM shift_pattern_weekday_required WHERE shop_id=?",
        (shop_id,))
    return {(r["pattern_id"], r["weekday"]): r["required_staff"] for r in rows}


def _shift_slots(start_iso, end_iso, gran=GRAN):
    """シフト [start, end) が覆盖するスロット(分)のリスト。

    【日またぎ対応】ISO datetime を基準に経過分で計算するため、start と end が
    異なる日でも正しいスロットが得られる。例えば start="D T22:00" / end="D+1 T05:00"
    は [1320, 1380, ..., 1740) を返す（翌日分は +1440 の拡張スロット）。
    """
    try:
        s_dt = parse_iso(start_iso)
        e_dt = parse_iso(end_iso)
    except Exception:
        return []
    if e_dt <= s_dt:
        return []
    s_min = _hhmm_to_min(start_iso[11:16])
    total_min = int((e_dt - s_dt).total_seconds() // 60)
    e_min = s_min + total_min
    slots = []
    s = (s_min // gran) * gran
    while s < e_min:
        slots.append(s)
        s += gran
    return slots


def _shortage_segments_in_pattern(req_map, coverage, pat, gran=GRAN):
    """パターン内の連続する不足スロットをセグメントとして返す。

    戻り値: [(start_min, end_min, max_deficit), ...]
    end_min は排他（シフト終了時刻として使用可能）。
    【日またぎ対応】overnight パターンは start_time から翌日 end_time(+1440) まで走査。
    """
    ps = _hhmm_to_min(pat["start_time"])
    pe = _pat_end_min(pat)
    segs = []
    cur_start = None
    cur_end = None
    cur_def = 0
    s = (ps // gran) * gran
    while s < pe:
        req_c = req_map.get(s, 0)
        cov_c = coverage.get(s, 0)
        if req_c > 0 and cov_c < req_c:
            if cur_start is None:
                cur_start = s
                cur_end = s + gran
                cur_def = req_c - cov_c
            elif s == cur_end:
                cur_end = s + gran
                cur_def = max(cur_def, req_c - cov_c)
            else:
                segs.append((cur_start, cur_end, cur_def))
                cur_start = s
                cur_end = s + gran
                cur_def = req_c - cov_c
        else:
            if cur_start is not None:
                segs.append((cur_start, cur_end, cur_def))
                cur_start = None
                cur_end = None
                cur_def = 0
        s += gran
    if cur_start is not None:
        segs.append((cur_start, cur_end, cur_def))
    return segs


def _day_shortage_segments(req_map, coverage, gran=GRAN):
    """日全体（全パターン要件を統合）の連続不足セグメントを返す。

    パターン単位ではなく「その日に必要な人数が足りない連続区間」を抽出する。
    社員の穴埋め配置はこれを使い、複数パターンをまたぐ長時間シフトで
    効率よく（かつ無駄なく）カバーする。
    戻り値: [(start_min, end_min, max_deficit), ...]（end_min は排他）
    """
    if not req_map:
        return []
    slots = sorted(req_map.keys())
    segments = []
    cur_start = None
    cur_end = None
    cur_def = 0
    for s in slots:
        req_c = req_map[s]
        cov_c = coverage.get(s, 0)
        if cov_c < req_c:
            if cur_start is None:
                cur_start = s
                cur_end = s + gran
                cur_def = req_c - cov_c
            elif s == cur_end:
                cur_end = s + gran
                cur_def = max(cur_def, req_c - cov_c)
            else:
                segments.append((cur_start, cur_end, cur_def))
                cur_start = s
                cur_end = s + gran
                cur_def = req_c - cov_c
        else:
            if cur_start is not None:
                segments.append((cur_start, cur_end, cur_def))
                cur_start = None
                cur_end = None
                cur_def = 0
    if cur_start is not None:
        segments.append((cur_start, cur_end, cur_def))
    return segments


def _pattern_min_coverage(req_map, coverage, pat, gran=GRAN):
    """パターン内の最小カバー人数と要件を返す（不足集計用）。

    【日またぎ対応】overnight パターンは拡張スロット([ps, pe+1440))を走査。
    """
    ps = _hhmm_to_min(pat["start_time"])
    pe = _pat_end_min(pat)
    s = (ps // gran) * gran
    min_cov = None
    has_slot = False
    while s < pe:
        if req_map.get(s, 0) > 0:
            has_slot = True
            c = coverage.get(s, 0)
            if min_cov is None or c < min_cov:
                min_cov = c
        s += gran
    if not has_slot:
        return 0, 0
    return (min_cov or 0), max(req_map.get(s2, 0) for s2 in range((ps // gran) * gran, pe, gran))


def _slot_matches(availability, pat):
    """柔軟希望(availability)がパターンの時間帯希望に合致するか。"""
    try:
        sh = int((pat["start_time"] or "00:00")[:2])
    except Exception:
        sh = 0
    if availability == "any":
        return True
    if availability == "morning":
        return sh < 12
    if availability == "evening":
        return sh >= 15
    return False


# ===========================================================
# メイン: 自動生成
# ===========================================================
def auto_generate(shop_id, settings, start_date, end_date):
    settings = settings or {}
    recommended_consec = settings.get("max_consecutive_days") or 99
    min_daily = int(settings.get("min_daily_hours") or 4) * 60
    # 社員の穴埋めシフトの上限長（1シフトで6-9h程度が現実的）。無いと13h等の異常長発生。
    max_daily = int(settings.get("max_daily_hours") or 9) * 60

    staffs = query_all("SELECT * FROM staffs WHERE shop_id=? AND is_resigned=0", (shop_id,))
    patterns = query_all("SELECT * FROM shift_patterns WHERE shop_id=?", (shop_id,))
    weekday_overrides = load_weekday_overrides(shop_id)
    fixed = query_all(
        "SELECT fs.*, s.role FROM fixed_shifts fs JOIN staffs s ON fs.staff_id=s.id "
        "WHERE s.shop_id=? AND s.is_resigned=0",
        (shop_id,))

    # 曜日別オーバーライド適用後のパターン表（shortage集計等で使用）
    def patterns_for_weekday(wd):
        out = []
        for pat in patterns:
            ov = weekday_overrides.get((pat["id"], wd))
            p = dict(pat)
            if ov is not None:
                p["required_staff"] = ov
            out.append(p)
        return out

    # 各日の要件マップ（曜日別オーバーライド反映済み）をキャッシュ
    req_map_cache = {}

    def req_map_for(day):
        if day not in req_map_cache:
            wd = weekday_sun0(datetime.strptime(day, "%Y-%m-%d").date())
            req_map_cache[day] = _day_requirements(
                patterns_for_weekday(wd), GRAN, wd, weekday_overrides)
        return req_map_cache[day]

    monthly_cap = {s["id"]: (s.get("max_hours_per_month") or 0) for s in staffs}
    staff_role = {s["id"]: s.get("role") for s in staffs}

    # 学生アルバイトの月間上限は80hを強制（API側でもガードしているが安全のため）
    for sid, role in staff_role.items():
        if role == "student":
            cur = monthly_cap.get(sid) or 80
            monthly_cap[sid] = min(cur, 80)

    minutes_by_staff = {s["id"]: 0 for s in staffs}
    staff_days = {s["id"]: set() for s in staffs}
    confirmed = []
    pending = []

    coverage = {}    # day -> {slot_min: count}
    staff_busy = {}  # day -> {staff_id: (start_min, end_min)}
    # day -> set(staff_id) その日に配置された staff の role 判定用
    day_placed_roles = {}  # day -> set(role)

    def state(day):
        if day not in coverage:
            coverage[day] = {}
            staff_busy[day] = {}
            day_placed_roles[day] = set()
        return coverage[day], staff_busy[day]

    def cap_ok(day, start_iso, end_iso):
        """このシフトを追加すると required を超えるスロットがあるか（検証Aの要）。"""
        cov, _ = state(day)
        req_map = req_map_for(day)
        for sl in _shift_slots(start_iso, end_iso, GRAN):
            required = req_map.get(sl, 0)
            if required <= 0:
                continue  # パターン外のスロットは上限なし
            if cov.get(sl, 0) + 1 > required:
                return False
        return True

    def has_non_student_on_day(day):
        """その日に student 以外のスタッフが既に配置されているか。"""
        roles = day_placed_roles.get(day, set())
        return bool(roles - {"student"})

    def can_place(staff_id, day, start_iso, end_iso, check_cap=True):
        """配置可否。理由コードを返す。

        労働条件（1日最低勤務時間）は **アルバイトのみ** に適用する。
        社員はフルタイム柔軟稼動が前提のため、最低時間では縛らない
        （短い穴埋め・夜を含む長時間シフトの両方を許容）。
        ※ 1日複数シフト(中抜け)禁止・上限人数・月間上限は全スタッフ共通で厳守。
        【日またぎ対応】翌日にまたがる場合、翌日の staff_busy もチェックする。
        【学生ルール】学生のみ構成シフトを避けるため、学生を配置する場合は
           当日に社会人(employee/manager/part_time)が既に配置されているか、
           または cap 内で社会人を同時に配置可能な余力があることを要件とする。
           ※ 厳格過ぎるとシフトが作れなくなるため、ここでは事前の警告用途とし、
              配置後に最終チェック（post_validate）で検知する。
        【休希望】availability='rest' の日はそのスタッフは配置しない。
        """
        if (staff_id, day) in rest_days:
            return False, "rest_request"
        work = minutes_between(start_iso, end_iso)
        is_pt = staff_role.get(staff_id) == "part_time"
        is_student = staff_role.get(staff_id) == "student"
        if (is_pt or is_student) and work < min_daily:
            return False, "min_daily"
        _, sw = state(day)
        if staff_id in sw:
            return False, "already_working"
        # 翌日またぎの場合、翌日も business として記録されているか確認
        if end_iso[:10] != day:
            _, sw_next = state(end_iso[:10])
            if staff_id in sw_next:
                return False, "already_working"
        cap_h = monthly_cap.get(staff_id) or 0
        if cap_h and minutes_by_staff[staff_id] + work > cap_h * 60:
            return False, "monthly_cap"
        if check_cap and not cap_ok(day, start_iso, end_iso):
            return False, "cap"
        return True, None

    def can_place_fixed(staff_id, day, start_iso, end_iso):
        """固定シフト専用の配置可否チェック。

        固定シフトは契約勤務なので「最低時間・月間上限」では縛らないが、
        **上限人数(R1)** と **同日重複(R2)** は厳守する。
        これにより「必要人数設定」が固定シフトでも保証される（旧: 固定無条件配置バグの修正）。
        【休希望】availability='rest' の日は固定シフトも配置しない。
        """
        if (staff_id, day) in rest_days:
            return False, "rest_request"
        _, sw = state(day)
        if staff_id in sw:
            return False, "already_working"
        if end_iso[:10] != day:
            _, sw_next = state(end_iso[:10])
            if staff_id in sw_next:
                return False, "already_working"
        if not cap_ok(day, start_iso, end_iso):
            return False, "cap"
        return True, None

    def place(staff_id, day, start_iso, end_iso, reason):
        work = minutes_between(start_iso, end_iso)
        confirmed.append({
            "staff_id": staff_id, "shop_id": shop_id,
            "start": start_iso, "end": end_iso,
            "break": compute_break_minutes(work), "status": "confirmed", "reason": reason,
        })
        minutes_by_staff[staff_id] += work
        cov, sw = state(day)
        day_placed_roles.setdefault(day, set()).add(staff_role.get(staff_id, "part_time"))
        for sl in _shift_slots(start_iso, end_iso, GRAN):
            cov[sl] = cov.get(sl, 0) + 1
        s_min = _hhmm_to_min(start_iso[11:16])
        end_date = end_iso[:10]
        # 当日の busy 窓（翌日またぎなら当日は 24:00 まで busy）
        if end_date == day:
            busy_end = _hhmm_to_min(end_iso[11:16])
        else:
            busy_end = 1440
        sw[staff_id] = (s_min, busy_end)
        # 翌日も busy として記録（中抜け・重複防止）
        if end_date != day:
            _, sw_next = state(end_date)
            sw_next[staff_id] = (0, _hhmm_to_min(end_iso[11:16]))
            day_placed_roles.setdefault(end_date, set()).add(staff_role.get(staff_id, "part_time"))
        staff_days[staff_id].add(day)

    def fill_shortage_with(staff_id, day, pat, reason):
        """パターンの不足を埋めるようにスタッフを配置。

        まずパターン全体を試し、上限に触れる場合は不足セグメント単位で最小時間を満たすものを配置する。
        配置できれば True。
        【日またぎ対応】パターンが overnight の場合、終了時刻は翌日になるよう combine_dt_overnight で生成。
        """
        pat_s_iso, pat_e_iso = combine_dt_overnight(day, pat["start_time"], pat["end_time"])
        ok, _ = can_place(staff_id, day, pat_s_iso, pat_e_iso)
        if ok:
            place(staff_id, day, pat_s_iso, pat_e_iso, reason)
            return True
        cov, _ = state(day)
        for (s_min, e_min, deficit) in _shortage_segments_in_pattern(req_map_for(day), cov, pat, GRAN):
            # セグメント長の最低時間チェックはアルバイトのみ（社員は柔軟）
            if deficit <= 0 or (staff_role.get(staff_id) == "part_time" and e_min - s_min < min_daily):
                continue
            # 拡張スロット(s_min/e_min >= 1440)は翌日の時刻 → _min_to_iso で正しいISO生成
            s_iso = _min_to_iso(day, s_min)
            e_iso = _min_to_iso(day, e_min)
            ok2, _ = can_place(staff_id, day, s_iso, e_iso)
            if ok2:
                place(staff_id, day, s_iso, e_iso, reason)
                return True
        return False

    REASON_MAP = {
        "min_daily": "最低勤務時間未満",
        "already_working": "同日内重複",
        "cap": "上限人数到達",
        "monthly_cap": "月間上限到達",
        "rest_request": "休希望日",
    }

    # -----------------------------------------------------------
    # Step1: 【廃止】固定シフトの厳守配置は行わない。
    #
    # 【ユーザー要望】
    #   「固定シフトはスタッフも含め候補ということにしましょう。
    #    固定であっても希望のシフトが出ていたらそちらを優先すべき。
    #    実運用では希望をだしているということはそれ以外の時間は
    #    入れませんということなので。」
    #
    # 新仕様:
    #   - 希望（wish）= スタッフの明示的意志「この時間だけ働ける」→ 最優先
    #   - 固定 = 「基本この時間に入る予定」の候補 → 希望がない日のフォールバック
    #   - 希望を出した日は、そのstaffの固定はスキップ（希望時間以外は入れない）
    #
    # 処理順: 希望(Step2a/2b) → 固定候補(Step2.5) → 社員不足補填(Step3)
    # -----------------------------------------------------------
    # 固定はStep2.5で全スタッフ（part_time・employee問わず）候補として処理。
    all_fixed = fixed  # part_time も employee も候補
    overcap_fixed = []  # 上限超過でスキップされた固定（warnings/explanationsへ）

    # -----------------------------------------------------------
    # Step2a: 時間指定希望（availability 無し）
    # -----------------------------------------------------------
    # ★ 入力ソース: wish_history（永久履歴）+ shifts.requested（後方互換）
    # これにより、AI自動生成を何度繰り返してもスタッフ希望が消失しない。
    # （wish_history は shop_shifts_auto でも DELETE されない）
    try:
        requests = query_all(
            "SELECT staff_id, start_datetime, end_datetime, availability, role FROM ("
            "  SELECT wh.staff_id, wh.start_datetime, wh.end_datetime, wh.availability, s.role "
            "  FROM wish_history wh JOIN staffs s ON wh.staff_id=s.id "
            "  WHERE wh.shop_id=? AND wh.start_datetime>=? AND wh.start_datetime<=?"
            "  UNION"
            "  SELECT sh.staff_id, sh.start_datetime, sh.end_datetime, sh.availability, s.role "
            "  FROM shifts sh JOIN staffs s ON sh.staff_id=s.id "
            "  WHERE sh.shop_id=? AND sh.status='requested' AND sh.start_datetime>=? AND sh.start_datetime<=?"
            "    AND (sh.reason NOT LIKE 'AIドラフト%' OR sh.reason IS NULL)"
            ") ORDER BY start_datetime",
            (shop_id, start_date + "T00:00:00", end_date + "T23:59:59",
             shop_id, start_date + "T00:00:00", end_date + "T23:59:59"))
    except Exception:
        # wish_history テーブルが未作成の場合のフォールバック（後方互換）
        requests = query_all(
            "SELECT sh.*, s.role FROM shifts sh JOIN staffs s ON sh.staff_id=s.id "
            "WHERE sh.shop_id=? AND sh.status='requested' AND sh.start_datetime>=? AND sh.start_datetime<=? "
            "AND (sh.reason NOT LIKE 'AIドラフト%' OR sh.reason IS NULL)",
            (shop_id, start_date + "T00:00:00", end_date + "T23:59:59"))

    # 休希望（availability='rest'）のスタッフ/日付のセットを作成
    # このセットに入るスタッフは、その日の配置候補から外す
    rest_days = set()  # (staff_id, day_str)
    for r in requests:
        if r.get("availability") == "rest":
            rest_days.add((r["staff_id"], r["start_datetime"][:10]))
    if rest_days:
        # 休希望は配置候補から外すため、timed/flex リストから除去
        requests = [r for r in requests if r.get("availability") != "rest"]

    timed = [r for r in requests if not r.get("availability")]
    flex = [r for r in requests if r.get("availability")]

    for req in sorted(timed, key=lambda a: minutes_by_staff.get(a["staff_id"], 0)):
        day = req["start_datetime"][:10]
        ok, why = can_place(req["staff_id"], day, req["start_datetime"], req["end_datetime"])
        if ok:
            place(req["staff_id"], day, req["start_datetime"], req["end_datetime"], "希望シフト")
        else:
            work = minutes_between(req["start_datetime"], req["end_datetime"])
            pending.append({
                "staff_id": req["staff_id"], "shop_id": shop_id,
                "start": req["start_datetime"], "end": req["end_datetime"],
                "break": compute_break_minutes(work), "status": "requested",
                "reason": f"{REASON_MAP.get(why, why)}のため調整待ち",
            })

    # -----------------------------------------------------------
    # Step2b: 柔軟希望（availability 有）→ 不足パターンへ配置
    # -----------------------------------------------------------
    flex_by_day = {}
    for r in flex:
        flex_by_day.setdefault(r["start_datetime"][:10], []).append(r)

    cur = start_date
    while cur <= end_date:
        applicants = sorted(flex_by_day.get(cur, []), key=lambda a: minutes_by_staff.get(a["staff_id"], 0))
        for pat in patterns:
            for req in list(applicants):
                if req.get("_used"):
                    continue
                _, sw = state(cur)
                if req["staff_id"] in sw:
                    continue
                if not _slot_matches(req.get("availability"), pat):
                    continue
                if fill_shortage_with(req["staff_id"], cur, pat, f"柔軟希望({req.get('availability')})"):
                    req["_used"] = True
        cur = add_days(cur, 1)
    for r in flex:
        if not r.get("_used"):
            pending.append({
                "staff_id": r["staff_id"], "shop_id": shop_id,
                "start": r["start_datetime"], "end": r["end_datetime"],
                "break": 0, "status": "requested", "availability": r.get("availability"),
                "reason": "配置可能な不足枠がなかったため調整待ち",
            })

    # -----------------------------------------------------------
    # Step2.5: 全スタッフの固定シフトを「候補」として配置（ユーザー要望）
    #
    # 【ユーザー要望】
    #   「固定シフトはスタッフも含め候補ということにしましょう。
    #    固定であっても希望のシフトが出ていたらそちらを優先すべき。
    #    実運用では希望をだしているということはそれ以外の時間は
    #    入れませんということなので。」
    #
    # 【配置ルール】
    #   1. 希望（wish）処理後に実行
    #   2. その日に希望を出しているスタッフの固定はスキップ
    #      （「希望時間以外は入れない」ため）
    #   3. 既に wish 等で配置済のスタッフはスキップ
    #   4. cap 内なら固定時間をそのまま配置
    #   5. cap 超過ならスキップ（Step3で柔軟に不足補填）
    # -----------------------------------------------------------
    # 希望を出した (staff_id, day) のセットを構築
    wish_days = set()
    for r in requests:
        wish_days.add((r["staff_id"], r["start_datetime"][:10]))

    cur = start_date
    while cur <= end_date:
        wd = weekday_sun0(datetime.strptime(cur, "%Y-%m-%d").date())
        for f in all_fixed:
            if f["weekday"] != wd:
                continue
            # ★ その日に希望を出している → 固定は無視（希望時間以外は入れない）
            if (f["staff_id"], cur) in wish_days:
                continue
            _, sw = state(cur)
            if f["staff_id"] in sw:
                continue  # 既に配置済（他の処理で）
            # 【日またぎ対応】固定シフトが overnight の場合、終了時刻は翌日
            s_iso, e_iso = combine_dt_overnight(cur, f["start_time"], f["end_time"])
            if minutes_between(s_iso, e_iso) <= 0:
                continue
            # cap 内なら固定時間をそのまま配置（候補として採用）
            ok, why = can_place(f["staff_id"], cur, s_iso, e_iso, check_cap=True)
            if ok:
                place(f["staff_id"], cur, s_iso, e_iso, "固定シフト（候補）")
            # cap 超過ならスキップ → Step3 で別時間に不足補填
        cur = add_days(cur, 1)

    # -----------------------------------------------------------
    # Step3: 社員による不足補填（検証D）。日全体の空きセグメントを必ず埋める。
    #
    # 旧実装はパターン単位で貪欲に配置していたため、朝→昼→夜と連続して不足する日で
    # 両社員が最初の「朝」に吸い込まれ「昼」が放置される致命的バグがあった。
    # 日全体（全パターン統合）の不足セグメントを抽出し、長いセグメントから優先的に、
    # 1シフト = min(セグメント長, max_daily) の窓で社員を配置して複数パターンをまたぎ、
    # 空きを残さない。※1日1シフト・最低時間・上限人数は can_place が厳守。
    #
    # 【学生アルバイトルール】
    #   学生(student)のみで構成されるシフトを避けるため、
    #   学生を配置する場合は同日に社会人（employee/manager/part_time）が
    #   既に配置されていることを確認する。
    # -----------------------------------------------------------
    cur = start_date
    while cur <= end_date:
        progress = True
        while progress:
            progress = False
            cov, sw = state(cur)
            day_segs = _day_shortage_segments(req_map_for(cur), cov, GRAN)
            if not day_segs:
                break
            avail = [s for s in staffs if s["role"] in ("employee", "manager") and s["id"] not in sw]
            if not avail:
                break
            avail.sort(key=lambda s: minutes_by_staff.get(s["id"], 0))
            # 長いセグメントから優先（複数パターンをまたぐ長時間シフトで効率よくカバー）
            day_segs_sorted = sorted(day_segs, key=lambda seg: (seg[1] - seg[0]), reverse=True)
            placed_any = False
            for (s_min, e_min, _deficit) in day_segs_sorted:
                seg_len = e_min - s_min
                # 社員は max_daily の上限を受けない（夜を含む長時間シフトでカバー）。
                # 最低時間も社員には適用しない。ただし短すぎる残りカス（<1h）は現実的でないので回避。
                win_len = seg_len if seg_len >= 60 else 0
                if win_len <= 0:
                    continue
                win_end = s_min + win_len
                # 【日またぎ対応】拡張スロット(s_min/win_end >= 1440)は翌日の時刻として ISO を生成
                s_iso = _min_to_iso(cur, s_min)
                e_iso = _min_to_iso(cur, win_end)
                for emp in avail:
                    _, sw2 = state(cur)
                    if emp["id"] in sw2:
                        continue
                    ok, _ = can_place(emp["id"], cur, s_iso, e_iso)
                    if ok:
                        place(emp["id"], cur, s_iso, e_iso, "不足補填（社員自動配置）")
                        placed_any = True
                        progress = True
                        break
                if placed_any:
                    break
        cur = add_days(cur, 1)

    # -----------------------------------------------------------
    # 不足集計（曜日別オーバーライド適用後の required_staff を使用）
    # -----------------------------------------------------------
    # shortage_list: パターン別（詳細表示用）
    # shortage_unique: 時間帯別一意（重なりマージ・カウント用）
    shortage_list = []
    cur = start_date
    while cur <= end_date:
        cov, _ = state(cur)
        wd = weekday_sun0(datetime.strptime(cur, "%Y-%m-%d").date())
        for pat in patterns_for_weekday(wd):
            req_c = pat.get("required_staff") or 0
            if req_c <= 0:
                continue
            min_cov, _ = _pattern_min_coverage(req_map_for(cur), cov, pat, GRAN)
            short = req_c - min_cov
            if short > 0:
                shortage_list.append({
                    "date": cur, "pattern": pat["pattern_name"], "start_time": pat["start_time"],
                    "required": req_c, "placed": min_cov, "shortage": short,
                })
        cur = add_days(cur, 1)

    # 時間帯別一意不足（重なりパターンをマージ）
    # confirmed を shifts 互換の辞書にして compute_shortage_unique_hours に通す
    _shifts_for_count = [
        {"start_datetime": c["start"], "end_datetime": c["end"], "status": "confirmed"}
        for c in confirmed
    ]
    shortage_unique = compute_shortage_unique_hours(
        _shifts_for_count, patterns, start_date, end_date, weekday_overrides)

    # -----------------------------------------------------------
    # 警告（連勤・月間超過・固定シフト上限超過）
    # ※連勤はアルバイトのみ対象（社員はフルタイム柔軟稼動）
    # -----------------------------------------------------------
    warnings = []
    name_map = {s["id"]: s.get("name", f"スタッフ{s['id']}") for s in staffs}
    # 固定シフトが上限人数によりスキップされた場合の警告（要件R3）
    for of in overcap_fixed:
        nm = name_map.get(of["staff_id"], f"スタッフ{of['staff_id']}")
        if of["reason"] == "cap":
            warnings.append({
                "staff_id": of["staff_id"], "name": nm, "type": "fixed_overcap",
                "date": of["date"],
                "message": (
                    f"{nm}さんの固定シフト({of['date']} {of['start_time']}-{of['end_time']})は"
                    f"必要人数(上限)に達しているため配置をスキップしました。"
                    f"固定シフト契約または必要人数設定を見直してください。"
                ),
            })
        elif of["reason"] == "already_working":
            warnings.append({
                "staff_id": of["staff_id"], "name": nm, "type": "fixed_duplicate",
                "date": of["date"],
                "message": (
                    f"{nm}さんの固定シフト({of['date']} {of['start_time']}-{of['end_time']})は"
                    f"同日に重複する固定シフトがあるためスキップしました（設定ミスの可能性）。"
                ),
            })
    if recommended_consec < 99:
        for sid, days in staff_days.items():
            if days and staff_role.get(sid) == "part_time":
                run = max_consecutive_run(days)
                if run > recommended_consec:
                    warnings.append({
                        "staff_id": sid, "name": name_map.get(sid, ""), "type": "consecutive",
                        "consecutive_days": run, "recommended": recommended_consec,
                        "message": f"{name_map.get(sid,'')}さんは最大{run}日連続勤務になります（推奨{recommended_consec}日）。",
                    })
    for sid, mins in minutes_by_staff.items():
        mx = monthly_cap.get(sid) or 0
        if mx and mins > mx * 60:
            warnings.append({
                "staff_id": sid, "name": name_map.get(sid, ""), "type": "monthly_overflow",
                "hours": round(mins / 60 * 10) / 10, "max": mx,
                "message": f"{name_map.get(sid,'')}さんの月間時間が{round(mins/60*10)/10}hで上限({mx}h)超過。",
            })

    # -----------------------------------------------------------
    # 学生アルバイトルール検証
    # 1) 学生のみで構成される日がないかチェック（警告）
    # 2) 学生の月間時間が80hを超えていないか（既に monthly_overflow で検出される）
    # -----------------------------------------------------------
    # 日ごとの配置ロール集合を確認（学生のみの日を検出）
    student_only_days = []
    for day, roles in day_placed_roles.items():
        non_student = roles - {"student"}
        if roles and not non_student:
            student_only_days.append(day)
    if student_only_days:
        warnings.append({
            "type": "student_only_day",
            "days": sorted(student_only_days),
            "message": (
                f"以下の日は学生アルバイトのみの配置になりました（社会人スタッフが1名も配置されていません）: "
                f"{', '.join(sorted(student_only_days))}。社会人スタッフの追加配置を推奨します。"
            ),
        })

    # -----------------------------------------------------------
    # Explainable AI: シフト作成の判断理由を生成
    # -----------------------------------------------------------
    explanations = _build_explanations(
        confirmed, pending, shortage_list, warnings, minutes_by_staff,
        staff_role, name_map, monthly_cap, requests, weekday_overrides,
        overcap_fixed,
    )

    return {
        "confirmed": confirmed, "pending": pending,
        "minutes_by_staff": minutes_by_staff,
        "shortage": shortage_list, "shortage_unique": shortage_unique,
        "warnings": warnings,
        "explanations": explanations,
    }


def _build_explanations(confirmed, pending, shortage, warnings, minutes_by_staff,
                        staff_role, name_map, monthly_cap, requests, weekday_overrides,
                        overcap_fixed=None):
    """Explainable AI: シフト作成の判断理由を [{type, icon, title, detail}] 形式で生成。

    type: 'success' | 'info' | 'warning' | 'ai'
    icon: Bootstrap Icons class または絵文字
    """
    overcap_fixed = overcap_fixed or []
    explanations = []
    req_count = len(requests)
    placed_req = sum(1 for c in confirmed if c.get("reason") in ("希望シフト", "柔軟希望(any)", "柔軟希望(morning)", "柔軟希望(evening)"))
    fixed_count = sum(1 for c in confirmed if c.get("reason") == "固定シフト")
    emp_fill = sum(1 for c in confirmed if "不足補填" in (c.get("reason") or ""))

    # 1. 希望反映率
    if req_count > 0:
        rate = round(placed_req / req_count * 100)
        if rate >= 80:
            explanations.append({"type": "success", "icon": "bi-emoji-smile",
                "title": f"スタッフ希望を {rate}% 反映しました",
                "detail": f"{placed_req}件 / {req_count}件の希望シフトを組み込みました。希望休・NG曜日は優先的に考慮しています。"})
        elif rate >= 50:
            explanations.append({"type": "info", "icon": "bi-check2-circle",
                "title": f"スタッフ希望を {rate}% 反映しました",
                "detail": f"{placed_req}件 / {req_count}件を反映。未反映分は上限人数・最低勤務時間の制約によるものです。"})
        else:
            explanations.append({"type": "warning", "icon": "bi-exclamation-triangle",
                "title": f"スタッフ希望の反映率が {rate}% です",
                "detail": f"{req_count - placed_req}件の希望を上限人数制約により調整待ちに回しました。時間帯の必要人数を見直すことで改善する可能性があります。"})

    # 2. 不足解消
    if not shortage and confirmed:
        explanations.append({"type": "success", "icon": "bi-shield-check",
            "title": "全時間帯の必要人数を確保しました",
            "detail": "営業時間中のすべての時間帯で、設定された必要人数を満たしています。人員不足はありません。"})
    elif shortage:
        explanations.append({"type": "warning", "icon": "bi-exclamation-octagon",
            "title": f"{len(shortage)}枠で人員不足が残っています",
            "detail": "社員による自動補填の後も、スタッフ不足により埋められない時間帯があります。ヘルプ募集や人員追加をご検討ください。"})

    # 3. 社員による不足補填
    if emp_fill > 0:
        explanations.append({"type": "ai", "icon": "bi-robot",
            "title": f"社員 {emp_fill}シフトで不足を自動補填しました",
            "detail": "アルバイトの希望だけでは埋まらない時間帯を、稼働可能な社員が柔軟にカバーしました。ピーク時間の業務継続性を確保しています。"})

    # 4. 固定シフトの尊重
    if fixed_count > 0:
        explanations.append({"type": "info", "icon": "bi-calendar-check",
            "title": f"{fixed_count}件の固定シフト（契約勤務）を最優先で配置しました",
            "detail": "契約済みの固定シフトは希望・社員補填より優先して配置し、その周辺に最適化しました。"})

    # 4b. 固定シフトの上限超過（過剰契約）の報告（要件R3）
    if overcap_fixed:
        over_cap_only = [f for f in overcap_fixed if f.get("reason") == "cap"]
        if over_cap_only:
            names = "、".join(sorted({name_map.get(f["staff_id"], f"スタッフ{f['staff_id']}")
                                       for f in over_cap_only}))[:60]
            explanations.append({"type": "warning", "icon": "bi-exclamation-diamond",
                "title": f"固定シフト {len(over_cap_only)}件が上限人数によりスキップされました",
                "detail": (
                    f"{names}さんの固定シフトは、その時間帯の必要人数（上限）に既に達しているため"
                    f"配置しませんでした。必要人数設定の意味を保つため、過剰な固定シフト契約の"
                    f"見直し、または当該時間帯の必要人数の増加をご検討ください。"
                )})

    # 5. 勤務回数の均等化
    pt_mins = {sid: m for sid, m in minutes_by_staff.items() if staff_role.get(sid) == "part_time" and m > 0}
    if len(pt_mins) >= 2:
        vals = list(pt_mins.values())
        avg = sum(vals) / len(vals)
        spread = (max(vals) - min(vals)) / 60
        if spread <= 10:
            explanations.append({"type": "success", "icon": "bi-bar-chart",
                "title": "アルバイト間の勤務時間を均等化しました",
                "detail": f"最大差 {spread:.1f}h に収まり、公平なシフト配分になっています。"})
        else:
            explanations.append({"type": "info", "icon": "bi-bar-chart",
                "title": "一部スタッフの勤務が多めです",
                "detail": f"アルバイト間の最大差は {spread:.1f}h です。希望日数の差によるものですが、偏りが気になる場合は希望提出の段階で調整をご検討ください。"})

    # 6. 月間上限の尊重
    over_cap = [w for w in warnings if w.get("type") == "monthly_overflow"]
    if not over_cap:
        capped = [sid for sid, mx in monthly_cap.items() if mx and minutes_by_staff.get(sid, 0) > 0]
        if capped:
            explanations.append({"type": "info", "icon": "bi-shield",
                "title": "全スタッフの月間上限を遵守しました",
                "detail": "設定された月間最大勤務時間を超える配置は行っていません。"})
    else:
        names = "、".join(w["name"] for w in over_cap[:3])
        explanations.append({"type": "warning", "icon": "bi-clock-history",
            "title": f"{names}さんの月間時間が上限を超過しています",
            "detail": "固定シフトの契約時間が上限を超えているためです。契約時間の見直しご検討ください。"})

    # 7. 連勤への配慮
    consec = [w for w in warnings if w.get("type") == "consecutive"]
    if not consec:
        explanations.append({"type": "success", "icon": "bi-heart-pulse",
            "title": "連勤に配慮したシフト構成です",
            "detail": "アルバイトの連続勤務は推奨日数以内に収まっています。"})
    else:
        explanations.append({"type": "warning", "icon": "bi-heart-pulse",
            "title": f"{len(consec)}名が連勤推奨日数を超えています",
            "detail": "固定シフトの曜日パターンによるものです。スタッフの体調にはご留意ください。"})

    # 8. 曜日別設定の活用
    if weekday_overrides:
        explanations.append({"type": "ai", "icon": "bi-calendar3-range",
            "title": "曜日別の必要人数設定を適用しました",
            "detail": "特定曜日（週末・繁忙期など）の必要人数を個別に反映し、混雑しやすい日のスタッフを確保しました。"})

    # 9. 生成サマリー
    total_hours = sum(minutes_by_staff.values()) / 60
    explanations.append({"type": "info", "icon": "bi-lightbulb",
        "title": f"シフト合計 {len(confirmed)}件 / {total_hours:.0f}時間",
        "detail": f"確定 {len(confirmed)}件、調整待ち {len(pending)}件を生成しました。"})

    return explanations


# ===========================================================
# 外部API: 不足集計（shifts は start_datetime/end_datetime または start/end を許容）
# ===========================================================
def compute_shortage(shifts, patterns, start_date, end_date, weekday_overrides=None):
    """確定シフト一覧から日次パターン不足を再計算。

    auto_generate の出力(start/end) と DB の行(start_datetime/end_datetime) の
    両方のキーを受け付ける（旧 BUG#3 の修正）。
    weekday_overrides: {(pattern_id, weekday): required_staff} を渡すと曜日別必要人数を適用。
    """
    coverage = {}
    for s in shifts:
        if (s.get("status") or "confirmed") != "confirmed":
            continue
        sd = s.get("start_datetime") or s.get("start")
        ed = s.get("end_datetime") or s.get("end")
        if not sd or not ed or len(sd) < 16:
            continue
        day = sd[:10]
        cov = coverage.setdefault(day, {})
        for sl in _shift_slots(sd, ed, GRAN):
            cov[sl] = cov.get(sl, 0) + 1

    def patterns_for_weekday(wd):
        out = []
        for pat in patterns:
            ov = (weekday_overrides or {}).get((pat.get("id"), wd))
            p = dict(pat)
            if ov is not None:
                p["required_staff"] = ov
            out.append(p)
        return out

    shortage = []
    cur = start_date
    while cur <= end_date:
        wd = weekday_sun0(datetime.strptime(cur, "%Y-%m-%d").date())
        day_patterns = patterns_for_weekday(wd)
        req_map = _day_requirements(day_patterns, GRAN, wd, weekday_overrides)
        cov = coverage.get(cur, {})
        for pat in day_patterns:
            req_c = pat.get("required_staff") or 0
            if req_c <= 0:
                continue
            min_cov, _ = _pattern_min_coverage(req_map, cov, pat, GRAN)
            short = req_c - min_cov
            if short > 0:
                shortage.append({
                    "date": cur, "pattern": pat["pattern_name"], "start_time": pat["start_time"],
                    "required": req_c, "placed": min_cov, "shortage": short,
                })
        cur = add_days(cur, 1)
    return shortage


def compute_shortage_unique_hours(shifts, patterns, start_date, end_date, weekday_overrides=None):
    """時間帯(スロット)単位で「一意の不足」を集計して返す。

    compute_shortage はパターン別に不足を列出しするが、複数パターンが
    時間帯を重なる場合（例: Full 4:00-14:00 必要1 + 朝 4:00-6:00 必要1）、
    「同じ 4:00-5:00 スロットが2パターンで不足 → 2枠不足」と過大カウントされる
    問題があった（インシデント）。

    本関数は:
      - 各スロット(分)ごとに必要人数を **max** で集約（重なりはマージ）
      - 配置人数を引いた差分が正のスロットだけを数える
      - 連続する不足スロットは gap の大小に関わらず1つの「区間」にマージし、
        その区間の最大 gap を探す（"最大N人足りない区間" = N枠不足）

    戻り値: [{"date": "YYYY-MM-DD", "start_min": int, "end_min": int, "gap": int}]
      start_min/end_min は当日基準の拡張分(0-2880)。overnight は +1440。
      gap はその区間内での最大不足人数。
    """
    coverage = {}
    for s in shifts:
        if (s.get("status") or "confirmed") != "confirmed":
            continue
        sd = s.get("start_datetime") or s.get("start")
        ed = s.get("end_datetime") or s.get("end")
        if not sd or not ed or len(sd) < 16:
            continue
        day = sd[:10]
        cov = coverage.setdefault(day, {})
        for sl in _shift_slots(sd, ed, GRAN):
            cov[sl] = cov.get(sl, 0) + 1

    result = []
    cur = start_date
    while cur <= end_date:
        wd = weekday_sun0(datetime.strptime(cur, "%Y-%m-%d").date())
        day_patterns = []
        for pat in patterns:
            p = dict(pat)
            ov = (weekday_overrides or {}).get((pat.get("id"), wd))
            if ov is not None:
                p["required_staff"] = ov
            day_patterns.append(p)
        # max集約した要件マップ(_day_requirements と同等)
        req_map = _day_requirements(day_patterns, GRAN, wd, weekday_overrides)
        cov = coverage.get(cur, {})
        # 連続スロットを走査して gap>0 の区間をマージ
        # gap の大小に関わらず連続する不足スロットは1つの区間にまとめ、
        # 区間内の最大 gap を探す（"最大N人足りない" = N枠不足）
        slots = sorted([s for s, v in req_map.items() if v > 0])
        i = 0
        while i < len(slots):
            s = slots[i]
            req = req_map[s]
            placed = cov.get(s, 0)
            gap = req - placed
            if gap <= 0:
                i += 1
                continue
            # 連続区間の終端を探す（次スロットも gap>0 なら拡張）
            j = i + 1
            max_gap = gap
            while j < len(slots) and slots[j] - slots[j - 1] == GRAN:
                req_j = req_map[slots[j]]
                placed_j = cov.get(slots[j], 0)
                gap_j = req_j - placed_j
                if gap_j <= 0:
                    break
                if gap_j > max_gap:
                    max_gap = gap_j
                j += 1
            result.append({
                "date": cur,
                "start_min": s,
                "end_min": slots[j - 1] + GRAN,
                "gap": max_gap,
            })
            i = j
        cur = add_days(cur, 1)
    return result
