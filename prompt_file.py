from datetime import datetime, timedelta
from config import IST

def get_prompt(task_text):
    # Get current context in IST
    now = datetime.now(IST).replace(second=0, microsecond=0)
    query_day = now.strftime("%A")
    current_date = now.strftime("%d:%m")
    current_time = now.strftime("%H:%M")

    prompt = f"""
    ### Context (IST India Time)
    - Today: {query_day}
    - Date: {current_date}
    - Time: {current_time}

    ### Role
    You are a task scheduler. Extract the deadline from the text.
    
    ### Time Parsing Rules (CRITICAL)
    1. **Sloppy Numbers**: Treat 3-4 digit integers as times. 
       - "230" -> "02:30"
       - "900" -> "09:00"
       - "1700" -> "17:00"
    2. **Short Numbers**: 
       - "at 5" -> "05:00"
       - "by 2" -> "02:00"
    3. **AM/PM Inference**:
       - If AM/PM is missing, return 24-hour format closest to logical business hours or defaults.
       - "afternoon" = 14:00, "evening" = 19:00, "tonight" = 21:00.
    4. **Relative**:
       - "in 30 mins" -> Add to current time {current_time}.
    
    ### Date Rules
    - "today" -> {current_date}
    - "tomorrow" -> (add +1 day to {current_date})
    - Weekdays (e.g., "Friday"): If today is Friday, assume NEXT Friday.
    
    ### Output Format
    Return strictly valid JSON.
    {{
        "date": "DD:MM" (or "" if implied today/tomorrow),
        "time": "HH:MM" (24-hour format, or ""),
        "day": "WeekdayName" (or ""),
        "text": "Cleaned task text"
    }}

    Task Input: "{task_text}"
    """
    return prompt