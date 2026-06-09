"""
从 PythonAnywhere 同步数据库到本地（带本地数据保护）
==================================================
用法：
    python sync_db.py             下载数据库（安全模式：本地数据多时跳过）
    python sync_db.py --force     强制覆盖本地（云端数据为准）
    python sync_db.py --run       下载数据库并启动本地服务

安全规则：
    - 云端不可达 → 跳过，本地不受影响
    - 云端数据少于本地 → 跳过（说明本地有云端宕机期间新增的数据）
    - 使用 --force 可强制以云端为准
"""

import os
import sys
import shutil
import sqlite3
import urllib.request
from datetime import datetime

# Windows 中文编码兼容
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

REMOTE_URL = 'https://daxsvg.pythonanywhere.com/api/db/download?token=wlgz-sync-2026'
LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'material.db')
FORCE = '--force' in sys.argv

try:
    # 1. 下载云端数据
    print(f'[{datetime.now().strftime("%H:%M:%S")}] 检查云端...', end=' ')
    with urllib.request.urlopen(REMOTE_URL, timeout=30) as resp:
        if resp.status != 200:
            print(f'云端返回 {resp.status}，跳过同步')
            sys.exit(1)
        remote_data = resp.read()

    if len(remote_data) < 1024:
        print('云端数据异常（太小），跳过同步')
        sys.exit(1)

    # 2. 如果本地没数据，直接写入
    if not os.path.exists(LOCAL_FILE):
        with open(LOCAL_FILE, 'wb') as f:
            f.write(remote_data)
        print(f'初始化完成 {len(remote_data)/1024:.1f} KB')
        sys.exit(0)

    # 3. 对比内容，相同则跳过
    with open(LOCAL_FILE, 'rb') as f:
        local_data = f.read()
    if local_data == remote_data:
        print(f'已是最新，跳过')
        sys.exit(0)

    # 4. 对比记录数：本地多于云端 → 说明本地有云端丢失的数据
    with open(LOCAL_FILE + '.tmp', 'wb') as f:
        f.write(remote_data)
    local_db = sqlite3.connect(LOCAL_FILE)
    remote_db = sqlite3.connect(LOCAL_FILE + '.tmp')
    local_count = local_db.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
    remote_count = remote_db.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
    local_db.close()
    remote_db.close()
    os.remove(LOCAL_FILE + '.tmp')

    if local_count > remote_count and not FORCE:
        print(f'\n  ⚠ 警告：本地 {local_count} 条 > 云端 {remote_count} 条！')
        print(f'  可能云端宕机期间本地新增了数据，已跳过同步')
        print(f'  如需以云端为准：python sync_db.py --force')
        print(f'  本地新增的 SN 请在云端恢复后手动补录')
        sys.exit(1)

    # 5. 安全：备份后覆盖
    shutil.copy2(LOCAL_FILE, LOCAL_FILE + '.bak')
    with open(LOCAL_FILE, 'wb') as f:
        f.write(remote_data)
    print(f'同步完成 ({remote_count} 条, {len(remote_data)/1024:.1f} KB)')

except urllib.error.HTTPError as e:
    print(f'云端不可达 (HTTP {e.code})，本地数据未受影响')
    sys.exit(1)
except urllib.error.URLError as e:
    print(f'网络不通，本地数据未受影响')
    sys.exit(1)
except Exception as e:
    print(f'错误: {e}，本地数据未受影响')
    sys.exit(1)

# 如果带 --run 参数，下载后自动启动本地服务
if '--run' in sys.argv:
    print()
    print('▶ 启动本地服务...')
    import subprocess
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), 'app.py')])
