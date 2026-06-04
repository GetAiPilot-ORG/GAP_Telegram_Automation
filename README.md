# Deployment Notes

> [!WARNING]
> Do not run this bot manually (directly via `python llm_bot.py`) and through PM2 at the same time. This will create duplicate Telethon client sessions for the same bot token, leading to session file conflicts and update receiving errors.

## Recommended Usage

To manage the bot lifecycle safely under PM2, use only:
```bash
pm2 restart 5
pm2 logs 5
```
