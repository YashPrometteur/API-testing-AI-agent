import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from groq import AsyncGroq
from app.config.settings import settings

client = AsyncGroq(api_key=settings.GROQ_API_KEY)

models_to_test = [
    'qwen/qwen3.6-27b',
    'qwen/qwen3.8-27b',
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b'
]

async def test_m(m):
    try:
        res = await client.chat.completions.create(
            model=m,
            messages=[{'role': 'user', 'content': 'Generate JSON: {"status": "ok"}'}],
            response_format={'type': 'json_object'},
            max_tokens=50
        )
        print(f"Model {m}: SUCCESS -> {res.choices[0].message.content}")
    except Exception as e:
        print(f"Model {m}: ERROR -> {e}")

async def main():
    for m in models_to_test:
        await test_m(m)

if __name__ == "__main__":
    asyncio.run(main())
