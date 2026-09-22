"""    python -m jobs.rag_ingest [--path playbooks]

Exit code: 0 = ingested (or nothing to do), 1 = the corpus has problems (nothing was written).
"""
import argparse
import json
import sys

from jobs.rag_ingest.corpus import CorpusError
from jobs.rag_ingest.ingest import ingest
from shared.envfile import load_dotenv


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="playbooks")
    args = ap.parse_args(argv)
    try:
        print(json.dumps(ingest(args.path), indent=2))
    except CorpusError as e:
        print(f"corpus problems, nothing was ingested:\n{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
