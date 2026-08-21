import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from groq import RateLimitError
from app.llm.groq_client import GroqClient
from pydantic import BaseModel

class DummyResponse(BaseModel):
    message: str

@pytest.mark.asyncio
async def test_groq_client_rate_limit_retry_success():
    client = GroqClient(api_key="mock_key")

    mock_resp = AsyncMock()
    mock_resp.choices = [AsyncMock(message=AsyncMock(content='{"message": "success"}'))]

    # First attempt raises RateLimitError, second succeeds
    mock_create = AsyncMock(side_effect=[
        RateLimitError("Rate limit reached", response=AsyncMock(status_code=429, headers={"retry-after": "0.1"}), body=None),
        mock_resp
    ])

    with patch.object(client.client.chat.completions, "create", mock_create):
        res = await client.generate_structured("test prompt", DummyResponse)
        assert res.message == "success"
        assert mock_create.call_count == 2
