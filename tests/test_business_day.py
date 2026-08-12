"""tests/test_business_day.py - 日をまたぐ営業（例 04:00〜翌02:00）の「営業日」変換。

営業日 = その日の営業が始まってから終わるまでの一続き。04:00〜翌02:00 の店なら
8/17 の営業日は 8/17 04:00 〜 8/18 02:00 で、8/18 00:00〜02:00 の勤務は
「8/17 のシフト」である。カレンダー日と営業日を同一視していたために

  - 17日に 24時（＝翌0時）と入れると 18日のシフトとして扱われる
  - 固定シフト「月曜 00:00〜02:00」が日曜の深夜（月曜0時）に置かれる
  - 22:00〜02:00 が同日終了として保存され表示が壊れる

という3つの不具合が起きていた。ここではその変換の中核だけを検証する。
"""
import pytest

from utils import (
    min_to_iso, day_cutoff_min, business_day_of, combine_dt_business,
    combine_dt_overnight,
)


class TestMinToIso:
    """拡張分（当日0:00起点、翌日以降は +1440）→ 実 ISO datetime。"""

    def test_same_day(self):
        assert min_to_iso("2026-08-17", 9 * 60) == "2026-08-17T09:00:00"

    def test_midnight_is_next_day(self):
        assert min_to_iso("2026-08-17", 1440) == "2026-08-18T00:00:00"

    def test_next_day_early_morning(self):
        assert min_to_iso("2026-08-17", 26 * 60) == "2026-08-18T02:00:00"

    def test_two_days_ahead(self):
        """+2880 以上でも1日しか進めない実装だと 24:00 に化ける（旧 _min_to_iso の穴）。"""
        assert min_to_iso("2026-08-17", 2880) == "2026-08-19T00:00:00"

    def test_month_boundary(self):
        assert min_to_iso("2026-08-31", 25 * 60) == "2026-09-01T01:00:00"


class TestDayCutoffMin:
    """営業終了が翌日に食い込む分（＝この分より前の時刻は前営業日）。"""

    def test_overnight_business(self):
        assert day_cutoff_min("04:00", "02:00") == 120

    def test_daytime_business_has_no_cutoff(self):
        assert day_cutoff_min("09:00", "22:00") == 0

    def test_late_night_business(self):
        assert day_cutoff_min("22:00", "02:00") == 120

    def test_equal_times_is_24h_business(self):
        """開始＝終了は24時間営業。深夜帯を前日に寄せると当日が消えるので 0 にする。"""
        assert day_cutoff_min("00:00", "00:00") == 0

    def test_invalid_input_is_zero(self):
        assert day_cutoff_min("", "") == 0
        assert day_cutoff_min(None, None) == 0


class TestBusinessDayOf:
    """実 ISO datetime → その勤務が属する営業日。"""

    def test_late_night_belongs_to_previous_day(self):
        assert business_day_of("2026-08-18T00:30:00", 120) == "2026-08-17"

    def test_boundary_belongs_to_own_day(self):
        """02:00 ちょうどは営業終了。次の営業日側に数える。"""
        assert business_day_of("2026-08-18T02:00:00", 120) == "2026-08-18"

    def test_daytime_belongs_to_own_day(self):
        assert business_day_of("2026-08-18T09:00:00", 120) == "2026-08-18"

    def test_no_cutoff_keeps_calendar_date(self):
        assert business_day_of("2026-08-18T00:30:00", 0) == "2026-08-18"

    def test_month_boundary(self):
        assert business_day_of("2026-09-01T01:00:00", 120) == "2026-08-31"


class TestCombineDtBusiness:
    """営業日 + 時刻 → 実 ISO のペア。"""

    def test_midnight_start_goes_to_next_calendar_day(self):
        """課題1/3の核心。営業日17日の 00:00〜02:00 は 18日 00:00〜02:00。"""
        assert combine_dt_business("2026-08-17", "00:00", "02:00", 120) == (
            "2026-08-18T00:00:00", "2026-08-18T02:00:00")

    def test_overnight_shift(self):
        """課題2。22:00〜02:00 は当日22時〜翌2時。"""
        assert combine_dt_business("2026-08-17", "22:00", "02:00", 120) == (
            "2026-08-17T22:00:00", "2026-08-18T02:00:00")

    def test_daytime_shift_unchanged(self):
        assert combine_dt_business("2026-08-17", "09:00", "18:00", 120) == (
            "2026-08-17T09:00:00", "2026-08-17T18:00:00")

    def test_open_to_close(self):
        """営業時間そのもの（04:00〜02:00）。"""
        assert combine_dt_business("2026-08-17", "04:00", "02:00", 120) == (
            "2026-08-17T04:00:00", "2026-08-18T02:00:00")

    def test_extended_hour_notation(self):
        """「24:00」「26:00」表記も受ける（希望取り込みで LLM が返しうる）。"""
        assert combine_dt_business("2026-08-17", "24:00", "26:00", 120) == (
            "2026-08-18T00:00:00", "2026-08-18T02:00:00")

    def test_cutoff_zero_matches_overnight_helper(self):
        """cutoff=0（日をまたがない店）は既存の combine_dt_overnight と完全一致。"""
        for s, e in [("09:00", "18:00"), ("22:00", "05:00"), ("00:00", "02:00"),
                     ("17:00", "17:00"), ("7:00", "9:00")]:
            assert combine_dt_business("2026-08-17", s, e, 0) == \
                combine_dt_overnight("2026-08-17", s, e)

    def test_same_start_and_end_is_24h(self):
        assert combine_dt_business("2026-08-17", "10:00", "10:00", 120) == (
            "2026-08-17T10:00:00", "2026-08-18T10:00:00")

    def test_start_before_cutoff_end_after(self):
        """00:00〜05:00（営業終了02:00をまたぐ）。開始が深夜側なので両方翌日。"""
        assert combine_dt_business("2026-08-17", "00:00", "05:00", 120) == (
            "2026-08-18T00:00:00", "2026-08-18T05:00:00")

    def test_zero_padding(self):
        assert combine_dt_business("2026-08-17", "7:00", "9:30", 120) == (
            "2026-08-17T07:00:00", "2026-08-17T09:30:00")
