import json
import time
import logging
import sqlite3
import calendar
import pytz
import re
import json
from datetime import datetime, timedelta
from dateparser.search import search_dates
from google.genai import types
# from prompt_file import get_prompt
from groq import Groq
from config import IST,  gemini_client, client, socketio, GROQ_API_KEY,DATABASE_URL
from database import get_username, get_task_db, add_task_db, delete_task_internal,get_db_connection

groq_client = Groq(api_key=GROQ_API_KEY)


# --- CONFIG ---
OFFICE_START = 10  # 10 AM
OFFICE_END = 19    # 7 PM

def get_prompt(task_text):
    now = datetime.now(IST).replace(second=0, microsecond=0)
    
    prompt = f"""
    Current IST Time: {now.strftime("%H:%M")}
    Current Date: {now.strftime("%d/%m")} ({now.strftime("%A")})
    
    Task: "{task_text}"

    You are a scheduler. Extract a clear deadline from the task.
    If the task does NOT include any explicit time, ALWAYS set the final time to exactly 24 hours from now.

    
    
    RULES:
    1. Time "230" or "2 30" -> "02:30". "9" -> "09:00". "530" -> "05:30".
    2. "Today" -> Date is {now.strftime("%d/%m")}.
    3. Return 24-hour time format (HH:MM). 
    4. If AM/PM is unclear, default to AM (Python logic will fix it).
    
    Return strict JSON:
    {{
        "date": "DD/MM",
        "time": "HH:MM", 
        "day": "Weekday",
        "explicit_today": true/false,
        "text": "Task text only"
    }}
    """
    return prompt

def parse_flexible_time(time_str):
    """
    Tries multiple formats to prevent crashing if LLM gives '2:30 PM' 
    instead of '14:30' or '2.30'.
    Returns a datetime.time object or None.
    """
    if not time_str: return None
    
    # Clean string: "2.30" -> "2:30", " 02:30 " -> "02:30"
    t_str = time_str.strip().replace(".", ":").upper()
    
    formats = ["%H:%M", "%I:%M %p", "%I:%M%p", "%H %M"]
    
    for fmt in formats:
        try:
            return datetime.strptime(t_str, fmt).time()
        except ValueError:
            continue
    return None

def extract_due_date(task_text):

    print(f" task text:{task_text}")
    now = datetime.now(IST).replace(second=0, microsecond=0)

    print(f"now_time {now}")

    
    
    prompt = get_prompt(task_text)

    print(f"promp is {prompt}")

    try:
        print("Inside try ")
        # 1. LLM Call
        response = groq_client.chat.completions.create(

            model="llama-3.1-8b-instant",
            messages=[ {"role": "system", "content": "Respond ONLY with valid JSON. No markdown. No explanation."},
    {"role": "user", "content": prompt}],
            temperature=0,
        )
        raw = response.choices[0].message.content
        print(f" output is {raw}")
        # 2. Extract JSON safely
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            json_str = match.group(0) if match else raw
            data = json.loads(json_str)
        except Exception as e:
            print(f"JSON Parse Error: {raw}")
            raise e

        # 3. Extract Fields
        date_str = data.get("date", "").strip()
        time_str = data.get("time", "").strip()
        day_str = data.get("day", "").strip()
        explicit_today = data.get("explicit_today", False)
        cleaned_text = data.get("text", task_text).strip()

        # 4. Resolve Date
        final_date = None
        
        # A. If date string exists (e.g., "01/12")
        if date_str:
            try:
                # Try parsing DD/MM or DD:MM
                clean_date = date_str.replace(":", "/")
                parsed_date = datetime.strptime(f"{clean_date}/{now.year}", "%d/%m/%Y")
                final_date = parsed_date.date()
            except:
                pass # Fail silently, fallback to logic below

        # B. If no date, but weekday provided
        if not final_date and day_str:
            weekday_map = {day.lower(): i for i, day in enumerate(calendar.day_name)}
            if day_str.lower() in weekday_map:
                target_idx = weekday_map[day_str.lower()]
                today_idx = now.weekday()
                days_ahead = target_idx - today_idx
                if days_ahead <= 0: days_ahead += 7
                final_date = (now + timedelta(days=days_ahead)).date()

        # C. Default to Today
        if not final_date:
            final_date = now.date()

        # 5. Resolve Time & Apply Office Logic
        final_time = parse_flexible_time(time_str)
        
         # If no time found, default to end of day
        if not final_time:
            final_time = time(23, 59)

            
        dt = datetime.combine(final_date, final_time).replace(tzinfo=IST)

        # --- OFFICE HOUR LOGIC ---
        # If user says "230", LLM likely returns "02:30".
        # We assume they mean PM if it's currently Office Hours or if 2:30 AM is absurd.
        if dt.hour < OFFICE_START:
            # Shift +12 hours (e.g., 02:30 -> 14:30)
            dt_pm = dt + timedelta(hours=12)
            
            # Use PM version if:
            # 1. User specifically said "today" (so we must stay on today)
            # 2. OR if extracting 2:30 AM would put us in the past (and PM fixes it)
            if explicit_today:
                dt = dt_pm
            elif dt < now and dt_pm > now:
                dt = dt_pm
            elif dt.hour < 6: # Even if not "today", assume nobody means 2 AM deadline
                dt = dt_pm

        # --- PAST TIME CHECK ---
        # If extracted time is still in the past (e.g. 10:00 AM today, but it's 2:00 PM)
        # AND user didn't write "today" explicitly -> Move to Tomorrow
        if dt < now and not explicit_today:
            dt = dt + timedelta(days=1)

        return (
            dt.strftime("%d:%m"),
            dt.strftime("%H:%M"),
            dt.strftime("%A"),
            cleaned_text
        )

    except Exception as e:
        # PRINT THE ERROR to see why it fails
        print(f"!!! Extraction Failed: {e}")
        
        # Fallback
        default_due = now + timedelta(hours=24)
        return (
            default_due.strftime("%d:%m"),
            default_due.strftime("%H:%M"),
            default_due.strftime("%A"),
            task_text
        )

def reminder_loop():
    """
    Background thread that checks for tasks due soon and sends reminders.
    """
    tz = pytz.timezone("Asia/Kolkata")  # IST
    sent_reminders = set()  # format: f"{task_id}:{assigned_to}:{type}:{date}"

    while True:
        try:
            now = datetime.now(tz)
            date_key = now.strftime("%Y-%m-%d")

            # --- Fetch all pending assignments with task info ---
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("""
                SELECT t.id, ta.assigned_to, t.text, ta.done, t.due
                FROM task_assignments ta
                JOIN tasks t ON t.id = ta.task_id
                WHERE ta.done = TRUE AND t.due IS NOT NULL
            """)
            rows = c.fetchall()
            conn.close()

            for task_id, assigned_to, text, done, due_str in rows:
                if not assigned_to:
                    continue  # skip if no assigned user

                # Parse due datetime
                try:
                    due_dt = datetime.fromisoformat(due_str)
                    if due_dt.tzinfo is None:
                        due_dt = tz.localize(due_dt)
                except Exception:
                    logging.exception(f"Failed to parse due datetime for task {task_id}")
                    continue

                time_left = (due_dt - now).total_seconds()

                # --- Daily reminder at 10 AM ---
                daily_key = f"{task_id}:{assigned_to}:daily:{date_key}"
                if 10 <= now.hour < 11 and daily_key not in sent_reminders:
                    try:
                        dm = client.conversations_open(users=assigned_to)
                        dm_channel = dm["channel"]["id"]
                        client.chat_postMessage(
                            channel=dm_channel,
                            text=f"🌤 Gentle reminder: Task *{text}* (ID: {task_id}) is still pending."
                        )
                        sent_reminders.add(daily_key)
                    except Exception:
                        logging.exception(f"Daily reminder failed for task {task_id} -> user {assigned_to}")

                # --- 1-hour reminder ---
                hour_key = f"{task_id}:{assigned_to}:hour:{date_key}"
                if 0 < abs(time_left - 3600) < 65 and hour_key not in sent_reminders:
                    try:
                        dm = client.conversations_open(users=assigned_to)
                        dm_channel = dm["channel"]["id"]
                        client.chat_postMessage(
                            channel=dm_channel,
                            text=f"⏰ Reminder: Task *{text}* (ID: {task_id}) is due in 1 hour!"
                        )
                        sent_reminders.add(hour_key)
                    except Exception:
                        logging.exception(f"1-hour reminder failed for task {task_id} -> user {assigned_to}")

                # --- 30-minute reminder ---
                half_key = f"{task_id}:{assigned_to}:half:{date_key}"
                if 0 < abs(time_left - 1800) < 65 and half_key not in sent_reminders:
                    try:
                        dm = client.conversations_open(users=assigned_to)
                        dm_channel = dm["channel"]["id"]
                        client.chat_postMessage(
                            channel=dm_channel,
                            text=f"⚠️ Reminder: Task *{text}* (ID: {task_id}) is due in 30 minutes!"
                        )
                        sent_reminders.add(half_key)
                    except Exception:
                        logging.exception(f"30-min reminder failed for task {task_id} -> user {assigned_to}")

        except Exception:
            logging.exception("Reminder loop error")

        time.sleep(60)  # run every minute

def complete_task_logic(task_id, user_who_clicked, slack_channel=None, message_ts=None, note=""):
    """
    Marks a task complete and saves remarks with the user's signature.
    """
    task = get_task_db(task_id)
    if not task:
        return False, "Task not found."

    task_text = task[2] or "[No description]"
    creator_id = task[1]
    user_name = get_username(user_who_clicked)
    # due_str = task[4]

    # --- ✅ CHECK IF LATE ---
    # is_late = False
    # if due_str:
    #     try:
    #         # Assuming due_str is ISO format
    #         due_dt = datetime.fromisoformat(due_str)
    #         # Ensure timezone awareness for comparison (using server local time if naive)
    #         if due_dt.tzinfo is None:
    #             due_dt = due_dt.replace(tzinfo=None) 
            
    #         if datetime.now() > due_dt:
    #             is_late = True
    #     except Exception as e:
    #         logging.error(f"Date comparison failed: {e}")
    
    final_remark = ""
    if note:
        final_remark = f"{note}\n\n— Added by @{user_name}"

    conn = get_db_connection()
    c = conn.cursor()

    c.execute(
        "SELECT id, done FROM task_assignments WHERE task_id=? AND assigned_to=?",
        (task_id, user_who_clicked)
    )
    assignment = c.fetchone()
    timestamp = datetime.now().isoformat()

    if assignment:
        assignment_id, done = assignment
        if done:
            conn.close()
            return False, "Task already completed."
        
        c.execute(
            "UPDATE task_assignments SET done=1, completed_at=?, remarks=? WHERE id=?",
            (timestamp, final_remark, assignment_id)
        )
    elif user_who_clicked == creator_id:
        # Creator marks complete: mark all assignments done
        c.execute(
            "UPDATE task_assignments SET done=1, completed_at=?, remarks=? WHERE task_id=? AND done=TRUE",
            (timestamp, final_remark, task_id)
        )
    else:
        conn.close()
        return False, "You are not allowed to complete this task."

    # Update main task if all assignments done
    c.execute("SELECT COUNT(*) FROM task_assignments WHERE task_id=? AND done=TRUE", (task_id,))
    remaining = c.fetchone()[0]
    if remaining == 0:
        c.execute("UPDATE tasks SET done=1, completed_at=? WHERE id=?",
                  (timestamp, task_id))

    conn.commit()
    conn.close()
 
    # Refresh dashboard
    socketio.emit("task_update", {})

    if slack_channel and message_ts:
        try:
            client.chat_update(
                channel=slack_channel,
                ts=message_ts,
                text=f"✅ *Completed!* {task_text}\n_Completed by <@{user_who_clicked}>_",
                blocks=[]
            )
        except Exception:
            logging.exception("Slack update failed")

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

    return True, f"🎉 <@{user_who_clicked}> completed the task: *{task_text}* (ID: {task_id})"

def edit_task(task_id, new_assignees, editor_user_id, client, logger, new_text=None, new_due=None):
    conn = get_db_connection()
    c = conn.cursor()

    # 1. Fetch task details AND creator_id
    c.execute("SELECT user_id, text, due, file_url FROM tasks WHERE id=?", (task_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        return {"success": False, "error": "Task not found"}

    creator_id, old_text, old_due, file_url = row
    conn.close()

    # --- SECURITY CHECK ---
    if creator_id != editor_user_id:
        return {"success": False, "error": "Permission Denied: Only the task creator can edit this task."}
    
    # --- APPLY EDITS ---
    updated_text = new_text if new_text else old_text
    updated_due = new_due if new_due else old_due

    # --- 2. CREATE NEW TASK WITH UPDATED FIELDS ---
    new_task_id = add_task_db(
        creator=editor_user_id,
        assignees=new_assignees,
        text=updated_text,
        due=updated_due,
        file_url=file_url
    )

    # --- 3. Notify assignees ---
    for assigned_user in new_assignees:
        if assigned_user == editor_user_id:
            continue
        try:
            dm = client.conversations_open(users=assigned_user)
            dm_channel = dm["channel"]["id"]

            msg_text = (
                f"🔔 *Updated Task Assigned to You!*\n"
                f"<@{editor_user_id}> updated a task and assigned it to you:\n\n"
                f"*Task:* {updated_text}\n"
                f"*Due:* {updated_due or 'No due date'}\n"
                f"🆕 *Task ID:* {new_task_id}"
            )
            client.chat_postMessage(channel=dm_channel, text=msg_text)
        except Exception as e:
            logger.exception(f"DM failed for new assignee: {e}")

    # --- 4. DELETE OLD TASK ---
    try:
        delete_task_internal(task_id, editor_user_id, client, logger)
    except Exception as e:
        logger.exception("Delete inside edit failed:", e)

    # --- 5. Notify the editor (creator) ---
    try:
        client.chat_postMessage(
            channel=editor_user_id,
            text=(
                f"✏️ *Task Updated Successfully*\n"
                f"Old Task ID: {task_id}\n"
                f"New Task ID: {new_task_id}"
            )
        )
    except Exception as e:
        logger.exception("Creator notification failed:", e)

    return {"success": True, "new_task_id": new_task_id}