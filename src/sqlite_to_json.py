#!/usr/bin/env python3
import os
import json
import sqlite3
import sys
from datetime import datetime

def convert_sqlite_to_json(sqlite_path, json_path):
    """Convert SQLite database to JSON format"""
    if not os.path.exists(sqlite_path):
        print(f"Error: SQLite file not found: {sqlite_path}")
        return False

    try:
        # Connect to SQLite database
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row  # This enables column access by name
        cursor = conn.cursor()

        # Get all messages
        cursor.execute('''
        SELECT id, date, date_retrieved, from_id, text, 
               reply_to_msg_id, forward_from, media_type,
               sender, media_files
        FROM messages
        ORDER BY date ASC
        ''')

        messages = []
        for row in cursor:
            # Convert row to dict
            message = {
                'id': row['id'],
                'date': datetime.fromisoformat(row['date']).isoformat(),  # Ensure ISO format
                'from_id': row['from_id'],
                'text': row['text'],
                'reply_to_msg_id': row['reply_to_msg_id'],
                'forward_from': row['forward_from'],
                'media_type': row['media_type']
            }

            # Parse JSON fields
            if row['sender']:
                try:
                    message['sender'] = json.loads(row['sender'])
                except json.JSONDecodeError:
                    print(f"Warning: Could not parse sender JSON for message {row['id']}")
                    message['sender'] = None

            if row['media_files']:
                try:
                    message['media_files'] = json.loads(row['media_files'])
                except json.JSONDecodeError:
                    print(f"Warning: Could not parse media_files JSON for message {row['id']}")
                    message['media_files'] = []
            else:
                message['media_files'] = []

            messages.append(message)

        # Write to JSON file
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)

        print(f"Successfully converted {len(messages)} messages")
        print(f"SQLite: {sqlite_path}")
        print(f"JSON: {json_path}")
        
        return True

    except sqlite3.Error as e:
        print(f"SQLite error: {str(e)}")
        return False
    except Exception as e:
        print(f"Error: {str(e)}")
        return False
    finally:
        if conn:
            conn.close()

def print_usage():
    print("Usage: python sqlite_to_json.py <sqlite_file> <json_file>")
    print("Example: python sqlite_to_json.py ./data/123456/messages.db ./data/123456/messages_export.json")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print_usage()
        sys.exit(1)

    sqlite_path = sys.argv[1]
    json_path = sys.argv[2]

    if convert_sqlite_to_json(sqlite_path, json_path):
        print("Conversion completed successfully!")
    else:
        print("Conversion failed!")
        sys.exit(1) 