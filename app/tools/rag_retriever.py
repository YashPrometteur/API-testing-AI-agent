import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
try:
    import chromadb
except ImportError:
    chromadb = None

from app.config.settings import settings
from app.tools import ToolResult
from app.tools.embeddings import EmbeddingTool

logger = logging.getLogger("rag_retriever")

# Global lazy-loaded Chroma client or fallback
_chroma_client = None
_use_chroma_fallback = False


def check_chroma_support() -> bool:
    """Check if Chroma is supported and can perform write operations without DLL crashes."""
    if chromadb is None:
        return False
    try:
        # Run a quick upsert in an isolated subprocess to detect C++ library aborts
        cmd = (
            "import chromadb; "
            "client = chromadb.PersistentClient(path='./chroma_db_test'); "
            "col = client.get_or_create_collection('test'); "
            "col.upsert(ids=['1'], documents=['t'], embeddings=[[0.0]*384])"
        )
        res = subprocess.run(
            [sys.executable, "-c", cmd],
            capture_output=True,
            text=True,
            timeout=3.0
        )
        return res.returncode == 0
    except Exception:
        return False


class MockChromaCollection:
    """Pure-Python fallback collection mimicking Chroma's vector query interfaces."""

    def __init__(self, name: str, persist_dir: str):
        self.name = name
        self.persist_path = os.path.join(persist_dir, f"fallback_{name}.json")
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.persist_path):
            try:
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"ids": [], "documents": [], "embeddings": []}

    def _save(self):
        os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
        try:
            with open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"[MOCK_CHROMA] Failed to save fallback DB: {e}")

    def count(self) -> int:
        return len(self.data["ids"])

    def upsert(self, ids: List[str], documents: List[str], embeddings: List[List[float]]):
        for doc_id, doc, emb in zip(ids, documents, embeddings):
            if doc_id in self.data["ids"]:
                idx = self.data["ids"].index(doc_id)
                self.data["documents"][idx] = doc
                self.data["embeddings"][idx] = emb
            else:
                self.data["ids"].append(doc_id)
                self.data["documents"].append(doc)
                self.data["embeddings"].append(emb)
        self._save()

    def query(self, query_embeddings: List[List[float]], n_results: int) -> dict:
        if not query_embeddings or not self.data["ids"]:
            return {"documents": [[]], "ids": [[]], "distances": [[]]}

        q_emb = query_embeddings[0]
        scored_results = []

        for doc_id, doc, emb in zip(self.data["ids"], self.data["documents"], self.data["embeddings"]):
            # Cosine similarity: (A . B) / (||A|| * ||B||)
            dot = sum(a * b for a, b in zip(q_emb, emb))
            norm1 = sum(a * a for a in q_emb) ** 0.5
            norm2 = sum(b * b for b in emb) ** 0.5
            sim = dot / (norm1 * norm2) if (norm1 > 0 and norm2 > 0) else 0.0
            distance = 1.0 - sim
            scored_results.append((doc, doc_id, distance))

        # Sort by distance ascending (closest first)
        scored_results.sort(key=lambda x: x[2])
        top = scored_results[:n_results]

        return {
            "documents": [[x[0] for x in top]],
            "ids": [[x[1] for x in top]],
            "distances": [[x[2] for x in top]]
        }


class MockChromaClient:
    """Pure-Python fallback client mimicking Chroma's persistent client."""

    def __init__(self, path: str):
        self.path = path

    def get_or_create_collection(self, name: str, metadata: Optional[dict] = None, embedding_function: Any = None):
        return MockChromaCollection(name, self.path)

    def get_collection(self, name: str):
        return MockChromaCollection(name, self.path)


# Initialize fallback status
_use_chroma_fallback = not check_chroma_support()
if _use_chroma_fallback:
    logger.warning("[RAG_RETRIEVER] Isolated check failed for Chroma vector DB. Falling back to pure-Python Mock database.")


def get_chroma_client():
    global _chroma_client, _use_chroma_fallback
    if _chroma_client is None:
        if _use_chroma_fallback:
            _chroma_client = MockChromaClient(path=settings.CHROMA_PERSIST_DIR)
        else:
            try:
                _chroma_client = chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIR)
            except Exception as e:
                logger.warning(f"[RAG_RETRIEVER] Failed to initialize standard Chroma client ({e}). Swapping to fallback.")
                _use_chroma_fallback = True
                _chroma_client = MockChromaClient(path=settings.CHROMA_PERSIST_DIR)
    return _chroma_client


class RAGRetrieverTool:
    """Tool for retrieving relevant context from Chroma collections using local embeddings."""

    @staticmethod
    async def retrieve(
        query: str,
        top_k: int = 3,
        doc_type: Optional[str] = None
    ) -> ToolResult:
        """Retrieve similarity-matched document chunks from Chroma collections.

        Fails closed on timeout (5s) or database errors, returning an empty list.
        """
        if not query:
            return ToolResult(success=True, data=[])

        try:
            # 1. Generate query embedding vector
            embed_res = await EmbeddingTool.embed(query)
            if not embed_res.success or not embed_res.data:
                logger.warning(f"[RAG_RETRIEVER] Failed to embed query: {embed_res.error}")
                return ToolResult(success=True, data=[])

            query_vector = embed_res.data

            # 2. Query vector DB asynchronously with a 5s timeout
            try:
                results = await asyncio.wait_for(
                    asyncio.to_thread(
                        RAGRetrieverTool._query_sync,
                        query_vector,
                        top_k,
                        doc_type
                    ),
                    timeout=5.0
                )
                return ToolResult(success=True, data=results)
            except asyncio.TimeoutError:
                logger.warning("[RAG_RETRIEVER] Chroma query timed out (exceeded 5s limit). Failing closed.")
                return ToolResult(success=True, data=[])
            except Exception as ex:
                logger.warning(f"[RAG_RETRIEVER] Chroma query failed: {ex}. Failing closed.")
                return ToolResult(success=True, data=[])

        except Exception as e:
            logger.warning(f"[RAG_RETRIEVER] Retrieval failed with unexpected error: {e}. Failing closed.")
            return ToolResult(success=True, data=[])

    @staticmethod
    def _query_sync(
        query_vector: List[float],
        top_k: int,
        doc_type: Optional[str]
    ) -> List[Tuple[str, str, float]]:
        """Synchronous query implementation run inside a worker thread."""
        client = get_chroma_client()
        collections_to_query = []

        available_types = ["past_defects", "api_docs", "coding_standards"]
        if doc_type:
            if doc_type in available_types:
                collections_to_query.append(doc_type)
            else:
                logger.warning(f"[RAG_RETRIEVER] Requested invalid doc_type '{doc_type}'. Defaulting to all.")
                collections_to_query.extend(available_types)
        else:
            collections_to_query.extend(available_types)

        all_matches: List[Tuple[str, str, float]] = []

        for col_name in collections_to_query:
            try:
                # Get the collection
                collection = client.get_collection(name=col_name)
                # If collection is empty, skip
                if collection.count() == 0:
                    continue

                res = collection.query(
                    query_embeddings=[query_vector],
                    n_results=top_k
                )

                if res and res.get("documents") and res["documents"][0]:
                    docs = res["documents"][0]
                    ids = res["ids"][0]
                    distances = res.get("distances", [[0.0] * len(docs)])[0]

                    for doc, doc_id, dist in zip(docs, ids, distances):
                        # Convert distance to similarity score
                        similarity = 1.0 - float(dist)
                        all_matches.append((doc, doc_id, similarity))
            except Exception as e:
                logger.debug(f"[RAG_RETRIEVER] Collection '{col_name}' query skipped or failed: {e}")

        # Sort combined results from multiple collections by similarity score descending
        all_matches.sort(key=lambda x: x[2], reverse=True)
        return all_matches[:top_k]
