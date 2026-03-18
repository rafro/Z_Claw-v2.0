@echo off
echo.
echo  ==========================================
echo   OpenClaw // Mission Control
echo  ==========================================
echo.
echo  Starting local server...
echo  Dashboard: http://localhost:3000/dashboard
echo.
echo  TIP: For always-on persistence across reboots, use PM2:
echo    npm install -g pm2
echo    pm2 start server.js --name openclaw
echo    pm2 startup   (run the printed command once)
echo    pm2 save
echo.
start "" "http://localhost:3000/dashboard"
node C:\Users\Matty\OpenClaw-Orchestrator\server.js
