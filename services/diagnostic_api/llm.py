"""LLM providers behind one interface, so the loop never depends on a vendor SDK.

    create(system, tools, messages) -> {"content": [blocks], "stop_reason": str}

Blocks are plain dicts ({"type": "text" | "tool_use", ...}) so a conversation is JSON-serialisable
(sessions persist it in Postgres later). Providers:
    ScriptedLLM    replays canned responses: offline development and tests, no key, no network
    AnthropicLLM   Claude via the Anthropic API (LLM_PROVIDER=anthropic, key in ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import itertools
import json
from typing import Callable, Protocol

from shared.config import load_config


class LLM(Protocol):
    def create(self, system: str, tools: list[dict], messages: list[dict]) -> dict: ...


# ---------------- scripted (offline) ----------------
def text(s: str) -> dict:
    return {"type": "text", "text": s}


def tool_use(name: str, args: dict, id: str | None = None) -> dict:
    return {"type": "tool_use", "id": id or "", "name": name, "input": args}


class ScriptedLLM:
    """Each step is a list of blocks, or a function(messages) -> blocks so a step can use earlier tool
    results. tool_use ids are filled in automatically. Running out of steps is an error."""

    def __init__(self, steps: list[list[dict] | Callable[[list[dict]], list[dict]]]):
        self.steps, self.calls, self._ids = list(steps), [], itertools.count(1)

    def create(self, system, tools, messages):
        self.calls.append({"system": system, "tools": tools, "messages": json.loads(json.dumps(messages))})
        if len(self.calls) > len(self.steps):
            raise RuntimeError("ScriptedLLM has no response left for this call")
        step = self.steps[len(self.calls) - 1]
        blocks = step(messages) if callable(step) else step
        blocks = [{**b, "id": b["id"] or f"toolu_{next(self._ids)}"} if b["type"] == "tool_use" else b for b in blocks]
        return {"content": blocks, "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"}


def last_tool_results(messages: list[dict]) -> list[dict]:
    """The parsed JSON of every tool_result in the latest user message (for scripted steps)."""
    return [json.loads(b["content"]) for b in messages[-1]["content"] if b["type"] == "tool_result"]


def canonical_script(week: str, brand: str = "Brand A", regions: tuple[str, ...] = ("VIC", "NSW")) -> ScriptedLLM:
    """The section 3.3 investigation: brand -> skus -> diagnose -> SAP conditions -> API log -> answer.
    The final text is assembled from tool results only, standing in for the model's explanation."""
    regions = list(regions)

    def diagnose(msgs):
        return [tool_use("diagnose_batch", {"week": week, "skus": last_tool_results(msgs)[0]["skus"], "regions": regions})]

    def follow_up(msgs):
        d = last_tool_results(msgs)[0]
        skus = sorted({s for p in d["patterns"] for s in p["sample_skus"]})
        return [tool_use("get_sap_conditions", {"week": week, "skus": skus, "regions": regions}),
                tool_use("get_api_log", {"week": week, "skus": skus, "regions": regions})]

    def answer(msgs):
        d = next(json.loads(b["content"]) for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                 for b in m["content"] if b["type"] == "tool_result" and "by_region" in b["content"])
        lines = [f"{brand}, {week}: " + "; ".join(f"{r} {dict(c)}" for r, c in d["by_region"].items()) + "."]
        lines += [f"{p['count']} x {p['code']} ({p['reason']}) in {p['region']}, e.g. {', '.join(p['sample_skus'])}"
                  for p in d["patterns"] if p["region"] in regions]
        return [text("\n".join(lines))]

    return ScriptedLLM([[tool_use("resolve_products", {"week": week, "brand": brand, "regions": regions})],
                        diagnose, follow_up, answer])


# ---------------- Anthropic ----------------
class AnthropicLLM:
    def __init__(self, client=None, model: str | None = None, max_tokens: int = 4096):
        if client is None:
            import anthropic                       # only needed for the live provider
            client = anthropic.Anthropic()
        self.client, self.max_tokens = client, max_tokens
        self.model = model or load_config().anthropic_model

    def create(self, system, tools, messages):
        r = self.client.messages.create(model=self.model, max_tokens=self.max_tokens, system=system,
                                        tools=tools, messages=messages)
        blocks = []
        for b in r.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return {"content": blocks, "stop_reason": r.stop_reason}


def get_llm(week: str | None = None) -> LLM:
    cfg = load_config()
    if cfg.llm_provider == "anthropic":
        return AnthropicLLM()
    if cfg.llm_provider == "scripted":            # offline demo of the canonical investigation
        return canonical_script(week or cfg.default_week)
    raise ValueError(f"unknown LLM_PROVIDER={cfg.llm_provider}")
