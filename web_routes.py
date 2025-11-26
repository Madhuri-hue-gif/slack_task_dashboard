import os
import sqlite3
import logging
import requests
from flask import jsonify, send_from_directory, render_template_string, request
from config import flask_app, socketio, client, SLACK_BOT_TOKEN, WEB_STYLE_PATH, WEB_DASH_PATH, DB_FILE
from database import get_tasks_for_user, delete_task_internal
from helpers import edit_task, complete_task_logic

@flask_app.route("/style/<path:filename>")
def serve_style(filename):
    return send_from_directory(WEB_STYLE_PATH, filename)

@flask_app.route("/dashboard/<user_id>")
def dashboard(user_id):
    with open(os.path.join(WEB_DASH_PATH, "dashboard.html"), encoding="utf-8") as f:
        html = f.read().replace("{{ user_id }}", user_id)
    return render_template_string(html)

@flask_app.route("/api/tasks/<user_id>")
def api_tasks(user_id):
    return jsonify(get_tasks_for_user(user_id))

@flask_app.route("/api/slack_users")
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

@flask_app.route("/api/edit_task", methods=["POST"])
def api_edit_task():
    data = request.get_json()
    task_id = data["task_id"]
    new_text = data["new_text"]
    new_due = data["new_due"]
    new_assignees = data["new_assignees"]
    editor_user_id = data["editor_user_id"]

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

@flask_app.route("/api/complete_task", methods=["POST"])
def api_complete_task():
    data = request.get_json()
    task_id = data.get("task_id")
    user_id = data.get("user_id")
    note = data.get("note", "")
    
    if not task_id or not user_id:
        return jsonify({"success": False, "message": "Missing task_id or user_id"}), 400

    try:
        success, message = complete_task_logic(task_id, user_id, note=note)
        return jsonify({"success": success, "message": message})
    except Exception as e:
        logging.exception("Error in completing task")
        return jsonify({"success": False, "message": str(e)}), 500

@flask_app.route("/api/delete_task", methods=["POST"])
def api_delete_task():
    data = request.get_json()
    task_id = data.get("task_id")
    user_id = data.get("user_id")

    if task_id is None or user_id is None:
        return jsonify({"success": False, "error": "Missing task_id or user_id"}), 400

    try:
        task_id = int(task_id)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid task_id"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, text FROM tasks WHERE id=?", (task_id,))
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