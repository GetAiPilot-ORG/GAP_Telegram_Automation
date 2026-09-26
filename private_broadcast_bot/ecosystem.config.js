module.exports = {
  apps: [
    {
      name: "private-broadcast-bot",
      script: "main.py",
      interpreter: "python3", // or path to venv, e.g. "venv/bin/python"
      cwd: __dirname,
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "400M",
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
