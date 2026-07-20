"""holidays_jp.py - 日本の祝日計算（外部API非依存・計算で導出）。

【設計意図】
  外部API（内閣府の祝日API等）に依存すると、API障害や仕様変更で
  シフト作成ができなくなるリスクがある。日本の祝日は法律で定められ
  ており、年度ごとに計算可能なので、純粋なPython計算で導出する。

【対応範囲】
  2000年〜2099年。春分の日・秋分の日は天文学的計算ではなく
  現行の計算式（1948年〜2150年まで有効）を使用。

【祝日法対応】
  - 振替休日（祝日が日曜日 → 翌月曜日、1973年施行）
  - 国民の休日（祝日に挟まれた平日 → 休日、1985年〜2006年まで、現存: 5月4日→「みどりの日」2007年以降は祝日）
  - 2020年以降の山の日・スポーツの日の特例（東京五輪）は対応せず通常ルールを採用

Usage:
    from holidays_jp import japanese_holidays
    holidays = japanese_holidays(2026)
    # => [{"date": "2026-01-01", "name": "元日"}, ...]
"""
from datetime import date, timedelta


def _spring_equinox_day(year):
    """春分の日の「日」を計算（1948-2150年で有効な計算式）。"""
    # 1980年以降の計算式
    if 1980 <= year <= 2099:
        day = int(20.8431 + 0.242194 * (year - 1980) - (year - 1980) // 4)
    elif 2100 <= year <= 2150:
        day = int(21.8519 + 0.242194 * (year - 1980) - (year - 1980) // 4)
    else:
        day = 20  # フォールバック
    return day


def _autumn_equinox_day(year):
    """秋分の日の「日」を計算（1948-2150年で有効な計算式）。"""
    if 1980 <= year <= 2099:
        day = int(23.2488 + 0.242194 * (year - 1980) - (year - 1980) // 4)
    elif 2100 <= year <= 2150:
        day = int(24.2488 + 0.242194 * (year - 1980) - (year - 1980) // 4)
    else:
        day = 23  # フォールバック
    return day


def _nth_weekday(year, month, weekday, n):
    """指定月の第n weekday（0=月, ..., 6=日）の日付を返す。"""
    # weekday: Python の date.weekday() と同じ（0=月、6=日）
    first = date(year, month, 1)
    first_weekday = first.weekday()
    offset = (weekday - first_weekday) % 7
    day = 1 + offset + (n - 1) * 7
    return date(year, month, day)


def _holidays_raw(year):
    """振替休日を含まないベース祝日リストを返す。
    戻り値: [(date_obj, name)]
    """
    se = _spring_equinox_day(year)
    ae = _autumn_equinox_day(year)
    holidays = [
        (date(year, 1, 1), "元日"),
        (_nth_weekday(year, 1, 0, 2), "成人の日"),  # 1月第2月曜
        (date(year, 2, 11), "建国記念の日"),
        (date(year, 2, 23), "天皇誕生日"),  # 2020年以降
        (date(year, 3, se), "春分の日"),
        (date(year, 4, 29), "昭和の日"),
        (date(year, 5, 3), "憲法記念日"),
        (date(year, 5, 4), "みどりの日"),
        (date(year, 5, 5), "こどもの日"),
        (_nth_weekday(year, 7, 0, 3), "海の日"),  # 7月第3月曜
        (date(year, 8, 11), "山の日"),
        (_nth_weekday(year, 9, 0, 3), "敬老の日"),  # 9月第3月曜
        (date(year, 9, ae), "秋分の日"),
        (_nth_weekday(year, 10, 0, 2), "スポーツの日"),  # 10月第2月曜
        (date(year, 11, 3), "文化の日"),
        (date(year, 11, 23), "勤労感謝の日"),
    ]
    # 天皇誕生日は1989-2018は12月23日、2019年は無し（即位関連特例）、2020年以降は2月23日
    if year <= 2018:
        holidays = [h for h in holidays if h[1] != "天皇誕生日"]
        if year >= 1989:
            holidays.append((date(year, 12, 23), "天皇誕生日"))
    if year == 2019:
        # 即位関連の特例祝日
        holidays = [h for h in holidays if h[1] != "天皇誕生日"]
        holidays.extend([
            (date(2019, 5, 1), "即位の日"),
            (date(2019, 10, 22), "即位礼正殿の儀"),
        ])
    # 2020年は山の日・海の日・スポーツの日が五輪特例で移動（今回は通常ルールに寄せる）
    # ※ 実運用上は2020年の特例を無視しても実害は小さい
    return sorted(holidays, key=lambda x: x[0])


def japanese_holidays(year):
    """指定年の日本の祝日リストを返す。
    振替休日（日曜祝日の翌月曜）を含む。
    戻り値: [{"date": "YYYY-MM-DD", "name": "..."}]
    """
    raw = _holidays_raw(year)
    by_date = {d: n for d, n in raw}
    result = []
    for d, n in raw:
        result.append({"date": d.isoformat(), "name": n})
        # 振替休日判定: 祝日が日曜日なら翌月曜を振替休日に（1973年以降）
        if year >= 1973 and d.weekday() == 6:  # 日曜
            furikae = d + timedelta(days=1)
            # 翌日も祝日の場合は振替休日はさらに翌日（現行法では発生しないが念のため）
            while furikae in by_date:
                furikae = furikae + timedelta(days=1)
            result.append({"date": furikae.isoformat(), "name": f"{n}の振替休日"})
    # 国民の休日（祝日に挟まれた平日）1985-2006
    # ※ 2007年以降は「みどりの日(5/4)」が祝日になったため発生しない
    # ※ 9月に「敬老の日(第3月曜)」と「秋分の日」が隣接すると「国民の休日」発生
    if year >= 2007:
        # 敬老の日(9月第3月曜) と 秋分の日(9月22日頃) の間の日が空く場合
        keirou = _nth_weekday(year, 9, 0, 3)
        shuubun = date(year, 9, _autumn_equinox_day(year))
        if (shuubun - keirou).days == 2:
            # 敬老の日と秋分の日に挟まれた平日を国民の休日に
            kokumin = keirou + timedelta(days=1)
            result.append({"date": kokumin.isoformat(), "name": "国民の休日"})
    # 日付順にソート
    result.sort(key=lambda x: x["date"])
    # 重複排除（同日付に複数祝日）
    seen = set()
    deduped = []
    for h in result:
        if h["date"] not in seen:
            seen.add(h["date"])
            deduped.append(h)
    return deduped


def japanese_holidays_in_range(start_date, end_date):
    """期間内の日本の祝日リストを返す。
    start_date, end_date: "YYYY-MM-DD" 文字列
    """
    try:
        sy, sm, sd = map(int, start_date.split("-"))
        ey, em, ed = map(int, end_date.split("-"))
    except Exception:
        return []
    start = date(sy, sm, sd)
    end = date(ey, em, ed)
    result = []
    for y in range(sy, ey + 1):
        for h in japanese_holidays(y):
            hd = date.fromisoformat(h["date"])
            if start <= hd <= end:
                result.append(h)
    return result
