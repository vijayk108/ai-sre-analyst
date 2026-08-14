"""
KB loader.

Reads markdown runbooks, splits them by H2 sections, embeds each
chunk via Vertex AI text-embedding-005, and upserts into Qdrant.
Run once on bootstrap or via a Helm post-install hook.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import uuid
from pathlib import Path

import vertexai
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kb-loader")

EMBED_MODEL = "text-embedding-005"
VECTOR_DIM = 768
SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split by H2 headings -> [(title, body), ...]."""
    parts = SECTION_RE.split(text)
    if len(parts) == 1:
        return [("intro", text.strip())]
    chunks: list[tuple[str, str]] = []
    # parts looks like [preamble, h2title, body, h2title, body, ...]
    if parts[0].strip():
        chunks.append(("intro", parts[0].strip()))
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        chunks.append((title, body))
    return chunks


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runbooks-dir", required=True)
    p.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL"))
    p.add_argument("--collection", default="runbooks")
    p.add_argument("--project", default=os.getenv("GCP_PROJECT"))
    p.add_argument("--location", default=os.getenv("GCP_LOCATION", "us-central1"))
    args = p.parse_args()

    if not (args.qdrant_url and args.project):
        log.error("QDRANT_URL and GCP_PROJECT are required")
        return 2

    vertexai.init(project=args.project, location=args.location)
    embedder = TextEmbeddingModel.from_pretrained(EMBED_MODEL)
    client = QdrantClient(url=args.qdrant_url)

    existing = {c.name for c in client.get_collections().collections}
    if args.collection not in existing:
        client.create_collection(
            collection_name=args.collection,
            vectors_config=qm.VectorParams(
                size=VECTOR_DIM, distance=qm.Distance.COSINE
            ),
        )
        log.info("Created collection %s", args.collection)

    points: list[qm.PointStruct] = []
    for path in sorted(Path(args.runbooks_dir).rglob("*.md")):
        body = path.read_text()
        for title, chunk in chunk_markdown(body):
            text = chunk.strip()
            if len(text) < 40:
                continue
            emb = embedder.get_embeddings(
                [TextEmbeddingInput(text=text, task_type="RETRIEVAL_DOCUMENT")]
            )[0].values
            points.append(
                qm.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=emb,
                    payload={
                        "title": f"{path.stem} :: {title}",
                        "text": text,
                        "source": str(path.relative_to(args.runbooks_dir)),
                    },
                )
            )
            log.info("Embedded %s :: %s (%d chars)", path.name, title, len(text))

    if not points:
        log.warning("No runbooks found under %s", args.runbooks_dir)
        return 0

    client.upsert(collection_name=args.collection, points=points)
    log.info("Upserted %d runbook chunks into %s", len(points), args.collection)
    return 0


if __name__ == "__main__":
    sys.exit(main())
