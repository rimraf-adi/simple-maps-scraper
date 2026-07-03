import asyncio
import os
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

async def test_key(idx, key):
    client = AsyncOpenAI(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=key
    )
    try:
        response = await client.chat.completions.create(
            model="gemini-3.1-flash-lite",
            messages=[{"role": "user", "content": "hello"}]
        )
        print(f"Key {idx} SUCCESS")
    except Exception as e:
        print(f"Key {idx} ERROR: {str(e)}")

async def main():
    for i in range(1, 11):
        k = os.getenv(f"LLM_API_KEY_{i}")
        if k:
            await test_key(i, k)

asyncio.run(main())
