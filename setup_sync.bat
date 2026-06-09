@echo off
:: 物料系统 — 自动同步定时任务安装
:: 每天 9:00-20:00，每小时自动从云端拉一次最新数据到本地
:: 双击此文件即可安装

schtasks /Create /SC DAILY /TN "WlgzSyncDB" /TR "python C:\Users\1\material-tracking\sync_db.py" /ST 09:00 /RI 60 /DU 11:00 /F

if %errorlevel%==0 (
    echo.
    echo ============================================
    echo   已设置！每天 9:00-20:00 每小时同步一次
    echo   云端有新数据，本地自动就有了
    echo ============================================
) else (
    echo 安装失败，请右键此文件 → 以管理员身份运行
)
pause
