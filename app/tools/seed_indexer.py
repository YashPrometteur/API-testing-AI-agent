import asyncio
import logging
import sys
from typing import Dict, List
import chromadb

sys.path.insert(0, ".")
from app.config.settings import settings
from app.tools.embeddings import EmbeddingTool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_indexer")

# Seed datasets
SEED_DATA: Dict[str, List[Dict[str, str]]] = {
    "past_defects": [
        {
            "id": "defect_001",
            "content": "404 Not Found error on deletePet endpoint due to missing path parameter validation in router."
        },
        {
            "id": "defect_002",
            "content": "500 Internal Server Error on updatePet endpoint because database schema rejected null fields for category. Always validate required fields in request body."
        },
        {
            "id": "defect_003",
            "content": "401 Unauthorized on getInventory order query. Authentication token signature expired or was missing in headers."
        }
    ],
    "api_docs": [
        {
            "id": "doc_001",
            "content": "OpenAPI spec requires that GET endpoints return 200 OK with the target resource representation. Path parameters must match exactly."
        },
        {
            "id": "doc_002",
            "content": "POST /pet schema: Requires body containing pet object with non-empty name string. Returns 200 OK on success."
        },
        {
            "id": "doc_003",
            "content": "DELETE /pet/{petId} expects petId path parameter as positive integer. Returns 200 OK or 400 Bad Request."
        }
    ],
    "coding_standards": [
        {
            "id": "std_001",
            "content": "All destructive endpoints (DELETE, PUT) require both authentication and authorization scopes. Verify credentials before resource modification."
        },
        {
            "id": "std_002",
            "content": "SQL queries should use prepared statements to avoid SQL injection vulnerability. Never concatenate raw request fields directly."
        },
        {
            "id": "std_003",
            "content": "Input sanitization: Strip special characters and check string length against schema boundaries before calling database functions."
        }
    ]
}


from app.tools.rag_retriever import get_chroma_client

async def seed_database():
    """Async script to index seed data into Chroma collections."""
    logger.info(f"Initializing Chroma persistent client at: {settings.CHROMA_PERSIST_DIR}")
    client = get_chroma_client()

    for col_name, documents in SEED_DATA.items():
        logger.info(f"Seeding collection '{col_name}' with {len(documents)} items...")
        
        # Get or create collection using cosine similarity metric
        collection = client.get_or_create_collection(
            name=col_name,
            metadata={"hnsw:space": "cosine"}
        )

        ids: List[str] = []
        texts: List[str] = []
        embeddings: List[List[float]] = []

        for doc in documents:
            doc_id = doc["id"]
            content = doc["content"]

            # Generate embedding vector asynchronously
            embed_res = await EmbeddingTool.embed(content, input_type="search_document")
            if not embed_res.success or not embed_res.data:
                logger.error(f"Failed to generate embedding for '{doc_id}': {embed_res.error}")
                continue

            ids.append(doc_id)
            texts.append(content)
            embeddings.append(embed_res.data)

        if ids:
            # Insert or update documents into collection
            collection.upsert(
                ids=ids,
                documents=texts,
                embeddings=embeddings
            )
            logger.info(f"Successfully seeded {len(ids)} documents in collection '{col_name}'.")

    logger.info("Chroma database seeding completed successfully.")


if __name__ == "__main__":
    asyncio.run(seed_database())
