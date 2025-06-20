#!/usr/bin/env python3
import os
import json
import time
import sqlite3
import asyncio
import argparse
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import (
    Dialog, Channel, Chat, User, PeerUser, PeerChannel, PeerChat,
    MessageMediaPhoto, MessageMediaDocument, ChatParticipantCreator
)
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from tqdm import tqdm

# Load environment variables
load_dotenv()

# Configure client
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
DOWNLOAD_PATH = os.getenv('DOWNLOAD_PATH', './data')

# Rate limiting configuration
MAX_REQUESTS_PER_SECOND = 50
REQUEST_INTERVAL = 1.0 / MAX_REQUESTS_PER_SECOND

# Media size limit (50MB in bytes)
MAX_MEDIA_SIZE = 50 * 1024 * 1024

# Version information
__version__ = "1.0"
__app_name__ = "memefax"

def format_size(size_bytes):
    """Format size in bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"

class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None
        self.cursor = None

    def connect(self):
        """Connect to the database and create tables if they don't exist"""
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        
        # Create messages table if it doesn't exist
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id BIGINT PRIMARY KEY,
            date TIMESTAMP NOT NULL,
            date_retrieved TIMESTAMP,  -- Remove default, we'll set it explicitly
            from_id BIGINT,
            text TEXT,
            reply_to_msg_id BIGINT,
            forward_from BIGINT,
            media_type TEXT,
            sender TEXT,
            media_files TEXT
        )
        ''')

        # Create indexes if they don't exist
        self.cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_date 
        ON messages(date)
        ''')

        self.cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_from_id 
        ON messages(from_id)
        ''')

        self.cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_reply_to 
        ON messages(reply_to_msg_id)
        ''')

        self.cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_date_retrieved 
        ON messages(date_retrieved)
        ''')

        self.conn.commit()

    def disconnect(self):
        """Close the database connection"""
        if self.conn:
            self.conn.close()
            self.conn = None
            self.cursor = None

    def insert_message(self, message_data):
        """Insert a message into the database"""
        try:
            # Convert ISO format date string to timestamp with UTC
            date = datetime.fromisoformat(message_data['date'])
            if date.tzinfo is None:  # If no timezone info, assume UTC
                from datetime import timezone
                date = date.replace(tzinfo=timezone.utc)
            
            # Get current time in UTC with offset
            from datetime import timezone
            date_retrieved = datetime.now().replace(tzinfo=timezone.utc)
            
            # Convert dictionaries to JSON strings
            sender_json = json.dumps(message_data.get('sender')) if message_data.get('sender') else None
            media_files_json = json.dumps(message_data.get('media_files')) if message_data.get('media_files') else None

            self.cursor.execute('''
            INSERT OR REPLACE INTO messages (
                id, date, from_id, text, reply_to_msg_id,
                forward_from, media_type, sender, media_files,
                date_retrieved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                message_data['id'],
                date.isoformat(),  # This will include the UTC offset
                message_data.get('from_id'),
                message_data.get('text'),
                message_data.get('reply_to_msg_id'),
                message_data.get('forward_from'),
                message_data.get('media_type'),
                sender_json,
                media_files_json,
                date_retrieved.isoformat()  # This will include the UTC offset
            ))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error inserting message {message_data.get('id')}: {str(e)}")
            return False

    def get_latest_message_id(self):
        """Get the ID of the latest message in the database"""
        self.cursor.execute('SELECT MAX(id) FROM messages')
        result = self.cursor.fetchone()
        return result[0] if result and result[0] is not None else 0

    def get_latest_message_date(self):
        """Get the date of the latest message in the database"""
        self.cursor.execute('SELECT MAX(date) FROM messages')
        result = self.cursor.fetchone()
        return result[0] if result and result[0] is not None else None

class RateLimiter:
    def __init__(self, max_requests_per_second):
        self.max_requests = max_requests_per_second
        self.interval = 1.0 / max_requests_per_second
        self.last_request_time = 0
        self._request_count = 0
        self._window_start = time.time()

    async def wait(self):
        """Wait if necessary to maintain the rate limit"""
        current_time = time.time()
        
        # Reset counter if we're in a new second
        if current_time - self._window_start >= 1.0:
            self._request_count = 0
            self._window_start = current_time

        self._request_count += 1
        
        # If we've exceeded our rate limit, wait until the next window
        if self._request_count > self.max_requests:
            wait_time = 1.0 - (current_time - self._window_start)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                self._request_count = 1
                self._window_start = time.time()
        
        # Ensure minimum interval between requests
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.interval:
            await asyncio.sleep(self.interval - time_since_last)
        
        self.last_request_time = time.time()

def ensure_download_dir(chat_id):
    """Ensure the download directory exists for the given chat"""
    # Convert chat_id to string preserving negative signs and leading digits
    chat_dir_name = str(chat_id)  # This will preserve negative signs and all digits
    
    chat_dir = os.path.join(DOWNLOAD_PATH, chat_dir_name)
    media_dir = os.path.join(chat_dir, 'media')
    os.makedirs(chat_dir, exist_ok=True)
    os.makedirs(media_dir, exist_ok=True)
    return chat_dir, media_dir

def get_display_name(entity):
    """Get a display name for any type of chat entity"""
    if isinstance(entity, User):
        return f"{entity.first_name} {entity.last_name if entity.last_name else ''}".strip()
    elif isinstance(entity, (Chat, Channel)):
        return entity.title
    return "Unknown"

def get_peer_id(peer):
    """Extract the numeric ID from a Peer object"""
    if isinstance(peer, (PeerUser, PeerChannel, PeerChat)):
        return peer.user_id if isinstance(peer, PeerUser) else peer.channel_id if isinstance(peer, PeerChannel) else peer.chat_id
    return None

def get_media_type(media):
    """Determine media type and file extension"""
    if isinstance(media, MessageMediaPhoto):
        return 'photo', '.jpg'
    elif isinstance(media, MessageMediaDocument):
        # Try to determine the type from mime_type or attributes
        mime_type = media.document.mime_type
        
        # Handle common mime types
        if mime_type:
            if mime_type.startswith('video/'):
                return 'video', '.mp4'
            elif mime_type.startswith('audio/'):
                return 'audio', '.mp3' if 'mpeg' in mime_type else '.ogg'
            elif mime_type.startswith('image/'):
                ext = mime_type.split('/')[-1]
                return 'photo', f'.{ext}'
        
        # Try to get filename from attributes
        for attr in media.document.attributes:
            if hasattr(attr, 'file_name'):
                _, ext = os.path.splitext(attr.file_name)
                return 'document', ext if ext else '.dat'
        
        return 'document', '.dat'
    
    return None, None

def get_media_size(media):
    """Get the size of media in bytes"""
    if isinstance(media, MessageMediaDocument):
        return media.document.size
    elif isinstance(media, MessageMediaPhoto):
        # Photos are typically small, return None to allow download
        return None
    return None

async def download_media_file(message, media_dir, rate_limiter):
    """Download a media file and return its information"""
    if not message.media:
        return None

    try:
        # Check media size first
        media_size = get_media_size(message.media)
        if media_size and media_size > MAX_MEDIA_SIZE:
            return {
                'type': 'skipped',
                'filename': None,
                'size': media_size,
                'skipped_reason': f'File size ({format_size(media_size)}) exceeds limit of {format_size(MAX_MEDIA_SIZE)}'
            }

        media_type, extension = get_media_type(message.media)
        if not media_type:
            return None

        # Generate unique filename
        timestamp = message.date.strftime("%Y%m%d_%H%M%S")
        filename = f"{media_type}_{message.id}_{timestamp}{extension}"
        filepath = os.path.join(media_dir, filename)

        # Skip if file already exists
        if os.path.exists(filepath):
            return {
                'type': media_type,
                'filename': filename,
                'size': os.path.getsize(filepath)
            }

        # Download the media
        await rate_limiter.wait()
        await message.download_media(filepath)

        actual_size = os.path.getsize(filepath)
        return {
            'type': media_type,
            'filename': filename,
            'size': actual_size
        }

    except Exception as e:
        print(f"\nError downloading media from message {message.id}: {str(e)}")
        return None

async def download_messages(client, chat, chat_dir):
    """Download all messages from the specified chat"""
    chat_name = get_display_name(chat)
    print(f"\nDownloading messages from: {chat_name}")
    
    # Get the correct chat ID based on entity type
    if isinstance(chat, (Channel, Chat)):  # Handle both Channel and Chat entities
        chat_id = chat.id  # Use the raw ID
    else:
        chat_id = chat.id
        
    chat_dir, media_dir = ensure_download_dir(chat_id)
    
    # Initialize database
    db_path = os.path.join(chat_dir, 'messages.db')
    db = Database(db_path)
    db.connect()
    
    # Get the latest message ID from database
    latest_msg_id = db.get_latest_message_id()
    
    # Initialize JSON storage
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    messages_file = os.path.join(chat_dir, f"messages_{timestamp}.json")
    messages = []
    
    messages_processed = 0
    rate_limiter = RateLimiter(MAX_REQUESTS_PER_SECOND)
    
    try:
        # Get total message count for progress bar
        await rate_limiter.wait()
        
        if latest_msg_id:
            print(f"Found existing messages in database. Latest message ID: {latest_msg_id}")
            print("Downloading only new messages...")
            # Get count of new messages only. TODO it's buggy, total_messages count even with min_id set is always max messages
            total_messages = await client.get_messages(chat, min_id=latest_msg_id)
        else:
            print("No existing messages found. Downloading all messages...")
            total_messages = await client.get_messages(chat, limit=0)
        
        total_count = total_messages.total

        if total_count == 0:
            print("No new messages to download.")
            return True

        print(f"Total new messages to download: {total_count}")
        
        with tqdm(total=total_count, desc="Downloading messages") as pbar:
            # If we have existing messages, only get new ones
            message_iterator = client.iter_messages(chat, min_id=latest_msg_id) if latest_msg_id else client.iter_messages(chat)
            
            async for message in message_iterator:
                await rate_limiter.wait()  # Rate limit each message request
                
                # Download media if present
                media_info = await download_media_file(message, media_dir, rate_limiter) if message.media else None
                
                # Convert message to dict, handling common attributes
                message_data = {
                    'id': message.id,
                    'date': message.date.isoformat(),
                    'from_id': get_peer_id(message.from_id) if message.from_id else None,
                    'text': message.text,
                    'reply_to_msg_id': message.reply_to_msg_id,
                    'forward_from': get_peer_id(message.forward.from_id) if message.forward and message.forward.from_id else None,
                    'media_type': type(message.media).__name__ if message.media else None,
                    'media_files': [media_info] if media_info else []
                }

                # Add sender info if available
                if message.sender:
                    message_data['sender'] = {
                        'id': message.sender.id,
                        'name': get_display_name(message.sender),
                        'username': getattr(message.sender, 'username', None),
                        'bot': getattr(message.sender, 'bot', False),
                    }
                
                # Store message in both formats
                if db.insert_message(message_data):
                    messages_processed += 1
                    messages.append(message_data)
                
                # Save JSON periodically to avoid memory issues
                if len(messages) % 1000 == 0:
                    with open(messages_file, 'w', encoding='utf-8') as f:
                        json.dump(messages, f, ensure_ascii=False, indent=2)
                
                pbar.update(1)

        # Final JSON save
        if messages:  # Only save JSON if we have new messages
            with open(messages_file, 'w', encoding='utf-8') as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)

            print(f"\nSuccessfully downloaded {messages_processed} new messages")
            print(f"Data stored in:")
            print(f"- SQLite: {db_path}")
            print(f"- JSON: {messages_file}")
        else:
            print("\nNo new messages were downloaded")
        
    except Exception as e:
        print(f"\nError downloading messages: {str(e)}")
        return False
    
    finally:
        db.disconnect()
        
    return True

class ManifestDatabase:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None
        self.cursor = None

    def connect(self):
        """Connect to the database and create tables if they don't exist"""
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        
        # Create chats table if it doesn't exist
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS chats (
            chat_id BIGINT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            username TEXT,
            first_seen TIMESTAMP NOT NULL,
            last_seen TIMESTAMP NOT NULL,
            last_updated TIMESTAMP NOT NULL,
            
            -- Privacy and Settings
            join_date TIMESTAMP,
            
            -- Group/Channel Specific
            broadcast BOOLEAN,
            participants_count INTEGER,
            kicked_count INTEGER,
            left_count INTEGER,
            online_count INTEGER,
            
            -- Historical Data
            messages_count INTEGER,
            last_message_date TIMESTAMP,
            first_message_date TIMESTAMP,
            created_date TIMESTAMP,
            
            -- User-Specific
            phone TEXT,
            is_bot BOOLEAN
        )
        ''')

        # Create indexes
        self.cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_chat_type 
        ON chats(type)
        ''')

        self.cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_last_seen 
        ON chats(last_seen)
        ''')

        self.conn.commit()

    def disconnect(self):
        """Close the database connection"""
        if self.conn:
            self.conn.close()
            self.conn = None
            self.cursor = None

    def update_chat(self, chat_data):
        """Update or insert a chat entry"""
        try:
            # Check if chat exists
            self.cursor.execute('''
            SELECT first_seen, join_date FROM chats WHERE chat_id = ?
            ''', (chat_data['chat_id'],))
            
            result = self.cursor.fetchone()
            current_time = datetime.now(timezone.utc).isoformat()
            
            if result:
                # Preserve join_date if it exists
                first_seen, existing_join_date = result
                if 'join_date' not in chat_data and existing_join_date:
                    chat_data['join_date'] = existing_join_date
                
                # Update existing chat
                self.cursor.execute('''
                UPDATE chats 
                SET name = ?, type = ?, username = ?, 
                    last_seen = ?, last_updated = ?,
                    join_date = ?,
                    broadcast = ?, participants_count = ?,
                    kicked_count = ?, left_count = ?, online_count = ?,
                    messages_count = ?, last_message_date = ?,
                    first_message_date = ?, created_date = ?,
                    phone = ?, is_bot = ?
                WHERE chat_id = ?
                ''', (
                    chat_data['name'],
                    chat_data['type'],
                    chat_data.get('username'),
                    current_time,
                    current_time,
                    chat_data.get('join_date'),
                    chat_data.get('broadcast'),
                    chat_data.get('participants_count'),
                    chat_data.get('kicked_count'),
                    chat_data.get('left_count'),
                    chat_data.get('online_count'),
                    chat_data.get('messages_count'),
                    chat_data.get('last_message_date'),
                    chat_data.get('first_message_date'),
                    chat_data.get('created_date'),
                    chat_data.get('phone'),
                    chat_data.get('is_bot'),
                    chat_data['chat_id']
                ))
            else:
                # Insert new chat
                self.cursor.execute('''
                INSERT INTO chats (
                    chat_id, name, type, username,
                    first_seen, last_seen, last_updated,
                    join_date, broadcast, participants_count,
                    kicked_count, left_count, online_count,
                    messages_count, last_message_date,
                    first_message_date, created_date,
                    phone, is_bot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    chat_data['chat_id'],
                    chat_data['name'],
                    chat_data['type'],
                    chat_data.get('username'),
                    current_time,
                    current_time,
                    current_time,
                    chat_data.get('join_date'),
                    chat_data.get('broadcast'),
                    chat_data.get('participants_count'),
                    chat_data.get('kicked_count'),
                    chat_data.get('left_count'),
                    chat_data.get('online_count'),
                    chat_data.get('messages_count'),
                    chat_data.get('last_message_date'),
                    chat_data.get('first_message_date'),
                    chat_data.get('created_date'),
                    chat_data.get('phone'),
                    chat_data.get('is_bot')
                ))
            
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error updating chat {chat_data.get('chat_id')}: {str(e)}")
            return False

    def get_all_chats(self):
        """Get all chats from the database"""
        self.cursor.execute('''
        SELECT chat_id, name, type, username, 
               first_seen, last_seen, last_updated,
               join_date, broadcast, participants_count,
               kicked_count, left_count, online_count,
               messages_count, last_message_date,
               first_message_date, created_date,
               phone, is_bot
        FROM chats
        ORDER BY last_seen DESC
        ''')
        return self.cursor.fetchall()

async def get_full_chat_info(client, dialog, entity):
    """Get detailed information about a chat"""
    chat_info = {
        "chat_id": entity.id,
        "name": dialog.name,
        "type": "Unknown",
        "username": getattr(entity, 'username', None)
    }
    
    try:
        if isinstance(entity, Channel):
            chat_info["type"] = "Channel" if entity.broadcast else "Supergroup"
            chat_info["broadcast"] = entity.broadcast
            
            # Get full channel/supergroup info
            full_chat = await client(GetFullChannelRequest(entity))
            chat_info.update({
                "participants_count": full_chat.full_chat.participants_count,
                "online_count": getattr(full_chat.full_chat, 'online_count', None),
                "kicked_count": getattr(full_chat.full_chat, 'kicked_count', None),
                "left_count": getattr(full_chat.full_chat, 'left_count', None),
                "messages_count": getattr(full_chat.full_chat, 'messages_count', None),
                "created_date": entity.date.isoformat() if entity.date else None
            })
            
        elif isinstance(entity, Chat):
            chat_info["type"] = "Group"
            
            # Get full group info
            full_chat = await client(GetFullChatRequest(entity.id))
            chat_info.update({
                "participants_count": len(full_chat.full_chat.participants.participants),
                "messages_count": None,  # Not available for normal groups
                "created_date": None  # Not available for normal groups
            })
            
        elif isinstance(entity, User):
            chat_info["type"] = "User"
            chat_info.update({
                "phone": entity.phone if hasattr(entity, 'phone') else None,
                "is_bot": entity.bot if hasattr(entity, 'bot') else False
            })
    
    except Exception as e:
        print(f"Warning: Could not fetch full info for chat {chat_info['name']}: {str(e)}")
    
    return chat_info

async def list_chats(download_all_users=False, download_all_groups=False):
    """Connect to Telegram and list all available chats"""
    if not all([API_ID, API_HASH]):
        print("Error: Please configure API_ID and API_HASH in .env file")
        return

    print("Connecting to Telegram...")
    client = TelegramClient('test_session', API_ID, API_HASH)
    
    try:
        await client.start()

        if not await client.is_user_authorized():
            print("\nFirst time login - you'll need to verify your phone number")
            print("Check your Telegram app for the verification code")
            await client.send_code_request(input("Enter your phone number (including country code): "))
            await client.sign_in(code=input("Enter the code you received: "))

        print("\nFetching chat list...")
        
        # Store chats in dictionaries for easy lookup
        chats = {}
        user_chats = {}  # Store only user chats for --all-users option
        group_chats = {}  # Store only group chats for --all-groups option
        chat_manifest = []  # Store chat information for manifest
        
        # Get all dialogs (chats)
        async for dialog in client.iter_dialogs():
            # Get the entity and full chat info
            entity = dialog.entity
            chat_info = await get_full_chat_info(client, dialog, entity)
            
            # Store in appropriate dictionaries
            chat_id = chat_info['chat_id']
            if chat_info['type'] == "User":
                user_chats[chat_id] = dialog
            elif chat_info['type'] in ["Group", "Supergroup"]:
                group_chats[chat_id] = dialog
            
            chat_manifest.append(chat_info)
            print(f"{chat_info['type']}: {chat_info['name']} (ID: {chat_id})")
            chats[chat_id] = dialog
            
        print("\nChat list fetched successfully!")

        # Update manifest in both JSON and SQLite
        os.makedirs(DOWNLOAD_PATH, exist_ok=True)
        
        # Write JSON manifest
        manifest_path = os.path.join(DOWNLOAD_PATH, 'manifest.json')
        try:
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "version": __version__,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                    "chats": chat_manifest
                }, f, ensure_ascii=False, indent=2)
            print(f"Chat manifest written to: {manifest_path}")
        except Exception as e:
            print(f"Warning: Failed to write JSON manifest: {str(e)}")

        # Update SQLite manifest
        manifest_db_path = os.path.join(DOWNLOAD_PATH, 'manifest.db')
        try:
            manifest_db = ManifestDatabase(manifest_db_path)
            manifest_db.connect()
            
            for chat_info in chat_manifest:
                manifest_db.update_chat(chat_info)
            
            manifest_db.disconnect()
            print(f"Chat manifest database updated: {manifest_db_path}")
        except Exception as e:
            print(f"Warning: Failed to update manifest database: {str(e)}")

        if download_all_users:
            if not user_chats:
                print("No private chats found.")
                return
            
            print(f"\nDownloading messages from {len(user_chats)} private chats...")
            for chat_id, dialog in user_chats.items():
                print(f"\nProcessing chat with: {dialog.name}")
                chat_dir = ensure_download_dir(chat_id)[0]
                if await download_messages(client, dialog.entity, chat_dir):
                    print(f"Successfully downloaded messages from chat with {dialog.name}")
                else:
                    print(f"Failed to download messages from chat with {dialog.name}")
            print("\nFinished downloading all private chats!")
            return

        if download_all_groups:
            if not group_chats:
                print("No group chats found.")
                return
            
            print(f"\nDownloading messages from {len(group_chats)} group chats...")
            for chat_id, dialog in group_chats.items():
                print(f"\nProcessing group: {dialog.name}")
                chat_dir = ensure_download_dir(chat_id)[0]
                if await download_messages(client, dialog.entity, chat_dir):
                    print(f"Successfully downloaded messages from group {dialog.name}")
                else:
                    print(f"Failed to download messages from group {dialog.name}")
            print("\nFinished downloading all group chats!")
            return
        
        # Ask user to select a chat if not downloading all users or groups
        while True:
            try:
                selected_id = int(input("\nEnter the ID of the chat you want to download (or 0 to exit): "))
                if selected_id == 0:
                    break
                    
                if selected_id in chats:
                    selected_chat = chats[selected_id]
                    chat_dir = ensure_download_dir(selected_id)[0]
                    
                    print(f"\nSelected: {selected_chat.name}")
                    if await download_messages(client, selected_chat.entity, chat_dir):
                        print("Download completed successfully!")
                    break
                else:
                    print("Invalid chat ID. Please try again.")
            except ValueError:
                print("Please enter a valid number.")

    except Exception as e:
        print(f"\nError occurred: {str(e)}")
    
    finally:
        await client.disconnect()

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        #description=f'{__app_name__} {__version__} - Telegram chat history downloader and archiver',
        description="""
    __  ___ ______ __  ___ ______ ______ ___ __   __ 
   /  |/  // ____//  |/  // ____// ____//   |\ \ / / 
  / /|_/ // /__  / /|_/ // /__ // /__  / /| | \ / / 
 / /  / // ___/ / /  / // ___/ / ___/ / ___ | / \ \ 
/_/  /_/ \____//_/  /_/ \____/ \_/   /_/  |_//_/ \_\ 

""" + f'{__app_name__} {__version__} - Telegram chat history downloader and archiver',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Interactive mode: list chats and select one to download
  %(prog)s --all-users        # Download all private chats automatically
  %(prog)s --all-groups       # Download all group chats automatically
""")
    
    parser.add_argument('--version', action='version', 
                       version=f'{__app_name__} {__version__}')
    parser.add_argument('--all-users', action='store_true',
                       help='automatically download messages from all private chats')
    parser.add_argument('--all-groups', action='store_true',
                       help='automatically download messages from all group chats (including supergroups)')
    
    args = parser.parse_args()

    if args.all_users and args.all_groups:
        print("Error: Please use either --all-users or --all-groups, not both")
        return

    # Run the async event loop
    asyncio.run(list_chats(download_all_users=args.all_users, download_all_groups=args.all_groups))

if __name__ == "__main__":
    main() 