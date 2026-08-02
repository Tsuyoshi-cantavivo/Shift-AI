"""tests/test_weekly_hours.py — 外国籍アルバイトの週28時間上限を判定する純関数。

実行: ./.venv/bin/python -m pytest tests/test_weekly_hours.py -v

背景: 資格外活動許可で働く在留資格（留学・家族滞在）は入管法上1週間28時間以内。
超えると本人の在留資格だけでなく、雇用主も不法就労助長罪の対象になる。

不変量: 「1週間」は暦週ではなく **任意の連続7日間**。入管の運用が
「どの曜日から起算しても1週間28時間以内」であるため、暦週で数えると
前の週の後半に28h・次の週の前半に28h という組み方（連続7日で56h）が
素通りする。この抜け穴を塞ぐことがこのモジュールの存在理由。
"""
from datetime import date

import pytest

from weekly_hours import WEEKLY_CAP_MINUTES, minutes_by_day, exceeds_weekly_cap


def _span(day, start_hhmm, end_hhmm, brk=0, end_day=None):
    """(start_iso, end_iso, break_minutes) を組み立てる。"""
    return (f"{day}T{start_hhmm}:00", f"{end_day or day}T{end_hhmm}:00", brk)


class TestMinutesByDay:
    def test_single_shift(self):
        assert minutes_by_day([_span("2026-08-10", "09:00", "17:00")]) == {"2026-08-10": 480}

    def test_break_is_subtracted(self):
        """28hは実労働時間の上限。休憩込みで数えると本来働ける時間を不当に削る。"""
        assert minutes_by_day([_span("2026-08-10", "09:00", "18:00", brk=60)]) == {"2026-08-10": 480}

    def test_same_day_shifts_are_summed(self):
        r = minutes_by_day([
            _span("2026-08-10", "09:00", "12:00"),
            _span("2026-08-10", "18:00", "21:00"),
        ])
        assert r == {"2026-08-10": 360}

    def test_overnight_shift_is_split_by_day(self):
        """日をまたぐシフトは各日に分けて数える（実労働時間で見るため）。"""
        r = minutes_by_day([_span("2026-08-10", "22:00", "06:00", end_day="2026-08-11")])
        assert r == {"2026-08-10": 120, "2026-08-11": 360}

    def test_overnight_break_comes_off_the_first_day(self):
        r = minutes_by_day([_span("2026-08-10", "22:00", "06:00", brk=60, end_day="2026-08-11")])
        assert r == {"2026-08-10": 60, "2026-08-11": 360}

    def test_overnight_break_longer_than_first_day_spills_over(self):
        """開始日の分を超える休憩は翌日から引く（開始日が負にならないこと）。"""
        r = minutes_by_day([_span("2026-08-10", "23:30", "06:00", brk=60, end_day="2026-08-11")])
        assert r == {"2026-08-10": 0, "2026-08-11": 330}

    def test_ends_exactly_at_midnight_does_not_touch_next_day(self):
        r = minutes_by_day([_span("2026-08-10", "20:00", "00:00", end_day="2026-08-11")])
        assert r == {"2026-08-10": 240}

    def test_empty_input(self):
        assert minutes_by_day([]) == {}


class TestExceedsWeeklyCap:
    def test_under_cap_returns_none(self):
        dm = {"2026-08-10": 27 * 60 + 54}  # 27.9h
        assert exceeds_weekly_cap(dm) is None

    def test_exactly_at_cap_is_allowed(self):
        """ちょうど28時間は「28時間以内」なので許される。"""
        dm = {"2026-08-10": WEEKLY_CAP_MINUTES}
        assert exceeds_weekly_cap(dm) is None

    def test_one_minute_over_is_detected(self):
        dm = {"2026-08-10": WEEKLY_CAP_MINUTES + 1}
        hit = exceeds_weekly_cap(dm)
        assert hit is not None
        assert hit[2] == WEEKLY_CAP_MINUTES + 1

    def test_seven_day_window_is_inclusive(self):
        """7日窓は両端を含む（8/10〜8/16 が1つの窓）。"""
        dm = {"2026-08-10": 14 * 60, "2026-08-16": 15 * 60}  # 計29h
        assert exceeds_weekly_cap(dm) is not None

    def test_eight_days_apart_is_not_one_window(self):
        """8日離れていれば同じ窓に入らない（8/10 と 8/17）。"""
        dm = {"2026-08-10": 14 * 60, "2026-08-17": 15 * 60}
        assert exceeds_weekly_cap(dm) is None

    def test_calendar_week_boundary_hole_is_closed(self):
        """暦週で数える実装に退化したら落ちるテスト。

        2026-08-10(月)〜16(日) で28h、17(月)〜23(日) で28h。暦週で見れば
        どちらも上限ちょうどで合法だが、8/14(金)〜8/20(木) の連続7日間は
        28h+28h の大半が集まり28hを超える。入管の運用ではこれは違反。
        """
        dm = {
            "2026-08-14": 14 * 60, "2026-08-15": 14 * 60,   # 前の週の後半に28h
            "2026-08-17": 14 * 60, "2026-08-18": 14 * 60,   # 次の週の前半に28h
        }
        hit = exceeds_weekly_cap(dm)
        assert hit is not None, "暦週でなく任意の連続7日間で数えていない"
        assert hit[2] == 56 * 60

    def test_target_day_limits_windows_to_those_containing_it(self):
        """target_day を渡すと、その日を含む7通りの窓だけを見る。"""
        dm = {"2026-08-01": 40 * 60, "2026-08-20": 60}
        # 8/20 を含む窓（8/14〜8/26 の範囲）に 8/01 は入らない
        assert exceeds_weekly_cap(dm, target_day="2026-08-20") is None
        # target_day 無しなら 8/01 の窓が超過として見つかる
        assert exceeds_weekly_cap(dm) is not None

    def test_returns_the_worst_window(self):
        """超過する窓が複数あるときは合計が最大の窓を返す（店長に最も厳しい窓を見せる）。"""
        dm = {"2026-08-10": 29 * 60, "2026-08-20": 35 * 60}
        hit = exceeds_weekly_cap(dm)
        assert hit[2] == 35 * 60
        assert hit[0] <= "2026-08-20" <= hit[1]

    def test_window_bounds_are_returned(self):
        dm = {"2026-08-10": 30 * 60}
        start, end, total = exceeds_weekly_cap(dm)
        assert start <= "2026-08-10" <= end
        # 窓は7日間（両端含む）
        d0 = date.fromisoformat(start)
        d1 = date.fromisoformat(end)
        assert (d1 - d0).days == 6

    def test_empty_input(self):
        assert exceeds_weekly_cap({}) is None
        assert exceeds_weekly_cap({}, target_day="2026-08-10") is None

    def test_custom_cap(self):
        dm = {"2026-08-10": 41 * 60}
        assert exceeds_weekly_cap(dm, cap_minutes=40 * 60) is not None
        assert exceeds_weekly_cap(dm, cap_minutes=42 * 60) is None
