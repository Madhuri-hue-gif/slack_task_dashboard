import json
import time
import logging
import sqlite3
import calendar
import pytz
from datetime import datetime, timedelta
from dateparser.search import search_dates
from google.genai import types
from prompt_file import get_prompt
from groq import Groq
from config import IST, DB_FILE, gemini_client, client, socketio, GROQ_API_KEY
from database import get_username, get_task_db, add_task_db, delete_task_internal

client = Groq(api_key=GROQ_API_KEY)

def extract_due_date(task_text):
    now = datetime.now(IST).replace(second=0, microsecond=0)
    
    # 1. Get Prompt
    prompt = get_prompt(task_text)

    try:
        # 2. Call LLM
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0, # Keep temp 0 for consistency
        )

        raw = response.choices[0].message.content

        # 3. Robust JSON Parsing
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Regex to find the first valid JSON object in text
            match = re.search(r"\{.*?\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
            else:
                raise Exception("No JSON found")

        date_str = data.get("date", "").strip()
        time_str = data.get("time", "").strip()
        day_str = data.get("day", "").strip()
        cleaned_text = data.get("text", task_text).strip()

        # 4. Logic Resolution
        
        # A. Handle Weekday (e.g., "Friday")
        if day_str and not date_str:
            weekday_map = {day.lower(): i for i, day in enumerate(calendar.day_name)}
            target_idx = weekday_map.get(day_str.lower())
            
            if target_idx is not None:
                current_weekday_idx = now.weekday()
                days_ahead = (target_idx - current_weekday_idx)
                
                # If it's today or past day in current week, move to next week
                if days_ahead <= 0: 
                    days_ahead += 7
                    
                target_date = now + timedelta(days=days_ahead)
                date_str = target_date.strftime("%d:%m")

        # B. Handle Missing Date (Implies Today)
        if not date_str:
            date_str = now.strftime("%d:%m")

        # C. Handle Time Parsing & Smart Adjustment
        final_dt = None
        
        if date_str and time_str:
            # Parse extracted date and time
            parsed_dt = datetime.strptime(
                f"{date_str}:{now.year} {time_str}", "%d:%m:%Y %H:%M"
            ).replace(tzinfo=IST)

            # Smart Logic: If the user said "2:30" (AM implied) but it's currently 4 PM,
            # and the date is Today, they likely meant 2:30 PM (14:30) or Tomorrow.
            
            # Case 1: Time passed today? 
            if parsed_dt < now:
                # Try adding 12 hours (e.g., 2:30 -> 14:30)
                pm_shift = parsed_dt + timedelta(hours=12)
                if pm_shift > now and pm_shift.day == parsed_dt.day:
                    final_dt = pm_shift
                else:
                    # If even PM is past (or it was already PM), assume Tomorrow
                    final_dt = parsed_dt + timedelta(days=1)
            else:
                final_dt = parsed_dt
        
        elif date_str and not time_str:
            # Default to End of Day if no time specified
            final_dt = datetime.strptime(
                f"{date_str}:{now.year} 23:59", "%d:%m:%Y %H:%M"
            ).replace(tzinfo=IST)

        # Output formatting
        if final_dt:
            data = (
                final_dt.strftime("%d:%m"),
                final_dt.strftime("%H:%M"),
                final_dt.strftime("%A"),
                cleaned_text
            )
            print(f"the data is {data}")
            # return data

    except Exception as e:
        print(f"Extraction Error: {e}")

    # --- Fallback: 24 Hours from now ---
    fallback_dt = now + timedelta(days=1)
    return (
        fallback_dt.strftime("%d:%m"),
        fallback_dt.strftime("%H:%M"),
        fallback_dt.strftime("%A"),
        task_text.strip() # Return original text on fail
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
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""
                SELECT t.id, ta.assigned_to, t.text, ta.done, t.due
                FROM task_assignments ta
                JOIN tasks t ON t.id = ta.task_id
                WHERE ta.done = 0 AND t.due IS NOT NULL
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

    conn = sqlite3.connect(DB_FILE)
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
            "UPDATE task_assignments SET done=1, completed_at=?, remarks=? WHERE task_id=? AND done=0",
            (timestamp, final_remark, task_id)
        )
    else:
        conn.close()
        return False, "You are not allowed to complete this task."

    # Update main task if all assignments done
    c.execute("SELECT COUNT(*) FROM task_assignments WHERE task_id=? AND done=0", (task_id,))
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
    conn = sqlite3.connect(DB_FILE)
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