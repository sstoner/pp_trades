import os
import asyncio
import aiohttp
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

async def test_telegram():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Error: Missing token or chat id in .env")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "Hello! This is a test message from your Polymarket bot."
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            result = await response.json()
            if response.status == 200:
                print("✅ Success! Message sent successfully.")
            else:
                print(f"❌ Failed to send message.\nStatus Code: {response.status}\nResponse: {result}")

if __name__ == "__main__":
    asyncio.run(test_telegram())
