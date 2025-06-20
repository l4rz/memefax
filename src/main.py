#!/usr/bin/env python3
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# Load environment variables
load_dotenv()

# Configure client
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')

async def main():
    """Main function to handle Telegram client operations"""
    if not all([API_ID, API_HASH]):
        print("Error: Please configure API_ID and API_HASH in .env file")
        return

    # Create the client and connect
    client = TelegramClient('session_name', API_ID, API_HASH)
    await client.start()

    if not await client.is_user_authorized():
        print("Error: User is not authorized")
        return

    # Get information about yourself
    me = await client.get_me()
    print(f"Successfully connected as {me.first_name} ({me.username})")

    # Close the client
    await client.disconnect()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main()) 