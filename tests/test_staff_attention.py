"""tests/test_staff_attention.py — スタッフの働き方の変化を検出する純関数。

実行: ./.venv/bin/python -m pytest tests/test_staff_attention.py -v

設計書: docs/superpowers/specs/2026-08-02-staff-attention-design.md

この関数が返すのは「データがこう変わった」という事実だけで、原因や状態
（離職・体調など）は判定しない。判定しているように見える戻り値を足さないこと。
"""
from datetime import date, timedelta

import pytest

from staff_attention import (
    MIN_BASE_ATTENDANCE, DROP_RATIO, MIN_RECENT_REQUESTS, REQUEST_SPIKE_RATIO,
    find_attention,
)

TODAY = "2026-08-31"


def _d(days_ago):
    """TODAY から days_ago 日前の "YYYY-MM-DD"。"""
    return (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()


def _staff(sid, name, resigned=0):
    return {"id": sid, "name": name, "is_resigned": resigned}


def _shifts(staff_id, days_ago_list):
    """指定の「何日前」に confirmed シフトが1本ずつあることにする。"""
    return [{"staff_id": staff_id, "start_datetime": f"{_d(x)}T09:00:00"} for x in days_ago_list]


def _requests(staff_id, days_ago_list):
    return [{"staff_id": staff_id, "created_at": f"{_d(x)}T12:00:00"} for x in days_ago_list]


class TestAttendanceDrop:
    def test_clear_drop_is_detected(self):
        """基準30日あたり10日 → 直近3日。誰が見ても減っている。"""
        # 直近30日(0-29日前)に3日、その前60日(30-89日前)に20日 → base=10.0
        recent = [1, 5, 9]
        base = list(range(30, 50))
        r = find_attention([_staff(1, "田中")], _shifts(1, recent + base), [], TODAY)
        assert len(r) == 1
        reason = r[0]["reasons"][0]
        assert reason["type"] == "attendance_drop"
        assert reason["recent"] == 3
        assert reason["base"] == 10.0

    def test_ratio_boundary_is_not_detected(self):
        """ちょうど DROP_RATIO ぶん残っていれば「減った」としない。

        base=10.0 に対し recent=6（= 10.0*0.6）。境界は検出しない側に倒す
        （わずかな揺れで毎月名前が出ると、カードが出ること自体の意味が薄れる）。
        """
        base = list(range(30, 50))          # 20日 → base 10.0
        recent = [1, 3, 5, 7, 9, 11]        # 6日
        r = find_attention([_staff(1, "田中")], _shifts(1, recent + base), [], TODAY)
        assert r == []

    def test_just_below_boundary_is_detected(self):
        base = list(range(30, 50))          # base 10.0
        recent = [1, 3, 5, 7, 9]            # 5日 < 6
        r = find_attention([_staff(1, "田中")], _shifts(1, recent + base), [], TODAY)
        assert len(r) == 1

    def test_infrequent_staff_is_ignored(self):
        """もともと月1〜2日の人は対象外（0日になっても騒がない）。"""
        base = [35, 60]                      # 2日 → base 1.0（MIN_BASE_ATTENDANCE 未満）
        r = find_attention([_staff(1, "田中")], _shifts(1, base), [], TODAY)
        assert r == []

    def test_same_day_two_shifts_count_as_one(self):
        """同じ日に2本入っていても1日と数える（中抜けを二重に数えない）。"""
        base = [{"staff_id": 1, "start_datetime": f"{_d(x)}T09:00:00"} for x in range(30, 50)]
        base += [{"staff_id": 1, "start_datetime": f"{_d(x)}T18:00:00"} for x in range(30, 50)]
        # 直近は 5日 × 2本
        recent = [{"staff_id": 1, "start_datetime": f"{_d(x)}T09:00:00"} for x in (1, 3, 5, 7, 9)]
        recent += [{"staff_id": 1, "start_datetime": f"{_d(x)}T18:00:00"} for x in (1, 3, 5, 7, 9)]
        r = find_attention([_staff(1, "田中")], base + recent, [], TODAY)
        assert len(r) == 1
        assert r[0]["reasons"][0]["recent"] == 5
        assert r[0]["reasons"][0]["base"] == 10.0

    def test_resigned_staff_is_ignored(self):
        base = list(range(30, 50))
        r = find_attention([_staff(1, "田中", resigned=1)], _shifts(1, base), [], TODAY)
        assert r == []

    def test_new_staff_without_history_is_ignored(self):
        """基準期間に1日も出ていない人は比べる過去がない（入ったばかり）。"""
        r = find_attention([_staff(1, "新人")], _shifts(1, [1, 2, 3]), [], TODAY)
        assert r == []

    def test_new_staff_with_many_requests_is_ignored(self):
        """出勤実績がない人は、申請が多くても対象にしない。

        出勤の少なさは MIN_BASE_ATTENDANCE でも弾かれるが、申請の増加は
        別条件なので、出勤実績を見るガードが無いと新人が申請だけで挙がる。
        入ったばかりで予定が固まっていないだけかもしれず、比べる過去がない。
        """
        r = find_attention([_staff(1, "新人")], _shifts(1, [1, 2, 3]),
                           _requests(1, [2, 8, 14]), TODAY)
        assert r == []

    def test_no_shifts_at_all_is_ignored(self):
        r = find_attention([_staff(1, "田中")], [], [], TODAY)
        assert r == []


class TestRequestSpike:
    def test_spike_from_zero_is_detected(self):
        """それまで変更申請ゼロの人が直近30日で3件。"""
        base = list(range(30, 50))  # 出勤は十分あって減っていない
        recent = list(range(0, 20, 2))
        shifts = _shifts(1, recent + base)
        r = find_attention([_staff(1, "田中")], shifts, _requests(1, [2, 8, 14]), TODAY)
        assert len(r) == 1
        reason = [x for x in r[0]["reasons"] if x["type"] == "request_spike"][0]
        assert reason["recent"] == 3
        assert reason["base"] == 0.0

    def test_below_minimum_is_ignored(self):
        """2件では騒がない（たまたま重なることがある）。"""
        base = list(range(30, 50))
        recent = list(range(0, 20, 2))
        shifts = _shifts(1, recent + base)
        r = find_attention([_staff(1, "田中")], shifts, _requests(1, [2, 8]), TODAY)
        assert r == []

    def test_same_level_as_before_is_ignored(self):
        """もともと申請が多い人は、同じ水準なら「増えた」としない。"""
        base = list(range(30, 50))
        recent = list(range(0, 20, 2))
        shifts = _shifts(1, recent + base)
        # 基準60日で8件 → base 4.0。直近4件は 2倍(8件)に届かない
        reqs = _requests(1, [2, 8, 14, 20]) + _requests(1, [32, 38, 44, 50, 56, 62, 68, 74])
        r = find_attention([_staff(1, "田中")], shifts, reqs, TODAY)
        assert r == []

    def test_doubled_is_detected(self):
        base = list(range(30, 50))
        recent = list(range(0, 20, 2))
        shifts = _shifts(1, recent + base)
        # 基準60日で4件 → base 2.0。直近5件は 2.0*2=4.0 以上
        reqs = _requests(1, [1, 5, 9, 13, 17]) + _requests(1, [32, 40, 50, 60])
        r = find_attention([_staff(1, "田中")], shifts, reqs, TODAY)
        assert len(r) == 1
        assert r[0]["reasons"][0]["type"] == "request_spike"


class TestOrderingAndShape:
    def test_sorted_by_severity_then_staff_id(self):
        """変化の大きい順。同点は staff_id 昇順（表示順が毎回変わらないこと）。"""
        base = list(range(30, 50))          # base 10.0
        shifts = _shifts(1, [1] + base) + _shifts(2, [1, 3, 5] + base) + _shifts(3, [1] + base)
        staffs = [_staff(3, "C"), _staff(1, "A"), _staff(2, "B")]
        r = find_attention(staffs, shifts, [], TODAY)
        # 1日まで減った A(1) と C(3) が同点で先、次に 3日の B(2)
        assert [x["staff_id"] for x in r] == [1, 3, 2]

    def test_result_has_no_diagnosis_fields(self):
        """原因や状態を断定するフィールドを持たない（設計書のスコープ）。"""
        base = list(range(30, 50))
        r = find_attention([_staff(1, "田中")], _shifts(1, [1] + base), [], TODAY)
        assert set(r[0].keys()) == {"staff_id", "name", "reasons", "score"}

    def test_empty_inputs(self):
        assert find_attention([], [], [], TODAY) == []
