import asyncio
from groq import AsyncGroq
from app.config.settings import settings

async def list_available_models():
    client = AsyncGroq(api_key=settings.GROQ_API_KEY)
    try:
        models = await client.models.list()
        print("=== AVAILABLE MODELS ON YOUR GROQ ACCOUNT ===")
        for m in sorted(models.data, key=lambda x: x.id):
            print(f"- {m.id} (owned by: {m.owned_by})")
    except Exception as e:
        print(f"Error listing models: {e}")

asyncio.run(list_available_models())
