module.exports = {
  apps: [
    {
      name: "telegram-backend",
      script: "venv/bin/uvicorn",
      args: "Telesub:app --host 127.0.0.1 --port 8000",
      cwd: "/var/www/telesub",   // <-- update to your actual deploy path on the VPS
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "500M",
      env: {
        NODE_ENV: "production",
      },
      error_file: "./logs/err.log",
      out_file: "./logs/out.log",
      log_file: "./logs/combined.log",
      time: true,
    },
  ],
};
