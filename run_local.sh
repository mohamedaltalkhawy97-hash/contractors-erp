#!/bin/sh
# تشغيل محلي سريع: sh run_local.sh
python3 -m venv .venv 2>/dev/null; . .venv/bin/activate
pip install -q -r requirements.txt
LOAD_DEMO=${LOAD_DEMO:-1} ADMIN_PASSWORD=${ADMIN_PASSWORD:-admin12345} uvicorn app.main:app --reload --port 8000
