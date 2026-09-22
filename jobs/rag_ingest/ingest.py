"""Ingest the knowledge corpus into Postgres + pgvector: chunk, embed, store.

    python -m jobs.rag_ingest [--path playbooks]

Idempotent and incremental: a document whose content and embedding model are unchanged is skipped (nothing is embedded
again), a changed one has its chunks replaced, and a document no longer in the folder is deleted with its chunks. The
whole corpus is validated first, and everything is written in one transaction, so a bad document or a failure leaves the
store exactly as it was. `docs_version` changes whenever anything searchable changes.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from jobs.rag_ingest.chunker import chunk_doc
from jobs.rag_ingest.corpus import load_corpus
from shared import db
from shared.embedding import Embedder, get_embedder, vector_literal
from shared.knowledge_schema import EMBED_DIM, ddl, schema


def docs_version(docs, embedder_name: str) -> str:
    raw = "\n".join([embedder_name] + [f"{d.id}:{d.content_hash}" for d in sorted(docs, key=lambda d: d.id)])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def ingest(root: str = "playbooks", embedder: Embedder | None = None, database_url: str | None = None) -> dict:
    docs = load_corpus(root)                                        # raises CorpusError before anything is touched
    embedder = embedder or get_embedder()
    if embedder.dim != EMBED_DIM:
        raise ValueError(f"the embedder produces {embedder.dim}-wide vectors but the store holds {EMBED_DIM}")
    s, now = schema(), datetime.now(timezone.utc)
    summary = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    with db.connect(database_url) as con:
        con.execute(ddl())
        stored = {r["doc_id"]: r for r in con.execute(
            f"SELECT doc_id, content_hash, embedding_model, last_reviewed FROM {s}.docs").fetchall()}
        for d in docs:
            prev = stored.get(d.id)
            if prev and prev["content_hash"] == d.content_hash and prev["embedding_model"] == embedder.name:
                summary["unchanged"] += 1
                if prev["last_reviewed"] != date.fromisoformat(d.last_reviewed):     # a review date is not worth re-embedding
                    con.execute(f"UPDATE {s}.docs SET last_reviewed = %s WHERE doc_id = %s", (d.last_reviewed, d.id))
                continue
            chunks = chunk_doc(d)
            vectors = embedder.embed_documents([c.text for c in chunks])
            con.execute(f"""INSERT INTO {s}.docs (doc_id, doc_type, title, tags, owner, severity, last_reviewed,
                                content_hash, embedding_model, ingested_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (doc_id) DO UPDATE SET doc_type = EXCLUDED.doc_type, title = EXCLUDED.title,
                                tags = EXCLUDED.tags, owner = EXCLUDED.owner, severity = EXCLUDED.severity,
                                last_reviewed = EXCLUDED.last_reviewed, content_hash = EXCLUDED.content_hash,
                                embedding_model = EXCLUDED.embedding_model, ingested_at = EXCLUDED.ingested_at""",
                        (d.id, d.type, d.title, list(d.tags), d.owner, d.severity, d.last_reviewed, d.content_hash,
                         embedder.name, now))
            con.execute(f"DELETE FROM {s}.chunks WHERE doc_id = %s", (d.id,))
            with con.cursor() as cur:
                cur.executemany(f"INSERT INTO {s}.chunks (doc_id, chunk_no, section, text, embedding) "
                                "VALUES (%s,%s,%s,%s,%s::vector)",
                                [(c.doc_id, c.chunk_no, c.section, c.text, vector_literal(v)) for c, v in zip(chunks, vectors)])
            summary["updated" if prev else "added"] += 1
        gone = sorted(set(stored) - {d.id for d in docs})
        if gone:
            con.execute(f"DELETE FROM {s}.docs WHERE doc_id = ANY(%s)", (gone,))              # chunks go with them
            summary["removed"] = len(gone)
        version = docs_version(docs, embedder.name)
        con.execute(f"INSERT INTO {s}.kb_meta (key, value) VALUES ('docs_version', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (version,))
        chunk_count = con.execute(f"SELECT count(*) AS n FROM {s}.chunks").fetchone()["n"]
    return {**summary, "docs": len(docs), "chunks": chunk_count, "docs_version": version, "embedder": embedder.name}
