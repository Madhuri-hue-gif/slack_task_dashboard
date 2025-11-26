import threading
from slack_bolt.adapter.socket_mode import SocketModeHandler
from config import flask_app, socketio, slack_app, SLACK_APP_TOKEN, PUBLIC_HOST,FLASK_PORT
from database import init_db
from helpers import reminder_loop
import slack_handlers  # Import to register Slack commands
import web_routes      # Import to register Flask routes

def run_flask():
    socketio.run(
        flask_app,
        host="0.0.0.0",
        port=FLASK_PORT
    )
if __name__ == "__main__":
    init_db()
    
    # Start Web Server Thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Start Reminder Background Thread
    threading.Thread(target=reminder_loop, daemon=True).start()
    
    print(f"⚡ Running Slack Bot with Dashboard at {PUBLIC_HOST}")
    
    # Start Slack Socket Mode
    SocketModeHandler(slack_app, SLACK_APP_TOKEN).start()