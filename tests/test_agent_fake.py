"""
test_agent_fake.py — agent loop tests using a fake/mock provider.

Tests failure modes without real API calls:
  - Provider error handling
  - Turn limit enforcement
  - submit_claims not called → error status
  - Validation errors propagated correctly
"""
import asyncio
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from agent.agent import run_agent, AgentResult


FAKE_ANSWER = "The product has a 1-year warranty. It costs $99."
FAKE_BRAND_ID = "brand_test"
FAKE_ANSWER_ID = "A_FAKE_001"


def _make_tool_call_response(tool_name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }


def _make_text_response(text: str) -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": text, "tool_calls": None},
            "finish_reason": "stop",
        }]
    }


class TestAgentFake:
    @pytest.mark.asyncio
    async def test_happy_path_v1(self):
        """Agent calls get_brand_facts, then submit_claims → ok status."""
        calls = [
            _make_tool_call_response("get_brand_facts", {"brand_id": FAKE_BRAND_ID}, "call_1"),
            _make_tool_call_response("submit_claims", {
                "claims": [
                    {
                        "quote": "The product has a 1-year warranty.",
                        "label": "supported",
                        "fact_ids": ["F1"],
                        "reason": "Fact F1 states 1-year warranty.",
                    }
                ]
            }, "call_2"),
        ]
        call_iter = iter(calls)

        async def _side_effect(msgs, tools=None):
            return next(call_iter)

        with patch("agent.agent.chat_completion", new_callable=AsyncMock) as mock_cc, \
             patch("agent.tools._FACTS_CACHE", {"brand_test": [{"fact_id": "F1", "value": "1 year"}]}), \
             patch("agent.tools._ALLOWLIST", {"brand_test"}):
            mock_cc.side_effect = _side_effect

            result = await run_agent(
                answer_id=FAKE_ANSWER_ID,
                brand_id=FAKE_BRAND_ID,
                question="What is the warranty?",
                ai_answer=FAKE_ANSWER,
                version="v1",
            )

        assert result.status in ("ok", "partial"), f"Unexpected status: {result.status}"
        assert len(result.claims) >= 1

    @pytest.mark.asyncio
    async def test_submit_claims_never_called(self):
        """Model never calls submit_claims → error after max turns."""
        # Always return a text response with no tool calls
        with patch("agent.agent.chat_completion", new_callable=AsyncMock) as mock_cc:
            mock_cc.return_value = _make_text_response("I analyzed the answer.")

            result = await run_agent(
                answer_id=FAKE_ANSWER_ID,
                brand_id=FAKE_BRAND_ID,
                question="Test?",
                ai_answer=FAKE_ANSWER,
                version="v1",
            )

        assert result.status == "error"
        assert result.turns_used <= 3

    @pytest.mark.asyncio
    async def test_provider_error_propagated(self):
        """ProviderError should set result.status = error, not raise."""
        from agent.provider import ProviderError

        with patch("agent.agent.chat_completion", new_callable=AsyncMock) as mock_cc:
            mock_cc.side_effect = ProviderError(401, "Unauthorized")

            result = await run_agent(
                answer_id=FAKE_ANSWER_ID,
                brand_id=FAKE_BRAND_ID,
                question="Test?",
                ai_answer=FAKE_ANSWER,
                version="v1",
            )

        assert result.status == "error"
        assert "Provider error" in result.error_message

    @pytest.mark.asyncio
    async def test_invalid_quote_becomes_validation_error(self):
        """submit_claims with a quote not in ai_answer → validation error, not unknown."""
        calls = [
            _make_tool_call_response("get_brand_facts", {"brand_id": FAKE_BRAND_ID}, "call_1"),
            _make_tool_call_response("submit_claims", {
                "claims": [{
                    "quote": "This sentence does not exist in the answer.",
                    "label": "supported",
                    "fact_ids": [],
                    "reason": "test",
                }]
            }, "call_2"),
        ]
        call_iter = iter(calls)

        async def _side_effect2(msgs, tools=None):
            return next(call_iter)

        with patch("agent.agent.chat_completion", new_callable=AsyncMock) as mock_cc:
            mock_cc.side_effect = _side_effect2

            result = await run_agent(
                answer_id=FAKE_ANSWER_ID,
                brand_id=FAKE_BRAND_ID,
                question="Test?",
                ai_answer=FAKE_ANSWER,
                version="v1",
            )

        assert any(e.code == "quote_not_found" for e in result.errors)
        # Must NOT silently become unknown
        assert not any(c.label == "unknown" and c.quote == "This sentence does not exist in the answer."
                       for c in result.claims)

    @pytest.mark.asyncio
    async def test_max_turns_respected(self):
        """Agent must not exceed MAX_TURNS."""
        with patch("agent.agent.chat_completion", new_callable=AsyncMock) as mock_cc:
            mock_cc.return_value = _make_text_response("Still thinking...")

            result = await run_agent(
                answer_id=FAKE_ANSWER_ID,
                brand_id=FAKE_BRAND_ID,
                question="Test?",
                ai_answer=FAKE_ANSWER,
                version="v1",
            )

        assert result.turns_used <= 3
