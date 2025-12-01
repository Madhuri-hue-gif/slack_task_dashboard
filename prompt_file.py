from datetime import datetime, timedelta
from config import IST

def get_prompt(task_text):
    date_time = datetime.now(IST).replace(second=0, microsecond=0)
    query_day = date_time.strftime("%A")
    current_date = date_time.strftime("%d:%m")
    current_time = date_time.strftime("%H:%M")

    prompt = f"""
    Reference Context:
    - Current IST Day: {query_day}
    - Current IST Date: {current_date}
    - Current IST Time: {current_time}

    You are a precise date & time extractor for a task manager in India (IST).
    
    Rules:
    1. **Sloppy Time Handling**: 
       - If you see 3-4 digits like "230", "930", "1100" -> treat as HH:MM ("02:30", "09:30", "11:00").
       - "230 pm" -> "14:30".
    2. **Cleanup**: REMOVE the detected date/time string from the 'text' field completely.
    3. **Date Logic**:
       - Only time implied? -> "{current_date}"
       - "tomorrow" -> add +1 day to {current_date}
       - Weekday (e.g. "Friday") -> If today is Friday, assume NEXT Friday.
    
    Output JSON (Strictly):
    {{
        "date": "DD:MM" (or "" if unknown),
        "time": "HH:MM" (24-hour format),
        "day": "Weekday",
        "text": "Task description ONLY (no date/time words)"
    }}
    
    Task: "{task_text}"
    """
    return prompt