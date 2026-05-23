# Telegram Backend Production Deploy (FastAPI)

This backend must be reachable over **HTTPS** (e.g. `https://api.getaipilot.in`) for the live frontend (`https://getaipilot.in`) to call it.

## 1) Server requirements

- Ubuntu VPS (recommended)
- Python 3.10+
- Node.js (only if you use PM2)
- Nginx

## 2) Environment variables

Create `backend/.env` on the server:

- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_ANON_KEY`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `CORS_ORIGINS=https://getaipilot.in,https://www.getaipilot.in`
- `HOST=0.0.0.0`
- `PORT=8000`

## 3) Install + run

```bash
cd /var/www/bot-dashboard/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run directly (test)
PORT=8000 python3 main.py
```

## 4) Run as a service (recommended)

Use one of:

- systemd + uvicorn
- PM2 (see `backend/VPS_DEPLOY.md`)

## 5) Nginx reverse proxy + SSL

Point `api.getaipilot.in` to your VPS IP, then configure Nginx to proxy to `127.0.0.1:8000` and enable SSL.

After that, verify:

```bash
curl -s https://api.getaipilot.in/health
```

If this returns HTML (not JSON), your request is hitting the wrong server or SPA.
