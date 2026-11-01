"""Routing evaluation: does the classify step pick the right intent and the right slots?

    python -m evals.routing [--model claude-haiku-4-5] [--show-all]

Runs the labelled questions in evals/routing.json through router.classify with the real model (LLM_PROVIDER does not
matter: this always uses the Anthropic API, so ANTHROPIC_API_KEY must be set). Only the classify step runs: no
warehouse, SAP or Postgres is needed. A question may list other acceptable intents (`accept`) when it is genuinely
ambiguous. Slots are scored only on the keys a question expects; extra slots are allowed. Each question is scored
twice: on the model's slots alone, and after `router.backfill` fills the names the question mentions but the model
left out (the names are the synthetic generator's brands, categories and regions, standing in for the week's list).
Reports intent accuracy, slot accuracy both ways, per-intent results and every miss of the final routing. The
scoring functions are pure and tested.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_FILE = Path(__file__).with_name("routing.json")
WEEK = "2026-W39"


def load_questions(path: str | Path = DEFAULT_FILE) -> list[dict]:
    return json.loads(Path(path).read_text())


def _norm(value):
    if isinstance(value, list):
        return sorted(_norm(v) for v in value)
    return value.strip().lower() if isinstance(value, str) else value


def score(question: dict, predicted: dict) -> dict:
    """{intent_ok, slots_ok, wrong_slots} for one question. wrong_slots: {slot: (expected, predicted)}."""
    intent_ok = predicted.get("intent") in [question["intent"], *question.get("accept", [])]
    wrong = {k: (v, predicted.get(k)) for k, v in question["slots"].items() if _norm(predicted.get(k)) != _norm(v)}
    return {"intent_ok": intent_ok, "slots_ok": not wrong, "wrong_slots": wrong}


def summarise(rows: list[dict]) -> dict:
    """rows: {question, predicted, model_slots_ok, **score}. Totals and per-intent counts (by the expected intent)."""
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        per[r["question"]["intent"]][0] += r["intent_ok"]
        per[r["question"]["intent"]][1] += 1
    n = len(rows)
    return {"questions": n, "intent_accuracy": sum(r["intent_ok"] for r in rows) / n if n else 0.0,
            "model_slot_accuracy": sum(r["model_slots_ok"] for r in rows) / n if n else 0.0,
            "slot_accuracy": sum(r["slots_ok"] for r in rows) / n if n else 0.0,
            "both": sum(r["intent_ok"] and r["slots_ok"] for r in rows), "per_intent": dict(per)}


def known_names() -> dict[str, list[str]]:
    from jobs.data_gen.generate import BRANDS, CATEGORIES, REGIONS
    return {"brand": list(BRANDS), "category": list(CATEGORIES), "regions": list(REGIONS)}


def evaluate(questions: list[dict], llm, names: dict[str, list[str]] | None = None) -> list[dict]:
    """Classify every question; score the model's slots alone and after the code backfill."""
    from services.diagnostic_api.router import SCOPED, backfill, classify
    names = known_names() if names is None else names
    rows = []
    for q in questions:
        model = classify(q["question"], llm, WEEK, state_block=q.get("state", ""))
        scoped = model["intent"] in SCOPED or model["intent"] == "list_products"    # the router backfills only these
        final = backfill(model, q["question"], names) if scoped else model
        rows.append({"question": q, "predicted": final, "model_slots_ok": score(q, model)["slots_ok"], **score(q, final)})
    return rows


def report(rows: list[dict], show_all: bool = False) -> str:
    s = summarise(rows)
    lines = [f"{s['questions']} questions: intent {s['intent_accuracy']:.0%}, slots {s['slot_accuracy']:.0%} "
             f"({s['model_slot_accuracy']:.0%} from the model alone, before the code backfill), "
             f"both right {s['both']}/{s['questions']}", "", "per intent (expected):"]
    lines += [f"  {k:<14} {ok}/{n}" for k, (ok, n) in sorted(s["per_intent"].items())]
    lines += ["", "misses:" if not show_all else "all questions:"]
    for r in rows:
        if show_all or not (r["intent_ok"] and r["slots_ok"]):
            q, p = r["question"], r["predicted"]
            mark = "ok " if r["intent_ok"] and r["slots_ok"] else "MISS"
            lines.append(f"  {mark} {q['id']} {q['question']!r}: expected {q['intent']}, got {p.get('intent')}"
                         + "".join(f"; {k} expected {e!r} got {g!r}" for k, (e, g) in r["wrong_slots"].items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from shared.envfile import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="default: ANTHROPIC_MODEL")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--show-all", action="store_true")
    args = ap.parse_args(argv)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set: this evaluation calls the real model.", file=sys.stderr)
        return 2
    from services.diagnostic_api.llm import AnthropicLLM
    llm = AnthropicLLM(model=args.model)
    print(f"model {llm.model}\n")
    print(report(evaluate(load_questions(args.file), llm), args.show_all))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
