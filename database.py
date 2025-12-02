import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime,timezone,timedelta
from config import client, IST, DATABASE_URL
import pytz
IST = pytz.timezone("Asia/Kolkata")


# Cache for usernames
user_cache = {}

def get_db_connection():
    """Helper to get a Postgres connection"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print("❌ Database connection error:", e)
        raise
    return conn

def init_db():
    # Since you already created tables in PgAdmin, 
    # we can leave this strictly for creating them if they are missing.
    # Note: syntax for AUTOINCREMENT in Postgres is SERIAL.
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        text TEXT,
        created_at TIMESTAMP WITH TIME ZONE,
        due TIMESTAMP WITH TIME ZONE,
        file_url TEXT,
       done BOOLEAN DEFAULT FALSE,
       completed_at TIMESTAMP WITH TIME ZONE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS task_assignments (
        id SERIAL PRIMARY KEY,
        task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
        assigned_to TEXT,
        done BOOLEAN DEFAULT FALSE,
       completed_at TIMESTAMP WITH TIME ZONE,
        remarks TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS login_tokens (
        token_id TEXT PRIMARY KEY,
        user_id TEXT,
        expires_at DOUBLE PRECISION
    )
    """)

    conn.commit()
    conn.close()

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

def add_task_db(creator, assignees, text, due=None, file_url=None):
    created_at = datetime.now(IST).isoformat()
    conn = get_db_connection()
    c = conn.cursor()

    # UPDATED: Use %s and RETURNING id
    c.execute("""
    INSERT INTO tasks (user_id, text, created_at, due, file_url)
    VALUES (%s, %s, %s, %s, %s)
    RETURNING id
    """, (creator, text, created_at, due, file_url))

    task_id = c.fetchone()[0]

    for user in assignees:
        c.execute("""
        INSERT INTO task_assignments (task_id, assigned_to)
        VALUES (%s, %s)
        """, (task_id, user))
    
    conn.commit()
    conn.close()
    return task_id

def complete_task_db(task_id, user_id):
    conn = get_db_connection()
    c = conn.cursor()
    # UPDATED: Use %s
    c.execute("""UPDATE task_assignments 
                 SET done=1, completed_at=%s 
                 WHERE task_id=%s AND assigned_to=%s""",
              (datetime.now(IST), task_id, user_id))
    conn.commit()
    conn.close()

def get_task_db(task_id):
    conn = get_db_connection()
    # Use RealDictCursor to access columns by name easily, 
    # or standard cursor for tuple access (existing code expects tuples in some places)
    c = conn.cursor() 
    c.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_tasks_for_user(uid):
    conn = get_db_connection()
    # Using tuple cursor to match your existing index-based logic (r[0], r[1]...)
    c = conn.cursor()
    
    # UPDATED: Use %s
    c.execute("""
        SELECT t.id, t.user_id, ta.assigned_to, t.text, t.due, ta.done, t.created_at, ta.remarks
        FROM task_assignments ta
        JOIN tasks t ON ta.task_id = t.id
        WHERE ta.assigned_to = %s OR t.user_id = %s
        ORDER BY t.id DESC
    """, (uid, uid))
    rows = c.fetchall()
    conn.close()

    # for r in rows:
    #     print("DB Row:", r)
    
    # Note: Postgres boolean returns True/False. SQLite returned 0/1.
    # We cast bool(r[5]) to be safe.
    return [
            {
            "id": r[0],
            "creator_id": r[1],
            "creator": get_username(r[1]),
            "assigned_to_id": r[2],
            "assigned_to_name": get_username(r[2]),
            "text": r[3],

            # Send formatted string directly (no timezone conversion needed)
            "due": r[4].strftime("%d/%m/%Y %H:%M") if r[4] else "-",
            "done": bool(r[5]),
            "created_at": r[6].strftime("%d/%m/%Y %H:%M") if r[6] else "-",

            "remarks": r[7] or "",
        }

    for r in rows
]

def delete_task_internal(task_id, user_id, client, logger):
    conn = get_db_connection()
    c = conn.cursor()
    
    # UPDATED: Use %s
    c.execute("SELECT id, user_id, text FROM tasks WHERE id=%s", (task_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        logger.error(f"Delete internal failed: Task {task_id} not found")
        return False

    _, creator_id, task_text = row

    c.execute("SELECT assigned_to FROM task_assignments WHERE task_id=%s", (task_id,))
    assignees = [r[0] for r in c.fetchall()]
    conn.close()

    if user_id != creator_id and user_id not in assignees:
        logger.error("Permission denied for delete (internal call).")
        return False

    conn = get_db_connection()
    c = conn.cursor()
    # UPDATED: Use %s
    c.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
    # Note: If you set up ON DELETE CASCADE in Postgres, the next line is optional,
    # but keeping it is safer if you didn't set up cascades.
    c.execute("DELETE FROM task_assignments WHERE task_id=%s", (task_id,))
    conn.commit()
    conn.close()

    


    if creator_id != user_id:
        try:
            dm = client.conversations_open(users=creator_id)
            dm_channel = dm["channel"]["id"]
            msg = (f"❗ *Task Deleted*\n<@{user_id}> deleted your task:\n➡️ *{task_text}*")
            client.chat_postMessage(channel=dm_channel, text=msg)
        except Exception as e:
            logger.exception(f"DM to creator failed: {e}")

    for assigned_user in assignees:
        if assigned_user == user_id:
            continue
        try:
            dm = client.conversations_open(users=assigned_user)
            dm_channel = dm["channel"]["id"]
            msg = (f"❗ *Assigned Task Deleted*\nThe task assigned to you was deleted:\n➡️ *{task_text}*\nDeleted by: <@{user_id}>")
            client.chat_postMessage(channel=dm_channel, text=msg)
        except Exception as e:
            logger.exception(f"DM to assignee failed: {e}")

    return True














