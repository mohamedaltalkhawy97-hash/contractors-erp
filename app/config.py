import os, secrets


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./erp.db")
    # Neon/Render give postgres:// or postgresql:// ; SQLAlchemy needs the psycopg driver name
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _db_url()
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)  # set SECRET_KEY in production!
SESSION_HTTPS_ONLY = os.getenv("SESSION_HTTPS_ONLY", "0") == "1"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")  # if empty a random one is generated and printed once
COMPANY_NAME = os.getenv("COMPANY_NAME", "شركتي للمقاولات")
LOAD_DEMO = os.getenv("LOAD_DEMO", "0") == "1"
