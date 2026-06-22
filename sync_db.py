"""
数据库双向同步：PythonAnywhere ↔ 本地  (v2.7)
======================================
用法：
    python sync_db.py             自动双向同步（安全优先）
    python sync_db.py --force     强制以云端为准
    python sync_db.py --run       同步后启动本地服务

同步规则（v2.7 重写）：
    1. 网络不通 → 跳过，本地不受影响
    2. 数据相同 → 跳过
    3. 构建合并数据库（本地 ∪ 云端），每条记录按 updated_at 时间戳仲裁
    4. 合并后的数据库上传到云端，再保存到本地 → 两端完全一致
    5. 云端已删的记录 → 本地也删（通过 sync_state 追踪）
    6. --force → 云端直接覆盖本地
"""

import os
import sys
import shutil
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

REMOTE_BASE = 'https://daxsvg.pythonanywhere.com'
SYNC_TOKEN = os.environ.get('WLGZ_SYNC_TOKEN', '')
REMOTE_DB = REMOTE_BASE + '/api/db/download?token=' + urllib.parse.quote(SYNC_TOKEN, safe='')
REMOTE_UPLOAD = REMOTE_BASE + '/api/db/upload'
LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'material.db')
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync_state.json')
FORCE = '--force' in sys.argv


# ---------------------------------------------------------------------------
# 工具：加载/保存同步状态
# ---------------------------------------------------------------------------
def load_sync_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('last_cloud_sns', []))
        except Exception:
            pass
    return set()


def save_sync_state(cloud_sns):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'last_cloud_sns': sorted(cloud_sns),
            'last_sync_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具：上传数据库文件到云端
# ---------------------------------------------------------------------------
def upload_db_to_cloud(db_path):
    """上传完整数据库文件到云端"""
    import io
    boundary = '----WlgzSyncBoundary'
    body = io.BytesIO()
    # token 字段
    body.write(f'--{boundary}\r\n'.encode())
    body.write(b'Content-Disposition: form-data; name="token"\r\n\r\n')
    body.write(SYNC_TOKEN.encode())
    body.write(b'\r\n')
    # db 文件字段
    body.write(f'--{boundary}\r\n'.encode())
    body.write(b'Content-Disposition: form-data; name="db"; filename="material.db"\r\n')
    body.write(b'Content-Type: application/octet-stream\r\n\r\n')
    with open(db_path, 'rb') as f:
        body.write(f.read())
    body.write(f'\r\n--{boundary}--\r\n'.encode())
    body_bytes = body.getvalue()

    req = urllib.request.Request(
        REMOTE_UPLOAD,
        data=body_bytes,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# 核心：合并两个数据库
# ---------------------------------------------------------------------------
def _has_column(db, table, column):
    """检查表中是否有某列"""
    cols = [c[1] for c in db.execute(f"PRAGMA table_info({table})")]
    return column in cols


def _get_updated_at(row, table, db):
    """安全获取 updated_at，列不存在时返回空字符串"""
    if _has_column(db, table, 'updated_at'):
        return (row['updated_at'] or '').strip()
    return ''


def merge_databases(local_path, remote_path, merged_path, last_cloud_sns, first_sync):
    """
    将 local 和 remote 合并，结果写入 merged_path。
    冲突时以 updated_at 时间戳仲裁。
    """
    local = sqlite3.connect(local_path)
    remote = sqlite3.connect(remote_path)
    local.row_factory = sqlite3.Row
    remote.row_factory = sqlite3.Row

    # 以本地为基准（保留 schema + 本地数据）
    shutil.copy2(local_path, merged_path)
    merged = sqlite3.connect(merged_path)
    merged.row_factory = sqlite3.Row
    merged.execute("PRAGMA journal_mode=DELETE")  # 合并过程用 DELETE 模式

    # -------------------------------------------------------------------
    # 1. materials 表（主键：sn）
    # -------------------------------------------------------------------
    local_mats = {r['sn']: r for r in local.execute("SELECT * FROM materials")}
    remote_mats = {r['sn']: r for r in remote.execute("SELECT * FROM materials")}
    local_sns = set(local_mats.keys())
    remote_sns = set(remote_mats.keys())

    stats = {'mat_new_cloud': 0, 'mat_local_win': 0, 'mat_cloud_win': 0,
             'ob_new_cloud': 0, 'ob_local_win': 0, 'ob_cloud_win': 0,
             'deleted': 0}

    # 1a. 云端有、本地没有 → 插入本地
    for sn in sorted(remote_sns - local_sns):
        r = remote_mats[sn]
        cols = list(r.keys())
        placeholders = ','.join(['?'] * len(cols))
        merged.execute(
            f"INSERT OR IGNORE INTO materials ({','.join(cols)}) VALUES ({placeholders})",
            [r[c] for c in cols]
        )
        stats['mat_new_cloud'] += 1

    # 1b. 两端都有、status 不同 → 时间戳仲裁
    for sn in local_sns & remote_sns:
        lr, rr = local_mats[sn], remote_mats[sn]
        ls, rs = lr['status'], rr['status']
        lt = _get_updated_at(lr, 'materials', local)
        rt = _get_updated_at(rr, 'materials', remote)
        if ls != rs:
            local_wins = False
            if lt and rt and lt > rt:
                local_wins = True
            elif lt and not rt:
                local_wins = True
            # else: 云端赢 或 都没时间戳 → 云端为准

            if local_wins:
                merged.execute(
                    "UPDATE materials SET status=?, updated_at=? WHERE sn=?",
                    (ls, lt, sn)
                )
                stats['mat_local_win'] += 1
            else:
                merged.execute(
                    "UPDATE materials SET status=?, updated_at=? WHERE sn=?",
                    (rs, rt, sn)
                )
                stats['mat_cloud_win'] += 1
        # 如果 status 相同但云端 updated_at 更新 → 同步其他字段
        elif lt and rt and rt > lt:
            # 云端整体更新，复制所有字段
            for col in ['hw_version', 'sw_version', 'hw_description', 'sw_description', 'remarks', 'category_id']:
                merged.execute(f"UPDATE materials SET {col}=? WHERE sn=?", (rr[col], sn))
            if rt:
                merged.execute("UPDATE materials SET updated_at=? WHERE sn=?", (rt, sn))

    # 1c. 云端已删（上次有、这次没有的 SN）
    deleted_sns = sorted(local_sns - remote_sns)
    actually_deleted = 0
    for sn in deleted_sns:
        if sn in last_cloud_sns and not first_sync:
            # 确认是云端删除
            merged.execute("DELETE FROM materials WHERE sn=?", (sn,))
            # 级联删除关联记录
            for tbl in ('outbound_records', 'after_sales_records', 'fault_records',
                        'version_changes', 'operation_logs', 'inventory_checks'):
                merged.execute(f"DELETE FROM {tbl} WHERE sn=?", (sn,))
            actually_deleted += 1
    if actually_deleted:
        # 安全阀
        local_count = len(local_sns)
        if actually_deleted > local_count * 0.3:
            print(f'  🛑 安全阀触发！云端少了 {actually_deleted} 条（>{int(local_count*0.3)}），疑似云端重置，拒绝删除')
            local.close(); remote.close(); merged.close()
            os.remove(merged_path)
            return None
    stats['deleted'] = actually_deleted

    # -------------------------------------------------------------------
    # 2. outbound_records 表（主键：id）
    # -------------------------------------------------------------------
    local_obs = {r['id']: r for r in local.execute("SELECT * FROM outbound_records")}
    remote_obs = {r['id']: r for r in remote.execute("SELECT * FROM outbound_records")}
    local_ob_ids = set(local_obs.keys())
    remote_ob_ids = set(remote_obs.keys())

    ob_cols = [c[1] for c in local.execute("PRAGMA table_info(outbound_records)")]

    # 2a. 云端有、本地没有 → 插入
    for oid in sorted(remote_ob_ids - local_ob_ids):
        r = remote_obs[oid]
        placeholders = ','.join(['?'] * len(ob_cols))
        merged.execute(
            f"INSERT OR IGNORE INTO outbound_records ({','.join(ob_cols)}) VALUES ({placeholders})",
            [r[c] for c in ob_cols]
        )
        stats['ob_new_cloud'] += 1

    # 2b. 两端都有 → 逐字段比较，时间戳仲裁
    for oid in local_ob_ids & remote_ob_ids:
        lr, rr = local_obs[oid], remote_obs[oid]
        lt = _get_updated_at(lr, 'outbound_records', local)
        rt = _get_updated_at(rr, 'outbound_records', remote)
        # 判断哪个更新
        local_newer = (lt and rt and lt > rt) or (lt and not rt)
        cloud_newer = (rt and lt and rt > lt) or (rt and not lt)

        if local_newer:
            winner = lr
            stats['ob_local_win'] += 1
        else:
            winner = rr
            if cloud_newer or (oid in remote_ob_ids):  # 云端更新或都没时间戳→云端为准
                stats['ob_cloud_win'] += 1 if cloud_newer else 0

        # 更新 merged 中的记录为胜出版本
        for col in ob_cols:
            if col == 'id':
                continue
            merged.execute(
                f"UPDATE outbound_records SET {col}=? WHERE id=?",
                (winner[col], oid)
            )

    # 2c. 本地有、云端没有 → 保留（这些可能是本地新增的，上传时会一起推送）
    local_only_obs = local_ob_ids - remote_ob_ids
    if local_only_obs:
        print(f'  本地独有出库记录 {len(local_only_obs)} 条，保留并上传')

    # -------------------------------------------------------------------
    # 3. 其他表：直接合并（云端独有的插入，本地独有的保留）
    # -------------------------------------------------------------------
    other_tables = ['after_sales_records', 'fault_records', 'version_changes',
                    'operation_logs', 'inventory_checks', 'categories']
    for table in other_tables:
        try:
            local_rows = {r['id']: r for r in local.execute(f"SELECT * FROM {table}")}
            remote_rows = {r['id']: r for r in remote.execute(f"SELECT * FROM {table}")}
            cols = [c[1] for c in local.execute(f"PRAGMA table_info({table})")]
            for rid in sorted(set(remote_rows.keys()) - set(local_rows.keys())):
                r = remote_rows[rid]
                placeholders = ','.join(['?'] * len(cols))
                try:
                    merged.execute(
                        f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                        [r[c] for c in cols]
                    )
                except sqlite3.IntegrityError:
                    pass
        except sqlite3.OperationalError:
            pass  # 表不存在就跳过

    # -------------------------------------------------------------------
    # 4. sqlite_sequence：取两端最大值
    # -------------------------------------------------------------------
    for table in ['materials', 'outbound_records', 'after_sales_records', 'fault_records',
                   'version_changes', 'operation_logs', 'inventory_checks']:
        try:
            local_seq = local.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            remote_seq = remote.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            max_seq = max(
                local_seq['seq'] if local_seq else 0,
                remote_seq['seq'] if remote_seq else 0
            )
            merged.execute("INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, max_seq))
        except sqlite3.OperationalError:
            pass

    merged.commit()
    local.close()
    remote.close()
    merged.close()
    return stats


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

    # 4. --force 模式：云端直接覆盖
    if FORCE:
        shutil.copy2(LOCAL_FILE, LOCAL_FILE + '.bak')
        with open(LOCAL_FILE, 'wb') as f:
            f.write(remote_data)
        print(f'  --force: 云端覆盖本地 ({len(remote_data)/1024:.1f} KB)')
        final_db = sqlite3.connect(LOCAL_FILE)
        final_db.row_factory = sqlite3.Row
        final_sns = {r['sn'] for r in final_db.execute("SELECT sn FROM materials")}
        final_db.close()
        save_sync_state(final_sns)
        sys.exit(0)

    # 5. 保存云端数据到临时文件
    tmp = LOCAL_FILE + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(remote_data)

    # 统计两边的记录数
    local_db = sqlite3.connect(LOCAL_FILE)
    remote_db = sqlite3.connect(tmp)
    local_db.row_factory = sqlite3.Row
    remote_db.row_factory = sqlite3.Row
    local_count = local_db.execute("SELECT COUNT(*) AS cnt FROM materials").fetchone()['cnt']
    remote_count = remote_db.execute("SELECT COUNT(*) AS cnt FROM materials").fetchone()['cnt']
    local_ob = local_db.execute("SELECT COUNT(*) AS cnt FROM outbound_records").fetchone()['cnt']
    remote_ob = remote_db.execute("SELECT COUNT(*) AS cnt FROM outbound_records").fetchone()['cnt']
    local_db.close()
    remote_db.close()

    print(f'\n  本地 {local_count} 条物料 / {local_ob} 条出库 · 云端 {remote_count} 条物料 / {remote_ob} 条出库')

    # 6. 加载同步状态
    last_cloud_sns = load_sync_state()
    first_sync = (len(last_cloud_sns) == 0)

    # 7. 合并数据库
    merged_path = LOCAL_FILE + '.merged'
    stats = merge_databases(LOCAL_FILE, tmp, merged_path, last_cloud_sns, first_sync)
    if stats is None:
        os.remove(tmp)
        sys.exit(1)

    # 8. 显示变化摘要
    changes = []
    if stats['mat_new_cloud']:
        changes.append(f'云端新增 {stats["mat_new_cloud"]} 条物料')
    if stats['mat_local_win']:
        changes.append(f'本地赢 {stats["mat_local_win"]} 条物料状态冲突')
    if stats['mat_cloud_win']:
        changes.append(f'云端赢 {stats["mat_cloud_win"]} 条物料状态冲突')
    if stats['ob_new_cloud']:
        changes.append(f'云端新增 {stats["ob_new_cloud"]} 条出库记录')
    if stats['ob_local_win']:
        changes.append(f'本地赢 {stats["ob_local_win"]} 条出库记录冲突')
    if stats['ob_cloud_win']:
        changes.append(f'云端赢 {stats["ob_cloud_win"]} 条出库记录冲突')
    if stats['deleted']:
        changes.append(f'云端已删除 {stats["deleted"]} 条，同步删除本地')
    if changes:
        print(f'  合并结果: {", ".join(changes)}')
    else:
        print(f'  无差异，无需合并')

    # 9. 上传合并后的数据库到云端
    print(f'  上传合并数据库到云端...', end=' ')
    try:
        result = upload_db_to_cloud(merged_path)
        if result.get('success'):
            print('OK')
        else:
            print(f'失败: {result.get("error", result.get("message", "未知"))}')
    except Exception as e:
        print(f'失败: {e}')
        # 上传失败不阻塞，本地仍然保存合并结果

    # 10. 备份本地并替换为合并后的数据库
    shutil.copy2(LOCAL_FILE, LOCAL_FILE + '.bak')
    shutil.copy2(merged_path, LOCAL_FILE)
    os.remove(merged_path)
    os.remove(tmp)

    # 11. 保存同步状态
    final_db = sqlite3.connect(LOCAL_FILE)
    final_db.row_factory = sqlite3.Row
    final_sns = {r['sn'] for r in final_db.execute("SELECT sn FROM materials")}
    final_count = final_db.execute("SELECT COUNT(*) AS cnt FROM materials").fetchone()['cnt']
    final_ob = final_db.execute("SELECT COUNT(*) AS cnt FROM outbound_records").fetchone()['cnt']
    final_db.close()
    save_sync_state(final_sns)

    print(f'  同步完成 → 本地 {final_count} 条物料 / {final_ob} 条出库')

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
