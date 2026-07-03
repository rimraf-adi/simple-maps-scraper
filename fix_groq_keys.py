import asyncio
import os
import itertools
from openai import AsyncOpenAI

async def test_key(key):
    client = AsyncOpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=key
    )
    try:
        await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": "hi"}]
        )
        return True
    except Exception as e:
        return False

async def find_valid_key(base_key):
    indices = [i for i, c in enumerate(base_key) if c in 'Il']
    if not indices:
        return base_key if await test_key(base_key) else None
        
    for combo in itertools.product('Il', repeat=len(indices)):
        chars = list(base_key)
        for idx, char in zip(indices, combo):
            chars[idx] = char
        candidate = "".join(chars)
        if await test_key(candidate):
            return candidate
    return None

async def main():
    keys = {
        1: os.getenv("GROQ_API_KEY_1", "YOUR_DUMMY_KEY_1"),
        2: os.getenv("GROQ_API_KEY_2", "YOUR_DUMMY_KEY_2"),
        3: os.getenv("GROQ_API_KEY_3", "YOUR_DUMMY_KEY_3"),
        4: os.getenv("GROQ_API_KEY_4", "YOUR_DUMMY_KEY_4"),
        5: os.getenv("GROQ_API_KEY_5", "YOUR_DUMMY_KEY_5"),
        6: os.getenv("GROQ_API_KEY_6", "YOUR_DUMMY_KEY_6"),
        7: os.getenv("GROQ_API_KEY_7", "YOUR_DUMMY_KEY_7"),
        8: os.getenv("GROQ_API_KEY_8", "YOUR_DUMMY_KEY_8"),
        9: os.getenv("GROQ_API_KEY_9", "YOUR_DUMMY_KEY_9"),
        10: os.getenv("GROQ_API_KEY_10", "YOUR_DUMMY_KEY_10"),
    }
    for idx, key in keys.items():
        print(f"Testing combinations for Key {idx}...")
        valid = await find_valid_key(key)
        if valid:
            print(f"FOUND VALID KEY {idx}: {valid}")
        else:
            print(f"Could not find valid combination for Key {idx}")

asyncio.run(main())
