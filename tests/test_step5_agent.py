"""Step 5, slice 2: the LLM loop, providers and CLI. No network, no key, no Postgres.

The scripted provider stands in for the model; AnthropicLLM is tested against a stub SDK client.
Fixture `env` (tests/conftest.py) is the planted "VIC broken, NSW fine" scenario.
"""
import json
import re
from types import SimpleNamespace

import pytest

from jobs.data_gen.generate import WEEK
from services.diagnostic_api import chat
from services.diagnostic_api.agent import ESCALATION, run_turn, system_prompt
from services.diagnostic_api.llm import AnthropicLLM, ScriptedLLM, canonical_script, get_llm, text, tool_use
from services.diagnostic_api.tools import TOOL_SCHEMAS
from shared.config import current_week, load_config

QUESTION = "Brand A clearance this week: many prices didn't drop in VIC, NSW is fine. Why?"


def promo_brand(env):
    sku = sorted(env["promo"])[0][0]
    return next(c["brand"] for c in env["wh"].get_candidates(WEEK) if c["sku"] == sku and c["name"] is not None)


def assert_valid_history(messages):
    """Roles alternate from user, and every tool_use is answered in the very next user message."""
    assert messages[0]["role"] == "user"
    for prev, cur in zip(messages, messages[1:]):
        assert prev["role"] != cur["role"]
    for i, m in enumerate(messages):
        uses = [b["id"] for b in m["content"] if isinstance(m["content"], list) and b["type"] == "tool_use"]
        if uses:
            answered = [b["tool_use_id"] for b in messages[i + 1]["content"] if b["type"] == "tool_result"]
            assert sorted(uses) == sorted(answered)


# ======================= the loop =======================
def test_canonical_investigation_runs_end_to_end(env):
    brand = promo_brand(env)
    llm = canonical_script(WEEK, brand, ("VIC", "NSW"))
    out = run_turn(QUESTION, env["ctx"], llm, WEEK)
    assert [c["tool"] for c in out["tool_calls"]] == ["resolve_products", "diagnose_batch", "get_sap_conditions", "get_api_log"]
    assert not any(c["is_error"] for c in out["tool_calls"])
    assert out["steps"] == 4 and not out["escalated"] and not out["truncated"]      # 3 tool rounds + the answer
    assert "NOT_EFFECTIVE" in out["answer"] and "PROMOTION" in out["answer"] and "VIC" in out["answer"]
    assert_valid_history(out["messages"])
    json.dumps(out["messages"])                                              # history is JSON-serialisable
    # the two follow-up tools were requested together and answered together in one user message
    assert [len(m["content"]) for m in out["messages"][1:] if m["role"] == "user"] == [1, 1, 2]


def test_every_call_gets_the_system_prompt_and_all_tool_schemas(env):
    llm = canonical_script(WEEK, promo_brand(env))
    run_turn(QUESTION, env["ctx"], llm, WEEK)
    assert all(c["tools"] == TOOL_SCHEMAS and c["system"] == system_prompt(WEEK) for c in llm.calls)
    assert WEEK in llm.calls[0]["system"] and "Never judge" in llm.calls[0]["system"]


def test_tool_errors_are_returned_to_the_model_not_raised(env):
    llm = ScriptedLLM([[tool_use("delete_prices", {})], [text("I could not do that.")]])
    out = run_turn("delete everything", env["ctx"], llm, WEEK)
    assert out["tool_calls"] == [{"step": 1, "tool": "delete_prices", "input": {}, "is_error": True}]
    seen = llm.calls[1]["messages"][-1]["content"][0]                        # what the model saw next
    assert seen["is_error"] is True and "unknown tool" in json.loads(seen["content"])["error"]
    assert out["answer"] == "I could not do that." and not out["escalated"]
    assert_valid_history(out["messages"])


def test_step_limit_escalates_instead_of_looping(env):
    llm = ScriptedLLM([[tool_use("search_docs", {"query": "x"})]] * 10)
    out = run_turn(QUESTION, env["ctx"], llm, WEEK, max_steps=3)
    assert out["escalated"] and out["answer"] == ESCALATION and out["steps"] == 3
    assert len(llm.calls) == 3 and len(out["tool_calls"]) == 3               # never a 4th model call
    assert out["messages"][-1] == {"role": "assistant", "content": [{"type": "text", "text": ESCALATION}]}
    assert_valid_history(out["messages"])                                    # still safe to send back to the API


def test_history_is_passed_through_and_not_mutated(env):
    first = run_turn("hello", env["ctx"], ScriptedLLM([[text("hi")]]), WEEK)
    history = first["messages"]
    llm = ScriptedLLM([[text("again")]])
    second = run_turn("and now?", env["ctx"], llm, WEEK, history=history)
    assert len(history) == 2 and llm.calls[0]["messages"][:2] == history
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user", "assistant"]


def test_a_truncated_answer_is_flagged(env):
    class Cut(ScriptedLLM):
        def create(self, *a):
            return {**super().create(*a), "stop_reason": "max_tokens"}
    out = run_turn("q", env["ctx"], Cut([[text("half an ans")]]), WEEK)
    assert out["truncated"] and out["answer"] == "half an ans"


def test_the_model_cannot_pass_context_or_permissions(env):
    llm = ScriptedLLM([[tool_use("diagnose_batch", {"week": WEEK, "skus": ["1"], "ctx": {"allowed_regions": None}})],
                       [text("blocked")]])
    out = run_turn("q", env["ctx"], llm, WEEK)
    assert out["tool_calls"][0]["is_error"]


def test_system_prompt_tells_the_model_to_report_everything_and_not_to_invent():
    prompt = system_prompt(WEEK)
    for rule in ("Report every pattern", "contradict the user's premise", "not a shelf price", "not promotion dates",
                 "Never judge"):
        assert rule in prompt


# ======================= Anthropic provider (stub SDK client) =======================
class StubClient:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def reply(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


def test_anthropic_provider_converts_blocks_to_plain_dicts():
    client = StubClient(reply(SimpleNamespace(type="thinking", thinking="hm"),
                              SimpleNamespace(type="text", text="checking"),
                              SimpleNamespace(type="tool_use", id="tu_1", name="check_rules", input={"week": WEEK}),
                              stop="tool_use"))
    out = AnthropicLLM(client=client).create("sys", TOOL_SCHEMAS, [{"role": "user", "content": "q"}])
    assert out == {"content": [{"type": "text", "text": "checking"},
                               {"type": "tool_use", "id": "tu_1", "name": "check_rules", "input": {"week": WEEK}}],
                   "stop_reason": "tool_use"}
    req = client.requests[0]
    assert req["model"] == "claude-haiku-4-5" and req["system"] == "sys" and req["tools"] == TOOL_SCHEMAS
    assert req["messages"] == [{"role": "user", "content": "q"}] and req["max_tokens"] > 0


def test_anthropic_model_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    client = StubClient(reply(SimpleNamespace(type="text", text="ok")))
    AnthropicLLM(client=client).create("s", [], [{"role": "user", "content": "q"}])
    assert client.requests[0]["model"] == "claude-sonnet-5"


def test_agent_loop_works_with_the_anthropic_provider(env):
    client = StubClient(
        reply(SimpleNamespace(type="tool_use", id="tu_1", name="check_rules",
                              input={"week": WEEK, "skus": env["skus"]}), stop="tool_use"),
        reply(SimpleNamespace(type="text", text="No rule violations.")))
    out = run_turn("any rule problems?", env["ctx"], AnthropicLLM(client=client), WEEK)
    assert out["answer"] == "No rule violations." and out["tool_calls"][0]["tool"] == "check_rules"
    result = out["messages"][2]["content"][0]                                # the tool result sent on the 2nd call
    assert client.requests[1]["messages"][2]["content"][0] is result
    assert result["tool_use_id"] == "tu_1" and json.loads(result["content"])["violations"] == 0
    assert_valid_history(out["messages"])


# ======================= config, provider selection, CLI =======================
def test_get_llm_selects_the_provider_from_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "scripted")
    assert isinstance(get_llm(WEEK), ScriptedLLM)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")             # constructing a client makes no request
    assert isinstance(get_llm(), AnthropicLLM)
    monkeypatch.setenv("LLM_PROVIDER", "mystery")
    with pytest.raises(ValueError):
        get_llm()


def test_default_week_comes_from_env_else_today(monkeypatch):
    monkeypatch.setenv("DEFAULT_WEEK", "2026-W12")
    assert load_config().default_week == "2026-W12"
    monkeypatch.delenv("DEFAULT_WEEK")
    assert load_config().default_week == current_week() and re.fullmatch(r"\d{4}-W\d{2}", current_week())
    assert load_config().anthropic_model == "claude-haiku-4-5"


def test_dotenv_loader_strips_comments_and_keeps_real_env(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nFOO_A=1   # inline\nFOO_B=two\nFOO_C=already-set\n\nnot a line\n")
    monkeypatch.delenv("FOO_A", raising=False)
    monkeypatch.delenv("FOO_B", raising=False)
    monkeypatch.setenv("FOO_C", "real")
    chat.load_dotenv(str(f))
    import os
    assert (os.environ["FOO_A"], os.environ["FOO_B"], os.environ["FOO_C"]) == ("1", "two", "real")
    monkeypatch.delenv("FOO_A")
    monkeypatch.delenv("FOO_B")
    chat.load_dotenv(str(tmp_path / "missing.env"))                          # a missing file is fine


def test_cli_runs_the_offline_demo(env, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(chat, "make_context", lambda user, run_id=None: seen.append(run_id) or env["ctx"])
    monkeypatch.setattr(chat, "load_dotenv", lambda *a: None)
    monkeypatch.setenv("LLM_PROVIDER", "scripted")
    assert chat.main([QUESTION, "--week", WEEK, "--run-id", "run-1", "--quiet"]) == 0
    assert seen == ["run-1"]
    out = capsys.readouterr().out
    assert "Brand A" in out and "4 tool calls" in out and "ESCALATED" not in out
