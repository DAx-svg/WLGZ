"""
数据库双向同步：PythonAnywhere ↔ 本地
======================================
用法：
    python sync_db.py             自动双向同步（安全优先）
    python sync_db.py --force     强制以云端为准
    python sync_db.py --run       同步后启动本地服务

同步规则：
    1. 网络不通 → 跳过，本地不受影响
    2. 数据相同 → 跳过
    3. 本地有新数据，云端没有 → 自动推送到云端
    4. 云端有而本地没有 → 拉取到本地
    5. 本地有但云端已删（上次同步后消失的）→ 本地也删除
    6. 用 --force → 云端覆盖本地（包括删除）
"""

import os
import sys
import shutil
import json
import sqlite3
import urllib.request
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

REMOTE_BASE = 'https://daxsvg.pythonanywhere.com'
REMOTE_DB = REMOTE_BASE + '/api/db/download?token=wlgz-sync-2026'
LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'material.db')
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync_state.json')
FORCE = '--force' in sys.argv


# ---------------------------------------------------------------------------
# 工具：通过云端 API 添加物料
# ---------------------------------------------------------------------------
def push_material_to_cloud(sn, row):
    """通过云端 API 推送一条物料"""
    payload = json.dumps({
        'sn': row['sn'],
        'hw_version': row['hw_version'] or '',
        'sw_version': row['sw_version'] or '',
        'hw_description': row['hw_description'] or '',
        'sw_description': row['sw_description'] or '',
        'remarks': row['remarks'] or '',
        'category_id': row['category_id']
    }).encode('utf-8')
    req = urllib.request.Request(
        REMOTE_BASE + '/api/material/add',
        data=payload,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def delete_material_from_local(sn):
    """从本地数据库删除一条物料"""
    db = sqlite3.connect(LOCAL_FILE)
    db.execute("DELETE FROM materials WHERE sn=?", (sn,))
    db.commit()
    db.close()


def merge_status_to_cloud(sn, new_status):
    """通过云端编辑API同步状态变更"""
    payload = json.dumps({'status': new_status}).encode('utf-8')
    req = urllib.request.Request(
        REMOTE_BASE + '/api/material/edit/' + urllib.request.quote(sn, safe=''),
        data=payload, headers={'Content-Type': 'application/json'}, method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# 状态文件：记录上次同步时的云端 SN 集合，用于判断删除
# ---------------------------------------------------------------------------
def load_sync_state():
    """加载上次同步状态，返回 set of SNs 或空集合"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('last_cloud_sns', []))
        except Exception:
            pass
    return set()


def save_sync_state(cloud_sns):
    """保存本次同步后的云端 SN 集合"""
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'last_cloud_sns': sorted(cloud_sns),
            'last_sync_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
try:
    # 1. 下载云端数据库
    print(f'[{datetime.now().strftime("%H:%M:%S")}] 连接云端...', end=' ')
    with urllib.request.urlopen(REMOTE_DB, timeout=30) as resp:
        if resp.status != 200:
            print(f'云端返回 {resp.status}，跳过')
            sys.exit(1)
        remote_data = resp.read()

    if len(remote_data) < 1024:
        print('云端数据异常，跳过')
        sys.exit(1)

    # 2. 本地没数据 → 直接写入
    if not os.path.exists(LOCAL_FILE):
        with open(LOCAL_FILE, 'wb') as f:
            f.write(remote_data)
        print(f'初始化完成 {len(remote_data)/1024:.1f} KB')
        sys.exit(0)

    # 3. 内容相同 → 跳过
    with open(LOCAL_FILE, 'rb') as f:
        local_data = f.read()
    if local_data == remote_data:
        print('已是最新')
        sys.exit(0)

    # 4. 打开两个数据库做比较
    tmp = LOCAL_FILE + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(remote_data)
    local_db = sqlite3.connect(LOCAL_FILE)
    remote_db = sqlite3.connect(tmp)
    local_db.row_factory = sqlite3.Row
    remote_db.row_factory = sqlite3.Row

    local_sns = {r['sn'] for r in local_db.execute("SELECT sn FROM materials")}
    remote_sns = {r['sn'] for r in remote_db.execute("SELECT sn FROM materials")}
    local_count = len(local_sns)
    remote_count = len(remote_sns)

    # 5. 加载上次同步时的云端 SN（用于判断删除）
    last_cloud_sns = load_sync_state()
    first_sync = (len(last_cloud_sns) == 0)

    # 6. 本地有而云端没有的 SN
    only_local = sorted(local_sns - remote_sns)

    # 7. 云端有而本地没有的 SN（云端新增的，正常拉取即可，后续覆盖会带上）
    only_remote = sorted(remote_sns - local_sns)

    print(f'\n  本地 {local_count} 条 · 云端 {remote_count} 条')

    if only_remote:
        print(f'  云端新增 {len(only_remote)} 条: {", ".join(only_remote[:5])}'
              + ('...' if len(only_remote) > 5 else ''))

    if only_local and not FORCE:
        # 区分：哪些是本地新增的（不在上次云端SN里），哪些是云端删掉的（在上次云端SN里）
        new_local = [sn for sn in only_local if sn not in last_cloud_sns]
        deleted_from_cloud = [sn for sn in only_local if sn in last_cloud_sns]

        if deleted_from_cloud:
            if first_sync:
                print(f'  ⚠ 首次同步：本地多出 {len(deleted_from_cloud)} 条，保留不删')
            else:
                print(f'  云端已删除 {len(deleted_from_cloud)} 条，同步删除本地:')
                for sn in deleted_from_cloud:
                    delete_material_from_local(sn)
                    print(f'    ✗ {sn}')

        if new_local:
            print(f'  发现本地新增 {len(new_local)} 条，推送到云端...')
            pushed = 0
            failed = []
            for sn in new_local:
                row = local_db.execute("SELECT * FROM materials WHERE sn=?", (sn,)).fetchone()
                try:
                    result = push_material_to_cloud(sn, row)
                    if result.get('success'):
                        pushed += 1
                        print(f'    ✓ {sn}')
                    else:
                        failed.append((sn, result.get('message', '未知错误')))
                        print(f'    ✗ {sn} — {result.get("message", "")}')
                except Exception as e:
                    failed.append((sn, str(e)))
                    print(f'    ✗ {sn} — {e}')

            if pushed > 0:
                print(f'  已推送 {pushed} 条，重新拉取...')
                with urllib.request.urlopen(REMOTE_DB, timeout=30) as resp:
                    remote_data = resp.read()

            if failed:
                print(f'  失败 {len(failed)} 条，请手动检查')

        elif not deleted_from_cloud:
            print('  无新增数据')

    elif only_local and FORCE:
        print(f'  --force 模式：云端({remote_count}条)覆盖本地({local_count}条)')
        print(f'  本地独有的 {len(only_local)} 条将被丢弃')

    # 8. 状态变更检测：本地和云端都有但状态不同的SN
    if not FORCE:
        common_sns = local_sns & remote_sns
        status_conflicts = []
        for sn in common_sns:
            ls = local_db.execute("SELECT status FROM materials WHERE sn=?", (sn,)).fetchone()['status']
            rs = remote_db.execute("SELECT status FROM materials WHERE sn=?", (sn,)).fetchone()['status']
            if ls != rs:
                status_conflicts.append((sn, ls, rs))

        if status_conflicts:
            print(f'  发现 {len(status_conflicts)} 条状态不一致，以云端为准')
            for sn, local_st, remote_st in status_conflicts[:5]:
                print(f'    {sn}: 本地"{local_st}" → 云端"{remote_st}"')

    local_db.close()
    remote_db.close()
    os.remove(tmp)

    # 9. 备份并写入
    shutil.copy2(LOCAL_FILE, LOCAL_FILE + '.bak')
    with open(LOCAL_FILE, 'wb') as f:
        f.write(remote_data)

    # 10. 保存同步状态
    # 重新打开新的本地数据库读取最终 SN 集合
    final_db = sqlite3.connect(LOCAL_FILE)
    final_db.row_factory = sqlite3.Row
    final_sns = {r['sn'] for r in final_db.execute("SELECT sn FROM materials")}
    final_db.close()
    save_sync_state(final_sns)

    print(f'  同步完成 → {len(remote_data)/1024:.1f} KB')

except urllib.error.HTTPError as e:
    print(f'云端不可达 (HTTP {e.code})，本地数据未受影响')
    sys.exit(1)
except urllib.error.URLError as e:
    print(f'网络不通，本地数据未受影响')
    sys.exit(1)
except Exception as e:
    print(f'错误: {e}，本地数据未受影响')
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 启动服务
if '--run' in sys.argv:
    print()
    print('▶ 启动本地服务...')
    import subprocess
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), 'app.py')])
