"""utils.py - 共通ユーティリティ（Flask版・純粋Python）。"""
import json
import math
import re
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
    """労働基準法の休憩: 6h超→45分, 8h超→60分。

    【労基法コンプライアンス拡張】長時間労働に対する段階的休憩付与:
      - 6h超 → 45分
      - 8h超 → 60分
      - 10h超 → 90分
      - 12h超 → 120分
      - 14h超 → 150分
    労基法34条は「8hを超える場合は45分以上」を最低基準とするが、
    実務上は8h毎に45分追加が望ましく、過労死防止の観点から重要。
    """
    if work_minutes > 14 * 60:
        return 150
    if work_minutes > 12 * 60:
        return 120
    if work_minutes > 10 * 60:
        return 90
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


# ============================================================
# スタッフ勤務傾向（時間帯ヒストグラム）の学習・スコアリング
# ============================================================
def build_staff_tendency(confirmed_shifts, wish_shifts=None, day_count=90):
    """過去の確定シフト + 希望から、スタッフごとの時間帯ヒストグラムを構築。

    confirmed_shifts: [{"staff_id":int, "start_datetime":"...T HH:MM:SS", "end_datetime":...}, ...]
    wish_shifts: 同形式（wish_history 由来）。None の場合は希望を考慮しない。
    day_count: 過去何日分をサンプルするか（古いデータは重み減衰）。

    戻り値: {staff_id: [48個のfloat]} — 各時間帯(0-47, overnightは+24)の偏好スコア
      0.0 = 全く勤務実績がない時間帯
      高い = よく勤務している時間帯
      合計値は1.0に正規化（確率分布）

    【設計】
    - 確定シフトは重み2.0（実績として強いシグナル）
    - 希望は重み0.5（意向は反映するが実績より弱い）
    - 直近30日は重み×1.5（最近の傾向を優先）
    - 時間帯ヒストグラムは拡張時間(0-47)で構築（overnight対応）
    """
    from collections import defaultdict
    hist = defaultdict(lambda: [0.0] * 48)
    now = jst_now()

    def _add_shift(s, weight):
        sid = s.get("staff_id")
        if sid is None:
            return
        sd = s.get("start_datetime") or s.get("start") or ""
        ed = s.get("end_datetime") or s.get("end") or ""
        if not sd or not ed or "T" not in sd:
            return
        # 直近重み付け（日数ベースの減衰）
        try:
            d0 = datetime.strptime(sd[:10], "%Y-%m-%d").date()
            days_ago = (now.date() - d0).days
            if days_ago > day_count:
                return
            recent_boost = 1.5 if days_ago <= 30 else 1.0
        except ValueError:
            recent_boost = 1.0
        # 拡張分計算（当日0:00=0, 翌日0:00=1440）
        anchor = sd[:10]
        s_min = _ext_min_from_iso(sd, anchor)
        e_min = _ext_min_from_iso(ed, anchor)
        if e_min <= s_min:
            e_min = s_min + 60
        # 時間単位に集計
        s_h = s_min // 60
        e_h = (e_min + 59) // 60  # 切り上げ
        for h in range(s_h, min(e_h, 48)):
            hist[sid][h] += weight * recent_boost

    # 確定シフト（重み2.0）
    for s in (confirmed_shifts or []):
        _add_shift(s, 2.0)
    # 希望（重み0.5）
    for s in (wish_shifts or []):
        _add_shift(s, 0.5)

    # 正規化（スタッフごとに合計1.0）
    result = {}
    for sid, h in hist.items():
        total = sum(h)
        if total > 0:
            result[sid] = [v / total for v in h]
        else:
            result[sid] = [1.0 / 48] * 48  # 一様分布（データ無し）
    return result


def _ext_min_from_iso(iso, anchor_date):
    """ISO datetime を anchorDate 基準の拡張分に変換（翌日なら+1440）。"""
    iso = iso or ""
    if "T" not in iso:
        return 0
    iso_date = iso[:10]
    t_part = iso[11:]
    m = __import__("re").match(r"(\d{1,2}):(\d{2})", t_part)
    if not m:
        return 0
    h, mn = int(m.group(1)), int(m.group(2))
    if anchor_date and iso_date != anchor_date:
        a = datetime.strptime(anchor_date, "%Y-%m-%d").date()
        b = datetime.strptime(iso_date, "%Y-%m-%d").date()
        diff = (b - a).days
        h += diff * 24
    return h * 60 + mn


def score_shift_for_tendency(tendency_hist, start_iso, end_iso):
    """候補シフト [start, end) がスタッフの傾向とどれくらい合うかスコア化。

    tendency_hist: [48個のfloat]（build_staff_tendency の戻り値の要素）
    戻り値: 0.0〜1.0 のスコア。高いほど傾向に合致。

    【スコアリング】
    候補シフトの各時間帯の偏好スコアを足し合わせ、シフト時間長で正規化。
    傾向が明確な時間帯（ヒストグラムの山）と重なるほど高スコア。
    """
    if not tendency_hist or not start_iso or not end_iso:
        return 0.5  # 中立（データ無し）
    anchor = start_iso[:10]
    s_min = _ext_min_from_iso(start_iso, anchor)
    e_min = _ext_min_from_iso(end_iso, anchor)
    if e_min <= s_min:
        e_min = s_min + 60
    s_h = s_min // 60
    e_h = (e_min + 59) // 60
    if e_h <= s_h:
        return 0.5
    total = 0.0
    for h in range(s_h, min(e_h, 48)):
        total += tendency_hist[h]
    # 平均偏好を返す（時間長で割って正規化）
    avg = total / (e_h - s_h)
    # 傾向強度（一様分布からの乖離）を加味
    return avg


def validate_password(pw):
    """パスワード強度チェック。問題なければ None、問題あればメッセージ。"""
    if not pw or len(pw) < 8:
        return "パスワードは8文字以上で設定してください"
    if not any(c.isalpha() for c in pw):
        return "パスワードに英字を含めてください"
    if not any(c.isdigit() for c in pw):
        return "パスワードに数字を含めてください"
    return None


# 店舗コード／ユーザーコード／管理者IDの最大長。
# app.py の login() と admin_api.py の管理者作成の両方から参照する
# （作成時にこの上限を超えて弾かないと、login() 側で必ず400になり
# 二度とログインできない行を作ってしまうため、両者は同じ値を共有する必要がある）。
LOGIN_CODE_MAX = 64


def sanitize_login_code(value):
    """ログイン系コード（店舗コード・ユーザーコード・管理者ID）を正規化する。

    前後の空白除去 ＋ 改行の除去。

    【なぜ改行を落とすか】
      これらの値は監査ログの actor_name やレート制限キー、
      system_admins.admin_id にそのまま入る。改行が生で残ると、
      監査ログを1行1レコードとして読む運用（将来のCSV出力を含む）で
      偽の行を差し込めてしまう。UI 側は esc() を通すので XSS にはならないが、
      ログ偽装は残るため入口で落とす。

    str() を挟むのは、JSON で文字列以外（数値・配列・オブジェクト）を送られたときに
    .strip() が AttributeError になり、未認証クライアントに 500 が返るのを避けるため。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.replace("\r", "").replace("\n", "").strip()


def parse_settings(s):
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


# shops.settings のうち、意味が固定されている既知キー。
# src/admin_api.py の PUT /api/admin/shops/<id>/settings と
# src/app.py の PUT /api/shop/settings（店舗ユーザー向け）の両方から使う。
SETTINGS_KEYS = frozenset({
    "business_hours", "default_hourly_wage", "max_consecutive_days", "max_daily_hours",
    "max_employee_daily_hours", "min_daily_hours", "night_premium_rate",
    "operation_mode", "period_mode", "shift_hours", "transport_per_day",
})

_SETTINGS_NUMERIC_KEYS = frozenset({
    "default_hourly_wage", "max_consecutive_days", "max_daily_hours",
    "max_employee_daily_hours", "min_daily_hours", "night_premium_rate",
    "transport_per_day",
})

_SETTINGS_PERIOD_MODES = frozenset({"half", "month"})

# 運用モード。"staff" はスタッフもログインして自分で希望を出す通常運用、
# "manager_only" は店長だけがアプリを使い、紙やLINEで集めた希望を代理入力する運用。
# 画面の出し分けにしか使わない（サーバ側の権限・通知の挙動は変えない）。
# 未設定の店舗は "staff" 扱い（DEFAULT_OPERATION_MODE）。
_SETTINGS_OPERATION_MODES = frozenset({"staff", "manager_only"})
DEFAULT_OPERATION_MODE = "staff"


def operation_mode_of(settings):
    """shops.settings（parse 済み dict）から運用モードを取り出す。

    未設定・不正値は既定の "staff" に倒す。値の検証は保存時
    （validate_known_settings_values）に効くが、この関数が読むのは
    検証が入る前に保存された古い行でもありうるため、読み出し側でも丸める。
    """
    if not isinstance(settings, dict):
        return DEFAULT_OPERATION_MODE
    mode = settings.get("operation_mode")
    return mode if mode in _SETTINGS_OPERATION_MODES else DEFAULT_OPERATION_MODE

# "HH:MM-HH:MM" 形式（旧 business_hours 設定）。日またぎ営業を表すため時は0-47まで許可
# （src/app.py の _validate_hhmm / shift_hours の扱いと同じ範囲に揃えている）。
_BUSINESS_HOURS_RE = re.compile(
    r"^([01]?[0-9]|[2-3][0-9]|4[0-7]):[0-5][0-9]-([01]?[0-9]|[2-3][0-9]|4[0-7]):[0-5][0-9]$")


# "YYYY-MM-DD"。<input type="date"> の値と calc_next_period() の出力がこの形。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_numeric_field(value, label, default=None):
    """数値であるべきフィールドを検証し、数値（int / float）にして返す。

    【なぜ必要か】shops.settings と同じクラスの保存型XSSが、数値のはずの列に
    残っていた。SQLite の INTEGER affinity は数値として解釈できない TEXT を
    そのまま保持するため、staffs.hourly_wage に
    `" onfocus="alert(1)" autofocus x="` を保存でき、管理コンソールの
    スタッフ編集モーダル（無エスケープの `value="${s.hourly_wage}"`）で
    value 属性を抜けて独立した属性としてパースされ、ハンドラが実行された。
    店舗ユーザー → 運営管理者への権限昇格になる。
    描画側の esc() だけでなく、そもそも数値以外を保存させない「入口」も塞ぐ。

    None / "" は「未指定」として default を返す（既存の
    `body.get("hourly_wage") or 1000` 形式のフォールバックを保つため）。
    数値として読める文字列は数値に変換して受け入れる
    （<input type="number"> の値を文字列のまま送るクライアントを壊さない）。
    bool は Python では int のサブクラスなので明示的に弾く
    （validate_known_settings_values と同じ扱い）。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} は数値で指定してください")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} は数値で指定してください")
        return value
    if isinstance(value, str):
        try:
            # int を先に試す（"1100" を 1100.0 にせず整数のまま保つ）
            return int(value.strip())
        except ValueError:
            pass
        try:
            n = float(value.strip())
        except ValueError:
            raise ValueError(f"{label} は数値で指定してください")
        if not math.isfinite(n):
            raise ValueError(f"{label} は数値で指定してください")
        return n
    raise ValueError(f"{label} は数値で指定してください")


def validate_date_field(value, label, default=None):
    """日付であるべきフィールドを "YYYY-MM-DD" として検証して返す。

    数値フィールドと同じ理由（保存型XSS の入口封じ）。
    shift_request_periods の start_date / end_date / deadline は
    public/app.js の複数箇所で無エスケープの `value="${p.start_date}"` として
    描画されるため、日付以外を保存させない。
    形だけでなく実在する日付であることまで見る（"2026-02-30" を弾く）。
    """
    if value is None or value == "":
        return default
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"{label} は YYYY-MM-DD 形式で指定してください")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{label} に存在しない日付が指定されています")
    return value


def validate_known_settings_values(patch):
    """shops.settings に部分更新でマージする patch のうち、既知キー（SETTINGS_KEYS）
    だけを値の型で検証する。不正なら ValueError を送出する
    （呼び出し元 Flask アプリの @app.errorhandler(ValueError) が 400 に変換する）。

    【なぜ必要か】管理コンソールの店舗詳細「設定」タブ（public/admin.js）は
    shop.settings を JSON.parse() してから 6つの数値項目を
    `<input value="${num(s.xxx)}">` として描画していた。数値キーに文字列
    （例: `1000"><img src=x onerror=...>`）が保存されると、無エスケープの
    まま value 属性を抜けてタグを注入できる、保存型XSSが実際に成立した
    （管理者が設定タブを開くだけでJSが実行される・クリック不要）。
    描画側で esc() するだけでなく、そもそも数値以外を保存させない「入口」の
    防御も入れることで、他の描画箇所（例: public/app.js の
    深夜割増率表示・renderShopTab の value 属性、いずれも無エスケープ）が
    将来同じ穴を持っていても、型で事前に塞がれる。

    【未知キーは検証しない】PUT /api/shop/settings は既知キー以外の任意キーの
    保存を許容する既存契約がある
    （tests/test_admin_staff_apis.py::test_update_shop_name が
    `{"new_key": 1}` の保存を 200 で期待している）。ここで弾くと後方互換が
    壊れるため、既知キーだけを対象にする。
    """
    if not isinstance(patch, dict):
        raise ValueError("settings はオブジェクトで指定してください")
    for key, value in patch.items():
        if key not in SETTINGS_KEYS:
            continue
        if key in _SETTINGS_NUMERIC_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} は数値で指定してください")
        elif key == "period_mode":
            # isinstance(str) を先に見るのは、list/dict を `in frozenset` に渡すと
            # unhashable で TypeError になり、@errorhandler(ValueError) に拾われず
            # 400 のはずが 500 になるため（{"period_mode": []} で再現した）。
            if not isinstance(value, str) or value not in _SETTINGS_PERIOD_MODES:
                raise ValueError("period_mode は half か month で指定してください")
        elif key == "operation_mode":
            if not isinstance(value, str) or value not in _SETTINGS_OPERATION_MODES:
                raise ValueError("operation_mode は staff か manager_only で指定してください")
        elif key == "business_hours":
            if not isinstance(value, str) or not _BUSINESS_HOURS_RE.match(value):
                raise ValueError("business_hours の形式が不正です（例: 09:00-22:00）")
        elif key == "shift_hours":
            if not isinstance(value, dict):
                raise ValueError("shift_hours はオブジェクトで指定してください")
            # 内部の葉の値（時刻文字列等）は GET/PUT /api/shop/shift-hours 側の
            # _normalize_shift_hours() が読み出し時に必ず正規化するため、
            # ここでは dict であることだけを保証すれば描画時の安全は保たれる。
    return patch


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
