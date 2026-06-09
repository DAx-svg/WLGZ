"""
数据库自动备份脚本 — 用于 PythonAnywhere Scheduled Tasks
========================================================
在 Tasks 页面配置每天运行:
    python /home/DAxsvg/WLGZ/backup.py

备份保存在 /home/DAxsvg/WLGZ/backups/ 目录，保留最近30天。
"""

import os
import shutil
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'material.db')
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backups')

os.makedirs(BACKUP_DIR, exist_ok=True)

try:
    # 1. 创建备份
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f'material_backup_{timestamp}.db'
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    shutil.copy2(DB_PATH, backup_path)
    print(f"[OK] 备份成功: {backup_name} ({os.path.getsize(backup_path)/1024:.1f} KB)")

    # 2. 清理旧备份（保留30天）
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')])
    while len(backups) > 30:
        old = backups.pop(0)
        os.remove(os.path.join(BACKUP_DIR, old))
        print(f"[清理] 删除旧备份: {old}")

    print(f"[完成] 当前备份数量: {len(backups)}")
except Exception as e:
    print(f"[ERROR] 备份失败: {e}")
