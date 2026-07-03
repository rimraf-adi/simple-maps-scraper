import os
import asyncio
from openai import AsyncOpenAI

async def main():
    client = AsyncOpenAI(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_KEY")
    )
    try:
        response = await client.chat.completions.create(
            model="gemini-3.1-flash-lite",
            messages=[{"role": "user", "content": "hello"}]
        )
        print("SUCCESS:", response)
    except Exception as e:
        print("ERROR:", str(e))

asyncio.run(main())
