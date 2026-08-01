"""tests/test_wish_image_import.py — 希望表の画像取込。

実行: ./.venv/bin/python -m pytest tests/test_wish_image_import.py -v

外部APIは叩かない。_call_llm_messages を monkeypatch して検証ロジックだけを見る
（tests/test_wish_text_import.py の _use_llm と同じ作法）。
"""
import json

import pytest

from src import ai

_PNG_1PX = ("data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _use_vision(monkeypatch, response, capture=None):
    """LLM を有効にし、_call_llm_messages の戻り値を差し替える。"""
    monkeypatch.setattr(ai, "is_llm_available", lambda: True)

    def fake(messages, *a, **k):
        if capture is not None:
            capture.append(messages)
        return response

    monkeypatch.setattr(ai, "_call_llm_messages", fake)


class TestParseWishImage:
    def test_returns_none_when_llm_unavailable(self, monkeypatch):
        """AI未接続では画像を読めない。正規表現フォールバックは存在しない。"""
        monkeypatch.setattr(ai, "is_llm_available", lambda: False)
        assert ai.parse_wish_image([_PNG_1PX], "2026-08", []) is None

    def test_parses_valid_response(self, monkeypatch):
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "田中 8/3 休み",
            "entries": [{"staff_hint": "田中", "dates": ["2026-08-03"],
                         "availability": "rest", "start": None, "end": None,
                         "raw": "田中 8/3 休み"}],
            "unparsed": [],
        }))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", ["田中太郎"])
        assert r["entries"][0]["dates"] == ["2026-08-03"]
        assert r["entries"][0]["availability"] == "rest"
        assert r["ocr_text"] == "田中 8/3 休み"

    def test_image_is_sent_as_vision_content(self, monkeypatch):
        """messages の content が配列で、image_url を含むこと。"""
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert cap, "_call_llm_messages が呼ばれていない"
        user_msgs = [m for m in cap[0] if m.get("role") == "user"]
        assert user_msgs, "user メッセージが無い"
        content = user_msgs[-1]["content"]
        assert isinstance(content, list), "content が配列でない（vision 形式になっていない）"
        assert any(p.get("type") == "image_url" for p in content), "image_url が含まれていない"

    def test_multiple_images_are_all_sent(self, monkeypatch):
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX, _PNG_1PX], "2026-08", [])
        content = [m for m in cap[0] if m.get("role") == "user"][-1]["content"]
        assert sum(1 for p in content if p.get("type") == "image_url") == 2

    def test_non_json_response_does_not_crash(self, monkeypatch):
        _use_vision(monkeypatch, "すみません、読み取れませんでした")
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r is not None
        assert r["entries"] == []
        assert r["unparsed"], "読めなかった旨が unparsed に残っていない"

    def test_none_response_does_not_crash(self, monkeypatch):
        _use_vision(monkeypatch, None)
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r is not None
        assert r["entries"] == []

    def test_missing_ocr_text_defaults_to_empty(self, monkeypatch):
        _use_vision(monkeypatch, json.dumps({"entries": [], "unparsed": []}))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r["ocr_text"] == ""

    def test_invalid_entries_go_to_unparsed(self, monkeypatch):
        """既存の契約（検証で落ちた entry は捨てず unparsed に積む）を守ること。"""
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "x",
            "entries": [{"staff_hint": "田中", "dates": ["めちゃくちゃな日付"],
                         "availability": "rest", "raw": "田中 ??"}],
            "unparsed": [],
        }))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r["entries"] == []
        assert r["unparsed"], "落とした entry が unparsed に残っていない"

    def test_prompt_contains_injection_guard(self, monkeypatch):
        """画像内に書かれた指示に従わない旨がプロンプトに含まれること。"""
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        sys_msgs = [m for m in cap[0] if m.get("role") == "system"]
        assert sys_msgs
        text = json.dumps(sys_msgs, ensure_ascii=False)
        assert "指示" in text, "画像内の指示に従わない旨の記述が無い"


class TestPostLlmTimeout:
    def test_post_llm_accepts_timeout(self):
        """画像は推論が長いので timeout を延ばせること（署名の確認）。"""
        import inspect
        sig = inspect.signature(ai._post_llm)
        assert "timeout" in sig.parameters, "_post_llm に timeout 引数が無い"
