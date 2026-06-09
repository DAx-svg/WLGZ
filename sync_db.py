"""
从 PythonAnywhere 同步数据库到本地
==================================
用法：
    python sync_db.py             下载数据库
    python sync_db.py --run       下载数据库并启动本地服务

数据流向：PythonAnywhere → 本地（单向同步）
"""

import os
import sys
import shutil
import urllib.request

# 配置
REMOTE_URL = 'https://daxsvg.pythonanywhere.com/api/db/download?token=wlgz-sync-2026'
LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'material.db')

try:
    print('↓ 正在从 PythonAnywhere 下载数据库...')
    with urllib.request.urlopen(REMOTE_URL, timeout=30) as resp:
        if resp.status != 200:
            print(f'✗ 下载失败: HTTP {resp.status}')
            sys.exit(1)
        data = resp.read()

    if len(data) < 1024:
        print(f'✗ 下载数据异常（仅 {len(data)} 字节），未覆盖本地文件')
        sys.exit(1)

    # 先备份本地文件再覆盖
    if os.path.exists(LOCAL_FILE):
        backup = LOCAL_FILE + '.bak'
        shutil.copy2(LOCAL_FILE, backup)
        print(f'  本地旧数据库已备份到 material.db.bak')

    with open(LOCAL_FILE, 'wb') as f:
        f.write(data)
    print(f'✓ 同步完成！数据库大小: {len(data)/1024:.1f} KB')
    print(f'  本地路径: {LOCAL_FILE}')

except Exception as e:
    print(f'✗ 同步失败: {e}')
    print(f'  提示：请确保网络能访问 https://daxsvg.pythonanywhere.com')
    sys.exit(1)

# 如果带 --run 参数，下载后自动启动本地服务
if '--run' in sys.argv:
    print()
    print('▶ 启动本地服务...')
    import subprocess
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), 'app.py')])
