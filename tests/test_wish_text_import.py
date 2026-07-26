"""tests/test_wish_text_import.py — 希望テキスト取り込みのテスト。

実行: ./.venv/bin/python -m pytest tests/test_wish_text_import.py -v

解析は LLM を使わないフォールバック経路のみを検証する（外部APIに依存させない）。
LLM 経路は本番でのみ動き、失敗時は自動でフォールバックに落ちる設計。
"""
import pytest
from src import ai


class TestParseWishFallback:
    """正規表現ベースの解析。LLM 未設定でも機能が死なないことを保証する。"""

    def test_single_date_rest(self):
        r = ai._parse_wish_fallback("8/3は休みたいです", "2026-08")
        assert len(r["entries"]) == 1
        e = r["entries"][0]
        assert e["dates"] == ["2026-08-03"]
        assert e["availability"] == "rest"
        assert "8/3" in e["raw"]

    def test_multiple_dates_same_content(self):
        r = ai._parse_wish_fallback("8/3、8/5、8/7 は17時から22時まで入れます", "2026-08")
        e = r["entries"][0]
        assert e["dates"] == ["2026-08-03", "2026-08-05", "2026-08-07"]
        assert e["availability"] == "time"
        assert e["start"] == "17:00"
        assert e["end"] == "22:00"

    def test_different_content_splits_entries(self):
        r = ai._parse_wish_fallback("8/1 9-17\n8/3 13-22", "2026-08")
        assert len(r["entries"]) == 2
        assert r["entries"][0]["start"] == "09:00"
        assert r["entries"][1]["start"] == "13:00"

    def test_date_range(self):
        r = ai._parse_wish_fallback("8/10〜8/12 は休みです", "2026-08")
        assert r["entries"][0]["dates"] == ["2026-08-10", "2026-08-11", "2026-08-12"]
        assert r["entries"][0]["availability"] == "rest"

    def test_any_availability(self):
        r = ai._parse_wish_fallback("8/15 終日OK", "2026-08")
        assert r["entries"][0]["availability"] == "any"

    def test_unparsed_line_is_kept(self):
        r = ai._parse_wish_fallback("よろしくお願いします", "2026-08")
        assert r["entries"] == []
        assert "よろしくお願いします" in r["unparsed"]

    def test_empty_text_does_not_crash(self):
        r = ai._parse_wish_fallback("", "2026-08")
        assert r["entries"] == []

    def test_staff_hint_extracted(self):
        r = ai._parse_wish_fallback("小久保: 8/3休み", "2026-08", ["小久保", "佐藤"])
        assert r["entries"][0]["staff_hint"] == "小久保"

    def test_source_is_fallback(self):
        r = ai._parse_wish_fallback("8/3は休み", "2026-08")
        assert r["source"] == "fallback"


class TestParseWishText:
    """LLM が使えない環境では自動でフォールバックに落ちること。"""

    def test_falls_back_when_llm_unavailable(self, monkeypatch):
        monkeypatch.setattr(ai, "is_llm_available", lambda: False)
        r = ai.parse_wish_text("8/3は休み", "2026-08")
        assert r["source"] == "fallback"
        assert r["entries"][0]["availability"] == "rest"
