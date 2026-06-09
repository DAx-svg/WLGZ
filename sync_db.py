"""
从 PythonAnywhere 同步数据库到本地
==================================
用法：
    python sync_db.py             下载数据库
    python sync_db.py --run       下载数据库并启动本地服务

数据流向：PythonAnywhere → 本地（单向同步）
如果本地数据已是最新则不重复写入。
"""

import os
import sys
import shutil
import urllib.request
from datetime import datetime

# Windows 中文编码兼容
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 配置
REMOTE_URL = 'https://daxsvg.pythonanywhere.com/api/db/download?token=wlgz-sync-2026'
LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'material.db')

try:
    print(f'[{datetime.now().strftime("%H:%M:%S")}] 检查云端数据...', end=' ')
    with urllib.request.urlopen(REMOTE_URL, timeout=30) as resp:
        if resp.status != 200:
            print(f'下载失败: HTTP {resp.status}')
            sys.exit(1)
        remote_data = resp.read()

    if len(remote_data) < 1024:
        print('云端数据异常，跳过')
        sys.exit(1)

    # 比较：相同则跳过
    if os.path.exists(LOCAL_FILE):
        with open(LOCAL_FILE, 'rb') as f:
            local_data = f.read()
        if local_data == remote_data:
            print(f'已是最新，跳过 ({len(remote_data)/1024:.1f} KB)')
            sys.exit(0)

    # 不同则备份并覆盖
    if os.path.exists(LOCAL_FILE):
        shutil.copy2(LOCAL_FILE, LOCAL_FILE + '.bak')
        print('有更新，正在同步...')
    else:
        print('本地无数据，正在下载...')

    with open(LOCAL_FILE, 'wb') as f:
        f.write(remote_data)
    print(f'  同步完成 {len(remote_data)/1024:.1f} KB → {LOCAL_FILE}')

except Exception as e:
    print(f'同步失败: {e}')
    print(f'  提示：请确保网络能访问 https://daxsvg.pythonanywhere.com')
    sys.exit(1)

# 如果带 --run 参数，下载后自动启动本地服务
if '--run' in sys.argv:
    print()
    print('▶ 启动本地服务...')
    import subprocess
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), 'app.py')])
