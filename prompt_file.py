from datetime import datetime, timedelta
from config import IST

# --- CONFIGURATION ---
OFFICE_START_HOUR = 10  # 10 AM
OFFICE_END_HOUR = 19    # 7 PM (Buffer for 6:30 PM)

def get_prompt(task_text):
    now = datetime.now(IST).replace(second=0, microsecond=0)
    current_date = now.strftime("%d:%m")
    current_time = now.strftime("%H:%M")
    query_day = now.strftime("%A")

    prompt = f"""
    Context:
    - Current IST Time: {current_time}
    - Current Date: {current_date} ({query_day})
    - Office Hours: 10:00 AM to 06:30 PM
    - Office Hours can vary upto some hours on overtime in AM and PM

    Role: Task Scheduler. Extract deadline details.
    
    Rules for Parsing:
    1. **Sloppy Numbers**: "230" -> "02:30", "5" -> "05:00", "11" -> "11:00".
    2. **Explicit Keywords**: 
       - If text says "today", the date MUST be {current_date}.
       - If text says "tomorrow", the date is {current_date} + 1 day.
    3. **Cleanup**: Remove date/time words from 'text'.
    
    Return JSON:
    {{
        "date": "DD:MM" (or "" if implied),
        "time": "HH:MM" (24-hour format, essential),
        "day": "Weekday" (or ""),
        "explicit_today": true/false (true if word 'today' is in text),
        "text": "Cleaned task description"
    }}
    
    Task: "{task_text}"
    """
    return prompt