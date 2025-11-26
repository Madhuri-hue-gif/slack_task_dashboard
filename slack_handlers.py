import re
import sqlite3
from datetime import datetime
from config import slack_app, PUBLIC_HOST, DB_FILE
from database import add_task_db, delete_task_internal
from helpers import extract_due_date, complete_task_logic

@slack_app.command("/addtask")
def add_task(ack, body, client, logger):
    ack()
    user_id_invoker = body["user_id"]
    raw_text = body.get("text", "").strip()
    logger.info(f"COMMAND TEXT: {raw_text}")

    if not raw_text:
        client.chat_postMessage(
            channel=user_id_invoker,
            text="⚠️ Please provide a task. Example: `/addtask review the report <@U123> by tomorrow`"
        )
        return

    mentions = re.findall(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", raw_text)
    assigned_to_user_ids = mentions if mentions else [user_id_invoker]
    task_text = re.sub(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", "", raw_text).strip()

    date_str, time_str, day_str, task_text = extract_due_date(task_text)

    due = None
    if date_str and time_str:
        try:
            year = datetime.now().year
            due_dt = datetime.strptime(f"{date_str}:{year} {time_str}", "%d:%m:%Y %H:%M")
            due = due_dt.isoformat()
        except Exception as e:
            print("⚠️ Date parse error:", e)
    elif date_str and not time_str:
        try:
            year = datetime.now().year
            time_str = "23:59"
            due_dt = datetime.strptime(f"{date_str}:{year} {time_str}", "%d:%m:%Y %H:%M")
            due = due_dt.isoformat()
        except Exception as e:
            print("⚠️ Date parse error:", e)

    task_id = add_task_db(user_id_invoker, assigned_to_user_ids, task_text, due=due)

    if due:
        due_dt = datetime.fromisoformat(due)
        due_str = due_dt.strftime("%a, %b %d at %I:%M %p")
    else:
        due_str = "No due time"
    
    client.chat_postMessage(
        channel=user_id_invoker,
        text=f"✅ Task added: *{task_text}* (id: {task_id})\n⏰ *Due:* {due_str}"
    )
    
    for assigned_user in assigned_to_user_ids:
        if assigned_user == user_id_invoker:
            continue
        try:
            dm = client.conversations_open(users=assigned_user)
            dm_channel = dm["channel"]["id"]
            msg_text = (
                 f"🔔 *New Task Assigned!*\n" f"<@{user_id_invoker}> assigned you: *{task_text}*\n"
                 f"⏰ *Due:* {due_str}"
                 )
            client.chat_postMessage(channel=dm_channel, text=msg_text)
        except Exception as e:
            logger.exception(f"DM failed for {assigned_user}: {e}")

@slack_app.command("/deletetask")
def delete_task(ack, body, client, logger):
    ack()
    user_id = body["user_id"]
    text = body.get("text", "").strip()

    if not text.isdigit():
        client.chat_postMessage(channel=user_id, text="⚠️ Usage: `/deletetask <task_id>`")
        return

    task_id = int(text)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM tasks WHERE id=?", (task_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        client.chat_postMessage(channel=user_id, text="❌ Task not found.")
        return

    creator_id = row[0]
    if user_id != creator_id:
        client.chat_postMessage(
            channel=user_id, 
            text="🚫 Permission Denied: Only the creator can delete this task."
        )
        return

    delete_task_internal(task_id, user_id, client, logger)
    client.chat_postMessage(channel=user_id, text=f"🗑️ Task {task_id} deleted successfully.")

@slack_app.command("/completetasknew")
def complete_task_command(ack, body, client):
    ack()
    user_id = body["user_id"]
    task_id_text = body.get("text", "").strip()

    if not task_id_text.isdigit():
        client.chat_postMessage(channel=user_id, text="Usage: `/completetasknew <task_id>`")
        return

    task_id = int(task_id_text)
    success, msg = complete_task_logic(task_id, user_id)
    client.chat_postMessage(channel=user_id, text=f"{'✅' if success else '⚠️'} {msg}")

@slack_app.command("/mytasks")
def mytasks(ack, body, client):
    ack()
    user_id = body["user_id"]
    url = f"{PUBLIC_HOST}/dashboard/{user_id}"
    client.chat_postMessage(channel=user_id, text=f"🧭 Open your Task Dashboard: <{url}|Click here>")