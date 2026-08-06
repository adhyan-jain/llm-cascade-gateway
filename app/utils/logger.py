import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Create logs directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Setup rotating log handler
log_file = LOG_DIR / "gateway.log"
file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)

class JsonFormatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.msg, dict):
            import time
            payload = {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                **record.msg
            }
            return json.dumps(payload)
        
        # Fallback for plain string messages
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "message": record.getMessage()
        }
        return json.dumps(payload)

file_handler.setFormatter(JsonFormatter())

# Base audit logger
audit_logger = logging.getLogger("ai_gateway_audit")
audit_logger.setLevel(logging.INFO)
audit_logger.addHandler(file_handler)

# Add stream handler for console/systemd output
stream_handler = logging.StreamHandler()
class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.msg, dict):
            return f"AUDIT: {json.dumps(record.msg)}"
        return f"[{record.levelname}] {record.getMessage()}"

stream_handler.setFormatter(ConsoleFormatter())
audit_logger.addHandler(stream_handler)

# System logger (for application debug/info logs)
app_logger = logging.getLogger("ai_gateway")
app_logger.setLevel(logging.INFO)
app_handler = RotatingFileHandler(BASE_DIR / "logs" / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3)
app_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s [%(name)s]: %(message)s'))
app_logger.addHandler(app_handler)
app_logger.addHandler(stream_handler)
