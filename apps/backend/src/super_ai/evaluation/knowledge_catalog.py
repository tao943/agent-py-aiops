"""Content-free audits for reviewed troubleshooting-card catalogs."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from super_ai.documents.batch_import import KNOWLEDGE_CARD_CHUNKING, discover_markdown_files
from super_ai.documents.indexing import chunk_document_text

MIN_CHUNKS = 1
TARGET_MAX_CHUNKS = 10
HARD_MAX_CHUNKS = 12
OPERATIONAL_HEADINGS = (
    "适用现象",
    "候选原因",
    "建议证据",
    "如何区分",
    "安全恢复边界",
    "恢复后验证",
)


def audit_catalog(root: Path) -> dict[str, object]:
    """Return a deterministic, content-free chunk audit for Markdown cards."""
    documents: list[dict[str, object]] = []
    excluded = cast(list[str], KNOWLEDGE_CARD_CHUNKING["excludedHeadings"])
    for path in discover_markdown_files(root):
        chunks = chunk_document_text(
            path.read_text(encoding="utf-8"),
            strategy="markdown-heading",
            excluded_headings=tuple(excluded),
        )
        heading_paths = tuple(
            dict.fromkeys(chunk.heading_path for chunk in chunks if chunk.heading_path)
        )
        missing = tuple(
            heading
            for heading in OPERATIONAL_HEADINGS
            if not any(_path_contains_heading(path_value, heading) for path_value in heading_paths)
        )
        if missing:
            raise ValueError(f"{path.name} is missing operational chunks: {', '.join(missing)}")
        if len(chunks) < MIN_CHUNKS or len(chunks) > HARD_MAX_CHUNKS:
            raise ValueError(
                f"{path.name} must produce {MIN_CHUNKS}..{HARD_MAX_CHUNKS} chunks; "
                f"got {len(chunks)}"
            )
        documents.append(
            {
                "filename": path.name,
                "chunkCount": len(chunks),
                "headingPaths": list(heading_paths),
                "reviewRequired": len(chunks) > TARGET_MAX_CHUNKS,
            }
        )
    return {"totalDocuments": len(documents), "documents": documents}


def _path_contains_heading(path_value: str, heading: str) -> bool:
    return heading in (item.strip() for item in path_value.split("/"))
