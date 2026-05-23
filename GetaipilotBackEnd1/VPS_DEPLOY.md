# Telegram Backend - VPS Deployment Guide

## 🚀 Quick Deploy

### 1. Upload Code to VPS

```bash
# SSH into VPS
ssh root@your-vps-ip

# Clone repo
cd /var/www
git clone https://github.com/YourUsername/bot-dashboard.git
cd bot-dashboard/backend
```

### 2. Run Deployment Script

```bash
# Make executable
chmod +x deploy.sh

# Run
./deploy.sh
```

### 3. Configure Environment

```bash
# Edit .env file
nano .env
```

Add your credentials:

```env
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=your_key
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_hash
```

### 4. Start with PM2

```bash
# Install PM2 globally
npm install -g pm2

# Create logs directory
mkdir -p logs

# Start backend
pm2 start ecosystem.config.js

# View logs
pm2 logs telegram-backend

# Save PM2 config
pm2 save
pm2 startup
```

---

## 📋 Manual Commands

### Start Server (Development)

```bash
source venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Start Server (Production)

```bash
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
```

### With PM2

```bash
pm2 start ecosystem.config.js
pm2 status
pm2 restart telegram-backend
pm2 stop telegram-backend
pm2 logs telegram-backend
```

---

## 🔧 Updates

```bash
cd /var/www/bot-dashboard
git pull origin main
cd backend
source venv/bin/activate
pip install -r requirements.txt
pm2 restart telegram-backend
```

---

## 🌐 Nginx Configuration (Optional)

Create `/etc/nginx/sites-available/telegram-api`:

```nginx
server {
    listen 80;
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_cache_bypass $http_upgrade;
    }
}
```

Enable and test:

```bash
ln -s /etc/nginx/sites-available/telegram-api /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

Add SSL:

```bash
certbot --nginx -d api.yourdomain.com
```

---

## ✅ Health Check

```bash
# Local
curl http://localhost:8000/health

# External
curl https://api.yourdomain.com/health
```

Expected response:

```json
{
  "status": "healthy",
  "timestamp": "2024-01-12T12:00:00",
  "active_sessions": 0
}
```

---

## 📊 Monitoring

```bash
# PM2 dashboard
pm2 monit

# View logs
pm2 logs telegram-backend --lines 100

# Check CPU/Memory
pm2 list
```

---

## 🐛 Troubleshooting

**Backend not starting:**

```bash
# Check logs
pm2 logs telegram-backend

# Check Python
python3 --version

# Test dependencies
source venv/bin/activate
python3 -c "import fastapi, telethon, supabase"
```

**Port already in use:**

```bash
# Find process
lsof -i :8000

# Kill process
kill -9 <PID>
```

**Permission errors:**

```bash
# Fix sessions directory
chmod 700 sessions/
chown youruser:youruser sessions/
```
