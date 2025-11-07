import os
import re
import json
import time
import logging
import threading
import sqlite3
import calendar
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, send_from_directory, render_template_string, request
from flask_socketio import SocketIO, emit
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from dotenv import load_dotenv
from dateparser.search import search_dates
from google import genai
from google.genai import types

load_dotenv()

# --- IST timezone ---
IST = timezone(timedelta(hours=5, minutes=30))

# --- Gemini API key (replace with your valid key) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("❌ No API key provided. Please set GEMINI_API_KEY in your .env file.")

# --- Initialize Gemini client ---
gemini_client = genai.Client(api_key=GEMINI_API_KEY)



logging.basicConfig(level=logging.INFO)

DB_FILE = "tasks.db"
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "http://192.168.1.173:5000")
# PUBLIC_HOST = os.getenv("PUBLIC_HOST", "http://192.168.2.180:4000")


slack_app = App(token=SLACK_BOT_TOKEN)
client = WebClient(token=SLACK_BOT_TOKEN)
flask_app = Flask(__name__)
socketio = SocketIO(flask_app, cors_allowed_origins="*")

WEB_DASH_PATH = os.path.join("web", "dashboard")
WEB_STYLE_PATH = os.path.join("web", "style")

# ---------------- DATABASE ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        assigned_to TEXT,
        text TEXT,
        done INTEGER DEFAULT 0,
        created_at TEXT,
        completed_at TEXT,
        due TEXT,
        file_url TEXT
    )""")
    conn.commit()
    conn.close()

# ---------------- HELPERS ----------------
user_cache = {}
def get_username(uid):
    if not uid:
        return "-"
    if uid in user_cache:
        return user_cache[uid]
    try:
        info = client.users_info(user=uid)
        username = info["user"]["profile"]["display_name"] or info["user"]["name"]
        user_cache[uid] = username
        return username
    except Exception:
        return uid

def add_task_db(creator, assigned_to, text, due=None, file_url=None):
    if due:
        try:
            #convert due text to datetime
            due_dt=datetime.fromisoformat(due)
        except Exception:
            due_dt=None
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""INSERT INTO tasks (user_id, assigned_to, text, done, created_at, due, file_url)
                 VALUES (?, ?, ?, 0, ?, ?, ?)""",
              (creator, assigned_to, text, datetime.now().isoformat(), due, file_url))
    conn.commit()
    tid = c.lastrowid
    conn.close()
    return tid

def complete_task_db(task_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?", (datetime.now().isoformat(), task_id))
    conn.commit()
    conn.close()

def get_task_db(task_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_tasks_for_user(uid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT id, user_id, assigned_to, text, done, due FROM tasks
                 WHERE user_id=? OR assigned_to=? ORDER BY id DESC""", (uid, uid))
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "creator": get_username(r[1]),
            "assigned_to": get_username(r[2]),
            "text": r[3],
            "done": bool(r[4]),
            "due": r[5] or "-"
        }
        for r in rows
    ]


# 

def extract_due_date(task_text):
    """
    Extract due date, time, and weekday from task_text using Gemini + fallback.
    Returns (date_str, time_str, day_str, cleaned_text)
    """

    # --- Current IST context ---
    date_time = datetime.now(IST).replace(second=0, microsecond=0)
    query_day = date_time.strftime("%A")       # e.g. Monday
    current_date = date_time.strftime("%d:%m") # e.g. 07:11
    current_time = date_time.strftime("%H:%M") # e.g. 15:42

    # --- LLM Prompt ---
    prompt = f"""
Reference Context:
- Current IST Day: {query_day}
- Current IST Date: {current_date}
- Current IST Time: {current_time}

You are a precise date & time extractor for a task manager used in India (IST).

Your job:
1. Determine if the text implies a **deadline** — any date, weekday, time, or relative term like "tomorrow", "next week", etc.
2. Infer missing parts based on current IST context.

Rules:
- Only time → assume today ({query_day}, {current_date})
- "today" → use today
- "tomorrow" → add +1 day
- "yesterday" → skip (no deadline)
- Weekday ("Monday", "Tuesday", etc.):
    * If the weekday is today or has already passed this week, pick **next occurrence**.
    * If it's later in this week, pick that date.
- "before <weekday>" → deadline = one day before that weekday.
- Date-only (like "2nd") → assume current month.
- "2 Nov" or "Nov 2" → use that date directly.
- Missing both date/time → leave blank.
-  Convert vague times:
   - morning = 11:00
   - afternoon = 14:00
   - evening = 17:00
   - night = 21:00


Return output strictly as JSON:
{{
  "date": "DD:MM" or "",
  "time": "HH:MM" or "",
  "day": "Weekday" or "",
  "text": "remaining task text without date/time info"
}}

Task: "{task_text}"
"""

    try:
        fn_decl = {
            "name": "extract_due_date",
            "description": "Extracts first due date, time, and weekday from a task description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Due date in DD:MM format or empty"},
                    "time": {"type": "string", "description": "Due time in HH:MM (24h) format or empty"},
                    "day": {"type": "string", "description": "Weekday name or empty"},
                    "text": {"type": "string", "description": "Cleaned task text without date/time"},
                },
                "required": ["text"],
            },
        }

        tools = [types.Tool(function_declarations=[fn_decl])]
        config = types.GenerateContentConfig(tools=tools)

        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config=config,
        )

        candidate = response.candidates[0]
        date_str = time_str = day_str = ""
        cleaned_text = task_text.strip()

        if candidate.content.parts and candidate.content.parts[0].function_call:
            fn_call = candidate.content.parts[0].function_call
            args = fn_call.args
            if isinstance(args, str):
                args = json.loads(args)

            date_str = args.get("date") or ""
            time_str = args.get("time") or ""
            day_str = args.get("day") or ""
            cleaned_text = args.get("text", task_text).strip()

        # --- Defaulting Rules ---
        if time_str and not date_str:
            date_str = current_date
            day_str = query_day
        if not date_str and not time_str:
            return None, None, None, cleaned_text

        # --- Weekday fallback inference ---
        if not date_str and day_str:
            weekday_map = {day.lower(): i for i, day in enumerate(calendar.day_name)}
            if day_str.lower() in weekday_map:
                target_idx = weekday_map[day_str.lower()]
                today_idx = weekday_map[query_day.lower()]
                days_ahead = (target_idx - today_idx) % 7
                if days_ahead == 0:
                    days_ahead = 7
                due_dt = date_time + timedelta(days=days_ahead)
                return due_dt.strftime("%d:%m"), "23:59", day_str, cleaned_text

        # --- Final validation ---
        year = date_time.year
        if date_str:
            try:
                if time_str:
                    due_dt = datetime.strptime(f"{date_str}:{year} {time_str}", "%d:%m:%Y %H:%M").replace(tzinfo=IST)
                else:
                    due_dt = datetime.strptime(f"{date_str}:{year} 23:59", "%d:%m:%Y %H:%M").replace(tzinfo=IST)
                return due_dt.strftime("%d:%m"), due_dt.strftime("%H:%M"), due_dt.strftime("%A"), cleaned_text
            except Exception:
                pass

    except Exception as e:
        print(f"⚠️ LLM extraction failed: {e}")

    # --- Fallback using dateparser ---
    results = search_dates(task_text, settings={"PREFER_DATES_FROM": "future"})
    if results:
        matched_text, due_dt = results[0]
        cleaned_text = task_text.replace(matched_text, "").strip()
        return due_dt.strftime("%d:%m"), due_dt.strftime("%H:%M"), due_dt.strftime("%A"), cleaned_text

    return None, None, None, task_text.strip()



def reminder_loop():
    """
    Background thread that checks for tasks due soon and sends reminders.
    - Prevents duplicate reminders
    - Handles timezone correctly
    - Sends one daily reminder (9 AM)
    - Sends one 1-hour & one 30-min reminder per task
    """
    import pytz
    tz = pytz.timezone("Asia/Kolkata")  # change if needed

    sent_reminders = set()  # key format: f"{task_id}:{type}:{date}"

    while True:
        try:
            now = datetime.now(tz)

            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT id, assigned_to, text, done, due FROM tasks WHERE done=0 AND due IS NOT NULL")
            tasks = c.fetchall()
            conn.close()

            for task in tasks:
                task_id, assigned_to, text, done, due_str = task
                try:
                    due_dt = datetime.fromisoformat(due_str)
                    if due_dt.tzinfo is None:
                        due_dt = tz.localize(due_dt)
                except Exception:
                    continue

                time_left = (due_dt - now).total_seconds()
                date_key = now.strftime("%Y-%m-%d")

                # --- 1️⃣ Daily gentle reminder at 9 AM
                daily_key = f"{task_id}:daily:{date_key}"
                if now.hour == 9 and daily_key not in sent_reminders:
                    try:
                        dm = client.conversations_open(users=assigned_to)
                        dm_channel = dm["channel"]["id"]
                        client.chat_postMessage(
                            channel=dm_channel,
                            text=f"🌤 Gentle reminder: Task *{text}* (ID: {task_id}) is still pending."
                        )
                        sent_reminders.add(daily_key)
                    except Exception:
                        logging.exception("Daily reminder failed")

                # --- 2️⃣ 1-hour reminder
                hour_key = f"{task_id}:hour:{date_key}"
                if 0 < time_left <= 3600 and hour_key not in sent_reminders:
                    try:
                        dm = client.conversations_open(users=assigned_to)
                        dm_channel = dm["channel"]["id"]
                        client.chat_postMessage(
                            channel=dm_channel,
                            text=f"⏰ Reminder: Task *{text}* (ID: {task_id}) is due in 1 hour!"
                        )
                        sent_reminders.add(hour_key)
                    except Exception:
                        logging.exception("1-hour reminder failed")

                # --- 3️⃣ 30-minute reminder
                half_key = f"{task_id}:half:{date_key}"
                if 0 < time_left <= 1800 and half_key not in sent_reminders:
                    try:
                        dm = client.conversations_open(users=assigned_to)
                        dm_channel = dm["channel"]["id"]
                        client.chat_postMessage(
                            channel=dm_channel,
                            text=f"⚠️ Reminder: Task *{text}* (ID: {task_id}) is due in 30 minutes!"
                        )
                        sent_reminders.add(half_key)
                    except Exception:
                        logging.exception("30-minute reminder failed")

        except Exception:
            logging.exception("Reminder loop error")

        time.sleep(60)  # run every minute

# ---------------- COMMON COMPLETION LOGIC ----------------
def complete_task_logic(task_id, user_who_clicked, slack_channel=None, message_ts=None):
    """
    Marks a task complete and optionally updates Slack DM message.
    Returns (success, message)
    """
    task = get_task_db(task_id)
    if not task:
        return False, "Task not found."

    if task[4] == 1:  # done
        return False, "Task already completed."

    complete_task_db(task_id)
    socketio.emit("task_update", {})  # refresh dashboard

    task_text = task[3]
    creator_id = task[1]
    assigned_id = task[2]

    # Update Slack DM message if channel + ts provided
    if slack_channel and message_ts:
        client.chat_update(
            channel=slack_channel,
            ts=message_ts,
            text=f"✅ *Completed!* {task_text}",
            blocks=[{
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"✅ *Completed!* {task_text}\n_Completed by <@{user_who_clicked}>_"}
            }]
        )

    # Notify creator if different
    if creator_id and creator_id != user_who_clicked:
        try:
            dm = client.conversations_open(users=creator_id)
            dm_channel = dm["channel"]["id"]
            client.chat_postMessage(
                channel=dm_channel,
                text=f"🎉 <@{user_who_clicked}> completed the task: *{task_text}* (ID: {task_id})"
            )
        except Exception:
            logging.exception("Notify creator failed")

    return True, f"Task {task_id} marked complete."

# ---------------- SLACK COMMANDS ----------------
@slack_app.command("/addtask")
def add_task(ack, body, client, logger):
    ack()

    # --- Extract invoker & text ---
    user_id_invoker = body["user_id"]
    raw_text = body.get("text", "").strip()
    logger.info(f"COMMAND TEXT: {raw_text}")

    if not raw_text:
        client.chat_postMessage(
            channel=user_id_invoker,
            text="⚠️ Please provide a task. Example: `/addtask review the report <@U123> by tomorrow`"
        )
        return

    # --- Detect assignee mention ---
    mention_match = re.search(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", raw_text)
    assigned_to_user_id = user_id_invoker
    task_text = raw_text

    if mention_match:
        assigned_to_user_id = mention_match.group(1)
        task_text = re.sub(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", "", raw_text).strip()

    # --- Extract due date ---
    date_str, time_str, day_str, task_text = extract_due_date(task_text)

    # print("------------------------------------------------")
    # print(" Extractor returned:")
    # print(f"  Task text : {task_text}")
    # print(f"  Date      : {date_str}")
    # print(f"  Time      : {time_str}")
    # print(f"  Day       : {day_str}")
    # print("------------------------------------------------")

    # --- Build due datetime ---
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

    # --- Always add the task (even if due=None) ---
    task_id = add_task_db(user_id_invoker, assigned_to_user_id, task_text, due=due)

    # --- Build Slack-friendly due display ---
    if due:
        due_dt = datetime.fromisoformat(due)
        due_str = due_dt.strftime("%a, %b %d at %I:%M %p")
    else:
        due_str = "No due time"

    # --- Send confirmation to Slack ---
    client.chat_postMessage(
        channel=user_id_invoker,
        text=f"✅ Task added: *{task_text}* (id: {task_id})\n⏰ *Due:* {due_str}"
    )


    
    
    # DM assigned user
    if assigned_to_user_id != user_id_invoker:
        try:
            dm = client.conversations_open(users=assigned_to_user_id)
            dm_channel = dm["channel"]["id"]
            msg_text = (f"🔔 *New Task Assigned!*\n"
                        f"<@{user_id_invoker}> assigned you: *{task_text}*\n"f"⏰ *Due:* {due_str}"
            )
            client.chat_postMessage(
                channel=dm_channel,
                text=msg_text,
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": msg_text}},
                    {"type": "actions",
                     "elements": [{
                         "type": "button",
                         "text": {"type": "plain_text", "text": "✅ Mark Complete"},
                         "style": "primary",
                         "action_id": "complete_task",
                         "value": str(task_id)
                     }]
                    }
                ]
            )
        except Exception:
            logger.exception("DM failed")
            client.chat_postMessage(
                channel=user_id_invoker,
                text=f"⚠️ Could not DM <@{assigned_to_user_id}>. They might not have app access."
            )

@slack_app.command("/deletetask")
def delete_task(ack, body, client, logger):
    ack()
    user_id = body["user_id"]
    text = body.get("text", "").strip()

    if not text.isdigit():
        client.chat_postMessage(channel=user_id, text="⚠️ Usage: `/deletetask <task_id>`")
        return

    task_id = int(text)
    task = get_task_db(task_id)
    if not task:
        client.chat_postMessage(channel=user_id, text=f"❌ No task found with ID {task_id}.")
        return

    creator, assigned = task[1], task[2]
    if user_id not in (creator, assigned):
        client.chat_postMessage(channel=user_id, text="🚫 You’re not allowed to delete this task.")
        return

    # delete it
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

    client.chat_postMessage(channel=user_id, text=f"🗑️ Task {task_id} deleted successfully.")

@slack_app.command("/listtasks")
def list_tasks(ack, body, client, logger):
    ack()
    user_id = body["user_id"]

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT id, text, assigned_to, user_id, done, due, file_url
                 FROM tasks WHERE user_id = ? OR assigned_to = ? ORDER BY id DESC""", (user_id, user_id))
    rows = c.fetchall()
    conn.close()

    if not rows:
        client.chat_postMessage(channel=user_id, text="📭 You have no tasks yet.")
        return

    msg_lines = []
    for row in rows:
        task_id, text, assigned_to, creator, done, due, file_url = row
        status = "✅" if done else "🕒"
        due_text = f" (Due: {due})" if due else ""
        assigned_text = f" → Assigned to <@{assigned_to}>" if assigned_to != creator else ""
        file_text = f" 📎 <{file_url}|Attachment>" if file_url else ""
        msg_lines.append(f"{status} *{text}* — ID: `{task_id}`{due_text}{assigned_text}{file_text}")

    client.chat_postMessage(channel=user_id, text="🧾 *Your Tasks:*\n" + "\n".join(msg_lines))

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

# ---------------- SLACK BUTTON HANDLER ----------------
@slack_app.action("complete_task")
def handle_complete_task(ack, body, client, logger):
    ack()
    action = body["actions"][0]
    task_id = action.get("value")
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    if not task_id or not task_id.isdigit():
        client.chat_postEphemeral(channel=channel_id, user=user_id, text="⚠️ Invalid task ID.")
        return

    complete_task_logic(int(task_id), user_id, slack_channel=channel_id, message_ts=message_ts)

@slack_app.command("/mytasks")
def mytasks(ack, body, client):
    ack()
    user_id = body["user_id"]
    url = f"{PUBLIC_HOST}/dashboard/{user_id}"
    client.chat_postMessage(channel=user_id, text=f"🧭 Open your Task Dashboard: <{url}|Click here>")

# ---------------- FLASK ROUTES ----------------
@flask_app.route("/style/<path:filename>")
def serve_style(filename):
    return send_from_directory(WEB_STYLE_PATH, filename)

@flask_app.route("/dashboard/<user_id>")
def dashboard(user_id):
    with open(os.path.join(WEB_DASH_PATH, "dashboard.html")) as f:
        html = f.read().replace("{{ user_id }}", user_id)
    return render_template_string(html)

@flask_app.route("/api/tasks/<user_id>")
def api_tasks(user_id):
    return jsonify(get_tasks_for_user(user_id))

@flask_app.route("/api/complete_task", methods=["POST"])
def api_complete_task():
    data = request.get_json()
    task_id = data.get("task_id")
    complete_task_db(task_id)
    socketio.emit("task_update", {})
    return jsonify({"ok": True})

# ---------------- RUN ----------------
def run_flask():
    socketio.run(flask_app, host="0.0.0.0", port=5000)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    print(f"⚡ Running Slack Bot with Dashboard at {PUBLIC_HOST}")
    SocketModeHandler(slack_app, SLACK_APP_TOKEN).start()
