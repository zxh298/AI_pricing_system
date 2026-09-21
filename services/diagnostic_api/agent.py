"""The LLM tool-use loop. The model plans tool calls, combines their results and explains; every
judgement about a price or record comes from the deterministic tools (tools.py, core.py).

`run_turn` takes and returns plain-dict messages, so history is JSON-serialisable. Every tool_use is
answered by a tool_result in the next user message, so the list stays valid to send back to the API.
"""
from __future__ import annotations

import json

from services.diagnostic_api.llm import LLM
from services.diagnostic_api.tools import TOOL_SCHEMAS, ToolContext, execute_tool

MAX_STEPS = 8

ESCALATION = ("I could not reach a conclusion within the step limit. Here is what I checked so far; "
              "please escalate this to the pricing team to investigate further.")

SYSTEM = """You are a diagnostic assistant for a weekly retail clearance pricing system. SAP decides which \
products are on clearance and the business rules; the pricing engine sets the prices; SAP applies them.

The current pricing week is {week}. Use it when the user says "this week".

Rules:
- Never judge whether a price or a record is correct yourself, not even with a simple comparison. Call a \
tool: diagnose_batch for the status of records, check_rules for any question about floors, maximum markdown or \
price ladders.
- State only facts that appear in tool results. Quote counts exactly. Do not invent prices, skus, dates, \
priorities or causes. Use each field for what its name and the tool's "fields" note say; a clearance price is \
not a shelf price, and no tool gives promotion dates.
- Your answer must mention every pattern diagnose_batch returns, region by region, including ones unrelated to \
the question. If a problem appears in several regions, say so for each one. If the results contradict the user's \
premise (for example a region they call fine has problems), say so plainly instead of agreeing.
- For totals use the counts the tools give (records_per_region, by_region, totals); do not add numbers up \
yourself.
- Start with the finding. Do not narrate what you are doing or say that you have the answer.
- Before investigating a problem in depth, call get_findings for the week. If an open finding matches, say it \
is a known issue that a person confirmed, quote its root cause, owner and ticket from the result, and still \
verify with diagnose_batch.
- If diagnose_batch returns changes_since_last_check, lead with what changed (resolved, new, changed) using its \
counts. If it returns findings_resolved, say those findings were closed automatically because the problem is gone. \
A result with "cached": true is a reused result; its as_of says how old it is.
- Anything about current status must come from a tool call made in this turn. Earlier turns only tell you \
what the user is referring to.
- When the user names a brand, category or product, call resolve_products first. If it returns a group_id, \
pass that to the other tools instead of listing skus; otherwise use the skus it returns.
- Prefer batch tools over many small calls. Report patterns (counts by region, code and reason) with a few \
example skus, not long lists.
- If the evidence does not match a known cause, say the issue is unclassified, offer hypotheses clearly \
labelled low confidence, and recommend escalating to a person. Never present a hypothesis as a finding.
- Document search is not available yet, so do not claim to know playbooks. Owners and tickets may be quoted only \
from get_findings results; otherwise do not name teams or owners or say whom to contact. You may say what would \
need to change (for example "the promotion in SAP"), not who does it."""


def system_prompt(week: str, state_block: str = "") -> str:
    """The rules plus, when the session has any, the state block the service keeps (groups, earlier questions)."""
    return SYSTEM.format(week=week) + (f"\n\n{state_block}" if state_block else "")


def run_turn(question: str, ctx: ToolContext, llm: LLM, week: str, history: list[dict] | None = None,
             max_steps: int = MAX_STEPS, state_block: str = "") -> dict:
    """One user turn. Returns {"answer", "messages", "tool_calls", "steps", "escalated", "truncated"}."""
    system = system_prompt(week, state_block)
    messages = list(history or []) + [{"role": "user", "content": question}]
    calls: list[dict] = []
    for step in range(1, max_steps + 1):
        resp = llm.create(system, TOOL_SCHEMAS, messages)
        messages.append({"role": "assistant", "content": resp["content"]})
        uses = [b for b in resp["content"] if b["type"] == "tool_use"]
        if not uses:
            answer = "".join(b["text"] for b in resp["content"] if b["type"] == "text")
            return {"answer": answer, "messages": messages, "tool_calls": calls, "steps": step,
                    "escalated": False, "truncated": resp["stop_reason"] == "max_tokens"}
        results = []
        for u in uses:
            out, is_error = execute_tool(u["name"], u["input"], ctx)
            calls.append({"step": step, "tool": u["name"], "input": u["input"], "is_error": is_error})
            results.append({"type": "tool_result", "tool_use_id": u["id"],
                            "content": json.dumps(out, default=str), "is_error": is_error})
        messages.append({"role": "user", "content": results})
    messages.append({"role": "assistant", "content": [{"type": "text", "text": ESCALATION}]})
    return {"answer": ESCALATION, "messages": messages, "tool_calls": calls, "steps": max_steps,
            "escalated": True, "truncated": False}
