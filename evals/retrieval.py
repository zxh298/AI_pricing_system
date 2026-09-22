"""Retrieval evaluation: how well does the knowledge base find the right document, and stay silent when there is none?

    python -m evals.retrieval [--min-score 0.65] [--write]

Runs the labelled questions in evals/retrieval.json through the same search the search_docs tool uses, against the
knowledge base in Postgres (run `python -m jobs.rag_ingest` first) with the embedding model in EMBEDDING_PROVIDER. Reports:
  * similarity alone: is the right document ranked first / in the top 3, whatever the threshold?
  * the full search (tags first, then similarity above the threshold): hit rates, and how often an off-topic
    question wrongly finds something
  * a threshold sweep, and how well several gates (best score, its distance above the other scores, the margin over the
    second document) separate on-topic from off-topic questions, so the threshold is chosen from data
The scoring functions are pure and tested; only `evaluate` touches the database.
"""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("named", "plain", "vague", "offtopic")
ON_TOPIC = ("named", "plain")
DEFAULT_FILE = Path(__file__).with_name("retrieval.json")
TOP_SIMILAR = 2                                   # extra passages a search adds (services.diagnostic_api.knowledge)


def load_questions(path: str | Path = DEFAULT_FILE) -> list[dict]:
    return json.loads(Path(path).read_text())


@dataclass
class Probe:
    """Everything measured for one question, independent of the threshold."""
    id: str
    kind: str
    question: str
    expected: list[str]
    tag_docs: list[dict]                          # documents found by tag: [{"doc_id", "doc_type"}], in result order
    docs: list[dict] = field(default_factory=list)      # every document's best chunk, best first: doc_id, doc_type, score
    top1: float = 0.0
    margin: float = 0.0                           # best document's score minus the second best's
    z: float = 0.0                                # how many standard deviations the best chunk is above the mean chunk score
    zdoc: float = 0.0                             # the same over the best score of each document


def rank_docs(chunk_scores: list[dict]) -> list[dict]:
    """Best chunk per document, best first. chunk_scores: dicts with doc_id, doc_type, score."""
    best: dict[str, dict] = {}
    for c in chunk_scores:
        if c["doc_id"] not in best or c["score"] > best[c["doc_id"]]["score"]:
            best[c["doc_id"]] = {"doc_id": c["doc_id"], "doc_type": c["doc_type"], "score": c["score"]}
    return sorted(best.values(), key=lambda d: -d["score"])


def _z(top: float, values: list[float]) -> float:
    spread = statistics.pstdev(values)
    return (top - statistics.fmean(values)) / spread if spread else 0.0


def measure(chunk_scores: list[dict]) -> dict:
    """The threshold-independent numbers for one question."""
    docs = rank_docs(chunk_scores)
    top1 = docs[0]["score"]
    return {"docs": docs, "top1": top1, "margin": top1 - (docs[1]["score"] if len(docs) > 1 else 0.0),
            "z": _z(top1, [c["score"] for c in chunk_scores]), "zdoc": _z(top1, [d["score"] for d in docs])}


def results_at(p: Probe, threshold: float, related_min: float = 0.80, gate=None) -> list[str]:
    """The document ids a search would return for this question at a threshold: tag matches first, then similar documents
    that clear the bar (higher once a tag has found a playbook), at most TOP_SIMILAR. `gate(probe)` can veto similarity."""
    ids = [d["doc_id"] for d in p.tag_docs]
    bar = max(threshold, related_min) if any(d["doc_type"] == "playbook" for d in p.tag_docs) else threshold
    if gate is not None and not gate(p):
        return ids
    extra = [d["doc_id"] for d in p.docs if d["doc_id"] not in ids and d["score"] >= bar]
    return ids + extra[:TOP_SIMILAR]


def found_at(p: Probe, threshold: float, related_min: float = 0.80, gate=None) -> bool:
    """Whether the search says it found something relevant (a tag-matched playbook or incident, or any similar passage)."""
    if any(d["doc_type"] in ("playbook", "incident") for d in p.tag_docs):
        return True
    tagged = {d["doc_id"] for d in p.tag_docs}
    return any(i not in tagged for i in results_at(p, threshold, related_min, gate))


def hit(ids: list[str], expected: list[str], k: int) -> bool:
    return any(i in expected for i in ids[:k])


def rates(probes: list[Probe], threshold: float, related_min: float = 0.80, gate=None) -> dict:
    """Hit rates on on-topic questions and the false-positive rate on off-topic ones, at one threshold."""
    on = [p for p in probes if p.kind in ON_TOPIC]
    off = [p for p in probes if p.kind == "offtopic"]
    res = {p.id: results_at(p, threshold, related_min, gate) for p in on}
    return {"on_topic": len(on), "off_topic": len(off),
            "hit1": sum(hit(res[p.id], p.expected, 1) for p in on) / len(on),
            "hit3": sum(hit(res[p.id], p.expected, 3) for p in on) / len(on),
            "found": sum(found_at(p, threshold, related_min, gate) for p in on) / len(on),
            "false_positive": sum(found_at(p, threshold, related_min, gate) for p in off) / len(off)}


def similarity_rates(probes: list[Probe], kinds=ON_TOPIC) -> dict:
    """How well the embedding alone ranks the right document, with no tags and no threshold."""
    sel = [p for p in probes if p.kind in kinds]
    return {"n": len(sel), "hit1": sum(hit([d["doc_id"] for d in p.docs], p.expected, 1) for p in sel) / len(sel),
            "hit3": sum(hit([d["doc_id"] for d in p.docs], p.expected, 3) for p in sel) / len(sel)}


def auc(positive: list[float], negative: list[float]) -> float:
    """Probability that a random positive scores above a random negative (0.5 = no separation, 1.0 = perfect)."""
    pairs = [(a, b) for a in positive for b in negative]
    return sum(1.0 if a > b else 0.5 if a == b else 0.0 for a, b in pairs) / len(pairs)


FEATURES = {"top1": lambda p: p.top1, "margin": lambda p: p.margin, "z": lambda p: p.z, "zdoc": lambda p: p.zdoc}


def separation(probes: list[Probe]) -> dict:
    """For each feature: AUC between on-topic questions that only similarity can answer (no tag found) and off-topic ones,
    and the best cut-off that returns nothing for every off-topic question (and how many on-topic ones it keeps)."""
    pos = [p for p in probes if p.kind in ON_TOPIC and not p.tag_docs]
    neg = [p for p in probes if p.kind == "offtopic"]
    out = {}
    for name, f in FEATURES.items():
        a, b = [f(p) for p in pos], [f(p) for p in neg]
        cut = max(b) + 1e-9                                    # just above the highest off-topic value
        out[name] = {"auc": auc(a, b), "off_topic_max": max(b), "on_topic_min": min(a), "cut": cut,
                     "kept": sum(x >= cut for x in a) / len(a)}
    return out


def evaluate(kb, questions: list[dict]) -> list[Probe]:
    probes = []
    for q in questions:
        hit_ = kb.search(q["question"])                        # tags found for the question; similarity part unused here
        m = measure(kb.scores(q["question"]))
        probes.append(Probe(q["id"], q["kind"], q["question"], q["expected"],
                            [{"doc_id": d["doc_id"], "doc_type": d["doc_type"]} for d in hit_["docs"]], **m))
    return probes


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def report(probes: list[Probe], threshold: float, model: str, docs: int) -> str:
    on = [p for p in probes if p.kind in ON_TOPIC]
    lines = [f"# Retrieval evaluation", "",
             f"Embedding model: `{model}` · documents in the knowledge base: {docs} · questions: "
             + ", ".join(f"{sum(p.kind == k for p in probes)} {k}" for k in KINDS), ""]
    s_all, s_named, s_plain = (similarity_rates(probes, k) for k in (ON_TOPIC, ("named",), ("plain",)))
    lines += ["## Similarity alone (no tags, no threshold)", "",
              "| questions | hit@1 | hit@3 |", "|---|---|---|"]
    for name, s in (("named (mention a code or reason)", s_named), ("plain words", s_plain), ("all on-topic", s_all)):
        lines.append(f"| {name} ({s['n']}) | {_pct(s['hit1'])} | {_pct(s['hit3'])} |")
    r = rates(probes, threshold)
    lines += ["", f"## The search as built (tags first, similarity above {threshold})", "",
              f"* on-topic questions ({r['on_topic']}): right document first {_pct(r['hit1'])}, in the first three {_pct(r['hit3'])}, "
              f"something found {_pct(r['found'])}",
              f"* off-topic questions ({r['off_topic']}): wrongly found something {_pct(r['false_positive'])}", "",
              "### Questions where the right document was not first", ""]
    misses = [p for p in on if not hit(results_at(p, threshold), p.expected, 1)]
    for p in misses:
        got = results_at(p, threshold)
        lines.append(f"* `{p.id}` {p.question!r}: got {got[:3] or 'nothing'}, wanted one of {p.expected}; similarity ranked "
                     f"{[d['doc_id'] for d in p.docs[:2]]}")
    lines += ([] if misses else ["* none"]) + ["", "### Off-topic questions that found something", ""]
    wrong = [p for p in probes if p.kind == "offtopic" and found_at(p, threshold)]
    lines += [f"* `{p.id}` {p.question!r}: {results_at(p, threshold)} (best score {p.top1:.2f})" for p in wrong] or ["* none"]
    lines += ["", "## Threshold sweep", "", "| threshold | on-topic hit@1 | on-topic found | off-topic false positives |", "|---|---|---|---|"]
    for t in [x / 100 for x in range(50, 90, 5)]:
        x = rates(probes, t)
        lines.append(f"| {t:.2f} | {_pct(x['hit1'])} | {_pct(x['found'])} | {_pct(x['false_positive'])} |")
    sep = separation(probes)
    lines += ["", "## Which measure separates on-topic from off-topic best",
              "", "On-topic questions that only similarity can answer against off-topic ones. AUC 1.00 is perfect separation; "
              "`kept` is the share of those on-topic questions still answered with a cut-off that silences every off-topic one.", "",
              "| measure | AUC | highest off-topic | lowest on-topic | cut-off | on-topic kept |", "|---|---|---|---|---|---|"]
    for name, x in sorted(sep.items(), key=lambda kv: -kv[1]["auc"]):
        lines.append(f"| {name} | {x['auc']:.2f} | {x['off_topic_max']:.2f} | {x['on_topic_min']:.2f} | {x['cut']:.2f} | {_pct(x['kept'])} |")
    top = [p.top1 for p in on if not p.tag_docs]
    off = [p.top1 for p in probes if p.kind == "offtopic"]
    lines += ["", "## Best-score distributions", "",
              f"* on-topic, answered by similarity: min {min(top):.2f}, median {statistics.median(top):.2f}, max {max(top):.2f}",
              f"* off-topic: min {min(off):.2f}, median {statistics.median(off):.2f}, max {max(off):.2f}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from services.diagnostic_api.knowledge import KnowledgeBase
    from shared.config import load_config
    from shared.embedding import get_embedder
    from shared.envfile import load_dotenv
    from shared.knowledge_schema import schema
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=float, default=None, help="default: SEARCH_MIN_SCORE")
    ap.add_argument("--write", action="store_true", help="save the report as evals/RESULTS.md")
    args = ap.parse_args(argv)
    threshold = args.min_score if args.min_score is not None else load_config().search_min_score
    embedder = get_embedder()
    kb = KnowledgeBase(embedder=embedder, min_score=threshold)
    probes = evaluate(kb, load_questions())
    n_docs = len({d["doc_id"] for d in probes[0].docs})
    text = report(probes, threshold, embedder.name, n_docs)
    print(text)
    if args.write:
        Path(__file__).with_name("RESULTS.md").write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
