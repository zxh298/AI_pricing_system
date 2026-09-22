"""The read side of the knowledge base (Postgres + pgvector): tag lookup first, similarity search second.

Tag lookup is exact and free: a problem the tools have already named (`NOT_EFFECTIVE` with reason `PROMOTION`) maps to its
playbook through the tags in the documents' front matter, and tags can also be recognised in a free-text question
("HTTP 429", "promotions", "validity overlaps"). Similarity search over the chunk embeddings covers everything else and
returns only chunks above a relevance threshold, so an unrelated question finds nothing instead of the least-bad document.
The database is only read; the ingest job is the only writer.
"""
from __future__ import annotations

import re

from psycopg.errors import UndefinedTable

from shared import db
from shared.config import load_config
from shared.embedding import Embedder, get_embedder, vector_literal
from shared.knowledge_schema import schema

TYPE_RANK = {"playbook": 0, "incident": 1, "reference": 2, "policy": 3}
MAX_TAG_DOCS = 4
MAX_SIMILAR_DOCS = 2
RELATED_MIN_SCORE = 0.80          # once a tag has found a playbook, only very close extra passages are worth adding
MAX_STEPS_CHARS = 700
MAX_PLAYBOOK_CHARS = 2600
MAX_OTHER_CHARS = 900


class KnowledgeMissing(RuntimeError):
    """The knowledge base has not been built (the ingest job has not run)."""


def tags_for(code: str | None, reason: str | None) -> tuple[list[str], list[str]]:
    """(specific tags, fallback tags) for a problem the tools named. A reason may list several rules joined by commas
    (`BELOW_FLOOR,PRICE_ABOVE_LAST_WEEK`). The bare code is only a fallback: it finds the glossary page for the code."""
    if not code or not code.strip():
        return [], []
    code = code.strip().upper()
    reasons = [r.strip().upper() for r in (reason or "").split(",") if r.strip()]
    return ([f"{code}:{r}" for r in reasons] or [code]), [code]


def tags_in_query(query: str, known: set[str]) -> list[str]:
    """Known tags mentioned in a question, in words or as a number: 'validity overlaps' finds
    API_REJECTED:VALIDITY_OVERLAP, 'HTTP 429' or '429' finds API_REJECTED:HTTP_429, and a tag that is a bare code
    (PRICE_MISMATCH) is found by the words of the code. The words matched are the reason of a `CODE:reason` tag, so a
    question about a code alone does not pull in every reason of it."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", query.lower()) + " "
    hits = []
    for tag in sorted(known):
        reason = tag.split(":", 1)[1] if ":" in tag else tag
        phrase = re.escape(reason.lower().replace("_", " "))
        number = re.fullmatch(r"HTTP_(\d{3})", reason)
        if re.search(rf"(?<![a-z0-9]){phrase}s?(?![a-z0-9])", text) or (number and f" {number.group(1)} " in text):
            hits.append(tag)
    return hits


def join_chunks(texts: list[str]) -> str:
    """One section's chunk texts, in order, as a single text: without the title prefix each starts with, and without
    the paragraph the chunker repeats at the start of the next chunk."""
    blocks: list[str] = []
    for text in texts:
        for block in (text.split("\n", 1)[1] if "\n" in text else text).split("\n\n"):
            if block.strip() and (not blocks or block != blocks[-1]):
                blocks.append(block)
    return "\n\n".join(blocks)


def render(chunks: list[dict], limit: int) -> str:
    """The chunks of one document as text, without the repeated title prefix, cut at `limit` characters."""
    out = "\n\n".join(f"## {c['section']}\n{c['text'].split(chr(10), 1)[1] if chr(10) in c['text'] else c['text']}" for c in chunks)
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + " ..."


class KnowledgeBase:
    def __init__(self, database_url: str | None = None, embedder: Embedder | None = None, min_score: float | None = None):
        self.url, self._embedder = database_url, embedder
        self.min_score = min_score if min_score is not None else load_config().search_min_score

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = get_embedder()                        # loads the model on first use
        return self._embedder

    def warm(self) -> None:
        """Load the embedding model now (a server does this at startup instead of on the first question)."""
        self.embedder.embed_query("warm up")

    def _rows(self, sql: str, args=()) -> list[dict]:
        try:
            with db.connect(self.url) as con:
                return con.execute(sql, args).fetchall()
        except UndefinedTable:
            raise KnowledgeMissing("the knowledge base has not been built yet; run: python -m jobs.rag_ingest") from None

    def docs_version(self) -> str | None:
        try:
            rows = self._rows(f"SELECT value FROM {schema()}.kb_meta WHERE key = 'docs_version'")
        except KnowledgeMissing:
            return None
        return rows[0]["value"] if rows else None

    def known_tags(self) -> set[str]:
        return {r["tag"] for r in self._rows(f"SELECT DISTINCT unnest(tags) AS tag FROM {schema()}.docs")}

    def by_tags(self, tags: list[str]) -> list[dict]:
        """Documents carrying any of `tags`: playbooks first, then incidents, reference pages, policies."""
        if not tags:
            return []
        s = schema()
        docs = self._rows(f"SELECT doc_id, doc_type, title, owner, severity, tags FROM {s}.docs WHERE tags && %s::text[]", (tags,))
        docs.sort(key=lambda d: (TYPE_RANK.get(d["doc_type"], 9), d["doc_id"]))
        docs = docs[:MAX_TAG_DOCS]
        chunks = self._rows(f"SELECT doc_id, section, text FROM {s}.chunks WHERE doc_id = ANY(%s) ORDER BY doc_id, chunk_no",
                            ([d["doc_id"] for d in docs],))
        for d in docs:
            own = [c for c in chunks if c["doc_id"] == d["doc_id"]]
            d["matched"] = sorted(set(d["tags"]) & set(tags))
            d["text"] = render(own, MAX_PLAYBOOK_CHARS if d["doc_type"] == "playbook" else MAX_OTHER_CHARS)
        return docs

    def playbooks_for(self, tags: list[str]) -> dict[str, dict]:
        """For each tag, the playbook that carries it: id, title, owning team, severity and its 'What to do' text. Tag
        lookup only, so it needs no embedding model and is cheap enough to attach to every diagnosis."""
        if not tags:
            return {}
        s = schema()
        rows = self._rows(f"""SELECT d.doc_id, d.title, d.owner, d.severity, d.tags, c.text
                              FROM {s}.docs d LEFT JOIN {s}.chunks c ON c.doc_id = d.doc_id AND c.section = 'What to do'
                              WHERE d.doc_type = 'playbook' AND d.tags && %s::text[]
                              ORDER BY d.doc_id, c.chunk_no""", (tags,))                     # a long section has several chunks: the first one
        out = {}
        for tag in tags:
            row = next((r for r in rows if tag in r["tags"]), None)
            if row:
                steps = join_chunks([r["text"] for r in rows if r["doc_id"] == row["doc_id"] and r["text"]])
                if len(steps) > MAX_STEPS_CHARS:
                    steps = steps[:MAX_STEPS_CHARS].rsplit(" ", 1)[0] + " ..."
                out[tag] = {"doc_id": row["doc_id"], "title": row["title"], "owner": row["owner"],
                            "severity": row["severity"], "what_to_do": steps or None}
        return out

    def scores(self, query: str) -> list[dict]:
        """Every chunk with its similarity to the question, best first. Used to measure retrieval; search uses `similar`."""
        vec = vector_literal(self.embedder.embed_query(query))
        s = schema()
        rows = self._rows(f"""SELECT c.doc_id, d.doc_type, c.section, 1 - (c.embedding <=> %s::vector) AS score
                              FROM {s}.chunks c JOIN {s}.docs d USING (doc_id) ORDER BY score DESC""", (vec,))
        return [{**r, "score": float(r["score"])} for r in rows]

    def search(self, query: str | None = None, error_code: str | None = None, reason: str | None = None) -> dict:
        """Tag lookup first, similarity second. Returns {version, found, tags_used, docs (by tag), similar}. The one
        place this logic lives: the search_docs tool and the retrieval evaluation both call it."""
        version, known = self.docs_version(), self.known_tags()
        specific, fallback = tags_for(error_code, reason)
        used = list(dict.fromkeys(specific + tags_in_query(query or "", known)))
        docs = self.by_tags(used)
        if not docs and fallback:                                  # no playbook for that reason: the code's glossary page
            used, docs = fallback, self.by_tags(fallback)
        bar = max(self.min_score, RELATED_MIN_SCORE) if any(d["doc_type"] == "playbook" for d in docs) else None
        similar = self.similar(query, {d["doc_id"] for d in docs}, bar) if (query or "").strip() else []
        found = any(d["doc_type"] in ("playbook", "incident") for d in docs) or bool(similar)
        return {"version": version, "found": found, "tags_used": used, "docs": docs, "similar": similar}

    def similar(self, query: str, exclude: set[str] = frozenset(), min_score: float | None = None) -> list[dict]:
        """The best chunk of each document that is close enough to the question, best first."""
        bar = self.min_score if min_score is None else min_score
        vec = vector_literal(self.embedder.embed_query(query))
        s = schema()
        rows = self._rows(f"""SELECT DISTINCT ON (c.doc_id) c.doc_id, d.doc_type, d.title, d.owner, d.severity, d.tags,
                                     c.section, c.text, 1 - (c.embedding <=> %s::vector) AS score
                              FROM {s}.chunks c JOIN {s}.docs d USING (doc_id)
                              ORDER BY c.doc_id, c.embedding <=> %s::vector""", (vec, vec))
        rows = sorted((r for r in rows if r["doc_id"] not in exclude and r["score"] >= bar),
                      key=lambda r: -r["score"])[:MAX_SIMILAR_DOCS]
        for r in rows:
            r["text"] = render([r], MAX_OTHER_CHARS)
        return rows
