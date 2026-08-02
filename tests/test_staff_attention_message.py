"""tests/test_staff_attention_message.py — 声かけ文言の生成。

実行: ./.venv/bin/python -m pytest tests/test_staff_attention_message.py -v

conftest が LLM_API_KEY="" にするため、既定ではフォールバック経路を通る。
LLM経路は monkeypatch で差し替えて検証する。

不変量: 原因や状態を断定する語（離職・メンタル・やる気など）を出力しない。
勤務データから分かるのは「働き方が変わった」事実だけで、断定は店長に
外れた決めつけを渡すことになる（設計書「スコープを明確に区切る」）。
"""
import pytest

from src import ai

# 出してはいけない語。プロンプト・フォールバック文言・LLM出力の検査に使う。
FORBIDDEN = ["離職", "退職", "メンタル", "やる気", "不満", "病気", "うつ"]

DROP = [{"type": "attendance_drop", "recent": 4, "base": 10.0}]
SPIKE = [{"type": "request_spike", "recent": 5, "base": 1.0}]


class TestFallback:
    def test_drop_returns_rule_based_message(self):
        msg, source = ai.suggest_attention_message("田中太郎", DROP)
        assert source == "rule_based"
        assert msg

    def test_spike_returns_rule_based_message(self):
        msg, source = ai.suggest_attention_message("田中太郎", SPIKE)
        assert source == "rule_based"
        assert msg

    def test_fallback_has_no_diagnosis_words(self):
        for reasons in (DROP, SPIKE):
            msg, _ = ai.suggest_attention_message("田中太郎", reasons)
            for w in FORBIDDEN:
                assert w not in msg, f"定型文に断定的な語が入っている: {w} / {msg}"

    def test_unknown_reason_type_does_not_crash(self):
        msg, source = ai.suggest_attention_message("田中太郎", [{"type": "unknown_thing"}])
        assert isinstance(msg, str)

    def test_empty_reasons_does_not_crash(self):
        msg, source = ai.suggest_attention_message("田中太郎", [])
        assert isinstance(msg, str)


class TestLlmPath:
    def _use_llm(self, monkeypatch, reply, capture=None):
        monkeypatch.setattr(ai, "is_llm_available", lambda: True)

        def fake(system_prompt, user_prompt, temperature=0.3):
            if capture is not None:
                capture.append((system_prompt, user_prompt))
            return reply

        monkeypatch.setattr(ai, "call_llm", fake)

    def test_llm_reply_is_used(self, monkeypatch):
        self._use_llm(monkeypatch, "最近シフトが少なめですが、ご都合はいかがですか。")
        msg, source = ai.suggest_attention_message("田中太郎", DROP)
        assert source == "llm"
        assert "ご都合" in msg

    def test_prompt_forbids_diagnosis(self, monkeypatch):
        """断定を禁じる指示がプロンプトに入っていること。"""
        cap = []
        self._use_llm(monkeypatch, "ok", cap)
        ai.suggest_attention_message("田中太郎", DROP)
        system_prompt = cap[0][0]
        assert "断定" in system_prompt, "原因を断定しない指示がプロンプトに無い"

    def test_llm_output_with_diagnosis_word_is_rejected(self, monkeypatch):
        """LLMが断定的な語を返したら採用せず、定型文へ落とす。

        プロンプトで禁じても、モデルは指示を外すことがある。画面に出る直前で
        機械的に弾かないと、店長に決めつけを渡してしまう。
        """
        self._use_llm(monkeypatch, "田中太郎さんは離職の可能性があります。面談してください。")
        msg, source = ai.suggest_attention_message("田中太郎", DROP)
        assert source == "rule_based", "断定的な語を含むLLM出力が採用されている"
        assert "離職" not in msg

    def test_llm_failure_falls_back(self, monkeypatch):
        self._use_llm(monkeypatch, None)
        msg, source = ai.suggest_attention_message("田中太郎", DROP)
        assert source == "rule_based"
        assert msg
