#!/bin/bash

# VPS Deployment Script for Telegram Backend
# Run this on your VPS after cloning the repo

set -e  # Exit on error

echo "🚀 Starting Telegram Backend Deployment..."

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "⚠️  Please don't run as root. Use: sudo -u youruser ./deploy.sh"
    exit 1
fi

# Navigate to backend directory
cd "$(dirname "$0")"

echo "📦 Step 1: Installing system dependencies..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv

echo "📦 Step 2: Creating virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "✅ Virtual environment created"
else
    echo "✅ Virtual environment already exists"
fi

echo "📦 Step 3: Activating virtual environment..."
source venv/bin/activate

echo "📦 Step 4: Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "📝 Step 5: Checking environment file..."
if [ ! -f ".env" ]; then
    echo "⚠️  .env file not found!"
    echo "📋 Creating .env from template..."
    cp .env.example .env
    echo ""
    echo "⚠️  IMPORTANT: Edit .env file with your credentials:"
    echo "   nano .env"
    echo ""
    read -p "Press Enter after editing .env file..."
fi

echo "📁 Step 6: Creating sessions directory..."
mkdir -p sessions
chmod 700 sessions

echo "🧪 Step 7: Testing the application..."
python3 -c "import fastapi; import telethon; import supabase; print('✅ All dependencies OK')"

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Next steps:"
echo "1. Start the server:"
echo "   source venv/bin/activate"
echo "   uvicorn main:app --host 0.0.0.0 --port 8000"
echo ""
echo "2. Or use PM2 (recommended for production):"
echo "   pm2 start ecosystem.config.js"
echo ""
echo "3. Test the API:"
echo "   curl http://localhost:8000/"
echo ""
