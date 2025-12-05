import time
import asyncio
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError

# Import Flask and DB modules from app.py
from app import app, get_pending_messages, get_db, log_message, BOT_TOKEN

SCHEDULER_SLEEP_TIME = 60 # Check every 60 seconds

def update_message_status(msg_id, status):
    """Updates the status of a scheduled message."""
    with app.app_context():
        db = get_db()
        db.execute('UPDATE scheduled_messages SET status = ? WHERE id = ?', (status, msg_id))
        db.commit()

async def send_scheduled_message_async(message, bot):
    """Handles async sending of a single scheduled message."""
    
    target_chat_ids = message['target_ids']
    message_text = message['message_text']
    
    sent_count = 0
    
    for chat_id in set(target_chat_ids):
        try:
            # File/text sending logic
            if message['media_url'] and message['media_type']:
                if message['media_type'] == 'photo':
                    await bot.send_photo(chat_id=chat_id, photo=message['media_url'], caption=message_text, parse_mode='HTML')
                elif message['media_type'] == 'document':
                    await bot.send_document(chat_id=chat_id, document=message['media_url'], caption=message_text, parse_mode='HTML')
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
            print(f"[{datetime.now()}] SCHEDULER ERROR sending to {chat_id}: {e}")
        except Exception as e:
            log_message(chat_id, 'FAILED', str(e))
            print(f"[{datetime.now()}] SCHEDULER GENERAL ERROR: {e}")

    # Update message status
    if sent_count > 0 or len(target_chat_ids) == 0:
        update_message_status(message['id'], 'SENT')
    else:
        update_message_status(message['id'], 'FAILED')

def check_schedule_and_send():
    """Main scheduler loop."""
    print(f"[{datetime.now()}] Scheduler Service Started.")
    
    if not BOT_TOKEN:
        print("BOT_TOKEN is missing. Cannot start scheduler.")
        return

    bot = Bot(token=BOT_TOKEN)
    
    while True:
        with app.app_context():
            pending_messages = get_pending_messages()
            
        if pending_messages:
            print(f"[{datetime.now()}] Found {len(pending_messages)} messages to send.")
            
            for message in pending_messages:
                try:
                    # Async execution
                    asyncio.run(send_scheduled_message_async(message, bot))
                except Exception as e:
                    print(f"[{datetime.now()}] CRITICAL SCHEDULER FAIL: {e}")
                    update_message_status(message['id'], 'FAILED')
                    
        else:
            print(f"[{datetime.now()}] No pending messages.")

        time.sleep(SCHEDULER_SLEEP_TIME)

if __name__ == '__main__':
    # This service should run in the background
    check_schedule_and_send()
