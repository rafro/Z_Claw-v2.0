# OpenClaw + Mission Control — Startup & Shutdown Guide

---

## What Should Be Running

Two persistent processes managed by PM2:

| Name | What it is | Port |
|---|---|---|
| `openclaw-gateway` | J_Claw AI brain — Telegram, cron jobs, skill runner | 18789 |
| `openclaw` | Mission Control server — dashboard, API, queue | 3000 |

Both start **automatically on Windows login** via PM2 (registry startup entry). You should not need to do anything after a normal reboot.

---

## After a Reboot — Verify Everything Is Running

Open PowerShell and run:

```powershell
pm2 list
```

Expected output — both processes should show `online`:

```
│ 0  │ openclaw          │ ... │ online │
│ 2  │ openclaw-gateway  │ ... │ online │
```

Then open the dashboard:
```
http://localhost:3000/dashboard
```

Send a message to **@J_Claw_282_bot** on Telegram to confirm J_Claw responds.

---

## Starting Everything From Scratch

If PM2 shows nothing, or you've restarted PM2:

```powershell
# Start Mission Control
pm2 start C:\Users\Matty\OpenClaw-Orchestrator\server.js --name openclaw

# Start the gateway
pm2 start C:\Users\Matty\.openclaw\start-gateway.js --name openclaw-gateway

# Save the process list (so they survive reboots)
pm2 save
```

---

## Restarting Individual Processes

```powershell
# Restart the gateway (e.g. after a token issue or update)
pm2 restart openclaw-gateway

# Restart Mission Control server
pm2 restart openclaw

# Restart both
pm2 restart all
```

---

## Checking Logs

```powershell
# Gateway logs (Telegram, cron, billing errors)
pm2 logs openclaw-gateway --lines 30 --nostream

# Mission Control logs (job-intake, queue, XP)
pm2 logs openclaw --lines 30 --nostream

# Live-follow gateway logs
pm2 logs openclaw-gateway
```

---

## Stopping Everything (Clean Shutdown)

```powershell
# Stop both processes (keeps them in PM2 list)
pm2 stop all

# Or stop individually
pm2 stop openclaw
pm2 stop openclaw-gateway
```

To fully remove from PM2:
```powershell
pm2 delete openclaw
pm2 delete openclaw-gateway
```

---

## If J_Claw Stops Responding on Telegram

1. Check gateway logs for errors:
   ```powershell
   pm2 logs openclaw-gateway --lines 20 --nostream
   ```

2. If you see `billing` or `rate limit` errors → your Claude subscription session cap was hit. Wait for the cap to reset (usually within a few hours), then:
   ```powershell
   pm2 restart openclaw-gateway
   ```

3. If you see `token expired` → regenerate the subscription token:
   ```powershell
   claude setup-token
   openclaw models auth setup-token --provider anthropic
   # paste the token when prompted
   pm2 restart openclaw-gateway
   ```

4. If the gateway is crashed (status `errored`):
   ```powershell
   pm2 restart openclaw-gateway
   pm2 logs openclaw-gateway --lines 10 --nostream
   ```

---

## If the Dashboard Won't Load (localhost:3000)

```powershell
pm2 restart openclaw
# Then open http://localhost:3000/dashboard
```

---

## Flashing Terminal / Unexpected Windows

Normal causes (not a problem):
- **OpenClaw cron jobs** fire at 6AM, every 3h, 3PM, 6PM, 9PM — briefly active, invisible
- **PM2 startup** runs `pm2 resurrect` once on login via invisible VBS script

If you see a terminal flash on a short interval (~30–60s):
- Check if OpenClaw has a billing/rate-limit retry loop running:
  ```powershell
  pm2 logs openclaw-gateway --lines 30 --nostream | grep -i "retry\|billing\|timeout"
  ```
- If yes: restart the gateway + wait for session reset
- If no: check if server.js is crashing:
  ```powershell
  pm2 logs openclaw --lines 20 --nostream
  ```

---

## Quick Reference

| Task | Command |
|---|---|
| Check status | `pm2 list` |
| Restart gateway | `pm2 restart openclaw-gateway` |
| Restart dashboard | `pm2 restart openclaw` |
| View gateway logs | `pm2 logs openclaw-gateway --lines 30 --nostream` |
| View dashboard logs | `pm2 logs openclaw --lines 30 --nostream` |
| Save process list | `pm2 save` |
| Open dashboard | http://localhost:3000/dashboard |
