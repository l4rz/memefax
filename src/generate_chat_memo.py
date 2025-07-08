#!/usr/bin/env python3
"""Generate a markdown "chat memo" for a Telegram chat stored by download_telegram.py.

Usage:
    python3 generate_chat_memo.py --chat-id <id> --start-date 1-Jan-2025 --end-date 2-Jan-2025

The script:
1. Validates the chat_id exists in manifest.db (created by download_telegram.py)
2. Reads messages from data/<chat_id>/messages.db within the given UTC date range.
3. Compiles a participants list from the messages.
4. Writes a markdown file named chat-memo-<chat_id>-<start>-<end>.md in current directory.

Note: Topic summary, key topics and action items sections are left blank for now and
will be filled by an external LLM in later stages.
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any

import textwrap

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # will check later

# ---------------------------------------------------------------------------
# Configuration is shared with download_telegram.py via the same env vars
# ---------------------------------------------------------------------------
from dotenv import load_dotenv

load_dotenv()

DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "./data")
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://localhost:8080/completions")

# Maximum number of characters from the transcript passed to the LLM for summary
TRANSCRIPT_CLIP_LENGTH = 16000  # increase from previous 8000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date(date_str: str) -> datetime:
    """Parse a date in the form 1-Jan-2025 and return UTC datetime at midnight."""
    try:
        dt = datetime.strptime(date_str.strip(), "%d-%b-%Y")
    except ValueError:
        # Support full month name e.g. 1-January-2025
        dt = datetime.strptime(date_str.strip(), "%d-%B-%Y")
    return dt.replace(tzinfo=timezone.utc)


def format_participant(sender: Dict[str, Any]) -> str:
    """Return participant display string based on sender metadata."""
    name = sender.get("name", "Unknown")
    username = sender.get("username")
    is_bot = sender.get("bot", False)

    if is_bot and username:
        return f"bot @{username}"
    if username:
        return f"{name} (@{username})"
    return name


def build_participants_list(participants: List[str]) -> str:
    """Return participants list joined by comma and space, sorted alphabetically."""
    return ", ".join(sorted(participants))


def ensure_chat_exists(chat_id: int) -> str:
    """Verify chat_id is present in manifest.db and return chat name if found."""
    manifest_db_path = Path(DOWNLOAD_PATH) / "manifest.db"
    if not manifest_db_path.exists():
        raise FileNotFoundError(f"manifest.db not found at {manifest_db_path}")

    conn = sqlite3.connect(manifest_db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM chats WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Chat id {chat_id} not found in manifest.db")
        return row[0]
    finally:
        conn.close()


def load_messages(chat_id: int, since_iso: str, until_iso: str):
    """Yield messages for chat_id between since_iso and until_iso (inclusive)."""
    messages_db_path = Path(DOWNLOAD_PATH) / str(chat_id) / "messages.db"
    if not messages_db_path.exists():
        raise FileNotFoundError(f"Messages database not found at {messages_db_path}")

    conn = sqlite3.connect(messages_db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, date, text, reply_to_msg_id, media_files, sender
            FROM messages
            WHERE date BETWEEN ? AND ?
            ORDER BY date ASC
            """,
            (since_iso, until_iso),
        )
        for row in cur.fetchall():
            yield {
                "id": row[0],
                "date": row[1],
                "text": row[2] or "",
                "reply_to_msg_id": row[3],
                "media_files": row[4],
                "sender": row[5],
            }
    finally:
        conn.close()


def build_transcript_line(msg: Dict[str, Any], chat_id: int) -> str:
    """Return formatted transcript line for a single message including timestamp prefix."""
    # Sender info
    sender_json = msg["sender"]
    if sender_json:
        try:
            sender = json.loads(sender_json)
        except json.JSONDecodeError:
            sender = {}
    else:
        sender = {}

    participant_display = format_participant(sender)

    # Core text
    text = msg["text"].replace("\n", " ") if msg["text"] else ""

    # Suffixes
    suffixes = []
    if msg["reply_to_msg_id"]:
        suffixes.append(f"(Replying to message id {msg['reply_to_msg_id']})")

    media_json = msg["media_files"]
    if media_json:
        try:
            media_list = json.loads(media_json)
            if media_list:
                first_media = media_list[0]
                media_type = first_media.get("type", "media")
                filename = first_media.get("filename", "")
                file_path = Path(DOWNLOAD_PATH) / str(chat_id) / "media" / filename
                suffixes.append(f"(Attached media: {media_type} {file_path})")
        except json.JSONDecodeError:
            pass

    suffix_str = " ".join(suffixes)

    # Timestamp (UTC) formatted as YYYY-MM-DD HH:MM
    try:
        dt = datetime.fromisoformat(msg["date"])
    except ValueError:
        dt = None
    timestamp_str = dt.strftime("%Y-%m-%d %H:%M") if dt else ""  # fallback empty

    line = f"{msg['id']} [{timestamp_str}] {participant_display}: {text} {suffix_str}".strip()
    return line


# ---------------------------------------------------------------------------
# LLM summarisation helper
# ---------------------------------------------------------------------------

def generate_topic_summary(transcript: str) -> str:
    """Call local LLM endpoint to produce 2-3 sentence topic summary.

    Falls back to placeholder text on error or if `requests` is unavailable.
    """

    if requests is None:
        return "(summary unavailable – 'requests' not installed)"

    # Truncate transcript to reasonable length
    truncated_transcript = transcript[:TRANSCRIPT_CLIP_LENGTH]

    prompt = textwrap.dedent(
        f"""
        You are an assistant that reads Telegram chat transcripts. Please write a concise 2-3 sentence overview of the main topics discussed. Be very brief. Please avoid repetitions. Start your response with <summary> and end with </summary>

        Transcript:
        {truncated_transcript}

        Summary:
        """
    ).strip()

    payload = {
        "model": "qwen3-8b-instruct",
        "prompt": prompt + "/no_think",
        "max_tokens": 128,
        "temperature": 0.5,
        "enable_thinking": False,
    }

    headers = {"Content-Type": "application/json"}

    try:
        # Use ensure_ascii=False to keep UTF-8 characters verbatim
        payload_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = requests.post(LLM_ENDPOINT, data=payload_json, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        # Try various known payload shapes --------------------------------
        summary_text = ""

        # 1) Standard OpenAI-like
        try:
            summary_text = (
                data.get("choices", [{}])[0].get("text")
                or data.get("choices", [{}])[0].get("message", {}).get("content", "")
            ).strip()
        except Exception:
            pass

        # 2) Simple dict with 'content'
        if not summary_text and isinstance(data, dict) and "content" in data:
            summary_text = str(data["content"]).strip()

        # 3) Some endpoints wrap the result in a list
        if not summary_text and isinstance(data, list) and data:
            item = data[0]
            summary_text = (item.get("content") or item.get("text", "")).strip()

        # ------------------------------------------------------------------

        if summary_text:
            # Extract text between <summary> and </summary> if present
            import re

            match = re.search(r"<summary>(.*?)</summary>", summary_text, re.DOTALL | re.IGNORECASE)
            if match:
                summary_text = match.group(1).strip()

            print("Topic Summary (LLM):", summary_text)
            return summary_text

        # Debug: print raw response for inspection
        #print("LLM returned unexpected format. Raw response:", json.dumps(data, indent=2))
        return "(summary generation failed)"
    except Exception as e:
        return f"(summary error: {e})"


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate a markdown chat memo from stored Telegram chat history")
    parser.add_argument("--chat-id", type=int, required=True, help="Chat ID as stored in manifest.db")
    parser.add_argument("--start-date", required=True, help="Start date (e.g., 1-Jan-2025) in UTC")
    parser.add_argument("--end-date", required=True, help="End date (e.g., 2-Jan-2025) in UTC")
    args = parser.parse_args()

    # Validate and parse dates
    start_dt = parse_date(args.start_date)
    end_dt = parse_date(args.end_date)

    # Ensure chronological order
    if end_dt < start_dt:
        print("Error: end-date must be after or equal to start-date")
        return

    # Apply full-day boundaries
    start_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Convert to ISO strings for DB query
    since_iso = start_dt.isoformat()
    until_iso = end_dt.isoformat()

    try:
        chat_name = ensure_chat_exists(args.chat_id)
    except (FileNotFoundError, ValueError) as e:
        print(e)
        return

    # Load messages
    try:
        messages_iter = list(load_messages(args.chat_id, since_iso, until_iso))
    except FileNotFoundError as e:
        print(e)
        return

    if not messages_iter:
        print("No messages found in the specified date range.")
        return

    # Build participants list & transcript
    participants_set = set()
    transcript_lines: List[str] = []

    current_day = None

    for msg in messages_iter:
        # Add participant to set
        if msg["sender"]:
            try:
                sender = json.loads(msg["sender"])
                participants_set.add(format_participant(sender))
            except json.JSONDecodeError:
                pass

        # Day header logic
        try:
            dt_msg = datetime.fromisoformat(msg["date"])
        except ValueError:
            dt_msg = None

        if dt_msg:
            day_str = dt_msg.strftime("%Y-%m-%d")
            if day_str != current_day:
                current_day = day_str
                # Add a blank line before new day header unless it's the first entry
                if transcript_lines:
                    transcript_lines.append("")
                transcript_lines.append(f"--- {day_str}")

        # Append the message line
        transcript_lines.append(build_transcript_line(msg, args.chat_id))

    participants_str = build_participants_list(list(participants_set))

    # Memo header metadata
    memo_lines = []
    date_interval = f"{args.start_date} - {args.end_date}"
    memo_lines.append(f"CHAT MEMO - {date_interval} - {chat_name} (Chat id {args.chat_id})")
    memo_lines.append("")
    memo_lines.append(f"Participants: {participants_str}")
    memo_lines.append(
        f"Duration: {start_dt.strftime('%Y-%m-%d %H:%M')} - {end_dt.strftime('%Y-%m-%d %H:%M')}")

    # Topic Summary via LLM
    transcript_text_for_llm = "\n".join(transcript_lines)
    print(f"Transcript length (characters): {len(transcript_text_for_llm)}")
    summary = generate_topic_summary(transcript_text_for_llm)
    memo_lines.append("")
    memo_lines.append("Topic Summary:")
    memo_lines.append(summary)
    memo_lines.append("")
    memo_lines.append("CONVERSATION TRANSCRIPT:")
    memo_lines.extend(transcript_lines)

    # Write to file
    start_token = args.start_date.replace(" ", "-")
    end_token = args.end_date.replace(" ", "-")
    outfile_name = f"chat-memo-{args.chat_id}-{start_token}-{end_token}.md"

    with open(outfile_name, "w", encoding="utf-8") as f:
        f.write("\n".join(memo_lines))

    print(f"Memo written to {outfile_name}")


if __name__ == "__main__":
    main() 