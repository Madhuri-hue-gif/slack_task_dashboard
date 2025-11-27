import sqlite3
from datetime import datetime
from config import DB_FILE, client

# Cache for usernames
user_cache = {}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # --- Main tasks table ---
    c.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,             -- The creator of the task
        text TEXT,
        created_at TEXT,
        due TEXT,
        file_url TEXT,
        done INTEGER DEFAULT 0,
        completed_at TEXT
    )
    """)

    # --- Task assignments table ---
    c.execute("""
    CREATE TABLE IF NOT EXISTS task_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        assigned_to TEXT,
        done INTEGER DEFAULT 0,
        completed_at TEXT,
        remarks TEXT,
        FOREIGN KEY (task_id) REFERENCES tasks(id)
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
    created_at = datetime.now().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    INSERT INTO tasks (user_id, text, created_at, due, file_url)
    VALUES (?, ?, ?, ?, ?)
    """, (creator, text, created_at, due, file_url))

    task_id = c.lastrowid

    for user in assignees:
        c.execute("""
        INSERT INTO task_assignments (task_id, assigned_to)
        VALUES (?, ?)
        """, (task_id, user))
    
    conn.commit()
    conn.close()
    return task_id

def complete_task_db(task_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""UPDATE task_assignments 
                 SET done=1, completed_at=? 
                 WHERE task_id=? AND assigned_to=?""",
              (datetime.now().isoformat(), task_id, user_id))
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
    c.execute("""
        SELECT t.id, t.user_id, ta.assigned_to, t.text, t.due, ta.done, t.created_at, ta.remarks
        FROM task_assignments ta
        JOIN tasks t ON ta.task_id = t.id
        WHERE ta.assigned_to = ? OR t.user_id = ?
        ORDER BY t.id DESC
    """, (uid, uid))
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "creator_id": r[1],
            "creator": get_username(r[1]),
            "assigned_to_id": r[2],          
            "assigned_to_name": get_username(r[2]),
            "text": r[3],
            "due": r[4] or "-",
            "done": bool(r[5]),
            "created_at": r[6] or "-",
            "remarks": r[7] or "",
            # "completed_at": r[8]  
        }
        for r in rows
    ]

def delete_task_internal(task_id, user_id, client, logger):
    # 1. Fetch task info
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, user_id, text FROM tasks WHERE id=?", (task_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        logger.error(f"Delete internal failed: Task {task_id} not found")
        return False

    _, creator_id, task_text = row

    # 2. Fetch assignees
    c.execute("SELECT assigned_to FROM task_assignments WHERE task_id=?", (task_id,))
    assignees = [r[0] for r in c.fetchall()]

    conn.close()

    # 3. Permission check
    if user_id != creator_id and user_id not in assignees:
        logger.error("Permission denied for delete (internal call).")
        return False

    # 4. Delete task + its assignments
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    c.execute("DELETE FROM task_assignments WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()

    # 5. DM creator (if different from user deleting)
    if creator_id != user_id:
        try:
            dm = client.conversations_open(users=creator_id)
            dm_channel = dm["channel"]["id"]
            msg = (
                f"❗ *Task Deleted*\n"
                f"<@{user_id}> deleted your task:\n"
                f"➡️ *{task_text}*"
            )
            client.chat_postMessage(channel=dm_channel, text=msg)
        except Exception as e:
            logger.exception(f"DM to creator failed: {e}")

    # 6. DM assignees
    for assigned_user in assignees:
        if assigned_user == user_id:
            continue  # skip the user who deleted

        try:
            dm = client.conversations_open(users=assigned_user)
            dm_channel = dm["channel"]["id"]
            msg = (
                f"❗ *Assigned Task Deleted*\n"
                f"The task assigned to you was deleted:\n"
                f"➡️ *{task_text}*\n"
                f"Deleted by: <@{user_id}>"
            )
            client.chat_postMessage(channel=dm_channel, text=msg)
        except Exception as e:
            logger.exception(f"DM to assignee failed: {e}")

    return True