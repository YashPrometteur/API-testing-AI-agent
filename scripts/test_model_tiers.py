import asyncio
from groq import AsyncGroq
from app.config.settings import settings

async def test_all_models():
    client = AsyncGroq(api_key=settings.GROQ_API_KEY)
    models_to_test = [
        "llama-3.3-70b-versatile",
        "llama-3.1-70b-versatile",
        "openai/gpt-oss-120b",
        "qwen/qwen3.8-27b"
    ]
    
    for m in models_to_test:
        print(f"=== Testing model: {m} ===")
        try:
            res = await client.chat.completions.create(
                model=m,
                messages=[{"role": "user", "content": 'Respond with JSON {"status": "ok"}'}],
                response_format={"type": "json_object"},
                max_tokens=30
            )
            print(f"Result: SUCCESS! Content: {res.choices[0].message.content}")
        except Exception as e:
            print(f"Result: FAILED -> {e}")

asyncio.run(test_all_models())
