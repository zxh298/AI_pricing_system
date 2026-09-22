"""Step 6, part 4: the retrieval evaluation harness (evals/retrieval.py) and a regression check against the real corpus.

The scoring functions (rank_docs, measure, results_at, found_at, rates, auc, separation) are pure and tested with
synthetic data, no database needed. One regression test runs the whole evaluation set against the real playbooks with
the offline hash embedder, so a change that breaks tag recognition or the corpus fails a test, not just a manual report.
"""
import json
import os

import pytest

os.environ["SAP_SCHEMA"], os.environ["KNOWLEDGE_SCHEMA"] = "sap_test", "knowledge_test"

from evals.retrieval import (FEATURES, Probe, auc, evaluate, found_at, hit, load_questions, measure, rank_docs, rates,
                             report, results_at, separation, similarity_rates)
from jobs.rag_ingest.corpus import load_corpus
from jobs.rag_ingest.ingest import ingest
from services.diagnostic_api.knowledge import KnowledgeBase
from shared import db
from shared.embedding import HashEmbedder

try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pg = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")


# ======================= scoring functions (pure) =======================
def chunk(doc_id, doc_type, score):
    return {"doc_id": doc_id, "doc_type": doc_type, "score": score}


def test_rank_docs_keeps_the_best_chunk_of_each_document_best_first():
    ranked = rank_docs([chunk("A", "playbook", 0.5), chunk("B", "playbook", 0.9), chunk("A", "playbook", 0.7)])
    assert [(d["doc_id"], d["score"]) for d in ranked] == [("B", 0.9), ("A", 0.7)]
    assert rank_docs([]) == []


def test_measure_computes_top1_margin_and_z_scores():
    m = measure([chunk("A", "playbook", 0.9), chunk("B", "playbook", 0.6), chunk("C", "playbook", 0.3)])
    assert m["top1"] == 0.9 and round(m["margin"], 2) == 0.3
    assert m["z"] > 0 and m["zdoc"] > 0                                                    # the top score is above the mean
    flat = measure([chunk("A", "playbook", 0.5), chunk("B", "playbook", 0.5)])
    assert flat["z"] == 0.0 and flat["margin"] == 0.0                                      # no spread: no z-score division by zero
    one = measure([chunk("A", "playbook", 0.5)])
    assert one["margin"] == 0.5                                                            # nothing to subtract: the raw score


def probe(id="q", kind="plain", expected=("PB-X",), tag_docs=(), docs=(), top1=0.0, margin=0.0, z=0.0, zdoc=0.0):
    return Probe(id, kind, "question text", list(expected), list(tag_docs), list(docs), top1, margin, z, zdoc)


def test_results_at_lists_tag_matches_first_then_similar_documents_above_the_threshold():
    p = probe(tag_docs=[{"doc_id": "PB-TAG", "doc_type": "playbook"}],
             docs=[{"doc_id": "PB-TAG", "doc_type": "playbook", "score": 0.99}, {"doc_id": "PB-SIM", "doc_type": "playbook", "score": 0.85},
                   {"doc_id": "PB-LOW", "doc_type": "playbook", "score": 0.50}])
    assert results_at(p, 0.80) == ["PB-TAG", "PB-SIM"]                                     # the tagged doc itself is not repeated
    assert results_at(p, 0.90) == ["PB-TAG"]                                               # PB-SIM no longer clears the (raised) bar


def test_the_bar_is_raised_once_a_tag_has_found_a_playbook():
    p = probe(tag_docs=[{"doc_id": "PB-TAG", "doc_type": "playbook"}],
             docs=[{"doc_id": "PB-CLOSE", "doc_type": "playbook", "score": 0.70}])
    assert results_at(p, 0.65, related_min=0.80) == ["PB-TAG"]                             # 0.70 < 0.80: not close enough to add
    no_tag = probe(tag_docs=[], docs=[{"doc_id": "PB-CLOSE", "doc_type": "playbook", "score": 0.70}])
    assert results_at(no_tag, 0.65, related_min=0.80) == ["PB-CLOSE"]                      # without a tag, the plain threshold applies


def test_results_at_caps_the_number_of_similar_documents():
    p = probe(docs=[{"doc_id": f"PB-{i}", "doc_type": "playbook", "score": 0.9} for i in range(5)])
    assert len(results_at(p, 0.5)) == 2                                                    # TOP_SIMILAR


def test_a_gate_can_switch_off_similarity_without_touching_tag_matches():
    p = probe(tag_docs=[{"doc_id": "PB-TAG", "doc_type": "playbook"}],
             docs=[{"doc_id": "PB-SIM", "doc_type": "playbook", "score": 0.9}])
    assert results_at(p, 0.5, gate=lambda p: False) == ["PB-TAG"]
    assert results_at(p, 0.5, gate=lambda p: True) == ["PB-TAG", "PB-SIM"]


def test_found_at_is_true_for_a_tagged_playbook_even_if_it_is_a_reference_page_from_similarity():
    tagged_ref = probe(tag_docs=[{"doc_id": "REF-X", "doc_type": "reference"}])             # a bare-code fallback: not "found"
    assert found_at(tagged_ref, 0.9) is False
    tagged_playbook = probe(tag_docs=[{"doc_id": "PB-X", "doc_type": "playbook"}])
    assert found_at(tagged_playbook, 0.99) is True                                          # true whatever the threshold
    only_similar = probe(docs=[{"doc_id": "PB-X", "doc_type": "playbook", "score": 0.9}])
    assert found_at(only_similar, 0.5) is True and found_at(only_similar, 0.95) is False


def test_hit_checks_the_top_k_ids_against_the_expected_set():
    assert hit(["A", "B", "C"], ["C"], 3) and not hit(["A", "B", "C"], ["C"], 2)
    assert not hit([], ["A"], 3) and hit(["A"], [], 3) is False


def test_rates_reports_on_topic_hit_rates_and_off_topic_false_positives():
    probes = [probe("n1", "named", ["PB-A"], tag_docs=[{"doc_id": "PB-A", "doc_type": "playbook"}]),
             probe("p1", "plain", ["PB-B"], docs=[{"doc_id": "PB-WRONG", "doc_type": "playbook", "score": 0.9}]),
             probe("o1", "offtopic", [], docs=[{"doc_id": "PB-X", "doc_type": "playbook", "score": 0.9}])]
    r = rates(probes, 0.5)
    assert (r["on_topic"], r["off_topic"]) == (2, 1) and r["hit1"] == 0.5 and r["found"] == 1.0 and r["false_positive"] == 1.0
    assert rates(probes, 0.95)["false_positive"] == 0.0                                     # raising the bar silences it


def test_similarity_rates_ignores_tags_entirely():
    probes = [probe("p1", "plain", ["PB-A"], tag_docs=[{"doc_id": "PB-WRONG", "doc_type": "playbook"}],
                    docs=[{"doc_id": "PB-A", "doc_type": "playbook", "score": 0.7}])]
    assert similarity_rates(probes) == {"n": 1, "hit1": 1.0, "hit3": 1.0}                    # the tag match plays no part


def test_auc_measures_separation():
    assert auc([0.9, 0.8], [0.1, 0.2]) == 1.0                                               # perfectly separated
    assert auc([0.5], [0.5]) == 0.5                                                          # tie: no information
    assert auc([0.3], [0.9]) == 0.0                                                          # perfectly backwards


def test_separation_finds_the_cut_that_silences_every_off_topic_question():
    probes = [probe("p1", "plain", ["PB-A"], top1=0.9, margin=0.3, z=2.0, zdoc=2.0),
             probe("p2", "plain", ["PB-B"], top1=0.6, margin=0.1, z=1.0, zdoc=1.0),           # a harder on-topic question
             probe("o1", "offtopic", [], top1=0.5, margin=0.05, z=0.5, zdoc=0.5)]
    sep = separation(probes)
    assert set(sep) == set(FEATURES)
    assert sep["top1"]["cut"] > 0.5 and sep["top1"]["kept"] == 1.0                          # both on-topic questions clear it
    assert 0.0 <= sep["top1"]["auc"] <= 1.0
    tagged = [probe("n1", "named", ["PB-A"], tag_docs=[{"doc_id": "PB-A", "doc_type": "playbook"}], top1=0.4)]
    assert separation(tagged + probes)["top1"]["kept"] == sep["top1"]["kept"]                # a tag-found question plays no part


def test_report_is_readable_markdown_and_lists_misses():
    probes = [probe("n1", "named", ["PB-A"], tag_docs=[{"doc_id": "PB-A", "doc_type": "playbook"}], docs=[{"doc_id": "PB-A", "doc_type": "playbook", "score": 0.9}]),
             probe("p1", "plain", ["PB-B"], docs=[{"doc_id": "PB-WRONG", "doc_type": "playbook", "score": 0.7}]),
             probe("o1", "offtopic", [], docs=[{"doc_id": "PB-X", "doc_type": "playbook", "score": 0.9}])]
    text = report(probes, 0.65, "test-model", 3)
    assert "test-model" in text and "## Similarity alone" in text and "## Threshold sweep" in text
    assert "p1" in text and "PB-WRONG" in text                                              # the miss is listed
    assert "o1" in text                                                                     # the false positive is listed


# ======================= regression: the real corpus, offline embedder =======================
@pg
def test_the_real_corpus_covers_every_labelled_question_and_stays_clean_offline(tmp_path):
    import shutil
    from pathlib import Path
    with db.connect() as c:
        c.execute("DROP SCHEMA IF EXISTS knowledge_test CASCADE")
    corpus = tmp_path / "playbooks"
    shutil.copytree(Path(__file__).resolve().parents[1] / "playbooks", corpus)
    ingest(str(corpus), HashEmbedder())
    kb = KnowledgeBase(embedder=HashEmbedder(), min_score=0.0)
    questions = load_questions()
    ids = {d.id for d in load_corpus(corpus)}
    assert {i for q in questions for i in q["expected"]} <= ids                             # no typo'd document id in the eval set
    probes = evaluate(kb, questions)
    named = [p for p in probes if p.kind == "named"]
    assert all(hit([d["doc_id"] for d in p.tag_docs], p.expected, 1) for p in named), \
        [p.id for p in named if not hit([d["doc_id"] for d in p.tag_docs], p.expected, 1)]  # every named question's tag lookup finds it
    off = [p for p in probes if p.kind == "offtopic"]
    assert all(not p.tag_docs for p in off)                                                 # no off-topic question accidentally matches a tag


def test_the_question_file_is_well_formed():
    seen = set()
    for q in load_questions():
        assert q["id"] not in seen, f"duplicate id {q['id']}"
        seen.add(q["id"])
        assert q["kind"] in ("named", "plain", "vague", "offtopic")
        assert (q["kind"] in ("offtopic", "vague")) == (q["expected"] == [])                  # off-topic and vague expect nothing
        assert q["question"].strip() and isinstance(q["expected"], list)
    assert len(seen) >= 40
