import asyncio
from typing import List
from app.tools import ToolResult
from app.llm import get_llm_client


class EmbeddingTool:
    """Tool for generating text embeddings using the configured LLMClient."""

    @staticmethod
    async def embed(text: str, input_type: str = "search_query") -> ToolResult:
        """Generate embedding vector for the given text with 3x retry backoff on failure."""
        if not text:
            return ToolResult(success=False, error="Empty text query", data=[])

        client = get_llm_client()
        delays = [1.0, 2.0]
        last_exception = None

        import logging
        lg = logging.getLogger("embeddings_tool")

        for attempt in range(3):
            try:
                vector = await client.embed(text, input_type=input_type)
                return ToolResult(
                    success=True,
                    data=vector
                )
            except Exception as e:
                last_exception = e
                if attempt < 2:
                    delay = delays[attempt]
                    lg.warning(f"Embedding generation failed (attempt {attempt + 1}/3): {e}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    break

        return ToolResult(
            success=False,
            error=f"Embedding generation failed after 3 attempts: {str(last_exception)}",
            data=[]
        )
