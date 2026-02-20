"""
RAG (Retrieval-Augmented Generation) service using ChromaDB.

Responsibilities:
  - Load and chunk documents from the knowledge_base/ directory
  - Store embeddings in ChromaDB (persisted to disk)
  - Retrieve top-K relevant chunks for a given user query
"""
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions

from app.config import get_settings
from app.utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

KNOWLEDGE_BASE_DIR = Path("./knowledge_base")
COLLECTION_NAME = "hr_knowledge_base"


class RAGService:
    def __init__(self) -> None:
        os.makedirs(settings.chroma_persist_dir, exist_ok=True)
        self._client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        # Use Chroma's default embedding function (sentence-transformers)
        self._embedding_fn = embedding_functions.DefaultEmbeddingFunction()
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "ChromaDB ready",
            collection=COLLECTION_NAME,
            doc_count=self._collection.count(),
        )

    # ------------------------------------------------------------------
    # Document ingestion
    # ------------------------------------------------------------------

    def _chunk_text(self, text: str, source: str) -> list[dict]:
        """Split text into overlapping chunks suitable for embedding."""
        size = settings.chunk_size
        overlap = settings.chunk_overlap
        chunks = []
        start = 0
        idx = 0
        while start < len(text):
            end = min(start + size, len(text))
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(
                    {
                        "id": f"{source}_chunk_{idx}",
                        "text": chunk,
                        "metadata": {"source": source, "chunk_index": idx},
                    }
                )
            start += size - overlap
            idx += 1
        return chunks

    def ingest_file(self, file_path: Path) -> int:
        """Ingest a single file into ChromaDB. Returns number of chunks added."""
        content = file_path.read_text(encoding="utf-8")
        source = file_path.name
        chunks = self._chunk_text(content, source)
        if not chunks:
            return 0

        # Upsert so re-running is idempotent
        self._collection.upsert(
            ids=[c["id"] for c in chunks],
            documents=[c["text"] for c in chunks],
            metadatas=[c["metadata"] for c in chunks],
        )
        logger.info("Ingested file", file=source, chunks=len(chunks))
        return len(chunks)

    def ingest_all(self) -> int:
        """Scan knowledge_base/ and ingest every .txt / .md file."""
        if not KNOWLEDGE_BASE_DIR.exists():
            logger.warning("knowledge_base directory not found – RAG is empty")
            return 0

        total = 0
        for ext in ("*.txt", "*.md"):
            for fp in KNOWLEDGE_BASE_DIR.glob(ext):
                total += self.ingest_file(fp)
        logger.info("Ingestion complete", total_chunks=total)
        return total

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve(self, query: str, top_k: Optional[int] = None) -> list[dict]:
        """
        Return top-K document chunks most similar to the query.

        Each result dict:
          { "text": str, "source": str, "score": float }
        """
        k = top_k or settings.rag_top_k
        if self._collection.count() == 0:
            return []

        results = self._collection.query(
            query_texts=[query],
            n_results=min(k, self._collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        retrieved = []
        docs = results["documents"][0]
        metas = results["metadatas"][0]
        distances = results["distances"][0]

        for doc, meta, dist in zip(docs, metas, distances):
            # Cosine distance → similarity score (1 = identical, 0 = orthogonal)
            score = 1 - dist
            if score >= settings.rag_score_threshold:
                retrieved.append(
                    {"text": doc, "source": meta.get("source", "unknown"), "score": score}
                )

        logger.debug("RAG retrieved", query_preview=query[:60], results=len(retrieved))
        return retrieved

    def build_context(self, query: str) -> tuple[str, list[str]]:
        """
        Build a context string and list of sources for the AI prompt.

        Returns:
          (context_text, [source_filenames])
        """
        chunks = self.retrieve(query)
        if not chunks:
            return "", []

        context_parts = []
        sources = []
        for i, chunk in enumerate(chunks, start=1):
            context_parts.append(
                f"[เอกสาร {i}: {chunk['source']}]\n{chunk['text']}"
            )
            if chunk["source"] not in sources:
                sources.append(chunk["source"])

        context = "\n\n---\n\n".join(context_parts)
        return context, sources


# Singleton
_rag_service: Optional[RAGService] = None


def get_rag_service() -> RAGService:
    global _rag_service
    if _rag_service is None:
        _rag_service = RAGService()
    return _rag_service
