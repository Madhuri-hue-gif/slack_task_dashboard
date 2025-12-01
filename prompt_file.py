from datetime import datetime, timedelta
from config import IST

def get_prompt(task_text):
    date_time = datetime.now(IST).replace(second=0, microsecond=0)
    query_day = date_time.strftime("%A")
    current_date = date_time.strftime("%d:%m")
    current_time = date_time.strftime("%H:%M")

    # --- LLM Prompt ---
    prompt = f"""
        Reference Context:
        - Current IST Day: {query_day}
        - Current IST Date: {current_date}
        - Current IST Time: {current_time}
        You are a precise date & time extractor for a task manager used in India (IST).
        Your job:
        1. Determine if the text implies a **deadline** — specific dates, times, or relative durations ("in 30 mins", "next 1 hour").
        2. Infer missing parts based on current IST context.
        3. Remove the date/time reference from the text to clean it.
        Rules:
        - Only time implied? assume today ({query_day}, {current_date}).
        - "today" ? use today.
        - "tomorrow" ? add +1 day.
        - "yesterday" ? skip (no deadline).
        - Weekday ("Monday", "Tuesday", etc.):
            * If the weekday is today or has already passed this week, pick **next occurrence**.
            * If it's later in this week, pick that date.
        - "before <weekday>" ? deadline = one day before that weekday.
        - Date-only (like "2nd") ? assume current month.
        - "2 Nov" or "Nov 2" ? use that date directly.
        - Missing both date/time ? leave blank.
        IMPORTANT - Relative Duration Logic:
        - Phrases like "in X hours", "next X mins", "within X hours":
            * ADD that duration to the **Current IST Time**.
            * If the result crosses midnight, increment the **Current IST Date** by +1.
            * Example: If Current Time is 14:00 and input is "in 2 hours", result is 16:00 today.
        - Convert vague times:
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