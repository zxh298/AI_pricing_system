"""Step 6, part 2: chunker, embedders and the ingest job.

The chunker and embedder tests need nothing. The ingest tests need the Postgres container (docker compose up -d postgres)
with pgvector, and use the throwaway schema `knowledge_test` and the offline hash embedder, so no model is downloaded.
Tests of the real local model run only with RUN_MODEL_TESTS=1.
"""
import math
import os
import shutil
import sys
from pathlib import Path

import pytest

os.environ["KNOWLEDGE_SCHEMA"] = "knowledge_test"

from jobs.rag_ingest.chunker import MAX_CHARS, MIN_CHARS, chunk_doc, pack, sections
from jobs.rag_ingest.corpus import CorpusError, Doc, load_corpus
from jobs.rag_ingest.ingest import docs_version, ingest
from shared import db
from shared.embedding import HashEmbedder, get_embedder
from shared.knowledge_schema import EMBED_DIM

CORPUS = Path(__file__).resolve().parents[1] / "playbooks"

try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pg = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")


def make_doc(body, title="A title"):
    return Doc("PB-X", "playbook", title, ("A:B",), "SAP Pricing team", "warning", "2026-09-21", body, "x")


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


# ======================= chunker =======================
def test_sections_split_on_headings_and_keep_the_intro():
    assert sections("Intro text\n\n## One\nfirst\n\n## Two\nsecond\n") == [("Overview", "Intro text"), ("One", "first"), ("Two", "second")]
    assert sections("## Only\ntext") == [("Only", "text")]                              # no intro: no empty Overview


def test_each_section_becomes_one_chunk_with_the_title_in_front():
    body = "## Symptoms\n" + "It breaks. " * 20 + "\n\n## What to do\n" + "Fix it now. " * 20
    chunks = chunk_doc(make_doc(body, title="Broken thing"))
    assert [(c.chunk_no, c.section) for c in chunks] == [(0, "Symptoms"), (1, "What to do")]
    assert chunks[0].text.startswith("Broken thing - Symptoms\nIt breaks.") and chunks[0].doc_id == "PB-X"


def test_a_tiny_section_is_merged_into_the_previous_chunk():
    body = "## Symptoms\n" + "Long enough to stand alone. " * 10 + "\n\n## Related\nPB-OTHER, REF-THING\n"
    chunks = chunk_doc(make_doc(body))
    assert len(chunks) == 1 and chunks[0].text.endswith("Related: PB-OTHER, REF-THING")
    only_tiny = chunk_doc(make_doc("## Related\nPB-OTHER\n"))                          # nothing before it: keep it
    assert len(only_tiny) == 1 and only_tiny[0].section == "Related"


def test_a_long_section_is_split_at_paragraphs_and_repeats_the_last_one():
    paras = [f"Paragraph {i}. " + " ".join(["word"] * 60) for i in range(6)]          # ~ 300 chars each
    pieces = pack("\n\n".join(paras), limit=800)
    assert len(pieces) > 1 and all(len(p) <= 800 for p in pieces)
    for a, b in zip(pieces, pieces[1:]):
        assert a.split("\n\n")[-1] == b.split("\n\n")[0]                              # the overlap
    assert "\n\n".join(dict.fromkeys(p for piece in pieces for p in piece.split("\n\n"))) == "\n\n".join(paras)   # nothing lost


def test_one_huge_paragraph_is_split_at_sentences_then_at_spaces():
    text = " ".join(f"Sentence number {i} is here." for i in range(80))
    pieces = pack(text, limit=300)
    assert len(pieces) > 1 and all(len(p) <= 300 for p in pieces)
    assert " ".join(pieces).split() == text.split()
    unbroken = pack("x" * 50 + " " + "y" * 50 + " " + "z" * 500, limit=120)              # e.g. a long URL or hash
    assert all(len(p) <= 120 for p in unbroken) and "".join("".join(unbroken).split()) == "x" * 50 + "y" * 50 + "z" * 500


def test_a_list_or_table_is_never_split_when_it_fits():
    table = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
    assert pack("Before.\n\n" + table + "\n\nAfter.") == ["Before.\n\n" + table + "\n\nAfter."]


def test_chunking_the_real_corpus_is_deterministic_and_sane():
    docs = load_corpus(CORPUS)
    first = [c for d in docs for c in chunk_doc(d)]
    assert first == [c for d in docs for c in chunk_doc(d)]
    assert len(first) >= 90 and all(c.text.strip() for c in first)
    assert max(len(c.text) for c in first) <= MAX_CHARS + 120                          # the title prefix adds a little
    per_doc = {}
    for c in first:
        per_doc.setdefault(c.doc_id, []).append(c.chunk_no)
    assert all(nos == list(range(len(nos))) for nos in per_doc.values())               # numbered 0, 1, 2 ... per document
    playbook = [c for c in first if c.doc_id == "PB-PROMO-OVERRIDE"]
    assert [c.section for c in playbook] == ["Symptoms", "Why it happens", "How to confirm", "What to do", "What not to do"]
    assert MIN_CHARS < 200                                                              # sections are not all forced together


# ======================= embedders =======================
def test_the_hash_embedder_is_deterministic_normalised_and_the_right_width():
    e = HashEmbedder()
    v = e.embed_query("A promotion overrides the clearance markdown")
    assert len(v) == EMBED_DIM == e.dim and v == e.embed_query("A promotion overrides the clearance markdown")
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)
    assert e.embed_documents(["a b", "c d"]) == [e.embed_query("a b"), e.embed_query("c d")]
    assert e.embed_query("") == [0.0] * EMBED_DIM                                       # empty text: no crash, no NaN


def test_the_hash_embedder_scores_shared_words_higher():
    e = HashEmbedder()
    q = e.embed_query("promotion overrides the clearance markdown")
    near = e.embed_query("A promotion overrides the clearance markdown in SAP")
    far = e.embed_query("Validity dates overlap an existing condition record")
    assert cosine(q, near) > 0.6 > cosine(q, far) and cosine(q, far) < 0.25


def test_the_embedder_is_chosen_from_config_and_built_once(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    assert isinstance(get_embedder(), HashEmbedder) and get_embedder() is get_embedder()
    monkeypatch.setenv("EMBEDDING_PROVIDER", "mystery")
    with pytest.raises(ValueError, match="unknown EMBEDDING_PROVIDER"):
        get_embedder()


def test_a_missing_model_library_gives_a_helpful_error(monkeypatch):
    from shared.embedding import FastEmbedder
    monkeypatch.setitem(sys.modules, "fastembed", None)                                 # makes `import fastembed` fail
    with pytest.raises(RuntimeError, match="pip install fastembed"):
        FastEmbedder()


@pytest.mark.skipif(not os.environ.get("RUN_MODEL_TESTS"), reason="set RUN_MODEL_TESTS=1 to run the real local model")
def test_the_real_model_ranks_by_meaning():
    from shared.embedding import FastEmbedder
    e = FastEmbedder(cache_dir=os.path.expanduser("~/.cache/ai_pricing_fastembed"))
    docs = e.embed_documents(["A promotion overrides the clearance markdown.",
                              "SAP rejects a price because its validity dates overlap.",
                              "An item has no business rule so the engine cannot set a floor."])
    assert len(docs[0]) == EMBED_DIM
    q = e.embed_query("why are promotions beating our clearance prices?")
    assert max(range(3), key=lambda i: cosine(q, docs[i])) == 0


# ======================= ingest (Postgres) =======================
class Counting(HashEmbedder):
    def __init__(self, name="hash-v1"):
        self.name, self.embedded = name, 0

    def embed_documents(self, texts):
        self.embedded += len(texts)
        return super().embed_documents(texts)


@pytest.fixture
def kb(tmp_path):
    with db.connect() as c:
        c.execute("DROP SCHEMA IF EXISTS knowledge_test CASCADE")
    corpus = tmp_path / "playbooks"
    shutil.copytree(CORPUS, corpus)
    return corpus


def q(sql, args=()):
    with db.connect() as c:
        return c.execute(sql, args).fetchall()


@pg
def test_the_first_ingest_stores_every_document_and_chunk(kb):
    e = Counting()
    r = ingest(str(kb), e)
    assert (r["added"], r["updated"], r["unchanged"], r["removed"], r["docs"]) == (27, 0, 0, 0, 27)
    assert r["chunks"] == e.embedded >= 90 and r["embedder"] == "hash-v1" and len(r["docs_version"]) == 16
    doc = q("SELECT * FROM knowledge_test.docs WHERE doc_id = 'PB-PROMO-OVERRIDE'")[0]
    assert doc["tags"] == ["NOT_EFFECTIVE:PROMOTION"] and doc["owner"] == "Promotions team" and doc["severity"] == "warning"
    assert doc["doc_type"] == "playbook" and doc["embedding_model"] == "hash-v1"
    assert q("SELECT DISTINCT vector_dims(embedding) AS d FROM knowledge_test.chunks") == [{"d": EMBED_DIM}]
    assert q("SELECT value FROM knowledge_test.kb_meta WHERE key = 'docs_version'")[0]["value"] == r["docs_version"]


@pg
def test_running_it_again_changes_nothing_and_embeds_nothing(kb):
    first = ingest(str(kb), Counting())
    e = Counting()
    again = ingest(str(kb), e)
    assert (again["added"], again["updated"], again["unchanged"], again["removed"]) == (0, 0, 27, 0)
    assert e.embedded == 0 and again["docs_version"] == first["docs_version"] and again["chunks"] == first["chunks"]


@pg
def test_an_edited_document_is_re_embedded_and_the_others_are_left_alone(kb):
    first = ingest(str(kb), Counting())
    (kb / "PB-LADDER.md").write_text((kb / "PB-LADDER.md").read_text().replace("## Related", "Extra sentence about packs.\n\n## Related"))
    before = q("SELECT doc_id, ingested_at FROM knowledge_test.docs")
    e = Counting()
    r = ingest(str(kb), e)
    assert (r["added"], r["updated"], r["unchanged"]) == (0, 1, 26) and r["docs_version"] != first["docs_version"]
    assert 0 < e.embedded < 10                                                           # only that document's chunks
    assert "Extra sentence about packs." in " ".join(c["text"] for c in q("SELECT text FROM knowledge_test.chunks WHERE doc_id = 'PB-LADDER'"))
    after = {r["doc_id"]: r["ingested_at"] for r in q("SELECT doc_id, ingested_at FROM knowledge_test.docs")}
    assert [d for d, t in after.items() if t != dict((b["doc_id"], b["ingested_at"]) for b in before)[d]] == ["PB-LADDER"]


@pg
def test_a_review_date_alone_updates_the_row_without_embedding(kb):
    ingest(str(kb), Counting())
    p = kb / "PB-LADDER.md"
    p.write_text(p.read_text().replace("last_reviewed: 2026-09-21", "last_reviewed: 2026-10-05"))
    e = Counting()
    r = ingest(str(kb), e)
    assert e.embedded == 0 and r["unchanged"] == 27
    assert str(q("SELECT last_reviewed FROM knowledge_test.docs WHERE doc_id = 'PB-LADDER'")[0]["last_reviewed"]) == "2026-10-05"


@pg
def test_a_document_removed_from_the_folder_is_deleted_with_its_chunks(kb):
    ingest(str(kb), Counting())
    (kb / "PB-LADDER.md").unlink()
    (kb / "PB-PROMO-OVERRIDE.md").write_text((kb / "PB-PROMO-OVERRIDE.md").read_text().replace("PB-LADDER", "PB-NOT-IN-PRODUCTS"))
    r = ingest(str(kb), Counting())
    assert r["removed"] == 1 and r["docs"] == 26
    assert q("SELECT count(*) AS n FROM knowledge_test.chunks WHERE doc_id = 'PB-LADDER'")[0]["n"] == 0
    assert q("SELECT count(*) AS n FROM knowledge_test.docs")[0]["n"] == 26


@pg
def test_a_different_embedding_model_re_embeds_everything(kb):
    first = ingest(str(kb), Counting("model-a"))
    e = Counting("model-b")
    r = ingest(str(kb), e)
    assert r["updated"] == 27 and e.embedded == r["chunks"] and r["docs_version"] != first["docs_version"]
    assert {d["embedding_model"] for d in q("SELECT embedding_model FROM knowledge_test.docs")} == {"model-b"}


@pg
def test_a_bad_corpus_writes_nothing(kb):
    ingest(str(kb), Counting())
    before = q("SELECT doc_id, content_hash FROM knowledge_test.docs ORDER BY doc_id")
    (kb / "PB-LADDER.md").write_text((kb / "PB-LADDER.md").read_text().replace("owner: Pricing Data Science team", "owner: Nobody"))
    (kb / "PB-NOT-SENT.md").write_text((kb / "PB-NOT-SENT.md").read_text().replace("- ", "- ", 1) + "\n")   # a harmless edit too
    with pytest.raises(CorpusError, match="PB-LADDER.md: owner must be one of"):
        ingest(str(kb), Counting())
    assert q("SELECT doc_id, content_hash FROM knowledge_test.docs ORDER BY doc_id") == before


@pg
def test_a_failure_part_way_leaves_the_store_as_it_was(kb):
    ingest(str(kb), Counting())
    before = q("SELECT doc_id, content_hash FROM knowledge_test.docs ORDER BY doc_id")
    for name in ("PB-LADDER.md", "PB-NOT-SENT.md"):
        (kb / name).write_text((kb / name).read_text() + "\nA new closing remark.\n")

    class Breaks(Counting):
        def embed_documents(self, texts):
            if self.embedded:                                                            # the second document fails
                raise RuntimeError("model crashed")
            return super().embed_documents(texts)
    with pytest.raises(RuntimeError, match="model crashed"):
        ingest(str(kb), Breaks())
    assert q("SELECT doc_id, content_hash FROM knowledge_test.docs ORDER BY doc_id") == before


@pg
def test_an_embedder_of_the_wrong_width_is_refused(kb):
    class Wide(Counting):
        dim = 768
    with pytest.raises(ValueError, match="768-wide"):
        ingest(str(kb), Wide())


@pg
def test_stored_vectors_can_be_searched_by_cosine_distance(kb):
    e = HashEmbedder()
    ingest(str(kb), e)
    query = "[" + ",".join(format(x, ".7g") for x in e.embed_query("promotion overrides the clearance markdown")) + "]"
    top = q("SELECT doc_id, 1 - (embedding <=> %s::vector) AS score FROM knowledge_test.chunks ORDER BY embedding <=> %s::vector LIMIT 3",
            (query, query))
    assert top[0]["doc_id"] == "PB-PROMO-OVERRIDE" and top[0]["score"] > top[2]["score"]


@pg
def test_tags_can_be_searched_with_the_array_operator(kb):
    ingest(str(kb), Counting())
    hit = q("SELECT doc_id FROM knowledge_test.docs WHERE %s = ANY(tags) AND doc_type = 'playbook'", ("API_REJECTED:VALIDITY_OVERLAP",))
    assert [r["doc_id"] for r in hit] == ["PB-VALIDITY-OVERLAP"]
    assert q("SELECT doc_id FROM knowledge_test.docs WHERE 'API_REJECTED:HTTP_503' = ANY(tags) ORDER BY doc_id")[0]["doc_id"] == "PB-API-TRANSIENT"


def test_the_docs_version_depends_on_content_and_model_but_not_order():
    docs = load_corpus(CORPUS)
    v = docs_version(docs, "m")
    assert v == docs_version(list(reversed(docs)), "m") and v != docs_version(docs, "other") and v != docs_version(docs[1:], "m")


@pg
def test_the_command_line_ingests_and_reports_problems(kb, monkeypatch, capsys):
    from jobs.rag_ingest.__main__ import main
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    monkeypatch.setattr("jobs.rag_ingest.__main__.load_dotenv", lambda *a: None)
    assert main(["--path", str(kb)]) == 0
    assert '"added": 27' in capsys.readouterr().out
    (kb / "PB-LADDER.md").write_text("no front matter")
    assert main(["--path", str(kb)]) == 1
    assert "nothing was ingested" in capsys.readouterr().err
