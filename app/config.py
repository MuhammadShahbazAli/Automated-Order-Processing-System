import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_DATABASE_URL = "postgresql://inventory:inventory@localhost:5432/inventory"
LOCK_TIMEOUT = os.environ.get("LOCK_TIMEOUT", "5s")      # max wait for a row lock before answering 503
POOL_MIN = int(os.environ.get("DB_POOL_MIN", "2"))
POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
