"""Split a document into chunks for retrieval.

A chunk is one section of the document, prefixed with the document title so it makes sense on its own. A section longer
than MAX_CHARS (about 275 tokens) is split at paragraph boundaries, and at sentence boundaries if a single paragraph is
still too long, with the last paragraph repeated at the start of the next chunk so no thought is cut in half. A very short
section (for example "Related") is merged into the chunk before it. Chunking is deterministic: the same document always
gives the same chunks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from jobs.rag_ingest.corpus import Doc

MAX_CHARS = 1100
MIN_CHARS = 140


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    chunk_no: int
    section: str
    text: str


def sections(body: str) -> list[tuple[str, str]]:
    """[(heading, text)] for each `## ` section; text before the first heading is the 'Overview'."""
    parts = re.split(r"^## (.+?)\s*$", body, flags=re.M)          # [intro, heading, text, heading, text, ...]
    out = [("Overview", parts[0].strip())] + [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]
    return [(name, text) for name, text in out if text]


def _split_long(block: str, limit: int) -> list[str]:
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", block):
        while len(sentence) > limit:                                # a sentence with no break: cut at a space
            cut = sentence.rfind(" ", 0, limit)
            if cut <= 0:                                            # no space to cut at: cut in the middle of the word
                cut = limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    return pieces + ([current] if current else [])


def pack(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Greedily fill chunks with whole paragraphs (blank-line separated blocks, so a list or table stays together)."""
    blocks = [b for para in re.split(r"\n\s*\n", text) for b in ([para.strip()] if len(para.strip()) <= limit
                                                                  else _split_long(para.strip(), limit)) if b]
    chunks, current = [], []
    for block in blocks:
        if current and len("\n\n".join(current + [block])) > limit:
            chunks.append("\n\n".join(current))
            current = [current[-1], block] if len(current[-1]) + len(block) + 2 <= limit else [block]   # repeat the last block
        else:
            current.append(block)
    return chunks + (["\n\n".join(current)] if current else [])


def chunk_doc(doc: Doc) -> list[Chunk]:
    pieces: list[list[str]] = []                                    # [section, text]
    for name, text in sections(doc.body):
        for piece in pack(text):
            if pieces and len(piece) < MIN_CHARS:
                pieces[-1][1] += f"\n\n{name}: {piece}"             # too short to stand alone
            else:
                pieces.append([name, piece])
    return [Chunk(doc.id, i, name, f"{doc.title} - {name}\n{text}") for i, (name, text) in enumerate(pieces)]
