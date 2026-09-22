"""The knowledge base (RAG) in Postgres with pgvector (Cloud SQL for PostgreSQL on GCP). Schema name from KNOWLEDGE_SCHEMA.

docs      one row per document: type, title, tags, owner, severity, content hash, and which embedding model built its chunks
chunks    the text pieces the retrieval searches, each with its embedding
kb_meta   `docs_version`: changes whenever any document or the embedding model changes (cache key for search results)

CREATE EXTENSION needs a role that may create extensions (the local Postgres user is a superuser; on Cloud SQL use the
cloudsqlsuperuser role once). The vector width is fixed: an embedding model with another width needs a new schema.
"""
import os

EMBED_DIM = 384


def schema() -> str:
    return os.environ.get("KNOWLEDGE_SCHEMA", "knowledge")


def ddl() -> str:
    s = schema()
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.docs (
    doc_id TEXT PRIMARY KEY, doc_type TEXT NOT NULL, title TEXT NOT NULL, tags TEXT[] NOT NULL DEFAULT '{{}}',
    owner TEXT, severity TEXT, last_reviewed DATE NOT NULL, content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL, ingested_at TIMESTAMPTZ NOT NULL);
CREATE INDEX IF NOT EXISTS docs_tags ON {s}.docs USING GIN (tags);
CREATE TABLE IF NOT EXISTS {s}.chunks (
    doc_id TEXT NOT NULL REFERENCES {s}.docs (doc_id) ON DELETE CASCADE, chunk_no INT NOT NULL,
    section TEXT NOT NULL, text TEXT NOT NULL, embedding vector({EMBED_DIM}) NOT NULL,
    PRIMARY KEY (doc_id, chunk_no));
CREATE TABLE IF NOT EXISTS {s}.kb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
