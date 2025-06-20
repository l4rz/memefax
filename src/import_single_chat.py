import os
import sys
import sqlite3
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv
import numpy as np
import requests

# Load environment variables from .env
load_dotenv()

POSTGRES_PARAMS = {
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': os.getenv('POSTGRES_PORT', 5432),
    'dbname': os.getenv('POSTGRES_DB', 'postgres'),
    'user': os.getenv('POSTGRES_USER', 'postgres'),
    'password': os.getenv('POSTGRES_PASSWORD', '')
}

MANIFEST_DB_PATH = os.path.join('data', 'manifest.db')
CHAT_DB_TEMPLATE = os.path.join('data', '{chat_id}', 'messages.db')
LLAMA_SERVER_URL = os.getenv('LLAMA_SERVER_URL', 'http://localhost:8000/embed')

# --- Embedding via HTTP llama-server ---
def get_embedding_from_server(text, dim=1024):
    if not text or not text.strip():
        return np.zeros(dim, dtype=np.float32)
    try:
        response = requests.post(
            LLAMA_SERVER_URL,
            json={"content": text + "<|endoftext|>"}
        )
        response.raise_for_status()
        # The response is a list of dicts, each with "embedding": [[...]]
        emb = np.array(response.json()[0]["embedding"][0], dtype=np.float32)
        if emb.shape[0] > dim:
            emb = emb[:dim]
        elif emb.shape[0] < dim:
            emb = np.pad(emb, (0, dim - emb.shape[0]))
        return emb
    except Exception as e:
        print(f"Embedding server error: {e}")
        return np.zeros(dim, dtype=np.float32)

# --- Helper functions ---
def list_chats():
    if not os.path.exists(MANIFEST_DB_PATH):
        print(f"Manifest DB not found: {MANIFEST_DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(MANIFEST_DB_PATH)
    sqlite_cur = conn.cursor()
    sqlite_cur.execute("SELECT chat_id, name, type, username, participants_count FROM chats ORDER BY last_seen DESC")
    chats = sqlite_cur.fetchall()
    conn.close()
    return chats

def select_chat(chats):
    print("\nAvailable chats:")
    for idx, chat in enumerate(chats):
        chat_id, name, chat_type, username, participants = chat
        print(f"[{idx+1}] {name} (ID: {chat_id}, Type: {chat_type}, Username: {username}, Participants: {participants})")
    while True:
        sel = input("\nSelect a chat by number or ID: ").strip()
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(chats):
                return chats[idx][0]
        else:
            # Try to match by chat_id
            for chat in chats:
                if str(chat[0]) == sel:
                    return chat[0]
        print("Invalid selection. Please try again.")

def get_chat_db_path(chat_id):
    db_path = CHAT_DB_TEMPLATE.format(chat_id=chat_id)
    if not os.path.exists(db_path):
        print(f"Chat DB not found: {db_path}")
        sys.exit(1)
    return db_path

def connect_postgres():
    return psycopg2.connect(**POSTGRES_PARAMS)

# --- Main import logic ---
def import_chat(chat_id, chat_db_path, pg_conn):
    print(f"\nImporting chat {chat_id} from {chat_db_path}")
    sqlite_conn = sqlite3.connect(chat_db_path)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cur = sqlite_conn.cursor()
    pg_cur = pg_conn.cursor()

    # Ensure source exists (for now, use chat_id as name)
    pg_cur.execute("SELECT id FROM source WHERE name = %s", (str(chat_id),))
    src = pg_cur.fetchone()
    if src:
        source_id = src[0]
    else:
        pg_cur.execute("INSERT INTO source (name, kind, details) VALUES (%s, %s, %s) RETURNING id", (str(chat_id), 'chat', None))
        source_id = pg_cur.fetchone()[0]
        pg_conn.commit()

    # Read all messages
    sqlite_cur.execute("SELECT * FROM messages ORDER BY date ASC")
    messages = sqlite_cur.fetchall()
    print(f"Found {len(messages)} messages. Importing...")

    imported = 0
    for row in messages:
        msg_id = row['id']
        sent_ts = row['date']
        from_id = row['from_id']
        text = row['text'] or ''
        sender = row['sender']
        media_files = row['media_files']
        # --- Contact/alias logic (simplified) ---
        contact_id = None
        if sender:
            # Try to find contact by canonical name
            pg_cur.execute("SELECT id FROM contact WHERE canonical = %s", (sender,))
            c = pg_cur.fetchone()
            if c:
                contact_id = c[0]
            else:
                pg_cur.execute("INSERT INTO contact (canonical) VALUES (%s) RETURNING id", (sender,))
                contact_id = pg_cur.fetchone()[0]
                pg_conn.commit()
        # --- Document insert ---
        pg_cur.execute("""
            INSERT INTO document (source_id, ext_id, thread_id, sent_ts, author_contact_id, raw_body, clean_body, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, ext_id) DO NOTHING
            RETURNING id
        """, (source_id, str(msg_id), str(chat_id), sent_ts, contact_id, text, text, None))
        doc_row = pg_cur.fetchone()
        if doc_row:
            document_id = doc_row[0]
        else:
            # Already exists, skip
            continue
        # --- Attachments (if any) ---
        if media_files:
            try:
                import json
                files = json.loads(media_files)
                for f in files:
                    pg_cur.execute("""
                        INSERT INTO attachment (document_id, filename, mime_type, file_path, meta)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (document_id, f.get('name'), f.get('mime_type'), f.get('path'), None))
            except Exception as e:
                print(f"Warning: Could not parse media_files for message {msg_id}: {e}")
        # --- Passage chunking (for now, just use the whole text as one passage) ---
        passage_text = text.strip()
        if passage_text and len(passage_text.split()) > 10: # avoid adding empty/short passages as they poison the embeddings TODO: make this a parameter
            pg_cur.execute("""
                INSERT INTO passage (document_id, level, start_tok, end_tok, text)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (document_id, 'P', 0, len(passage_text.split()), passage_text))
            passage_id = pg_cur.fetchone()[0]
            # --- Embedding (via llama-server) ---
            emb = get_embedding_from_server(passage_text, 1024)
            if isinstance(emb, str):
                # Remove brackets and split by comma
                emb_vec = np.array([float(x) for x in emb.strip('[]').split(',')], dtype=np.float32)
                print("split")
            else:
                emb_vec = np.array(emb, dtype=np.float32)
            pg_cur.execute("""
                INSERT INTO passage_embedding (passage_id, model_id, embedding)
                VALUES (%s, %s, %s)
            """, (passage_id, 'llama-server', emb_vec.tolist()))

        imported += 1
        if imported % 10 == 0:
            pg_conn.commit()
            print(f"Imported {imported} messages...")
    pg_conn.commit()
    print(f"Import complete. {imported} messages imported.")
    pg_cur.close()
    sqlite_conn.close()

# --- Main script ---
def main():
    chats = list_chats()
    if not chats:
        print("No chats found in manifest.db.")
        sys.exit(1)
    chat_id = select_chat(chats)
    chat_db_path = get_chat_db_path(chat_id)
    pg_conn = connect_postgres()
    try:
        import_chat(chat_id, chat_db_path, pg_conn)
    finally:
        pg_conn.close()

if __name__ == "__main__":
    main() 