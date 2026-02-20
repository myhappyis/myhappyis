#!/usr/bin/env python3
"""
Standalone script to (re-)ingest all documents in knowledge_base/ into ChromaDB.

Usage:
  python scripts/ingest_docs.py [--reset]

Options:
  --reset   Delete the existing ChromaDB collection before ingesting (full re-index)
"""
import argparse
import sys
from pathlib import Path

# Add the project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.rag_service import COLLECTION_NAME, KNOWLEDGE_BASE_DIR, get_rag_service
from app.utils.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest HR documents into ChromaDB")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate the ChromaDB collection before ingesting",
    )
    args = parser.parse_args()

    rag = get_rag_service()

    if args.reset:
        logger.info("Resetting ChromaDB collection", collection=COLLECTION_NAME)
        rag._client.delete_collection(COLLECTION_NAME)
        # Recreate after deletion
        from chromadb.utils import embedding_functions
        rag._collection = rag._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=rag._embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("Collection recreated")

    files = list(KNOWLEDGE_BASE_DIR.glob("*.md")) + list(KNOWLEDGE_BASE_DIR.glob("*.txt"))
    if not files:
        logger.warning("No documents found in knowledge_base/")
        return

    logger.info("Found documents", count=len(files), files=[f.name for f in files])

    total = 0
    for fp in files:
        chunks = rag.ingest_file(fp)
        total += chunks
        print(f"  ✅  {fp.name}  →  {chunks} chunks")

    print(f"\nTotal chunks ingested: {total}")
    print(f"ChromaDB collection size: {rag._collection.count()} documents")


if __name__ == "__main__":
    main()
