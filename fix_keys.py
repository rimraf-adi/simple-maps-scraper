import os
import asyncio
import itertools
from openai import AsyncOpenAI

async def test_key(key):
    client = AsyncOpenAI(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=key
    )
    try:
        await client.chat.completions.create(
            model="gemini-3.1-flash-lite",
            messages=[{"role": "user", "content": "hi"}]
        )
        return True
    except Exception:
        return False

async def find_valid_key(base_key):
    # Find all occurrences of 'I' and 'l'
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
        2: os.getenv("CORRUPT_KEY_2", "YOUR_DUMMY_KEY_2"),
        6: os.getenv("CORRUPT_KEY_6", "YOUR_DUMMY_KEY_6"),
        7: os.getenv("CORRUPT_KEY_7", "YOUR_DUMMY_KEY_7")
    }
    for idx, key in keys.items():
        print(f"Testing combinations for Key {idx}...")
        valid = await find_valid_key(key)
        if valid:
            print(f"FOUND VALID KEY {idx}: {valid}")
        else:
            print(f"Could not find valid combination for Key {idx}")

asyncio.run(main())
