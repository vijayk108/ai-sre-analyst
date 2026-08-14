"""
Qdrant-backed vector store for runbook RAG.

Embeds runbooks at boot (kb-loader pushes raw markdown; the embedding
happens here) and provides:

- search(query, top_k)        -> nearest runbook chunks
- upsert_lesson(...)          -> append SRE corrections back into the
                                 store so the system gets smarter over
                                 time (closed-loop learning).

We use Vertex AI text-embedding-005 because we're already authenticated
to Vertex for Gemini, which keeps the credential surface small.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

from app.models import RunbookHit

log = logging.getLogger("ai-analyst.vectorstore")

EMBED_MODEL = "text-embedding-005"
VECTOR_DIM = 768


class RunbookStore:
    def __init__(self, qdrant_url: str, collection: str = "runbooks"):
        self._client = AsyncQdrantClient(url=qdrant_url)
        self._collection = collection
        self._embedder = TextEmbeddingModel.from_pretrained(EMBED_MODEL)

    async def ensure_collection(self) -> None:
        existing = await self._client.get_collections()
        names = {c.name for c in existing.collections}
        if self._collection not in names:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=qm.VectorParams(
                    size=VECTOR_DIM, distance=qm.Distance.COSINE
                ),
            )
            log.info("Created Qdrant collection %s", self._collection)

    def _embed(self, text: str) -> list[float]:
        # Vertex SDK is synchronous; in production we'd push this to a
        # threadpool. Kept sync here for clarity.
        result = self._embedder.get_embeddings(
            [TextEmbeddingInput(text=text, task_type="RETRIEVAL_QUERY")]
        )
        return result[0].values

    async def search(self, query: str, top_k: int = 4) -> list[RunbookHit]:
        if not query.strip():
            return []
        vec = self._embed(query)
        hits = await self._client.search(
            collection_name=self._collection,
            query_vector=vec,
            limit=top_k,
            with_payload=True,
        )
        return [
            RunbookHit(
                title=h.payload.get("title", "untitled"),
                excerpt=h.payload.get("text", "")[:600],
                score=float(h.score),
                source=h.payload.get("source", "kb"),
            )
            for h in hits
        ]

    async def upsert_lesson(
        self,
        incident_id: str,
        lesson: str,
        *,
        source_tag: str = "feedback-loop",
        submitted_by: str = "unknown",
        approved_by: str = "auto",
    ) -> None:
        """Closed-loop: approved SRE corrections get embedded back into the KB.

        Provenance fields make every retrieved hit traceable — when a
        future verdict cites a runbook from ``source: feedback-loop:approved``
        we know who submitted it and who approved it. Audit-friendly.
        """
        vec = self._embed(lesson)
        await self._client.upsert(
            collection_name=self._collection,
            points=[
                qm.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vec,
                    payload={
                        "title": f"SRE correction for {incident_id}",
                        "text": lesson,
                        "source": source_tag,
                        "submitted_by": submitted_by,
                        "approved_by": approved_by,
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            ],
        )
        log.info(
            "Embedded approved lesson for incident %s (submitted_by=%s approved_by=%s)",
            incident_id, submitted_by, approved_by,
        )
