"""weekly_hours.py - 外国籍アルバイト（資格外活動許可）の週28時間上限を判定する。

【なぜ必要か】
  資格外活動許可で働く在留資格（留学・家族滞在）は入管法上1週間28時間以内。
  超えると本人の在留資格が取り消されうるだけでなく、雇用主も不法就労助長罪
  （入管法73条の2）の対象になる。既存の労働制約は月単位（学生の月80h）しか
  無く、月の前半に集中させる組み方が素通りしていた。

【なぜ「任意の連続7日間」か】
  入管の運用は「どの曜日から起算しても1週間28時間以内」。暦週（月曜起算）で
  数えると、前の週の後半に28h・次の週の前半に28h という組み方が両方とも
  「週28h以内」を満たしてしまい、連続7日間では56hになる。この抜け穴を塞ぐ。

【なぜ純関数として切り出すか】
  同じ判定がシフト自動生成（shift_engine.py）と手動入力API（app.py）の両方で
  必要になる。どちらかに書いて他方で書き直すと、片方だけ直したときに食い違う。

外部依存なし（標準ライブラリのみ）。
"""
from datetime import date, timedelta

# 入管法上の上限。分で持つ（このモジュールの計算単位が分のため）。
WEEKLY_CAP_MINUTES = 28 * 60

# 窓の長さ（日数）。両端を含んで7日間。
_WINDOW_DAYS = 7


def _hhmm_to_min(iso):
    """ISO datetime の "HH:MM" 部分を分に直す。"""
    return int(iso[11:13]) * 60 + int(iso[14:16])


def minutes_by_day(spans):
    """(start_iso, end_iso, break_minutes) の列を {"YYYY-MM-DD": 実働分} に畳む。

    日をまたぐシフト（22:00〜翌6:00）は各日に分けて計上する。労基・入管が見るのは
    実労働時間であり、開始日にまとめて計上すると「その日8時間」という誤った形に
    なるため。shift_engine.py も日またぎを日ごとに扱っており、数え方を揃えないと
    生成と検証で結果がズレる。

    休憩は開始日から差し引く（休憩は勤務の途中に取るため）。開始日の実働を
    超える休憩は翌日から差し引く（開始日が負にならないようにする）。
    """
    out = {}
    for start_iso, end_iso, brk in spans:
        d0 = start_iso[:10]
        d1 = end_iso[:10]
        s_min = _hhmm_to_min(start_iso)
        e_min = _hhmm_to_min(end_iso)
        brk = int(brk or 0)
        if d0 == d1:
            out[d0] = out.get(d0, 0) + max(0, e_min - s_min - brk)
            continue
        # 日またぎ: 開始日は 24:00 まで、終了日は 00:00 から
        first = max(0, 1440 - s_min)
        second = e_min
        # 休憩を開始日から引き、足りない分を終了日から引く
        take = min(brk, first)
        first -= take
        second = max(0, second - (brk - take))
        out[d0] = out.get(d0, 0) + first
        if second > 0:
            out[d1] = out.get(d1, 0) + second
    return out


def exceeds_weekly_cap(day_minutes, target_day=None, cap_minutes=WEEKLY_CAP_MINUTES):
    """連続7日間の合計が cap を超える窓を探す。

    戻り値: 超過する窓があれば (窓開始日, 窓終了日, 合計分)、無ければ None。
    超過する窓が複数あるときは合計が最大の窓を返す（店長には最も厳しい窓を見せる）。

    target_day を渡すと、その日を含む7通りの窓だけを調べる。シフトを1件足すときは
    その日を含む窓しか合計が変わらないため、これで十分。
    省略すると、データに現れるすべての日を起点とする窓を調べる（生成結果全体の検証）。
    """
    if not day_minutes:
        return None
    if target_day:
        base = date.fromisoformat(target_day)
        starts = [base - timedelta(days=i) for i in range(_WINDOW_DAYS)]
    else:
        starts = [date.fromisoformat(d) for d in day_minutes]
    worst = None
    for st in starts:
        total = 0
        for i in range(_WINDOW_DAYS):
            total += day_minutes.get((st + timedelta(days=i)).isoformat(), 0)
        if total > cap_minutes and (worst is None or total > worst[2]):
            worst = (st.isoformat(), (st + timedelta(days=_WINDOW_DAYS - 1)).isoformat(), total)
    return worst
