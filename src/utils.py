"""utils.py - 共通ユーティリティ（Flask版・純粋Python）。"""
import json
from datetime import date, datetime, timedelta, timezone

_JST = timezone(timedelta(hours=9))


# ---------- 時刻・タイムゾーン ----------
def jst_now():
    """日本時間(JST, UTC+9)の現在日時。"""
    return datetime.now(_JST).replace(tzinfo=None)


def jst_today():
    return jst_now().date()


# ---------- 日付・時刻 ----------
def norm_hhmm(time_s):
    """時刻文字列を "HH:MM" 形式にゼロ埋め正規化。

    入力例: "7:00" → "07:00", "09:00" → "09:00", "" → "00:00"
    ※ DB に "7:00" のような非ゼロ埋め時刻が保存されているケースや、
      HTML <input type="time"> の値がブラウザ依存で "7:00" になるケースを救う。
    """
    if not time_s:
        return "00:00"
    parts = str(time_s).split(":")
    if len(parts) < 2:
        return "00:00"
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return "00:00"
    return f"{h:02d}:{m:02d}"


def norm_dt_iso(s):
    """ISO datetime を "YYYY-MM-DDTHH:MM:SS" 形式に正規化。

    入力例: "2026-08-01T7:00:00" → "2026-08-01T07:00:00"
            "2026-08-01T09:00"   → "2026-08-01T09:00:00"
    ※ DB に非ゼロ埋め時刻が混入したインシデントのデータ修正用。
    """
    if not s:
        return s
    s = str(s)
    if "T" not in s or len(s) < 11:
        return s
    date_part, _, time_part = s.partition("T")
    # 秒がない場合は補完
    if time_part.count(":") == 1:
        time_part = time_part + ":00"
    hh, _, rest = time_part.partition(":")
    try:
        hh_int = int(hh)
    except ValueError:
        return s
    norm_time = f"{hh_int:02d}:{rest}"
    return f"{date_part}T{norm_time}"


def combine_dt(date_s, time_s):
    return f"{date_s}T{norm_hhmm(time_s)}:00"


def combine_dt_overnight(date_s, start_time_s, end_time_s):
    """パターン/固定シフト用 combine。end_time が start_time 以前なら翌日扱い。

    例: combine_dt_overnight("2026-08-03", "22:00", "05:00")
      → ("2026-08-03T22:00:00", "2026-08-04T05:00:00")
    戻り値: (start_iso, end_iso)
    【時刻ゼロ埋め】start/end に "7:00" のような非ゼロ埋めが渡された場合も
      "07:00" に正規化して返す（DB登録時のインシデント再発防止）。
    """
    ns = norm_hhmm(start_time_s)
    ne = norm_hhmm(end_time_s)
    ps = _hhmm_to_min(ns)
    pe = _hhmm_to_min(ne)
    if pe <= ps:
        next_day = add_days(date_s, 1)
        return f"{date_s}T{ns}:00", f"{next_day}T{ne}:00"
    return f"{date_s}T{ns}:00", f"{date_s}T{ne}:00"


def parse_iso(s):
    """ISO日時をパース。秒なし(YYYY-MM-DDTHH:MM) と秒あり(YYYY-MM-DDTHH:MM:SS) の両方を許容。"""
    if not s:
        raise ValueError("empty datetime")
    if len(s) == 16:  # YYYY-MM-DDTHH:MM
        return datetime.strptime(s, "%Y-%m-%dT%H:%M")
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def normalize_iso(s):
    """秒なし 'YYYY-MM-DDTHH:MM' を 'YYYY-MM-DDTHH:MM:00' に正規化（DB保存前のクリーンアップ）。"""
    if s and len(s) == 16:
        return s + ":00"
    return s


def minutes_between(start_iso, end_iso):
    return int((parse_iso(end_iso) - parse_iso(start_iso)).total_seconds() // 60)


def compute_break_minutes(work_minutes):
    """労働基準法の休憩: 8h超→60分, 6h超→45分。"""
    if work_minutes > 8 * 60:
        return 60
    if work_minutes > 6 * 60:
        return 45
    return 0


def night_minutes(start_iso, end_iso):
    """深夜労働(22:00〜翌5:00重複)を分で計算。

    NOTE: かつて `cur = s.replace(hour=0,...)` で当日の22時以降の窓しか評価せず、
    前日 22:00〜当日 05:00 の窓（＝当日早朝の深夜労働）を見落とすバグがあった。
    開始時刻が 05:00 以前の場合は、前日 22:00 に遡って窓を評価する。
    """
    s = parse_iso(start_iso); e = parse_iso(end_iso)
    if e <= s:
        return 0
    total = 0
    # 開始日の前日 22:00 を起点にして日ごとの窓(22:00〜翌05:00)を評価する
    # → 当日早朝（0:00-5:00）の深夜労働も捕捉できる
    cur = s.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    while cur <= e:
        wstart = cur.replace(hour=22)
        wend = wstart + timedelta(hours=7)
        lo = max(s, wstart); hi = min(e, wend)
        if hi > lo:
            total += int((hi - lo).total_seconds() // 60)
        cur += timedelta(days=1)
    return total


def add_days(date_s, n):
    d = datetime.strptime(date_s, "%Y-%m-%d").date()
    return (d + timedelta(days=n)).strftime("%Y-%m-%d")


def weekday_sun0(d):
    """date から 0=日曜〜6=土曜。"""
    return (d.weekday() + 1) % 7


def max_consecutive_run(day_set):
    days = sorted(day_set)
    best = run = 0
    prev = None
    for d in days:
        if prev is not None and add_days(prev, 1) == d:
            run += 1
        else:
            run = 1
        if run > best:
            best = run
        prev = d
    return best


def time_overlaps(a_start, a_end, b_start, b_end):
    a_end = a_end if a_end > a_start else "23:59"
    return a_start < b_end and b_start < a_end


def shift_covers_pattern(start_iso, end_iso, day, pat_start, pat_end):
    """シフトが指定日のパターン時間帯と重なる（カバーする）か。"""
    if (start_iso or "")[:10] != day:
        return False
    return time_overlaps(start_iso[11:16], end_iso[11:16], pat_start, pat_end)


def _hhmm_to_min(t):
    h, _, m = (t or "").partition(":")
    try:
        return int(h) * 60 + int(m)
    except ValueError:
        return 0


def covers_pattern_substantial(start_iso, end_iso, day, pat_start, pat_end):
    """シフトが指定日のパターンを「実質的に」カバーするか。

    パターン時間帯の50%以上をカバーする場合に True とする。
    単純な時間重なり（shift_covers_pattern）では、固定9-18が夜17-22と
    1時間しか重ならないのに「夜カバー」と見なして不足を隠してしまうため、
    カバレッジ「集計」にはこちらを使う。
    （※パターンへの所属判定などには引き続き shift_covers_pattern を使用）
    """
    if (start_iso or "")[:10] != day:
        return False
    ss = (start_iso or "")[11:16]
    se = (end_iso or "")[11:16]
    if not ss or not se or not pat_start or not pat_end:
        return False
    s_min = _hhmm_to_min(ss)
    e_min = _hhmm_to_min(se)
    ps = _hhmm_to_min(pat_start)
    pe = _hhmm_to_min(pat_end)
    if e_min <= s_min or pe <= ps:
        return False
    overlap = min(e_min, pe) - max(s_min, ps)
    pat_len = pe - ps
    return overlap > 0 and overlap * 2 >= pat_len


def calc_next_period(now=None, mode="half"):
    """次のシフト募集期間を自動計算。mode='half' or 'month'。締切=開始の7日前。"""
    now = now or jst_today()
    y, m = now.year, now.month
    if mode == "month":
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        start = date(ny, nm, 1)
        first_next = date(ny + (1 if nm == 12 else 0), 1 if nm == 12 else nm + 1, 1)
        end = first_next - timedelta(days=1)
    else:
        if now.day <= 15:
            start = date(y, m, 16)
            first_next = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
            end = first_next - timedelta(days=1)
        else:
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            start = date(ny, nm, 1)
            end = date(ny, nm, 15)
    deadline = start - timedelta(days=7)
    return {"start_date": start.strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d"),
            "deadline": deadline.strftime("%Y-%m-%d")}


def validate_password(pw):
    """パスワード強度チェック。問題なければ None、問題あればメッセージ。"""
    if not pw or len(pw) < 8:
        return "パスワードは8文字以上で設定してください"
    if not any(c.isalpha() for c in pw):
        return "パスワードに英字を含めてください"
    if not any(c.isdigit() for c in pw):
        return "パスワードに数字を含めてください"
    return None


def parse_settings(s):
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def build_ics(shifts, staff_name, shop_name="ShiftAI"):
    """確定シフト配列から iCalendar(.ics) 文字列を生成。"""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ShiftAI//Shift//JA", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for s in shifts:
        if s.get("status") != "confirmed":
            continue
        sdt = parse_iso(s["start_datetime"]); edt = parse_iso(s["end_datetime"])
        uid = f"shiftai-{s.get('id')}-{sdt.strftime('%Y%m%d')}@shiftai"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{jst_now().strftime('%Y%m%dT%H%M%SZ')}",
                  f"DTSTART:{sdt.strftime('%Y%m%dT%H%M%S')}", f"DTEND:{edt.strftime('%Y%m%dT%H%M%S')}",
                  f"SUMMARY:{shop_name} シフト", f"DESCRIPTION:{staff_name}さんのシフト", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
