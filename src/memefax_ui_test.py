from flask import Flask, render_template, jsonify, request
import os
import sqlite3
from datetime import datetime
import json
from markupsafe import Markup

# Initialize Flask app with template and static folders in project root
app = Flask(__name__, 
           template_folder='../templates',
           static_folder='../static')

# Add custom filter for JSON parsing
@app.template_filter('from_json')
def from_json(value):
    try:
        return json.loads(value)
    except:
        return value

def get_db():
    db = sqlite3.connect('data/manifest.db')
    db.row_factory = sqlite3.Row
    return db

def get_messages_db(chat_id):
    db_path = f'data/{chat_id}/messages.db'
    if not os.path.exists(db_path):
        return None
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    return db

def format_chat(chat):
    return {
        "id": chat['chat_id'],
        "name": chat['name'],
        "type": chat['type'],
        "username": chat['username'],
        "last_message_date": chat['last_message_date'],
        "messages_count": chat['messages_count'],
        "participants_count": chat['participants_count'] if chat['type'] in ['Group', 'Channel'] else None,
        "is_bot": chat['is_bot'] if chat['type'] == 'User' else None
    }

def format_message(message):
    # Parse media files JSON if present
    media_files = []
    if message['media_files']:
        try:
            media_files = json.loads(message['media_files'])
        except:
            pass

    return {
        "id": message['id'],
        "text": message['text'] or "",
        "sender": message['sender'] or "Unknown",
        "time": datetime.fromisoformat(message['date']).strftime("%H:%M"),
        "date": datetime.fromisoformat(message['date']).strftime("%Y-%m-%d"),
        "media_type": message['media_type'],
        "media_files": media_files,
        "reply_to_msg_id": message['reply_to_msg_id']
    }

@app.route('/')
def index():
    db = get_db()
    cursor = db.execute('''
        SELECT * FROM chats 
        ORDER BY last_message_date DESC NULLS LAST, 
                 last_seen DESC
        LIMIT 10
    ''')
    chats = [format_chat(dict(row)) for row in cursor.fetchall()]
    db.close()
    return render_template('index.html', chats=chats)

@app.route('/api/chats')
def get_chats():
    db = get_db()
    cursor = db.execute('''
        SELECT * FROM chats 
        ORDER BY last_message_date DESC NULLS LAST, 
                 last_seen DESC
        LIMIT 10
    ''')
    chats = [format_chat(dict(row)) for row in cursor.fetchall()]
    db.close()
    return jsonify(chats)

@app.route('/api/chat/<int:chat_id>')
@app.route('/api/chat/<int:chat_id>/<int:offset>')
def get_chat(chat_id, offset=0):
    # Get chat info
    db = get_db()
    cursor = db.execute('SELECT * FROM chats WHERE chat_id = ?', (chat_id,))
    chat = cursor.fetchone()
    db.close()
    
    if chat is None:
        return "Chat not found", 404

    # Get messages from chat-specific database
    messages_db = get_messages_db(chat_id)
    if messages_db is None:
        return "Messages not found", 404

    # Get total message count
    cursor = messages_db.execute('SELECT COUNT(*) as count FROM messages')
    total_messages = cursor.fetchone()['count']

    # Get paginated messages
    cursor = messages_db.execute('''
        SELECT * FROM messages 
        ORDER BY date DESC 
        LIMIT 5 OFFSET ?
    ''', (offset,))
    messages = [format_message(dict(row)) for row in cursor.fetchall()]
    messages_db.close()

    # Reverse messages to show oldest first
    messages.reverse()
    
    # If this is a pagination request, return both the load more section and messages
    if offset > 0:
        next_offset = offset + 5
        has_more = next_offset < total_messages
        
        # Render both sections
        load_more_html = render_template('components/load_more.html',
                                       chat=format_chat(dict(chat)),
                                       offset=next_offset,
                                       has_more=has_more)
        messages_html = render_template('components/messages.html',
                                      messages=messages)
        
        # Insert the messages into the DOM
        return load_more_html + f'''
        <script>
            document.getElementById('messages-container').insertAdjacentHTML('afterbegin', {json.dumps(messages_html)});
        </script>
        '''
    
    # For initial load, return the full chat view
    return render_template('components/chat_content.html', 
                         messages=messages,
                         chat=format_chat(dict(chat)),
                         offset=5,
                         has_more=total_messages > 5)

@app.route('/api/search')
def search():
    query = request.args.get('q', '').lower()
    if not query:
        return get_chats()
    
    db = get_db()
    cursor = db.execute('''
        SELECT * FROM chats 
        WHERE LOWER(name) LIKE ? OR 
              LOWER(username) LIKE ? OR
              LOWER(type) LIKE ?
        ORDER BY last_message_date DESC NULLS LAST,
                 last_seen DESC
        LIMIT 10
    ''', (f'%{query}%', f'%{query}%', f'%{query}%'))
    
    chats = [format_chat(dict(row)) for row in cursor.fetchall()]
    db.close()
    return jsonify(chats)

if __name__ == '__main__':
    app.run(debug=True) 