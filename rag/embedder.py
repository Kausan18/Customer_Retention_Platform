"""
Chunking and embedding utilities for the RAG policy pipeline.
Pure transformation functions — no DB access here.
"""
import re
from pathlib import Path

from sentence_transformers import SentenceTransformer

_MODEL_NAME = "all-MiniLM-L6-v2"
_model = None  # lazy-loaded singleton, avoids reloading the model per call

MIN_CHUNK_CHARS = 50


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _slugify(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "_", slug)
    return slug.strip("_")


def _parse_header(header_text: str) -> dict:
    """Parses the block before the first '##' for Document ID / Version / Effective date."""
    doc_id_match = re.search(r"Document ID:\*{0,2}\s*(\S+)", header_text, re.IGNORECASE)
    version_match = re.search(r"Version:\*{0,2}\s*(\S+)", header_text, re.IGNORECASE)
    effective_match = re.search(r"Effective date:\*{0,2}\s*(\S+)", header_text, re.IGNORECASE)

    return {
        "document_id": doc_id_match.group(1) if doc_id_match else None,
        "version": version_match.group(1) if version_match else None,
        "effective_from": effective_match.group(1) if effective_match else None,
    }


def chunk_document(filepath: str) -> list[dict]:
    """
    Reads a policy .md file and splits it into chunks on '##' headings.
    Returns a list of dicts: document_id, section_id, content, char_count.
    Raises ValueError if no Document ID is found in the header.
    """
    path = Path(filepath)
    text = path.read_text(encoding="utf-8")

    parts = re.split(r"(?m)^##\s+", text)
    header_text = parts[0]
    meta = _parse_header(header_text)

    if not meta["document_id"]:
        raise ValueError(
            f"{filepath}: no 'Document ID' found in header block. "
            f"Add a line like 'Document ID: refund_policy_v1' before the first '##' heading."
        )

    chunks = []
    for section_block in parts[1:]:
        lines = section_block.split("\n", 1)
        heading = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        full_content = f"## {heading}\n{body}".strip()

        if len(full_content) < MIN_CHUNK_CHARS:
            continue

        chunks.append({
            "document_id": meta["document_id"],
            "section_id": _slugify(heading),
            "content": full_content,
            "char_count": len(full_content),
        })

    if not chunks:
        raise ValueError(f"{filepath}: no '##' sections found, or all were under {MIN_CHUNK_CHARS} chars.")

    return chunks, meta


def embed_chunks(texts: list[str]) -> list[list[float]]:
    """
    Embeds a list of strings with all-MiniLM-L6-v2, normalized so cosine
    similarity reduces to a dot product. Returns plain Python float lists
    (not numpy arrays) so they can be JSON-serialized for SQLite storage.
    """
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vectors]