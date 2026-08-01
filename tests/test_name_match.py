"""tests/test_name_match.py — 希望表の名前をスタッフに突き合わせる純関数。

実行: ./.venv/bin/python -m pytest tests/test_name_match.py -v

背景: 従来は by_name の完全一致のみ（src/app.py:3347）だったため、
「田中さん」「タナカ」のような表記ゆれが全部未割り当てになっていた。
OCR は表記ゆれ・誤認識を起こしやすいので、候補を出す仕組みが要る。

不変量: 同点の最有力候補が2件以上あるときは自動確定しない。
既存の _extract_staff_hint（src/ai.py:1087）が守っている
「名簿順で勝手に決めない」原則を引き継ぐ。
"""
import pytest

from src.name_match import normalize_name, match_staff, best_exact


def _staffs(*names):
    return [{"id": i + 1, "name": n} for i, n in enumerate(names)]


class TestNormalizeName:
    def test_full_width_becomes_half_width(self):
        assert normalize_name("ﾀﾅｶ") == normalize_name("タナカ")

    def test_spaces_are_removed(self):
        assert normalize_name("田中 太郎") == normalize_name("田中　太郎") == normalize_name("田中太郎")

    def test_honorifics_are_removed(self):
        base = normalize_name("田中")
        for suffix in ("さん", "サン", "くん", "君", "ちゃん", "様", "氏"):
            assert normalize_name("田中" + suffix) == base, f"{suffix} が除去されていない"

    def test_hiragana_and_katakana_match(self):
        assert normalize_name("たなか") == normalize_name("タナカ")

    def test_case_is_folded(self):
        assert normalize_name("Tanaka") == normalize_name("TANAKA")

    def test_empty_and_none_do_not_crash(self):
        assert normalize_name("") == ""
        assert normalize_name(None) == ""


class TestBestExact:
    def test_normalized_exact_match_resolves(self):
        st = _staffs("田中太郎", "佐藤花子")
        assert best_exact("田中太郎さん", st) == 1
        assert best_exact("たなかたろう", st) is None  # 読みは完全一致ではない

    def test_two_identical_names_do_not_resolve(self):
        """同姓同名が2人いたら自動確定しない（誤配属を防ぐ）。"""
        st = _staffs("田中", "田中")
        assert best_exact("田中", st) is None

    def test_no_match_returns_none(self):
        assert best_exact("存在しない", _staffs("田中太郎")) is None

    def test_empty_hint_returns_none(self):
        assert best_exact("", _staffs("田中太郎")) is None
        assert best_exact(None, _staffs("田中太郎")) is None


class TestMatchStaff:
    def test_exact_match_scores_highest(self):
        r = match_staff("田中太郎", _staffs("田中太郎", "田中花子"))
        assert r[0]["staff_id"] == 1
        assert r[0]["score"] == 1.0

    def test_surname_only_is_a_candidate(self):
        r = match_staff("田中", _staffs("田中太郎", "佐藤花子"))
        assert r, "姓のみでも候補に挙がるべき"
        assert r[0]["staff_id"] == 1
        assert 0.6 <= r[0]["score"] < 1.0

    def test_given_name_only_is_a_candidate(self):
        r = match_staff("太郎", _staffs("田中太郎", "佐藤花子"))
        assert r
        assert r[0]["staff_id"] == 1

    def test_typo_within_edit_distance_is_a_candidate(self):
        r = match_staff("田中太朗", _staffs("田中太郎", "佐藤花子"))
        assert r
        assert r[0]["staff_id"] == 1

    def test_unrelated_name_is_not_a_candidate(self):
        r = match_staff("山田", _staffs("田中太郎", "佐藤花子"))
        assert not r, f"無関係な名前が候補に出た: {r}"

    def test_results_are_sorted_by_score_desc(self):
        r = match_staff("田中", _staffs("佐藤花子", "田中太郎", "田中"))
        scores = [c["score"] for c in r]
        assert scores == sorted(scores, reverse=True)

    def test_each_candidate_has_a_reason(self):
        r = match_staff("田中", _staffs("田中太郎"))
        assert r[0]["reason"], "候補に理由が付いていないと UI で説明できない"

    def test_empty_hint_returns_empty(self):
        assert match_staff("", _staffs("田中太郎")) == []
        assert match_staff(None, _staffs("田中太郎")) == []

    def test_resigned_staff_are_not_passed_in(self):
        """呼び出し側が在籍者だけを渡す契約であることを、空リストで確認する。"""
        assert match_staff("田中", []) == []
