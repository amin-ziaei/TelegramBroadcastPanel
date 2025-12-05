import os
import sqlite3
import asyncio
import time
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, g
from telegram import Bot
# Required modules for error handling and database management
from telegram.error import TelegramError

# --- 1. Configuration ---
app = Flask(__name__)
app.secret_key = os.urandom(24) 
DATABASE = 'users.db'

# Get credentials from environment variables
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_USER = os.environ.get('ADMIN_USERNAME')
ADMIN_PASS = os.environ.get('ADMIN_PASSWORD')

# --- 2. Database Functions (SQLite) ---

def init_db():
    """Initializes the database schema with new tables."""
    with app.app_context():
        db = get_db()
        
        # 1. Users table (Telegram Users) - added tags column
        db.execute('''
            CREATE TABLE IF NOT EXISTS telegram_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                username TEXT NOT NULL DEFAULT 'User',
                tags TEXT DEFAULT '[]' -- Store tags as JSON list
            )
        ''')

        # 2. Scheduled messages table (Scheduled Messages)
        db.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_text TEXT NOT NULL,
                target_ids TEXT,             -- 'all' or list of chat_ids (JSON)
                media_url TEXT,              -- File URL (optional)
                media_type TEXT,             -- photo, document, etc. (optional)
                send_time INTEGER NOT NULL,  -- Send time as Unix Timestamp
                status TEXT NOT NULL DEFAULT 'PENDING' -- PENDING, SENT, FAILED
            )
        ''')
        
        # 3. Message logs table (Message Logs)
        db.execute('''
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                message_id INTEGER,
                send_time INTEGER NOT NULL,
                status TEXT NOT NULL,        -- SENT, FAILED, BLOCKED
                error_details TEXT
            )
        ''')
        
        db.commit()

def get_db():
    """Connects to the specific database."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    """Closes database connection at the end of the request."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def get_all_users():
    """Returns all users (chat_id, username, tags)."""
    db = get_db()
    cursor = db.execute('SELECT chat_id, username, tags FROM telegram_users ORDER BY username')
    return [
        {**dict(row), 'tags': json.loads(row['tags'])} 
        for row in cursor.fetchall()
    ]

def add_user(chat_id, username, tags_list):
    db = get_db()
    tags_json = json.dumps(tags_list)
    try:
        db.execute('''
            INSERT INTO telegram_users (chat_id, username, tags) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, tags=excluded.tags
        ''', (chat_id, username, tags_json))
        db.commit()
        return True, "User added/updated successfully."
    except Exception as e:
        return False, str(e)
        
def get_all_tags():
    """Extracts all unique tags from the users table."""
    users = get_all_users()
    all_tags = set()
    for user in users:
        for tag in user['tags']:
            all_tags.add(tag)
    return sorted(list(all_tags))
    
def get_users_by_tag(tag):
    """Returns chat_ids for users matching a specific tag."""
    db = get_db()
    cursor = db.execute(f"SELECT chat_id FROM telegram_users WHERE tags LIKE '%\"{tag}\"%'")
    return [row['chat_id'] for row in cursor.fetchall()]

def add_scheduled_message(text, target, media_url, media_type, send_time):
    db = get_db()
    db.execute('''
        INSERT INTO scheduled_messages 
        (message_text, target_ids, media_url, media_type, send_time) 
        VALUES (?, ?, ?, ?, ?)
    ''', (text, json.dumps(target), media_url, media_type, send_time))
    db.commit()

def get_pending_messages():
    db = get_db()
    now = int(time.time())
    cursor = db.execute('''
        SELECT id, message_text, target_ids, media_url, media_type
        FROM scheduled_messages 
        WHERE status = 'PENDING' AND send_time <= ?
    ''', (now,))
    return [
        {**dict(row), 'target_ids': json.loads(row['target_ids'])} 
        for row in cursor.fetchall()
    ]

def log_message(chat_id, status, error_details=None):
    db = get_db()
    db.execute('''
        INSERT INTO message_logs (chat_id, send_time, status, error_details)
        VALUES (?, ?, ?, ?)
    ''', (chat_id, int(time.time()), status, error_details))
    db.commit()

def get_log_stats():
    db = get_db()
    cursor = db.execute('''
        SELECT status, COUNT(*) as count 
        FROM message_logs 
        GROUP BY status
    ''')
    return {row['status']: row['count'] for row in cursor.fetchall()}

# --- 3. Authentication ---
def is_logged_in():
    return 'logged_in' in session and session['logged_in']

@app.before_request
def require_login():
    if request.endpoint not in ('login', 'static') and not is_logged_in():
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form['username'] != ADMIN_USER or request.form['password'] != ADMIN_PASS:
            error = 'Invalid Username or Password.'
        else:
            session['logged_in'] = True
            return redirect(url_for('broadcast'))
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# --- 4. Broadcast Panel (Main Logic) ---
@app.route('/', methods=['GET', 'POST'])
def broadcast():
    if not BOT_TOKEN:
        return "Error: TELEGRAM_BOT_TOKEN not set in .env", 500
        
    bot = Bot(token=BOT_TOKEN)
    message = None
    error = None
    
    # 1. Scheduling logic or immediate sending
    if request.method == 'POST' and 'message_text' in request.form:
        
        message_text = request.form['message_text']
        target_type = request.form['target_type']
        target_ids_input = request.form['target_ids'].strip()
        send_type = request.form['send_type']
        schedule_time_str = request.form.get('schedule_time')
        
        media_url = request.form.get('media_url', '').strip()
        media_type = request.form.get('media_type', '').strip()
        
        # 1.1. Determine recipients
        recipients = []
        if target_type == 'all':
            recipients = [user['chat_id'] for user in get_all_users()]
        elif target_type == 'tag':
            selected_tag = request.form['selected_tag']
            recipients = get_users_by_tag(selected_tag)
        elif target_type == 'multiple' and target_ids_input:
            ids_list = [i.strip() for i in target_ids_input.replace('\r\n', ',').split(',') if i.strip()]
            recipients.extend(ids_list)
        
        # 1.2. Schedule management
        if send_type == 'scheduled' and schedule_time_str:
            try:
                # Convert form date and time to Unix Timestamp
                schedule_dt = datetime.strptime(schedule_time_str, '%Y-%m-%dT%H:%M')
                send_timestamp = int(schedule_dt.timestamp())
                
                # Add message to scheduled_messages table
                add_scheduled_message(
                    message_text, 
                    recipients, 
                    media_url, 
                    media_type, 
                    send_timestamp
                )
                message = f"Message successfully scheduled for {schedule_dt.strftime('%Y-%m-%d %H:%M')}."
                
            except ValueError:
                error = "Invalid schedule time format. Please use the YYYY-MM-DD HH:MM format."
            except Exception as e:
                error = f"Failed to schedule message: {str(e)}"
                
        # 1.3. Immediate sending
        else:
            if recipients:
                sent_count = 0
                
                async def send_messages_async():
                    nonlocal sent_count
                    for chat_id in set(recipients):
                        try:
                            # File/text sending logic
                            if media_url and media_type:
                                if media_type == 'photo':
                                    await bot.send_photo(chat_id=chat_id, photo=media_url, caption=message_text, parse_mode='HTML')
                                elif media_type == 'document':
                                    await bot.send_document(chat_id=chat_id, document=media_url, caption=message_text, parse_mode='HTML')
                                else:
                                    await bot.send_message(chat_id=chat_id, text=message_text, parse_mode='HTML')
                            else:
                                await bot.send_message(chat_id=chat_id, text=message_text, parse_mode='HTML')
                                
                            sent_count += 1
                            log_message(chat_id, 'SENT')
                            
                        except TelegramError as e:
                            status = 'FAILED'
                            if 'bot was blocked by the user' in str(e):
                                status = 'BLOCKED'
                            log_message(chat_id, status, str(e))
                            print(f"Error sending to {chat_id}: {e}")
                        except Exception as e:
                            log_message(chat_id, 'FAILED', str(e))
                            print(f"General error sending to {chat_id}: {e}")


                try:
                    asyncio.run(send_messages_async())
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(send_messages_async())

                message = f"Message successfully sent to {sent_count} recipient(s)."
            else:
                error = "No recipients specified for broadcasting."

    # 2. User addition logic
    elif request.method == 'POST' and 'add_id' in request.form:
        new_id = request.form['new_chat_id'].strip()
        new_username = request.form['new_username'].strip() or 'User'
        new_tags_str = request.form.get('new_tags', '').strip()
        new_tags_list = [tag.strip().lower() for tag in new_tags_str.split(',') if tag.strip()]
        
        if new_id:
            success, msg = add_user(new_id, new_username, new_tags_list)
            if success:
                message = msg
            else:
                error = msg

    # Display users list and stats
    users = get_all_users()
    all_tags = get_all_tags()
    log_stats = get_log_stats()
    
    scheduled_count = len(get_pending_messages())
    
    return render_template(
        'broadcast.html', 
        users=users, 
        message=message, 
        error=error,
        all_tags=all_tags,
        log_stats=log_stats,
        scheduled_count=scheduled_count
    )


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
