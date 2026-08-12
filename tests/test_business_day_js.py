"""tests/test_business_day_js.py — 画面側の営業日変換（純関数）。

public/app.js の businessDayOf / businessTimesToIso を Node で直接実行する
（tests/test_required_bar_geometry.py と同じ作法）。

サーバ側（src/utils.py の business_day_of / combine_dt_business）と**同じ答え**を
返さなければならない。片方だけ直すと、画面が17日の欄に出しているシフトを
サーバは18日として保存する、という一番たちの悪いズレになる。
そのため、ここのケースは tests/test_business_day.py と対にしてある。
"""
import json

from helpers import extract_js_function, run_js
from utils import business_day_of, combine_dt_business

SRC = None


def _src():
    global SRC
    if SRC is None:
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "public/app.js"), encoding="utf-8") as f:
            SRC = f.read()
    return SRC


def _deps(cutoff):
    """対象関数と、その依存（_parseTimeParts / _localDateStr / _extHourToIsoTime）。

    dayCutoffMin は appState を読むので、テストでは値を固定した実装に差し替える。
    """
    return [
        extract_js_function(_src(), "businessDayOf"),
        extract_js_function(_src(), "businessTimesToIso"),
        extract_js_function(_src(), "_parseTimeParts"),
        extract_js_function(_src(), "_localDateStr"),
        extract_js_function(_src(), "_extHourToIsoTime"),
        f"function dayCutoffMin() {{ return {cutoff}; }}",
    ]


def _business_day(iso, cutoff):
    return json.loads(run_js(_deps(cutoff), f"JSON.stringify(businessDayOf({json.dumps(iso)}))"))


def _span(day, start, end, cutoff):
    out = run_js(_deps(cutoff),
                 f"JSON.stringify(businessTimesToIso({json.dumps(day)}, "
                 f"{json.dumps(start)}, {json.dumps(end)}))")
    d = json.loads(out)
    return (d["start"], d["end"])


class TestBusinessDayOfJs:
    def test_late_night_belongs_to_previous_day(self):
        assert _business_day("2026-08-18T00:30:00", 120) == "2026-08-17"

    def test_boundary_belongs_to_own_day(self):
        assert _business_day("2026-08-18T02:00:00", 120) == "2026-08-18"

    def test_no_cutoff_keeps_calendar_date(self):
        assert _business_day("2026-08-18T00:30:00", 0) == "2026-08-18"

    def test_month_boundary(self):
        assert _business_day("2026-09-01T01:00:00", 120) == "2026-08-31"

    def test_matches_server(self):
        for iso in ["2026-08-18T00:00:00", "2026-08-18T01:59:00", "2026-08-18T02:00:00",
                    "2026-08-18T09:00:00", "2026-09-01T00:30:00", "2026-01-01T00:30:00"]:
            for cutoff in (0, 120, 300):
                assert _business_day(iso, cutoff) == business_day_of(iso, cutoff), (iso, cutoff)


class TestBusinessTimesToIsoJs:
    def test_midnight_start_goes_to_next_calendar_day(self):
        assert _span("2026-08-17", "00:00", "02:00", 120) == \
            ("2026-08-18T00:00:00", "2026-08-18T02:00:00")

    def test_overnight_shift(self):
        """課題2の本体。手動追加で 22:00〜02:00 と入れたときの保存値。"""
        assert _span("2026-08-17", "22:00", "02:00", 120) == \
            ("2026-08-17T22:00:00", "2026-08-18T02:00:00")

    def test_overnight_shift_without_cutoff(self):
        """日中営業の店でも 22:00〜02:00 は翌日終了（終了 <= 開始 なら翌日）。"""
        assert _span("2026-08-17", "22:00", "02:00", 0) == \
            ("2026-08-17T22:00:00", "2026-08-18T02:00:00")

    def test_daytime_shift_unchanged(self):
        assert _span("2026-08-17", "09:00", "18:00", 120) == \
            ("2026-08-17T09:00:00", "2026-08-17T18:00:00")

    def test_matches_server(self):
        cases = [("09:00", "18:00"), ("22:00", "02:00"), ("00:00", "02:00"),
                 ("04:00", "02:00"), ("01:00", "05:00"), ("10:00", "10:00")]
        for cutoff in (0, 120):
            for s, e in cases:
                assert _span("2026-08-17", s, e, cutoff) == \
                    combine_dt_business("2026-08-17", s, e, cutoff), (s, e, cutoff)
