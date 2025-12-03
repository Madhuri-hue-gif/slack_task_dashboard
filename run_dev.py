import sys
import time
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# REPLACE "main.py" WITH THE NAME OF YOUR MAIN SCRIPT
TARGET_SCRIPT = "app.py"

class RestartHandler(FileSystemEventHandler):
    def __init__(self):
        self.process = None
        self.start_process()

    def start_process(self):
        if self.process:
            print("🛑 Stopping previous process...")
            self.process.kill()      # Kill old process
            self.process.wait()      # Ensure it fully stops

        print(f"🔄 Starting {TARGET_SCRIPT}...")
        self.process = subprocess.Popen([sys.executable, TARGET_SCRIPT])

    def on_modified(self, event):
        # restart only when Python files change
        if event.src_path.endswith(".py"):
            print(f"⚡ Detected change in: {event.src_path}. Restarting...")
            self.start_process()

if __name__ == "__main__":
    handler = RestartHandler()
    observer = Observer()

    # Watch current directory
    observer.schedule(handler, path=".", recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        if handler.process:
            handler.process.kill()
    observer.join()
