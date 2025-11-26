import os
import logging
import pytz
from datetime import timedelta, timezone
from flask import Flask
from flask_socketio import SocketIO
from slack_bolt import App
from slack_sdk import WebClient
from google import genai
from dotenv import load_dotenv

load_dotenv()

# --- Configurations ---
logging.basicConfig(level=logging.INFO)

# Timezones
IST = timezone(timedelta(hours=5, minutes=30))

# Paths
WEB_DASH_PATH = os.path.join("web", "dashboard")
WEB_STYLE_PATH = os.path.join("web", "style")
DB_FILE = "tasks.db"

# API Keys & Host
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
PUBLIC_HOST = os.getenv("PUBLIC_HOST")

FLASK_PORT = int(os.getenv("FLASK_PORT"))
if not GEMINI_API_KEY:
    raise ValueError("❌ No API key provided. Please set GEMINI_API_KEY in your .env file.")

# --- Initialize Objects ---
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

flask_app = Flask(__name__)
socketio = SocketIO(flask_app, cors_allowed_origins="*")

slack_app = App(token=SLACK_BOT_TOKEN)
client = WebClient(token=SLACK_BOT_TOKEN)