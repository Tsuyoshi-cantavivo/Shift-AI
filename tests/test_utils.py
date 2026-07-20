"""tests/test_utils.py - utils.py のユニットテスト（時刻・休憩・日付・ICS等）。"""
from datetime import datetime
import pytest

from utils import (
    jst_now, jst_today, combine_dt, parse_iso, normalize_iso,
    minutes_between, compute_break_minutes, night_minutes, add_days,
    weekday_sun0, max_consecutive_run, time_overlaps, shift_covers_pattern,
    covers_pattern_substantial, calc_next_period, validate_password,
    parse_settings, build_ics, _hhmm_to_min,
    norm_hhmm, norm_dt_iso, combine_dt_overnight,
)


class TestJst:
    def test_jst_now_no_tzinfo(self):
        """jst_now は tzinfo を持たない naive datetime。"""
        n = jst_now()
        assert n.tzinfo is None

    def test_jst_now_is_jst(self):
        """UTC との差が +9 時間。"""
        from datetime import timezone, timedelta
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        # 誤差 10 秒以内で JST (UTC+9)
        diff = (jst_now() - utc_now).total_seconds()
        assert 9 * 3600 - 30 < diff < 9 * 3600 + 30

    def test_jst_today_is_date(self):
        d = jst_today()
        assert hasattr(d, "year") and hasattr(d, "month") and hasattr(d, "day")


class TestParseIso:
    def test_parse_seconds(self):
        d = parse_iso("2026-08-05T09:00:00")
        assert d.year == 2026 and d.month == 8 and d.day == 5
        assert d.hour == 9 and d.minute == 0

    def test_parse_no_seconds(self):
        d = parse_iso("2026-08-05T09:00")
        assert d.hour == 9 and d.second == 0

    def test_both_formats_equal(self):
        assert parse_iso("2026-08-05T09:00:00") == parse_iso("2026-08-05T09:00")

    def test_parse_empty_raises(self):
        with pytest.raises(ValueError):
            parse_iso("")
        with pytest.raises(ValueError):
            parse_iso(None)

    def test_parse_invalid_format_raises(self):
        for bad in ["invalid", "2026/08/05 09:00", "2026-8-5T9:0", "abcd-ef-ghTij:kl"]:
            with pytest.raises(ValueError):
                parse_iso(bad)


class TestNormalizeIso:
    def test_normalize_no_seconds(self):
        assert normalize_iso("2026-08-05T09:00") == "2026-08-05T09:00:00"

    def test_normalize_with_seconds_unchanged(self):
        assert normalize_iso("2026-08-05T09:00:30") == "2026-08-05T09:00:30"

    def test_normalize_none(self):
        assert normalize_iso(None) is None
        assert normalize_iso("") == ""


class TestMinutesBetween:
    def test_simple(self):
        assert minutes_between("2026-08-05T09:00:00", "2026-08-05T12:00:00") == 180

    def test_zero(self):
        assert minutes_between("2026-08-05T09:00:00", "2026-08-05T09:00:00") == 0

    def test_cross_day(self):
        assert minutes_between("2026-08-05T22:00:00", "2026-08-06T02:00:00") == 240

    def test_negative(self):
        """end < start でも計算はする（符号負）。"""
        assert minutes_between("2026-08-05T12:00:00", "2026-08-05T09:00:00") == -180


class TestComputeBreak:
    def test_6h_exact_zero(self):
        assert compute_break_minutes(6 * 60) == 0

    def test_6h_plus_one_45(self):
        assert compute_break_minutes(6 * 60 + 1) == 45

    def test_8h_exact_45(self):
        assert compute_break_minutes(8 * 60) == 45

    def test_8h_plus_one_60(self):
        assert compute_break_minutes(8 * 60 + 1) == 60

    def test_zero(self):
        assert compute_break_minutes(0) == 0

    def test_negative(self):
        """負数は 0 になる（どの閾値も超えない）。"""
        assert compute_break_minutes(-100) == 0

    def test_5h_zero(self):
        assert compute_break_minutes(5 * 60) == 0

    def test_12h_60(self):
        assert compute_break_minutes(12 * 60) == 60

    def test_huge(self):
        assert compute_break_minutes(24 * 60) == 60


class TestNightMinutes:
    def test_no_overlap(self):
        """9-18 は深夜帯と重ならない → 0。"""
        assert night_minutes("2026-08-05T09:00:00", "2026-08-05T18:00:00") == 0

    def test_within_night(self):
        """22-翌5 全範囲 = 7h = 420分。"""
        assert night_minutes("2026-08-05T22:00:00", "2026-08-06T05:00:00") == 420

    def test_partial_overlap_before(self):
        """20-23 → 1h (22-23) の深夜。"""
        assert night_minutes("2026-08-05T20:00:00", "2026-08-05T23:00:00") == 60

    def test_partial_overlap_after(self):
        """翌4-翌8 → 1h (4-5) の深夜。【回帰: 旧バグで前日深夜帯を見逃していた】。"""
        assert night_minutes("2026-08-06T04:00:00", "2026-08-06T08:00:00") == 60

    def test_end_before_start_returns_zero(self):
        assert night_minutes("2026-08-05T12:00:00", "2026-08-05T10:00:00") == 0

    def test_multi_day(self):
        """2日間連続の深夜帯 = 840分。"""
        assert night_minutes("2026-08-05T22:00:00", "2026-08-07T05:00:00") == 840


class TestAddDays:
    def test_add_zero(self):
        assert add_days("2026-08-05", 0) == "2026-08-05"

    def test_add_positive(self):
        assert add_days("2026-08-05", 7) == "2026-08-12"

    def test_add_negative(self):
        assert add_days("2026-08-05", -7) == "2026-07-29"

    def test_cross_month(self):
        assert add_days("2026-08-31", 1) == "2026-09-01"

    def test_cross_year(self):
        assert add_days("2026-12-31", 1) == "2027-01-01"

    def test_leap_year(self):
        assert add_days("2024-02-28", 1) == "2024-02-29"


class TestWeekdaySun0:
    def test_sunday(self):
        assert weekday_sun0(datetime(2026, 8, 2).date()) == 0

    def test_monday(self):
        assert weekday_sun0(datetime(2026, 8, 3).date()) == 1

    def test_saturday(self):
        assert weekday_sun0(datetime(2026, 8, 1).date()) == 6


class TestMaxConsecutiveRun:
    def test_empty(self):
        assert max_consecutive_run([]) == 0
        assert max_consecutive_run(set()) == 0

    def test_single(self):
        assert max_consecutive_run(["2026-08-01"]) == 1

    def test_consecutive(self):
        days = ["2026-08-01", "2026-08-02", "2026-08-03"]
        assert max_consecutive_run(days) == 3

    def test_with_gap(self):
        days = ["2026-08-01", "2026-08-02", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08"]
        assert max_consecutive_run(days) == 4

    def test_unordered(self):
        """順不同でも正しく動作する。"""
        days = ["2026-08-03", "2026-08-01", "2026-08-02"]
        assert max_consecutive_run(days) == 3


class TestTimeOverlaps:
    def test_overlap(self):
        assert time_overlaps("09:00", "13:00", "12:00", "18:00") is True

    def test_no_overlap(self):
        assert time_overlaps("09:00", "13:00", "13:00", "18:00") is False

    def test_adjacent_touch(self):
        """境界接触は重なり無し。"""
        assert time_overlaps("09:00", "12:00", "12:00", "18:00") is False

    def test_cross_midnight_assumes_2359(self):
        """end <= start は 23:59 扱い。"""
        assert time_overlaps("22:00", "02:00", "23:00", "23:30") is True


class TestCoversPattern:
    def test_same_day_same_time(self):
        assert shift_covers_pattern("2026-08-05T09:00:00", "2026-08-05T13:00:00",
                                    "2026-08-05", "09:00", "13:00") is True

    def test_different_day(self):
        assert shift_covers_pattern("2026-08-05T09:00:00", "2026-08-05T13:00:00",
                                    "2026-08-06", "09:00", "13:00") is False

    def test_substantial_50pct(self):
        """50% 以上カバーなら True。"""
        # 9-13 (4h枠) に対し、10-13 は 3h (75%) → True
        assert covers_pattern_substantial("2026-08-05T10:00:00", "2026-08-05T13:00:00",
                                          "2026-08-05", "09:00", "13:00") is True

    def test_substantial_under_50pct(self):
        """50% 未満は False。"""
        # 9-13 に対し、12-13 は 1h (25%) → False
        assert covers_pattern_substantial("2026-08-05T12:00:00", "2026-08-05T13:00:00",
                                          "2026-08-05", "09:00", "13:00") is False


class TestHhmmToMin:
    def test_normal(self):
        assert _hhmm_to_min("09:30") == 570

    def test_zero(self):
        assert _hhmm_to_min("00:00") == 0

    def test_invalid(self):
        assert _hhmm_to_min("invalid") == 0
        assert _hhmm_to_min("") == 0
        assert _hhmm_to_min(None) == 0


class TestCalcNextPeriod:
    def test_half_mode_first_half(self):
        """月初 → その月の16日〜月末。"""
        p = calc_next_period(datetime(2026, 8, 1).date(), "half")
        assert p["start_date"] == "2026-08-16"
        assert p["end_date"] == "2026-08-31"

    def test_half_mode_second_half(self):
        """16日以降 → 翌月1-15日。"""
        p = calc_next_period(datetime(2026, 8, 20).date(), "half")
        assert p["start_date"] == "2026-09-01"
        assert p["end_date"] == "2026-09-15"

    def test_month_mode(self):
        p = calc_next_period(datetime(2026, 8, 5).date(), "month")
        assert p["start_date"] == "2026-09-01"
        assert p["end_date"] == "2026-09-30"

    def test_deadline_is_7_days_before_start(self):
        from datetime import date
        p = calc_next_period(date(2026, 8, 1), "half")
        from utils import add_days
        assert p["deadline"] == add_days(p["start_date"], -7)


class TestValidatePassword:
    def test_valid(self):
        assert validate_password("Abcdef12") is None

    def test_short(self):
        assert validate_password("Ab1") == "パスワードは8文字以上で設定してください"

    def test_empty(self):
        assert validate_password("") is not None
        assert validate_password(None) is not None

    def test_no_alpha(self):
        assert "英字" in validate_password("12345678")

    def test_no_digit(self):
        assert "数字" in validate_password("Abcdefgh")

    def test_exactly_8_ok(self):
        assert validate_password("Abcd1234") is None

    def test_unicode(self):
        """マルチバイト文字を含んでも OK（Python の isalpha は Unicode を英字扱い）。"""
        # 仕様: 英字(Unicode含む) + 数字 + 8文字以上 で有効扱い
        assert validate_password("パスワード123") is None  # 現仕様では有効
        # 純粋な記号のみは無効
        assert validate_password("????????") is not None


class TestParseSettings:
    def test_valid_json(self):
        assert parse_settings('{"a": 1}') == {"a": 1}

    def test_empty_string(self):
        assert parse_settings("") == {}

    def test_none(self):
        assert parse_settings(None) == {}

    def test_invalid_json(self):
        assert parse_settings("not json") == {}


class TestBuildIcs:
    def test_empty_shifts(self):
        ics = build_ics([], "山田", "店舗")
        assert "BEGIN:VCALENDAR" in ics
        assert "END:VCALENDAR" in ics
        assert "VEVENT" not in ics

    def test_confirmed_only(self):
        """status='confirmed' のみ VEVENT に含まれる。"""
        shifts = [
            {"id": 1, "status": "confirmed", "start_datetime": "2026-08-05T09:00:00",
             "end_datetime": "2026-08-05T18:00:00"},
            {"id": 2, "status": "requested", "start_datetime": "2026-08-06T09:00:00",
             "end_datetime": "2026-08-06T18:00:00"},
        ]
        ics = build_ics(shifts, "山田", "店舗")
        assert "20260805" in ics
        assert "20260806" not in ics  # requested は含まれない

    def test_crlf_line_endings(self):
        ics = build_ics([], "x", "y")
        assert "\r\n" in ics


class TestCombineDt:
    def test_combine(self):
        assert combine_dt("2026-08-05", "09:00") == "2026-08-05T09:00:00"

    def test_combine_pads_single_digit_hour(self):
        """【インシデント対策】"7:00" のような非ゼロ埋め時刻でも "07:00" に正規化。"""
        assert combine_dt("2026-08-05", "7:00") == "2026-08-05T07:00:00"
        assert combine_dt("2026-08-05", "4:00") == "2026-08-05T04:00:00"

    def test_combine_dt_overnight_pads_single_digit(self):
        s, e = combine_dt_overnight("2026-08-05", "7:00", "11:00")
        assert s == "2026-08-05T07:00:00"
        assert e == "2026-08-05T11:00:00"

    def test_combine_dt_overnight_next_day(self):
        """翌日またぎ: end <= start なら end は翌日扱い。"""
        s, e = combine_dt_overnight("2026-08-05", "22:00", "02:00")
        assert s == "2026-08-05T22:00:00"
        assert e == "2026-08-06T02:00:00"

    def test_combine_dt_overnight_full(self):
        """Full パターン 04:00-02:00 (翌日またぎ)。"""
        s, e = combine_dt_overnight("2026-08-05", "04:00", "02:00")
        assert s == "2026-08-05T04:00:00"
        assert e == "2026-08-06T02:00:00"


class TestNormTime:
    def test_norm_hhmm_pads(self):
        assert norm_hhmm("7:00") == "07:00"
        assert norm_hhmm("07:00") == "07:00"
        assert norm_hhmm("4:30") == "04:30"
        assert norm_hhmm("") == "00:00"
        assert norm_hhmm(None) == "00:00"
        assert norm_hhmm("invalid") == "00:00"

    def test_norm_dt_iso_pads_hour(self):
        assert norm_dt_iso("2026-08-01T7:00:00") == "2026-08-01T07:00:00"
        assert norm_dt_iso("2026-08-01T04:00:00") == "2026-08-01T04:00:00"
        assert norm_dt_iso("2026-08-01T09:00") == "2026-08-01T09:00:00"

    def test_norm_dt_iso_handles_edge(self):
        assert norm_dt_iso("") == ""
        assert norm_dt_iso(None) is None
        assert norm_dt_iso("invalid") == "invalid"

    def test_norm_dt_iso_no_change_when_valid(self):
        assert norm_dt_iso("2026-08-01T07:00:00") == "2026-08-01T07:00:00"
