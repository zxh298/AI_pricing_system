"""Ask the diagnostic assistant one question from the terminal (manual demo, not part of the tests).

    python -m services.diagnostic_api.chat "Brand A clearance this week: VIC prices did not drop, NSW is fine. Why?"

Reads .env from the current directory if present. LLM_PROVIDER=anthropic (and ANTHROPIC_API_KEY) uses
Claude; the default `scripted` provider replays a canned investigation offline. Needs the DuckDB
warehouse (pipeline has run) and mock-sap reachable at SAP_BASE_URL.
"""
from __future__ import annotations

import argparse
import logging

from services.diagnostic_api.agent import run_turn
from services.diagnostic_api.llm import get_llm
from services.diagnostic_api.tools import make_context
from shared.config import load_config
from shared.envfile import load_dotenv


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--week", default=None, help="default: DEFAULT_WEEK, else today's ISO week")
    ap.add_argument("--run-id", default=None, help="pin a pipeline run (default: the newest finished run of the week)")
    ap.add_argument("--user", default="cli")
    ap.add_argument("--quiet", action="store_true", help="hide the tool call log")
    args = ap.parse_args(argv)
    week = args.week or load_config().default_week
    if not args.quiet:
        logging.basicConfig(level=logging.INFO, format="  tool> %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
    out = run_turn(args.question, make_context(args.user, run_id=args.run_id), get_llm(week), week)
    print(f"\n{out['answer']}\n")
    print(f"[{out['steps']} model calls, {len(out['tool_calls'])} tool calls"
          + (", ESCALATED: step limit reached" if out["escalated"] else "")
          + (", answer truncated at max_tokens" if out["truncated"] else "") + "]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
