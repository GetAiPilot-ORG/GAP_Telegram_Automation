@echo off
echo Starting Telegram Backend Server...
echo.
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
