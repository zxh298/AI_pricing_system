"""Load and validate the knowledge corpus: the Markdown files in playbooks/ (see playbooks/README.md).

Each file starts with a small front-matter header, then Markdown. Loading checks the whole corpus and reports every
problem at once, so a bad document is caught before it reaches the vector store. The ingest job reuses this loader.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PREFIX = {"playbook": "PB-", "incident": "INC-", "policy": "POL-", "reference": "REF-"}
TEAMS = ("Promotions team", "SAP Pricing team", "Merchandising team", "Data Platform team",
         "Pricing Data Science team", "Product Data team")
SEVERITIES = ("blocking", "warning")
SECTIONS = {"playbook": ("Symptoms", "Why it happens", "How to confirm", "What to do", "What not to do"),
            "incident": ("Summary", "Timeline", "Root cause", "Resolution", "Lessons")}
KEYS = {"id", "type", "title", "tags", "owner", "severity", "last_reviewed"}
TAG_RE = re.compile(r"^[A-Z][A-Z0-9_]*(:[A-Z0-9_]+)?$")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


class CorpusError(ValueError):
    """Every problem found, one per line."""


@dataclass(frozen=True)
class Doc:
    id: str
    type: str
    title: str
    tags: tuple[str, ...]
    owner: str | None
    severity: str | None
    last_reviewed: str
    body: str
    path: str

    @property
    def content_hash(self) -> str:
        """Changes when anything the retrieval sees changes; lets ingest skip unchanged documents."""
        raw = "\n".join([self.id, self.type, self.title, ",".join(self.tags), self.owner or "", self.severity or "", self.body])
        return hashlib.sha256(raw.encode()).hexdigest()


def parse_front_matter(text: str) -> tuple[dict, str]:
    """`key: value` lines between two `---` lines, then the body. A value in [a, b] is a list."""
    m = FRONT_RE.match(text)
    if not m:
        raise CorpusError("missing front matter (a --- block at the top of the file)")
    meta: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise CorpusError(f"cannot read front matter line {line!r}")
        value = value.split("  #")[0].strip()                       # allow a trailing "  # comment"
        meta[key.strip()] = ([v.strip() for v in value[1:-1].split(",") if v.strip()]
                             if value.startswith("[") and value.endswith("]") else value)
    return meta, m.group(2).strip() + "\n"


def load_doc(path: Path) -> Doc:
    """One document, or a CorpusError listing what is wrong with it."""
    try:
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    except CorpusError as e:
        raise CorpusError(f"{path.name}: {e}") from None
    problems: list[str] = []

    def bad(msg: str) -> None:
        problems.append(f"{path.name}: {msg}")

    for key in sorted(set(meta) - KEYS):
        bad(f"unknown front matter key {key!r}")
    for key in ("id", "type", "title", "last_reviewed"):
        if not meta.get(key):
            bad(f"missing {key}")
    doc_type, doc_id = meta.get("type", ""), meta.get("id", "")
    if doc_type and doc_type not in PREFIX:
        bad(f"type must be one of {sorted(PREFIX)}, not {doc_type!r}")
    elif doc_type and not doc_id.startswith(PREFIX[doc_type]):
        bad(f"a {doc_type} id must start with {PREFIX[doc_type]}")
    if doc_id and path.stem != doc_id:
        bad(f"file name must be {doc_id}.md")
    try:
        date.fromisoformat(meta.get("last_reviewed", ""))
    except ValueError:
        bad("last_reviewed must be a date like 2026-09-21")
    tags = meta.get("tags", [])
    if not isinstance(tags, list):
        bad("tags must be a list like [CODE:reason]")
        tags = []
    for tag in tags:
        if not TAG_RE.match(tag):
            bad(f"tag {tag!r} must look like CODE or CODE:reason")
    owner, severity = meta.get("owner") or None, meta.get("severity") or None
    if doc_type in ("playbook", "incident"):
        if not tags:
            bad(f"a {doc_type} needs at least one tag")
        if owner not in TEAMS:
            bad(f"owner must be one of {list(TEAMS)}")
    if doc_type == "playbook" and severity not in SEVERITIES:
        bad(f"severity must be one of {list(SEVERITIES)}")
    if doc_type in SECTIONS:
        found = [h for h in re.findall(r"^## (.+?)\s*$", body, re.M) if h in SECTIONS[doc_type]]
        if found != list(SECTIONS[doc_type]):
            bad(f"sections must be {list(SECTIONS[doc_type])} in this order, found {found}")
    if problems:
        raise CorpusError("\n".join(problems))
    return Doc(doc_id, doc_type, meta["title"], tuple(tags), owner, severity, meta["last_reviewed"], body, str(path))


def load_corpus(root: str | Path = "playbooks") -> list[Doc]:
    """Every document in `root` (README.md is not one), sorted by id. Raises CorpusError with all problems."""
    docs, problems = [], []
    for path in sorted(Path(root).glob("*.md")):
        if path.name == "README.md":
            continue
        try:
            docs.append(load_doc(path))
        except CorpusError as e:
            problems.append(str(e))
    seen: dict[str, str] = {}
    for d in docs:
        if d.id in seen:
            problems.append(f"{Path(d.path).name}: id {d.id} is also used by {seen[d.id]}")
        seen[d.id] = Path(d.path).name
    if problems:
        raise CorpusError("\n".join(problems))
    return sorted(docs, key=lambda d: d.id)
