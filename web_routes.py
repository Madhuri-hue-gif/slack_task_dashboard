import os
import sqlite3
import logging
import requests
import jwt
import time
from functools import wraps
from flask import jsonify, send_from_directory, render_template_string, request, session, redirect, url_for
from config import flask_app, socketio, client, SLACK_BOT_TOKEN, WEB_STYLE_PATH, WEB_DASH_PATH, DATABASE_URL, SECRET_KEY
from database import get_tasks_for_user, delete_task_internal,get_db_connection
from helpers import edit_task, complete_task_logic

# --- HELPER: Decorator to require login ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Unauthorized. Please run /mytasks in Slack."}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- ROUTE: Serve Styles ---
@flask_app.route("/style/<path:filename>")
def serve_style(filename):
    return send_from_directory(WEB_STYLE_PATH, filename)

# --- ROUTE: One-Time Login Handler ---
@flask_app.route("/login")
def login():
    token = request.args.get('token')
    if not token:
        return "Missing token. Please request a new link via /mytasks in Slack.", 400
    
    try:
        # 1. Verify Signature
        data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        token_unique_id = data["jti"]
        user_id = data["user_id"]

        # 2. Check Database (Is token valid and unused?)
        conn = get_db_connection()
        c = conn.cursor()
        
        # Optional: cleanup old tokens
        c.execute("DELETE FROM login_tokens WHERE expires_at < %s", (time.time(),))

        c.execute("SELECT * FROM login_tokens WHERE token_id = %s", (token_unique_id,))
        row = c.fetchone()
        
        if not row:
            conn.close()
            return "<h3>Link Invalid or Expired</h3><p>This link has already been used. Please run <code>/mytasks</code> again.</p>"

        # 3. Burn the Token (Delete it)
        c.execute("DELETE FROM login_tokens WHERE token_id = %s", (token_unique_id,))
        conn.commit()
        conn.close()

        # 4. Set Secure Session
        session['user_id'] = user_id
        
        # 5. Redirect
        return redirect(url_for('dashboard'))

    except jwt.ExpiredSignatureError:
        return "Link expired."
    except jwt.InvalidTokenError:
        return "Invalid token."

# --- ROUTE: Dashboard (Secured) ---
# Note: No <user_id> in URL anymore
@flask_app.route("/dashboard")
def dashboard():
    if 'user_id' not in session:
        return "<h3>Unauthorized</h3><p>Please run <code>/mytasks</code> in Slack to log in.</p>"

    user_id = session['user_id']
    
    # Inject user_id into HTML for JS to use
    with open(os.path.join(WEB_DASH_PATH, "dashboard.html"), encoding="utf-8") as f:
        html = f.read().replace("{{ user_id }}", user_id)
    return render_template_string(html)

# --- API: Get Tasks (Secured) ---
@flask_app.route("/api/tasks/<user_id>")
@login_required
def api_tasks(user_id):
    # Ensure the session user matches the requested user data
    if session['user_id'] != user_id:
        return jsonify({"error": "Unauthorized access to another user's data"}), 403
    return jsonify(get_tasks_for_user(user_id))

# --- API: Get Slack Users (Secured) ---
@flask_app.route("/api/slack_users")
@login_required
def get_slack_users():
    url = "https://slack.com/api/users.list"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get(url, headers=headers).json()

    users = []
    for member in resp.get("members", []):
        if member.get("deleted") or member.get("is_bot") or member.get("id") == "USLACKBOT":
            continue
        users.append({
            "id": member["id"],
            "name": member["profile"].get("real_name") or member.get("name")
        })
    return jsonify(users)

# --- API: Edit Task (Secured) ---
@flask_app.route("/api/edit_task", methods=["POST"])
@login_required
def api_edit_task():
    data = request.get_json()
    task_id = data["task_id"]
    new_text = data["new_text"]
    new_due = data["new_due"]
    new_assignees = data["new_assignees"]
    
    # SECURITY: Use the logged-in user's ID, not what the frontend sends
    editor_user_id = session['user_id']

    local_logger = logging.getLogger("edit_task_api")
    result = edit_task(
        task_id,
        new_assignees,
        editor_user_id,
        client,
        local_logger,
        new_text=new_text,
        new_due=new_due
    )
    return jsonify(result)

# --- API: Complete Task (Secured) ---
@flask_app.route("/api/complete_task", methods=["POST"])
@login_required
def api_complete_task():
    data = request.get_json()
    task_id = data.get("task_id")
    # SECURITY: Use session ID
    user_id = session['user_id'] 
    note = data.get("note", "")
    
    if not task_id:
        return jsonify({"success": False, "message": "Missing task_id"}), 400

    try:
        success, message = complete_task_logic(task_id, user_id, note=note)
        return jsonify({"success": success, "message": message})
    except Exception as e:
        logging.exception("Error in completing task")
        return jsonify({"success": False, "message": str(e)}), 500

# --- API: Delete Task (Secured) ---
@flask_app.route("/api/delete_task", methods=["POST"])
@login_required
def api_delete_task():
    data = request.get_json()
    task_id = data.get("task_id")
    # SECURITY: Use session ID
    user_id = session['user_id']

    if task_id is None:
        return jsonify({"success": False, "error": "Missing task_id"}), 400

    try:
        task_id = int(task_id)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid task_id"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, text FROM tasks WHERE id=%s", (task_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"success": False, "error": "Task not found"}), 404

    creator_id, task_text = row

    if str(user_id).strip() != str(creator_id).strip():
        return jsonify({"success": False, "error": "Permission denied: Only the creator can delete this task."}), 403

    logger = logging.getLogger("delete_api")
    try:
        deleted = delete_task_internal(task_id, user_id, client, logger)
        if deleted:
            socketio.emit("task_update", {})
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Internal deletion logic failed"}), 500
    except Exception as e:
        logger.exception(f"Exception in delete_task_internal for task {task_id}")
        return jsonify({"success": False, "error": f"Server error: {str(e)}"}), 500