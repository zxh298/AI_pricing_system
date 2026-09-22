"""Step 6, part 1: the knowledge corpus (playbooks/) and its loader. Needs no database and no model.

The corpus is checked against what the system can actually emit, read from the source files: a new verdict, skip
reason, validation rule or SAP code makes these tests fail until a playbook covers it, and a playbook cannot use a tag
the system never produces.
"""
import re
from pathlib import Path

import pytest

from jobs.pricing_engine.sender import RETRYABLE_STATUS
from jobs.rag_ingest.corpus import PREFIX, TEAMS, CorpusError, load_corpus, load_doc, parse_front_matter

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "playbooks"
VERDICT_CODES = {"NOT_IN_LIST", "MISSING_PRICE", "RULE_VIOLATION", "API_REJECTED", "NOT_EFFECTIVE", "PRICE_MISMATCH",
                 "MISSING_IN_SAP"}


# ======================= the loader =======================
def doc(**over):
    meta = {"id": "PB-X", "type": "playbook", "title": "A title: with a colon", "tags": "[A_B:C_D, E_F]", "owner": "SAP Pricing team",
            "severity": "warning", "last_reviewed": "2026-09-21"} | over
    head = "\n".join(f"{k}: {v}" for k, v in meta.items() if v is not None)
    body = "\n\n".join(f"## {s}\ntext" for s in ("Symptoms", "Why it happens", "How to confirm", "What to do", "What not to do"))
    return f"---\n{head}\n---\n\n{body}\n"


def write(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    return tmp_path / name


def test_front_matter_is_parsed_including_lists_colons_and_comments():
    meta, body = parse_front_matter("---\nid: PB-X\ntitle: One: two\ntags: [A:B, C]   # a comment\nempty: []\n---\n\nBody\n")
    assert meta == {"id": "PB-X", "title": "One: two", "tags": ["A:B", "C"], "empty": []}
    assert body == "Body\n"
    with pytest.raises(CorpusError, match="missing front matter"):
        parse_front_matter("no header here")
    with pytest.raises(CorpusError, match="cannot read"):
        parse_front_matter("---\nnot a key value line\n---\nbody")


def test_a_valid_document_loads(tmp_path):
    d = load_doc(write(tmp_path, "PB-X.md", doc()))
    assert (d.id, d.type, d.tags, d.owner, d.severity) == ("PB-X", "playbook", ("A_B:C_D", "E_F"), "SAP Pricing team", "warning")
    assert d.title == "A title: with a colon" and d.body.startswith("## Symptoms")


@pytest.mark.parametrize("over,needle", [
    ({"owner": "The Wizards"}, "owner must be one of"),
    ({"owner": None}, "owner must be one of"),
    ({"severity": "meh"}, "severity must be one of"),
    ({"tags": "[]"}, "needs at least one tag"),
    ({"tags": "[lowercase]"}, "must look like CODE"),
    ({"tags": "NOT_A_LIST"}, "tags must be a list"),
    ({"last_reviewed": "yesterday"}, "must be a date"),
    ({"type": "essay"}, "type must be one of"),
    ({"id": "INC-X", "type": "playbook"}, "must start with PB-"),
    ({"title": None}, "missing title"),
    ({"colour": "blue"}, "unknown front matter key"),
])
def test_bad_front_matter_is_reported(tmp_path, over, needle):
    name = "INC-X.md" if over.get("id") == "INC-X" else "PB-X.md"
    with pytest.raises(CorpusError, match=needle):
        load_doc(write(tmp_path, name, doc(**over)))


def test_the_file_name_must_match_the_id_and_sections_must_be_in_order(tmp_path):
    with pytest.raises(CorpusError, match=r"file name must be PB-X\.md"):
        load_doc(write(tmp_path, "something-else.md", doc()))
    swapped = doc().replace("## Symptoms", "## TMP").replace("## What to do", "## Symptoms").replace("## TMP", "## What to do")
    with pytest.raises(CorpusError, match="sections must be"):
        load_doc(write(tmp_path, "PB-X.md", swapped))
    missing = doc().replace("## What not to do", "## Something else")
    with pytest.raises(CorpusError, match="sections must be"):
        load_doc(write(tmp_path, "PB-X.md", missing))


def test_the_whole_corpus_is_checked_and_every_problem_is_reported(tmp_path):
    write(tmp_path, "README.md", "not a document")
    write(tmp_path, "PB-A.md", doc(id="PB-A"))
    write(tmp_path, "PB-B.md", doc(id="PB-B", owner="Nobody"))
    write(tmp_path, "PB-C.md", doc(id="PB-C", severity="meh"))
    with pytest.raises(CorpusError) as e:
        load_corpus(tmp_path)
    assert "PB-B.md" in str(e.value) and "PB-C.md" in str(e.value) and "PB-A.md" not in str(e.value)
    (tmp_path / "PB-B.md").unlink()
    (tmp_path / "PB-C.md").unlink()
    assert [d.id for d in load_corpus(tmp_path)] == ["PB-A"]                    # README.md is not a document


def test_the_content_hash_changes_with_anything_retrieval_sees(tmp_path):
    base = load_doc(write(tmp_path, "PB-X.md", doc()))
    same = load_doc(write(tmp_path, "PB-X.md", doc(last_reviewed="2026-10-01")))     # a review date is not content
    assert base.content_hash == same.content_hash
    for over in ({"owner": "Data Platform team"}, {"tags": "[A_B]"}, {"title": "Another"}, {"severity": "blocking"}):
        assert load_doc(write(tmp_path, "PB-X.md", doc(**over))).content_hash != base.content_hash
    edited = write(tmp_path, "PB-X.md", doc() + "\nMore text.\n")
    assert load_doc(edited).content_hash != base.content_hash


# ======================= the real corpus =======================
@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CORPUS)


def test_the_corpus_loads_and_has_every_kind_of_document(corpus):
    by_type = {t: [d for d in corpus if d.type == t] for t in PREFIX}
    assert len(by_type["playbook"]) >= 17 and len(by_type["incident"]) >= 3
    assert len(by_type["policy"]) >= 3 and len(by_type["reference"]) >= 4
    assert len({d.id for d in corpus}) == len(corpus)


def test_the_readme_names_the_same_teams_as_the_loader():
    readme = (CORPUS / "README.md").read_text()
    assert all(team in readme for team in TEAMS)


def test_references_between_documents_point_at_documents_that_exist(corpus):
    ids = {d.id for d in corpus}
    broken = {(d.id, ref) for d in corpus for ref in re.findall(r"\b(?:PB|INC|POL|REF)-[A-Z0-9-]+\b", d.body) if ref not in ids}
    assert broken == set()


def test_no_placeholders_are_left_in_the_text(corpus):
    assert [d.id for d in corpus if re.search(r"\b(TODO|TBD|FIXME|lorem)\b", d.body, re.I)] == []


# ======================= coverage against what the system can emit =======================
def found(path, pattern):
    return set(re.findall(pattern, (ROOT / path).read_text()))


def emittable_tags():
    """Every CODE:reason (or bare CODE) the diagnostic tools can report, read from the source of the systems that
    produce them, so this list cannot go stale by itself."""
    engine = (found("jobs/pricing_engine/engine.py", r'row\["reason"\] = "([A-Z_]+)"')
              | found("shared/pricing_rules.py", r'"([A-Z_]+_ABOVE_PRICE)"'))
    rules = found("jobs/weekly_pipeline/validate.py", r'bad\(r, "([A-Z_]+)"')
    sap = found("services/mock_sap/main.py", r'(?:"code": "|return None, \(")([A-Z_]{4,})"') - {"OK"}
    http = {f"HTTP_{n}" for n in RETRYABLE_STATUS | {400, 401, 403, 413, 422}} | {"TRANSPORT"}
    assert {"MISSING_RULE", "FLOOR_ABOVE_PRICE"} <= engine and {"BELOW_FLOOR", "LADDER_VIOLATION"} <= rules
    assert {"VALIDITY_OVERLAP", "NOT_ON_LIST", "BAD_FORMAT", "IDEMPOTENCY_CONFLICT"} <= sap       # the readers still find things
    return ({f"MISSING_PRICE:{r}" for r in engine} | {"MISSING_PRICE:NO_RECOMMENDATION", "MISSING_PRICE:NOT_SENT"}
            | {f"RULE_VIOLATION:{r}" for r in rules} | {f"API_REJECTED:{r}" for r in sap | http}
            | {"NOT_IN_LIST:NOT_ON_LIST", "NOT_EFFECTIVE:PROMOTION", "PRICE_MISMATCH", "MISSING_IN_SAP"})


def covered(tag, tags):
    return tag in tags or tag.split(":")[0] in tags                              # a bare CODE covers every reason of it


def test_every_code_and_reason_the_system_can_emit_has_a_playbook(corpus):
    playbook_tags = {t for d in corpus if d.type == "playbook" for t in d.tags}
    missing = sorted(t for t in emittable_tags() if not covered(t, playbook_tags))
    assert missing == [], f"no playbook covers: {missing}"


def test_no_document_uses_a_tag_the_system_never_produces(corpus):
    emittable = emittable_tags()
    unknown = {(d.id, t) for d in corpus for t in d.tags if t not in emittable and t not in VERDICT_CODES}
    assert unknown == set()


def test_a_rule_violation_reason_with_several_rules_is_covered_by_one_tag_each(corpus):
    tags = {t for d in corpus if d.type == "playbook" for t in d.tags}
    assert {"RULE_VIOLATION:BELOW_FLOOR", "RULE_VIOLATION:PRICE_ABOVE_LAST_WEEK"} <= tags   # 'BELOW_FLOOR,PRICE_ABOVE_LAST_WEEK' splits on commas
