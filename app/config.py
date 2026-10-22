import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_DATABASE_URL = "postgresql://inventory:inventory@localhost:5432/inventory"
LOCK_TIMEOUT = os.environ.get("LOCK_TIMEOUT", "5s")      # max wait for a row lock before answering 503
POOL_MIN = int(os.environ.get("DB_POOL_MIN", "2"))
POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))

# Shown on invoices and in emails. Use | for line breaks in COMPANY_ADDRESS.
COMPANY = {
    "name": os.environ.get("COMPANY_NAME", "My Company"),
    "address_lines": [p.strip() for p in os.environ.get("COMPANY_ADDRESS", "").split("|") if p.strip()],
    "currency": os.environ.get("CURRENCY", "PKR"),
}
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")           # receives low-stock alerts (optional)
