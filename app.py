"""
物料全流程追溯系统 v2.0
====================
基于 Flask + SQLite 的公司内部物料追溯管理系统。
v2.0 新增：二级品类管理、品类统计汇总。

运行方式：
  1. pip install -r requirements.txt
  2. python app.py
  3. 浏览器访问 http://127.0.0.1:8080
"""

import os
import re
import sys
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, jsonify, g
import sqlite3

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True  # 开发阶段：模板修改即时生效
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
DATABASE = os.path.join(DATA_DIR, 'material.db')
JOURNAL_MODE = os.environ.get('JOURNAL_MODE', 'WAL')  # PythonAnywhere NFS 不支持 WAL，需设为 DELETE


def validate_sn(sn):
    """Validate SN format: 1-100 chars, alphanumeric + hyphens/underscores/dots/colons"""
    if not sn or len(sn) > 100:
        return False
    return bool(re.match(r'^[A-Za-z0-9\-_\.:：]+$', sn))


def escape_like(s):
    """Escape LIKE wildcards to prevent user-input % or _ from causing unintended matches"""
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
        g.db.execute("PRAGMA busy_timeout=5000")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


@app.after_request
def add_no_cache(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def init_db():
    # 启动时清理残留锁文件（DELETE 模式崩溃遗留）
    import glob as _glob
    for _lock in _glob.glob(DATABASE + '-*'):
        try:
            os.remove(_lock)
        except Exception:
            pass
    db = get_db()
    # 设置 journal 模式（本地 WAL，云端 DELETE）
    db.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            parent_id   INTEGER DEFAULT NULL,
            FOREIGN KEY (parent_id) REFERENCES categories(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS materials (
            sn              TEXT PRIMARY KEY,
            hw_version      TEXT DEFAULT '',
            sw_version      TEXT DEFAULT '',
            hw_description  TEXT DEFAULT '',
            sw_description  TEXT DEFAULT '',
            status          TEXT DEFAULT '在库',
            inbound_time    TEXT NOT NULL,
            remarks         TEXT DEFAULT '',
            category_id     INTEGER DEFAULT NULL,
            updated_at      TEXT DEFAULT '',
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
        );

        -- 兼容旧表：添加 updated_at 列（如果不存在）
        -- 使用 try/except 包裹，Python 端执行
        CREATE TABLE IF NOT EXISTS _migration_done (flag INTEGER);
        INSERT OR IGNORE INTO _migration_done (flag) VALUES (1);

        CREATE TABLE IF NOT EXISTS outbound_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sn              TEXT NOT NULL,
            outbound_time   TEXT NOT NULL,
            purpose         TEXT DEFAULT '',
            purpose_detail  TEXT DEFAULT '',
            courier_company TEXT DEFAULT '',
            tracking_number TEXT DEFAULT '',
            customer_name   TEXT DEFAULT '',
            customer_contact TEXT DEFAULT '',
            customer_company TEXT DEFAULT '',
            address         TEXT DEFAULT '',
            remarks         TEXT DEFAULT '',
            FOREIGN KEY (sn) REFERENCES materials(sn)
        );

        CREATE TABLE IF NOT EXISTS after_sales_records (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            sn               TEXT NOT NULL,
            created_time     TEXT NOT NULL,
            return_courier   TEXT DEFAULT '',
            return_tracking  TEXT DEFAULT '',
            send_back_courier TEXT DEFAULT '',
            send_back_tracking TEXT DEFAULT '',
            problem_description TEXT DEFAULT '',
            status           TEXT DEFAULT '处理中',
            remarks          TEXT DEFAULT '',
            FOREIGN KEY (sn) REFERENCES materials(sn)
        );

        CREATE TABLE IF NOT EXISTS version_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sn          TEXT NOT NULL,
            change_time TEXT NOT NULL,
            change_type TEXT NOT NULL,
            old_version TEXT DEFAULT '',
            new_version TEXT DEFAULT '',
            description TEXT DEFAULT '',
            FOREIGN KEY (sn) REFERENCES materials(sn)
        );

        CREATE TABLE IF NOT EXISTS fault_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sn              TEXT NOT NULL,
            created_time    TEXT NOT NULL,
            fault_reason    TEXT DEFAULT '',
            solution        TEXT DEFAULT '',
            status          TEXT DEFAULT '故障中',
            previous_status TEXT DEFAULT '在库',
            resolved_time   TEXT DEFAULT '',
            FOREIGN KEY (sn) REFERENCES materials(sn)
        );

        CREATE INDEX IF NOT EXISTS idx_outbound_sn ON outbound_records(sn);
        CREATE INDEX IF NOT EXISTS idx_aftersales_sn ON after_sales_records(sn);
        CREATE INDEX IF NOT EXISTS idx_version_sn ON version_changes(sn);
        CREATE INDEX IF NOT EXISTS idx_fault_sn ON fault_records(sn);
        CREATE INDEX IF NOT EXISTS idx_materials_category ON materials(category_id);

        CREATE TABLE IF NOT EXISTS operation_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            op_time     TEXT NOT NULL,
            op_type     TEXT NOT NULL,
            sn          TEXT DEFAULT '',
            detail      TEXT DEFAULT '',
            operator_ip TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS inventory_checks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            check_time  TEXT NOT NULL,
            sn          TEXT NOT NULL,
            found_status TEXT DEFAULT '',
            notes       TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_oplogs_time ON operation_logs(op_time);
        CREATE INDEX IF NOT EXISTS idx_oplogs_type ON operation_logs(op_type);
        CREATE INDEX IF NOT EXISTS idx_oplogs_sn ON operation_logs(sn);
        CREATE INDEX IF NOT EXISTS idx_invcheck_time ON inventory_checks(check_time);

    """)
    # 兼容旧数据库：category_id 列如果不存在则添加
    try:
        db.execute("ALTER TABLE materials ADD COLUMN category_id INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    # 兼容旧数据库：customer_company / address 列
    for col in ('customer_company', 'address'):
        try:
            db.execute(f"ALTER TABLE outbound_records ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    # v2.1: 出库用途
    try:
        db.execute("ALTER TABLE outbound_records ADD COLUMN purpose TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE outbound_records ADD COLUMN purpose_detail TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # v2.2: 售后完成时间
    try:
        db.execute("ALTER TABLE after_sales_records ADD COLUMN completed_time TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # v2.3: 出库记录寄回追踪（售后出库时记录客户需寄回的故障件信息）
    for col, default in [('return_status', "'无需寄回'"), ('return_sn', "''"),
                         ('return_courier', "''"), ('return_tracking', "''")]:
        try:
            db.execute(f"ALTER TABLE outbound_records ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    # v2.4: 故障寄修出库
    for col, default in [('repair_type', "'本地维修'"), ('supplier', "''"),
                         ('return_courier', "''"), ('return_tracking', "''")]:
        try:
            db.execute(f"ALTER TABLE fault_records ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    # v2.6: 时间戳冲突仲裁 — updated_at 字段 + 触发器（materials + outbound_records）
    for table in ('materials', 'outbound_records'):
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN updated_at TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    # materials 触发器：任何 UPDATE/INSERT 自动刷新 updated_at
    db.executescript("""
        DROP TRIGGER IF EXISTS tr_materials_upd;
        CREATE TRIGGER tr_materials_upd AFTER UPDATE ON materials
        FOR EACH ROW
        BEGIN
            UPDATE materials SET updated_at = datetime('now', 'localtime')
            WHERE sn = OLD.sn AND updated_at = OLD.updated_at;
        END;

        DROP TRIGGER IF EXISTS tr_materials_ins;
        CREATE TRIGGER tr_materials_ins AFTER INSERT ON materials
        FOR EACH ROW
        WHEN NEW.updated_at = ''
        BEGIN
            UPDATE materials SET updated_at = datetime('now', 'localtime')
            WHERE sn = NEW.sn;
        END;

        DROP TRIGGER IF EXISTS tr_outbound_upd;
        CREATE TRIGGER tr_outbound_upd AFTER UPDATE ON outbound_records
        FOR EACH ROW
        BEGIN
            UPDATE outbound_records SET updated_at = datetime('now', 'localtime')
            WHERE id = OLD.id AND updated_at = OLD.updated_at;
        END;

        DROP TRIGGER IF EXISTS tr_outbound_ins;
        CREATE TRIGGER tr_outbound_ins AFTER INSERT ON outbound_records
        FOR EACH ROW
        WHEN NEW.updated_at = ''
        BEGIN
            UPDATE outbound_records SET updated_at = datetime('now', 'localtime')
            WHERE id = NEW.id;
        END;
    """)
    db.commit()


def insert_sample_data():
    db = get_db()

    # 品类：无论有没有物料，空库就插入示例品类
    if db.execute("SELECT COUNT(*) AS cnt FROM categories").fetchone()['cnt'] == 0:
        db.execute("INSERT INTO categories VALUES (1,'电路板',NULL)")
        db.execute("INSERT INTO categories VALUES (2,'电源',NULL)")
        db.execute("INSERT INTO categories VALUES (3,'按钮',NULL)")
        db.execute("INSERT INTO categories VALUES (4,'控制板',1)")
        db.execute("INSERT INTO categories VALUES (5,'驱动板',1)")
        db.execute("INSERT INTO categories VALUES (6,'接口板',1)")
        db.execute("INSERT INTO categories VALUES (7,'开关电源',2)")
        db.execute("INSERT INTO categories VALUES (8,'线性电源',2)")
        db.execute("INSERT INTO categories VALUES (9,'机械按钮',3)")
        db.execute("INSERT INTO categories VALUES (10,'触摸按钮',3)")
        db.commit()

    # 物料：已有数据则跳过
    if db.execute("SELECT COUNT(*) AS cnt FROM materials").fetchone()['cnt'] > 0:
        print("[示例数据] 品类已就绪，物料已存在，跳过")
        return

    now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')

    # ---- 示例品类 ----
    print("[示例数据] 品类已插入，继续插入物料...")

    # ---- 示例物料（带 category_id）----
    materials = [
        ('SN-2024-001', 'V2.1', 'FW-3.4.1',
         '4G通信模组，支持GPS定位', '支持远程OTA升级', '在库',
         '2024-01-10 09:30:00', '首批试产样品', 4),
        ('SN-2024-002', 'V2.1', 'FW-3.4.1',
         '4G通信模组，支持GPS定位', '支持远程OTA升级', '已出库',
         '2024-01-15 10:30:00', '已发给客户A现场测试', 4),
        ('SN-2024-003', 'V2.2', 'FW-3.4.2',
         '4G通信模组，支持GPS+北斗双模定位', '支持远程OTA升级，新增断点续传', '已出库',
         '2024-02-20 14:00:00', '改进版，定位精度提升', 5),
        ('SN-2024-004', 'V2.2', 'FW-3.4.2',
         '4G通信模组，支持GPS+北斗双模定位', '支持远程OTA升级，新增断点续传', '售后中',
         '2024-02-20 14:05:00', '客户反馈开机异常', 5),
        ('SN-2024-005', 'V3.0', 'FW-4.0.0',
         '5G通信模组，支持GPS+北斗+GLONASS三模定位', '全新架构，支持边缘计算', '在库',
         '2024-03-10 09:00:00', '新一代旗舰产品', 7),
    ]
    for m in materials:
        db.execute(
            "INSERT INTO materials (sn, hw_version, sw_version, hw_description, "
            "sw_description, status, inbound_time, remarks, category_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)", m
        )

    # ---- 示例出库 ----
    outbounds = [
        ('SN-2024-002', '2024-02-01 11:00:00', '顺丰快递', 'SF1234567890',
         '张三（华东区负责人）', '13800000001', '寄往华东区进行现场部署测试'),
        ('SN-2024-003', '2024-03-01 15:30:00', '圆通快递', 'YT9876543210',
         '李四（上海分公司）', '13900000002', '发往上海分公司仓库'),
        ('SN-2024-002', '2024-04-15 09:00:00', '顺丰快递', 'SF1234567899',
         '王五（华南区负责人）', '13700000003', '华东测试完毕后转寄华南区'),
    ]
    for o in outbounds:
        db.execute(
            "INSERT INTO outbound_records (sn, outbound_time, courier_company, "
            "tracking_number, customer_name, customer_contact, "
            "customer_company, address, remarks) "
            "VALUES (?,?,?,?,?,?,'','',?)", o
        )

    # ---- 示例售后 ----
    db.execute(
        "INSERT INTO after_sales_records (sn, created_time, return_courier, "
        "return_tracking, problem_description, status, remarks) "
        "VALUES ('SN-2024-004','2024-05-01 16:00:00','中通快递','ZT1111111111',"
        "'设备上电后指示灯不亮，无法正常开机','处理中','收到后初步排查为电池接口虚焊')"
    )

    # ---- 示例版本变更 ----
    version_changes = [
        ('SN-2024-003', '2024-02-20 14:00:00', '硬件', 'V2.1', 'V2.2',
         'GPS模组升级为GPS+北斗双模，定位精度由5m提升至2.5m'),
        ('SN-2024-003', '2024-02-20 14:05:00', '软件', 'FW-3.4.1', 'FW-3.4.2',
         '新增固件断点续传功能'),
        ('SN-2024-005', '2024-03-10 09:00:00', '硬件', 'V2.2', 'V3.0',
         '核心芯片更换为5G方案'),
        ('SN-2024-005', '2024-03-10 09:00:00', '软件', 'FW-3.4.2', 'FW-4.0.0',
         '固件架构重构'),
    ]
    for v in version_changes:
        db.execute(
            "INSERT INTO version_changes (sn, change_time, change_type, "
            "old_version, new_version, description) VALUES (?,?,?,?,?,?)", v
        )

    db.commit()
    print(f"[示例数据] 已插入 10 个品类、{len(materials)} 个物料、"
          f"{len(outbounds)} 条出库、1 条售后、{len(version_changes)} 条版本变更")


def log_operation(op_type, sn='', detail=''):
    """记录操作日志（审计追溯）"""
    try:
        db = get_db()
        now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        db.execute(
            "INSERT INTO operation_logs (op_time, op_type, sn, detail, operator_ip) "
            "VALUES (?,?,?,?,?)",
            (now, op_type, sn, detail, request.remote_addr or '')
        )
        db.commit()
    except Exception as e:
        import sys
        print(f'[log_operation] 日志记录失败: {e}', file=sys.stderr)


# ==========================================================================
#                            页面路由
# ==========================================================================

@app.route('/')
def index():
    """首页：品类导航 + 物料列表"""
    db = get_db()
    search = request.args.get('search', '').strip()
    sub_cat_id = request.args.get('cat', '').strip()
    parent_id = request.args.get('parent', '').strip()
    show_orphan = request.args.get('orphan', '').strip()

    # If sub-category selected without explicit parent, infer the parent
    if sub_cat_id and not parent_id:
        try:
            parent_row = db.execute(
                "SELECT parent_id FROM categories WHERE id=?", (int(sub_cat_id),)
            ).fetchone()
            if parent_row and parent_row['parent_id']:
                parent_id = str(parent_row['parent_id'])
        except (ValueError, TypeError):
            pass

    filter_status = request.args.get('status', '').strip()
    filter_return = request.args.get('returning', '').strip()
    filter_purpose = request.args.get('purpose', '').strip()
    filter_ret_status = request.args.get('return_status', '').strip()
    show_month_in = request.args.get('month_in', '').strip()
    show_month_out = request.args.get('month_out', '').strip()
    this_month = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')

    # 分页参数
    try:
        page = max(1, int(request.args.get('page', '1').strip()))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = max(1, min(200, int(request.args.get('per_page', '20').strip())))
    except (ValueError, TypeError):
        per_page = 50

    # 精确 SN 搜索 → 直接跳转（仅单关键词时）
    if search:
        keywords = search.split()
        if len(keywords) == 1:
            exact = db.execute("SELECT sn FROM materials WHERE sn = ?", (search,)).fetchone()
            if exact:
                return redirect(url_for('detail', sn=search))
        # 多关键词 AND 搜索（匹配 SN、备注、出库联系人/公司）
        conditions = []
        params = []
        for kw in keywords:
            p = f'%{escape_like(kw)}%'
            conditions.append(
                '(m.sn LIKE ? OR m.remarks LIKE ? OR '
                'm.sn IN (SELECT sn FROM outbound_records WHERE customer_name LIKE ? ESCAPE \'\\\' OR customer_company LIKE ? ESCAPE \'\\\'))')
            params.extend([p, p, p, p])
        sql = ('SELECT DISTINCT m.* FROM materials m WHERE '
               + ' AND '.join(conditions) + ' ORDER BY m.inbound_time DESC')
        materials = db.execute(sql, params).fetchall()
    elif filter_return:
        # 筛选有待寄回出库记录的物料
        materials = db.execute(
            "SELECT DISTINCT m.* FROM materials m "
            "JOIN outbound_records o ON m.sn = o.sn "
            "WHERE o.return_status = '待寄回' "
            "ORDER BY m.inbound_time DESC"
        ).fetchall()
    elif show_month_in:
        materials = db.execute(
            "SELECT * FROM materials WHERE inbound_time LIKE ? ORDER BY inbound_time DESC",
            (f'{this_month}%',)
        ).fetchall()
    elif show_month_out:
        materials = db.execute(
            "SELECT DISTINCT m.* FROM materials m "
            "JOIN outbound_records o ON m.sn = o.sn "
            "WHERE o.outbound_time LIKE ? ORDER BY m.inbound_time DESC",
            (f'{this_month}%',)
        ).fetchall()
    else:
        # 组合筛选：status + category + purpose + return_status 可自由组合
        conditions = []
        params = []
        needs_join = bool(filter_purpose or filter_return)
        t = 'm.' if needs_join else ''
        if filter_status:
            conditions.append(f'{t}status = ?')
            params.append(filter_status)
        if sub_cat_id:
            conditions.append(f'{t}category_id = ?')
            params.append(sub_cat_id)
        elif parent_id:
            conditions.append(f'{t}category_id IN (SELECT id FROM categories WHERE parent_id = ?)')
            params.append(parent_id)
        elif show_orphan:
            conditions.append(f'{t}category_id IS NULL')
        if filter_purpose:
            conditions.append('o.purpose = ?')
            params.append(filter_purpose)
            if filter_ret_status:
                conditions.append('o.return_status = ?')
                params.append(filter_ret_status)
        if filter_return:
            conditions.append("o.return_status = '待寄回'")
        if conditions:
            where_clause = ' WHERE ' + ' AND '.join(conditions)
            if needs_join:
                materials = db.execute(
                    f"SELECT DISTINCT m.* FROM materials m "
                    f"JOIN outbound_records o ON m.sn = o.sn{where_clause} "
                    f"ORDER BY m.inbound_time DESC",
                    params
                ).fetchall()
            else:
                materials = db.execute(
                    f"SELECT * FROM materials{where_clause} ORDER BY inbound_time DESC",
                    params
                ).fetchall()
        else:
            materials = db.execute(
                "SELECT * FROM materials ORDER BY inbound_time DESC"
            ).fetchall()

    # 批量查询每个物料的最新出库用途
    if materials:
        sns = [m['sn'] for m in materials]
        placeholders = ','.join(['?'] * len(sns))
        rows = db.execute(
            f"SELECT sn, purpose, purpose_detail, return_status FROM outbound_records WHERE sn IN ({placeholders}) "
            "GROUP BY sn HAVING MAX(outbound_time)",
            sns
        ).fetchall()
        purpose_map = {r['sn']: r['purpose'] for r in rows}
        detail_map = {r['sn']: r['purpose_detail'] for r in rows}
        return_map = {r['sn']: r['return_status'] for r in rows}
        materials = [dict(m) for m in materials]
        for m in materials:
            # 已重新入库（在库状态）的物料不显示历史出库用途
            if m['status'] == '在库':
                m['latest_purpose'] = ''
                m['latest_purpose_detail'] = ''
                m['latest_return_status'] = ''
            else:
                m['latest_purpose'] = purpose_map.get(m['sn'], '')
                m['latest_purpose_detail'] = detail_map.get(m['sn'], '')
                m['latest_return_status'] = return_map.get(m['sn'], '')

    # 批量查询物料所属品类
    if materials:
        cat_ids = set(m.get('category_id') for m in materials if m.get('category_id'))
        cat_map = {}
        if cat_ids:
            cat_rows = db.execute(
                "SELECT c.id, c.name AS cat_name, p.name AS parent_name "
                "FROM categories c LEFT JOIN categories p ON c.parent_id=p.id "
                "WHERE c.id IN ({})".format(','.join('?' * len(cat_ids))),
                list(cat_ids)
            ).fetchall()
            cat_map = {r['id']: (r['cat_name'], r['parent_name']) for r in cat_rows}
        for m in materials:
            info = cat_map.get(m.get('category_id'))
            m['cat_name'] = info[0] if info else ''
            m['parent_name'] = info[1] if info else ''

    # 品类树：大类和旗下小类（带统计）
    parents = db.execute("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY id").fetchall()
    categories = []
    for p in parents:
        subs = db.execute("""
            SELECT c.*,
                COUNT(m.sn) AS total,
                SUM(CASE WHEN m.status='在库' THEN 1 ELSE 0 END) AS in_stock,
                SUM(CASE WHEN m.status='已出库' THEN 1 ELSE 0 END) AS outbound,
                SUM(CASE WHEN m.status='售后中' THEN 1 ELSE 0 END) AS after_sales,
                SUM(CASE WHEN m.status='故障中' THEN 1 ELSE 0 END) AS fault,
                SUM(CASE WHEN m.status='寄修中' THEN 1 ELSE 0 END) AS repair
            FROM categories c
            LEFT JOIN materials m ON m.category_id = c.id
            WHERE c.parent_id = ?
            GROUP BY c.id
            ORDER BY c.id
        """, (p['id'],)).fetchall()

        # 每个小类取前 20 个 SN（跟随当前状态筛选）
        sns_for_subs = {}
        for s in subs:
            sn_query = "SELECT sn FROM materials WHERE category_id=?"
            sn_params = [s['id']]
            if filter_status:
                sn_query += " AND status=?"
                sn_params.append(filter_status)
            sn_query += " ORDER BY inbound_time DESC LIMIT 20"
            rows = db.execute(sn_query, sn_params).fetchall()
            sns_for_subs[s['id']] = [r['sn'] for r in rows]

        categories.append({
            'id': p['id'],
            'name': p['name'],
            'subs': [{
                'id': s['id'], 'name': s['name'],
                'total': s['total'] or 0,
                'in_stock': s['in_stock'] or 0,
                'outbound': s['outbound'] or 0,
                'after_sales': s['after_sales'] or 0,
                'fault': s['fault'] or 0,
                'repair': s['repair'] or 0,
                'sns': sns_for_subs.get(s['id'], [])
            } for s in subs]
        })

    # 总计数（所有物料，含未分类）
    total_all = db.execute(
        "SELECT COUNT(*) AS cnt FROM materials"
    ).fetchone()['cnt']
    # 未归类物料数
    orphan = db.execute(
        "SELECT COUNT(*) AS cnt FROM materials WHERE category_id IS NULL"
    ).fetchone()['cnt']

    # 首页统计
    stats = {
        'in_stock': db.execute(
            "SELECT COUNT(*) AS cnt FROM materials WHERE status='在库'"
        ).fetchone()['cnt'],
        'outbound': db.execute(
            "SELECT COUNT(*) AS cnt FROM materials WHERE status='已出库'"
        ).fetchone()['cnt'],
        'after_sales': db.execute(
            "SELECT COUNT(*) AS cnt FROM materials WHERE status='售后中'"
        ).fetchone()['cnt'],
        'fault': db.execute(
            "SELECT COUNT(*) AS cnt FROM materials WHERE status='故障中'"
        ).fetchone()['cnt'],
        'repair': db.execute(
            "SELECT COUNT(*) AS cnt FROM materials WHERE status='寄修中'"
        ).fetchone()['cnt'],
        'after_sales_out': db.execute(
            "SELECT COUNT(DISTINCT sn) AS cnt FROM outbound_records WHERE purpose='售后出库'"
        ).fetchone()['cnt'],
        'fault_repair_out': db.execute(
            "SELECT COUNT(DISTINCT sn) AS cnt FROM outbound_records WHERE purpose='故障出库' AND return_status='待寄回'"
        ).fetchone()['cnt'],
    }
    this_month = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
    stats['month_in'] = db.execute(
        "SELECT COUNT(*) AS cnt FROM materials WHERE inbound_time LIKE ?",
        (f'{this_month}%',)
    ).fetchone()['cnt']
    stats['month_out'] = db.execute(
        "SELECT COUNT(DISTINCT sn) AS cnt FROM outbound_records WHERE outbound_time LIKE ?",
        (f'{this_month}%',)
    ).fetchone()['cnt']
    stats['month_as_new'] = db.execute(
        "SELECT COUNT(*) AS cnt FROM after_sales_records WHERE created_time LIKE ?",
        (f'{this_month}%',)
    ).fetchone()['cnt']
    stats['month_as_done'] = db.execute(
        "SELECT COUNT(*) AS cnt FROM after_sales_records WHERE completed_time LIKE ?",
        (f'{this_month}%',)
    ).fetchone()['cnt']
    stats['month_fault_new'] = db.execute(
        "SELECT COUNT(*) AS cnt FROM fault_records WHERE created_time LIKE ?",
        (f'{this_month}%',)
    ).fetchone()['cnt']
    stats['month_fault_done'] = db.execute(
        "SELECT COUNT(*) AS cnt FROM fault_records WHERE resolved_time LIKE ?",
        (f'{this_month}%',)
    ).fetchone()['cnt']
    stats['returning'] = db.execute(
        "SELECT COUNT(DISTINCT sn) AS cnt FROM outbound_records WHERE return_status='待寄回'"
    ).fetchone()['cnt']

    # 售后记录筛选参数
    show_month_as_new = request.args.get('month_as_new', '').strip()
    show_month_as_done = request.args.get('month_as_done', '').strip()
    as_records = []
    if show_month_as_new:
        as_records = db.execute("""
            SELECT a.*, m.category_id, c.name AS cat_name, p.name AS parent_name,
                   (SELECT o.customer_name FROM outbound_records o WHERE o.sn=a.sn ORDER BY o.outbound_time DESC LIMIT 1) AS customer_name,
                   (SELECT o.customer_contact FROM outbound_records o WHERE o.sn=a.sn ORDER BY o.outbound_time DESC LIMIT 1) AS customer_contact
            FROM after_sales_records a
            JOIN materials m ON a.sn = m.sn
            LEFT JOIN categories c ON m.category_id = c.id
            LEFT JOIN categories p ON c.parent_id = p.id
            WHERE a.created_time LIKE ?
            ORDER BY a.created_time DESC
        """, (f'{this_month}%',)).fetchall()
    elif show_month_as_done:
        as_records = db.execute("""
            SELECT a.*, m.category_id, c.name AS cat_name, p.name AS parent_name,
                   (SELECT o.customer_name FROM outbound_records o WHERE o.sn=a.sn ORDER BY o.outbound_time DESC LIMIT 1) AS customer_name,
                   (SELECT o.customer_contact FROM outbound_records o WHERE o.sn=a.sn ORDER BY o.outbound_time DESC LIMIT 1) AS customer_contact
            FROM after_sales_records a
            JOIN materials m ON a.sn = m.sn
            LEFT JOIN categories c ON m.category_id = c.id
            LEFT JOIN categories p ON c.parent_id = p.id
            WHERE a.completed_time LIKE ?
            ORDER BY a.completed_time DESC
        """, (f'{this_month}%',)).fetchall()

    # 故障记录筛选参数
    show_month_fault_new = request.args.get('month_fault_new', '').strip()
    show_month_fault_done = request.args.get('month_fault_done', '').strip()
    fault_records_list = []
    if show_month_fault_new:
        fault_records_list = db.execute("""
            SELECT f.*, m.category_id, c.name AS cat_name, p.name AS parent_name
            FROM fault_records f
            JOIN materials m ON f.sn = m.sn
            LEFT JOIN categories c ON m.category_id = c.id
            LEFT JOIN categories p ON c.parent_id = p.id
            WHERE f.created_time LIKE ?
            ORDER BY f.created_time DESC
        """, (f'{this_month}%',)).fetchall()
    elif show_month_fault_done:
        fault_records_list = db.execute("""
            SELECT f.*, m.category_id, c.name AS cat_name, p.name AS parent_name
            FROM fault_records f
            JOIN materials m ON f.sn = m.sn
            LEFT JOIN categories c ON m.category_id = c.id
            LEFT JOIN categories p ON c.parent_id = p.id
            WHERE f.resolved_time LIKE ?
            ORDER BY f.resolved_time DESC
        """, (f'{this_month}%',)).fetchall()

    # ===== 分页：对 materials 切片 =====
    total_materials = len(materials)
    total_pages = max(1, (total_materials + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    materials_page = materials[offset:offset + per_page]

    return render_template('index.html',
                           categories=categories,
                           materials=materials_page,
                           search=search,
                           current_cat=sub_cat_id,
                           current_parent=parent_id,
                           show_orphan=show_orphan,
                           filter_status=filter_status,
                           filter_return=filter_return,
                           filter_purpose=filter_purpose,
                           filter_ret_status=filter_ret_status,
                           show_month_in=show_month_in,
                           show_month_out=show_month_out,
                           show_month_as_new=show_month_as_new,
                           show_month_as_done=show_month_as_done,
                           show_month_fault_new=show_month_fault_new,
                           show_month_fault_done=show_month_fault_done,
                           as_records=as_records,
                           fault_records_list=fault_records_list,
                           total_all=total_all,
                           orphan=orphan,
                           stats=stats,
                           page=page,
                           total_pages=total_pages,
                           total_materials=total_materials,
                           per_page=per_page)


@app.route('/api/index_filter')
def api_index_filter():
    """AJAX 局部刷新：返回筛选后的表格 HTML 和子品类 SN"""
    db = get_db()
    # 复用和 index() 完全相同的筛选逻辑
    search = request.args.get('search', '').strip()
    sub_cat_id = request.args.get('cat', '').strip()
    parent_id = request.args.get('parent', '').strip()
    show_orphan = request.args.get('orphan', '').strip()

    if sub_cat_id and not parent_id:
        try:
            parent_row = db.execute(
                "SELECT parent_id FROM categories WHERE id=?", (int(sub_cat_id),)
            ).fetchone()
            if parent_row and parent_row['parent_id']:
                parent_id = str(parent_row['parent_id'])
        except (ValueError, TypeError):
            pass

    filter_status = request.args.get('status', '').strip()
    filter_return = request.args.get('returning', '').strip()
    filter_purpose = request.args.get('purpose', '').strip()
    filter_ret_status = request.args.get('return_status', '').strip()
    show_month_in = request.args.get('month_in', '').strip()
    show_month_out = request.args.get('month_out', '').strip()
    this_month = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
    show_month_as_new = request.args.get('month_as_new', '').strip()
    show_month_as_done = request.args.get('month_as_done', '').strip()
    show_month_fault_new = request.args.get('month_fault_new', '').strip()
    show_month_fault_done = request.args.get('month_fault_done', '').strip()

    # 分页参数
    try:
        page = max(1, int(request.args.get('page', '1').strip()))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = max(1, min(200, int(request.args.get('per_page', '20').strip())))
    except (ValueError, TypeError):
        per_page = 50

    # --- 查询逻辑（和 index() 完全相同） ---
    if search:
        keywords = search.split()
        if len(keywords) == 1:
            exact = db.execute("SELECT sn FROM materials WHERE sn = ?", (search,)).fetchone()
            if exact:
                return jsonify({'redirect': url_for('detail', sn=search)})
        conditions = []
        params = []
        for kw in keywords:
            p = f'%{escape_like(kw)}%'
            conditions.append(
                '(m.sn LIKE ? OR m.remarks LIKE ? OR '
                'm.sn IN (SELECT sn FROM outbound_records WHERE customer_name LIKE ? ESCAPE \'\\\' OR customer_company LIKE ? ESCAPE \'\\\'))')
            params.extend([p, p, p, p])
        sql = ('SELECT DISTINCT m.* FROM materials m WHERE '
               + ' AND '.join(conditions) + ' ORDER BY m.inbound_time DESC')
        materials = db.execute(sql, params).fetchall()
    elif filter_return:
        materials = db.execute(
            "SELECT DISTINCT m.* FROM materials m "
            "JOIN outbound_records o ON m.sn = o.sn "
            "WHERE o.return_status = '待寄回' "
            "ORDER BY m.inbound_time DESC"
        ).fetchall()
    elif show_month_in:
        materials = db.execute(
            "SELECT * FROM materials WHERE inbound_time LIKE ? ORDER BY inbound_time DESC",
            (f'{this_month}%',)
        ).fetchall()
    elif show_month_out:
        materials = db.execute(
            "SELECT DISTINCT m.* FROM materials m "
            "JOIN outbound_records o ON m.sn = o.sn "
            "WHERE o.outbound_time LIKE ? ORDER BY m.inbound_time DESC",
            (f'{this_month}%',)
        ).fetchall()
    else:
        conditions = []
        params = []
        needs_join = bool(filter_purpose or filter_return)
        t = 'm.' if needs_join else ''
        if filter_status:
            conditions.append(f'{t}status = ?')
            params.append(filter_status)
        if sub_cat_id:
            conditions.append(f'{t}category_id = ?')
            params.append(sub_cat_id)
        elif parent_id:
            conditions.append(f'{t}category_id IN (SELECT id FROM categories WHERE parent_id = ?)')
            params.append(parent_id)
        elif show_orphan:
            conditions.append(f'{t}category_id IS NULL')
        if filter_purpose:
            conditions.append('o.purpose = ?')
            params.append(filter_purpose)
            if filter_ret_status:
                conditions.append('o.return_status = ?')
                params.append(filter_ret_status)
        if filter_return:
            conditions.append("o.return_status = '待寄回'")
        if conditions:
            where_clause = ' WHERE ' + ' AND '.join(conditions)
            if needs_join:
                materials = db.execute(
                    f"SELECT DISTINCT m.* FROM materials m "
                    f"JOIN outbound_records o ON m.sn = o.sn{where_clause} "
                    f"ORDER BY m.inbound_time DESC",
                    params
                ).fetchall()
            else:
                materials = db.execute(
                    f"SELECT * FROM materials{where_clause} ORDER BY inbound_time DESC",
                    params
                ).fetchall()
        else:
            materials = db.execute(
                "SELECT * FROM materials ORDER BY inbound_time DESC"
            ).fetchall()

    # 批量查询最新出库用途
    if materials:
        sns = [m['sn'] for m in materials]
        placeholders = ','.join(['?'] * len(sns))
        rows = db.execute(
            f"SELECT sn, purpose, purpose_detail, return_status FROM outbound_records WHERE sn IN ({placeholders}) "
            "GROUP BY sn HAVING MAX(outbound_time)",
            sns
        ).fetchall()
        purpose_map = {r['sn']: r['purpose'] for r in rows}
        detail_map = {r['sn']: r['purpose_detail'] for r in rows}
        return_map = {r['sn']: r['return_status'] for r in rows}
        materials = [dict(m) for m in materials]
        for m in materials:
            if m['status'] == '在库':
                m['latest_purpose'] = ''
                m['latest_purpose_detail'] = ''
                m['latest_return_status'] = ''
            else:
                m['latest_purpose'] = purpose_map.get(m['sn'], '')
                m['latest_purpose_detail'] = detail_map.get(m['sn'], '')
                m['latest_return_status'] = return_map.get(m['sn'], '')
        # 品类名
        cat_ids = set(m.get('category_id') for m in materials if m.get('category_id'))
        cat_map = {}
        if cat_ids:
            cat_rows = db.execute(
                "SELECT c.id, c.name AS cat_name, p.name AS parent_name "
                "FROM categories c LEFT JOIN categories p ON c.parent_id=p.id "
                "WHERE c.id IN ({})".format(','.join('?' * len(cat_ids))),
                list(cat_ids)
            ).fetchall()
            cat_map = {r['id']: (r['cat_name'], r['parent_name']) for r in cat_rows}
        for m in materials:
            info = cat_map.get(m.get('category_id'))
            m['cat_name'] = info[0] if info else ''
            m['parent_name'] = info[1] if info else ''

    # 子品类 SN（跟随状态筛选）
    parents = db.execute("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY id").fetchall()
    categories = []
    for p in parents:
        subs = db.execute("""
            SELECT c.*,
                COUNT(m.sn) AS total,
                SUM(CASE WHEN m.status='在库' THEN 1 ELSE 0 END) AS in_stock,
                SUM(CASE WHEN m.status='已出库' THEN 1 ELSE 0 END) AS outbound,
                SUM(CASE WHEN m.status='售后中' THEN 1 ELSE 0 END) AS after_sales,
                SUM(CASE WHEN m.status='故障中' THEN 1 ELSE 0 END) AS fault,
                SUM(CASE WHEN m.status='寄修中' THEN 1 ELSE 0 END) AS repair
            FROM categories c
            LEFT JOIN materials m ON m.category_id = c.id
            WHERE c.parent_id = ?
            GROUP BY c.id
            ORDER BY c.id
        """, (p['id'],)).fetchall()
        sns_for_subs = {}
        for s in subs:
            sn_query = "SELECT sn FROM materials WHERE category_id=?"
            sn_params = [s['id']]
            if filter_status:
                sn_query += " AND status=?"
                sn_params.append(filter_status)
            sn_query += " ORDER BY inbound_time DESC LIMIT 20"
            rows = db.execute(sn_query, sn_params).fetchall()
            sns_for_subs[s['id']] = [r['sn'] for r in rows]
        categories.append({
            'id': p['id'], 'name': p['name'],
            'subs': [{
                'id': s['id'], 'name': s['name'],
                'total': s['total'] or 0, 'in_stock': s['in_stock'] or 0,
                'outbound': s['outbound'] or 0, 'after_sales': s['after_sales'] or 0,
                'fault': s['fault'] or 0, 'repair': s['repair'] or 0,
                'sns': sns_for_subs.get(s['id'], [])
            } for s in subs]
        })

    # ===== 分页：对 materials 切片 =====
    total_materials = len(materials)
    total_pages = max(1, (total_materials + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    materials_page = materials[offset:offset + per_page]

    return jsonify({
        'materials': materials_page,
        'categories': categories,
        'filter_status': filter_status,
        'filter_purpose': filter_purpose,
        'filter_ret_status': filter_ret_status,
        'filter_return': filter_return,
        'current_cat': sub_cat_id,
        'current_parent': parent_id,
        'show_orphan': show_orphan,
        'show_month_in': show_month_in,
        'show_month_out': show_month_out,
        'show_month_as_new': show_month_as_new,
        'show_month_as_done': show_month_as_done,
        'show_month_fault_new': show_month_fault_new,
        'show_month_fault_done': show_month_fault_done,
        'search': search,
        'orphan': db.execute("SELECT COUNT(*) AS cnt FROM materials WHERE category_id IS NULL").fetchone()['cnt'],
        'page': page,
        'total_pages': total_pages,
        'total_materials': total_materials,
        'per_page': per_page,
    })


@app.route('/api/sub_sns')
def api_sub_sns():
    """分页获取子品类下的 SN 列表"""
    db = get_db()
    sub_id = request.args.get('sub_id', '').strip()
    page = request.args.get('page', '1').strip()
    per_page = request.args.get('per_page', '20').strip()
    filter_status = request.args.get('status', '').strip()

    if not sub_id:
        return jsonify({'error': 'sub_id required'}), 400
    try:
        sub_id = int(sub_id)
        page = max(1, int(page))
        per_page = max(1, min(100, int(per_page)))
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid params'}), 400

    # 统计总数（尊重状态筛选）
    count_query = "SELECT COUNT(*) FROM materials WHERE category_id=?"
    count_params = [sub_id]
    if filter_status:
        count_query += " AND status=?"
        count_params.append(filter_status)
    total = db.execute(count_query, count_params).fetchone()[0]

    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages

    # 获取当前页
    offset = (page - 1) * per_page
    sn_query = "SELECT sn FROM materials WHERE category_id=?"
    sn_params = [sub_id]
    if filter_status:
        sn_query += " AND status=?"
        sn_params.append(filter_status)
    sn_query += " ORDER BY inbound_time DESC LIMIT ? OFFSET ?"
    sn_params.extend([per_page, offset])

    rows = db.execute(sn_query, sn_params).fetchall()

    return jsonify({
        'sub_id': sub_id,
        'sns': [r['sn'] for r in rows],
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': total_pages,
        'has_prev': page > 1,
        'has_next': page < total_pages,
    })


@app.route('/detail/<sn>')
def detail(sn):
    db = get_db()
    material = db.execute("""
        SELECT m.*, c.name AS cat_name, p.name AS parent_name
        FROM materials m
        LEFT JOIN categories c ON m.category_id = c.id
        LEFT JOIN categories p ON c.parent_id = p.id
        WHERE m.sn = ?
    """, (sn,)).fetchone()
    if not material:
        return render_template('404.html', sn=sn), 404

    outbounds = db.execute(
        "SELECT * FROM outbound_records WHERE sn=? ORDER BY outbound_time DESC", (sn,)
    ).fetchall()
    aftersales = db.execute(
        "SELECT * FROM after_sales_records WHERE sn=? ORDER BY created_time DESC", (sn,)
    ).fetchall()
    fault = db.execute(
        "SELECT * FROM fault_records WHERE sn=? ORDER BY created_time DESC", (sn,)
    ).fetchall()
    version_changes = db.execute(
        "SELECT * FROM version_changes WHERE sn=? ORDER BY change_time DESC", (sn,)
    ).fetchall()

    # 所有小类（给编辑弹窗用）
    cats = db.execute(
        "SELECT c.*, p.name AS parent_name FROM categories c "
        "LEFT JOIN categories p ON c.parent_id=p.id "
        "WHERE c.parent_id IS NOT NULL ORDER BY p.id, c.id"
    ).fetchall()

    # 查找活跃的售后工单 ID，同时清理多余的活跃记录（只保留最新一条）
    active = db.execute(
        "SELECT id FROM after_sales_records WHERE sn=? AND status='处理中' ORDER BY created_time DESC",
        (sn,)
    ).fetchall()
    if len(active) > 1:
        # 关闭多余的活跃记录
        for a in active[1:]:
            db.execute("UPDATE after_sales_records SET status='已完成', completed_time=? WHERE id=?",
                       (datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'), a['id']))
        db.commit()
    active_as_id = active[0]['id'] if active else 0

    # 查找活跃的故障记录 ID（含寄修中），清理多余活跃记录
    active_fault = db.execute(
        "SELECT id, repair_type FROM fault_records WHERE sn=? AND status IN ('故障中','寄修中') ORDER BY created_time DESC",
        (sn,)
    ).fetchall()
    if len(active_fault) > 1:
        for f in active_fault[1:]:
            db.execute("UPDATE fault_records SET status='已修复', resolved_time=? WHERE id=?",
                       (datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'), f['id']))
        db.commit()
    active_fault_id = active_fault[0]['id'] if active_fault else 0
    active_fault_repair_type = active_fault[0]['repair_type'] if active_fault else ''

    return render_template('detail.html',
                           material=material,
                           outbounds=outbounds,
                           aftersales=aftersales,
                           fault=fault,
                           version_changes=version_changes,
                           cats=cats,
                           active_as_id=active_as_id,
                           active_fault_id=active_fault_id,
                           active_fault_repair_type=active_fault_repair_type)


@app.route('/outbounds')
def outbounds_page():
    """出库记录全局视图"""
    db = get_db()
    outbounds = db.execute("""
        SELECT o.*, m.category_id, c.name AS cat_name, p.name AS parent_name
        FROM outbound_records o
        JOIN materials m ON o.sn = m.sn
        LEFT JOIN categories c ON m.category_id = c.id
        LEFT JOIN categories p ON c.parent_id = p.id
        ORDER BY o.outbound_time DESC
    """).fetchall()
    return render_template('outbounds.html', outbounds=outbounds)


@app.route('/add')
def add_page():
    db = get_db()
    cats = db.execute(
        "SELECT c.*, p.name AS parent_name FROM categories c "
        "LEFT JOIN categories p ON c.parent_id=p.id "
        "WHERE c.parent_id IS NOT NULL ORDER BY p.id, c.id"
    ).fetchall()
    return render_template('add.html', cats=cats)


@app.route('/logs')
def logs_page():
    """操作日志页面"""
    return render_template('logs.html')


@app.route('/inventory')
def inventory_page():
    """库存盘点页面"""
    db = get_db()
    parents = db.execute("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY id").fetchall()
    categories = []
    for p in parents:
        subs = db.execute(
            "SELECT c.*, COUNT(m.sn) AS total FROM categories c "
            "LEFT JOIN materials m ON m.category_id = c.id "
            "WHERE c.parent_id = ? GROUP BY c.id ORDER BY c.id",
            (p['id'],)
        ).fetchall()
        categories.append({'id': p['id'], 'name': p['name'], 'subs': [dict(s) for s in subs]})
    return render_template('inventory.html', categories=categories)


# ==========================================================================
#                          API：品类管理
# ==========================================================================

@app.route('/api/category', methods=['POST'])
def api_add_category():
    db = get_db()
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    parent_id = data.get('parent_id')  # None=大类, 数字=小类
    if not name:
        return jsonify({'success': False, 'message': '品类名称不能为空'})
    db.execute("INSERT INTO categories (name, parent_id) VALUES (?,?)",
               (name, parent_id or None))
    db.commit()
    log_operation('add_category', '', f'添加品类"{name}"')
    return jsonify({'success': True, 'message': f'品类"{name}"已添加'})


@app.route('/api/category/<int:cid>', methods=['PUT'])
def api_edit_category(cid):
    db = get_db()
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'message': '品类名称不能为空'})
    db.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
    db.commit()
    log_operation('edit_category', '', f'品类 #{cid} 更名为"{name}"')
    return jsonify({'success': True, 'message': f'品类已更名为"{name}"'})


@app.route('/api/category/<int:cid>', methods=['DELETE'])
def api_delete_category(cid):
    db = get_db()
    # 删除该品类及其子品类，关联的物料置为 NULL
    db.execute("UPDATE materials SET category_id=NULL WHERE category_id IN "
               "(SELECT id FROM categories WHERE id=? OR parent_id=?)", (cid, cid))
    db.execute("DELETE FROM categories WHERE id=? OR parent_id=?", (cid, cid))
    db.commit()
    log_operation('delete_category', '', f'删除品类 #{cid}')
    return jsonify({'success': True, 'message': '品类已删除，关联物料已取消分类'})


@app.route('/api/categories')
def api_get_categories():
    """返回所有品类（供品类管理弹窗动态刷新用）"""
    db = get_db()
    parents = db.execute(
        "SELECT * FROM categories WHERE parent_id IS NULL ORDER BY id"
    ).fetchall()
    subs = db.execute(
        "SELECT c.*, p.name AS parent_name FROM categories c "
        "LEFT JOIN categories p ON c.parent_id = p.id "
        "WHERE c.parent_id IS NOT NULL ORDER BY p.id, c.id"
    ).fetchall()
    return jsonify({
        'parents': [{'id': p['id'], 'name': p['name']} for p in parents],
        'subs': [{'id': s['id'], 'name': s['name'],
                  'parent_id': s['parent_id'],
                  'parent_name': s['parent_name']} for s in subs]
    })


# ==========================================================================
#                          API：物料操作
# ==========================================================================

@app.route('/api/material/add', methods=['POST'])
def api_add_material():
    db = get_db()
    data = request.get_json(force=True)
    sn = data.get('sn', '').strip()
    if not sn:
        return jsonify({'success': False, 'message': 'SN 不能为空'}), 400
    if not validate_sn(sn):
        return jsonify({'success': False, 'message': 'SN 格式无效，仅允许字母、数字、连字符(-)、下划线(_)、点(.)，长度 1-100'}), 400
    if db.execute("SELECT sn FROM materials WHERE sn=?", (sn,)).fetchone():
        return jsonify({'success': False, 'message': f'SN "{sn}" 已存在'}), 400

    cat_id = data.get('category_id')
    if cat_id:
        try:
            cat_id = int(cat_id)
        except (ValueError, TypeError):
            cat_id = None
        else:
            if not db.execute("SELECT id FROM categories WHERE id=?", (cat_id,)).fetchone():
                return jsonify({'success': False, 'message': f'品类 ID {cat_id} 不存在'}), 400

    now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        "INSERT INTO materials (sn, hw_version, sw_version, hw_description, "
        "sw_description, remarks, inbound_time, status, category_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (sn, data.get('hw_version', ''), data.get('sw_version', ''),
         data.get('hw_description', ''), data.get('sw_description', ''),
         data.get('remarks', ''), now, '在库', cat_id)
    )
    db.commit()
    log_operation('add_material', sn, f'添加物料 {sn}')
    return jsonify({'success': True, 'message': f'物料 {sn} 添加成功'})


@app.route('/api/material/batch_add', methods=['POST'])
def api_batch_add():
    db = get_db()
    data = request.get_json(force=True)
    sns_text = data.get('sns', '')
    sns = re.split(r'[,;；：:、\n\r]+', sns_text)
    sns = [s.strip() for s in sns if s.strip()]
    # 去重（保留顺序），防止重复SN导致第二个INSERT违规UNIQUE
    seen = set()
    sns = [s for s in sns if not (s in seen or seen.add(s))]
    invalid_sns = [s for s in sns if not validate_sn(s)]
    if invalid_sns:
        return jsonify({'success': False, 'message': f'以下SN格式无效: {", ".join(invalid_sns[:10])}'}), 400
    if not sns:
        return jsonify({'success': False, 'message': '未提供有效的SN'}), 400

    cat_id = data.get('category_id')
    if cat_id:
        try:
            cat_id = int(cat_id)
        except (ValueError, TypeError):
            cat_id = None

    new_sns, exist_sns = [], []
    for sn in sns:
        if db.execute("SELECT sn FROM materials WHERE sn=?", (sn,)).fetchone():
            exist_sns.append(sn)
        else:
            new_sns.append(sn)
    if not new_sns:
        return jsonify({'success': False, 'message': f'所有SN均已存在: {", ".join(exist_sns)}'}), 400

    now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    for sn in new_sns:
        db.execute(
            "INSERT INTO materials (sn, hw_version, sw_version, hw_description, "
            "sw_description, remarks, inbound_time, status, category_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sn, data.get('hw_version', ''), data.get('sw_version', ''),
             data.get('hw_description', ''), data.get('sw_description', ''),
             data.get('remarks', ''), now, '在库', cat_id)
        )
    db.commit()
    msg = f'成功添加 {len(new_sns)} 个物料'
    if exist_sns:
        msg += f'（{len(exist_sns)} 个已存在跳过）'
    for sn in new_sns:
        log_operation('add_material', sn, f'批量添加物料 {sn}')
    return jsonify({'success': True, 'message': msg})


@app.route('/api/material/edit/<sn>', methods=['POST'])
def api_edit_material(sn):
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn=?", (sn,)).fetchone()
    if not material:
        return jsonify({'success': False, 'message': '物料不存在'}), 404

    data = request.get_json(force=True)
    now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    old_hw, old_sw = material['hw_version'], material['sw_version']
    new_hw = data.get('hw_version', old_hw)
    new_sw = data.get('sw_version', old_sw)
    new_sn = data.get('new_sn', '').strip()

    # SN 变更：检查是否为空、是否重复
    if new_sn and new_sn != sn:
        if not validate_sn(new_sn):
            return jsonify({'success': False, 'message': '新 SN 格式无效，仅允许字母、数字、连字符(-)、下划线(_)、点(.)，长度 1-100'}), 400
        if db.execute("SELECT sn FROM materials WHERE sn=?", (new_sn,)).fetchone():
            return jsonify({'success': False, 'message': f'SN "{new_sn}" 已存在，请使用其他SN'})
        # 暂时关闭外键约束，更新所有关联表
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.execute("UPDATE materials SET sn=? WHERE sn=?", (new_sn, sn))
            db.execute("UPDATE version_changes SET sn=? WHERE sn=?", (new_sn, sn))
            db.execute("UPDATE outbound_records SET sn=? WHERE sn=?", (new_sn, sn))
            db.execute("UPDATE after_sales_records SET sn=? WHERE sn=?", (new_sn, sn))
            db.execute("UPDATE fault_records SET sn=? WHERE sn=?", (new_sn, sn))
        finally:
            db.execute("PRAGMA foreign_keys=ON")
        # 后续操作使用新 SN
        sn = new_sn

    cat_id = data.get('category_id')
    if cat_id:
        try:
            cat_id = int(cat_id)
        except (ValueError, TypeError):
            cat_id = None
        else:
            if not db.execute("SELECT id FROM categories WHERE id=?", (cat_id,)).fetchone():
                return jsonify({'success': False, 'message': f'品类 ID {cat_id} 不存在'}), 400

    # 状态只能通过出库/入库/售后/故障等工作流操作变更，不允许直接编辑
    db.execute(
        "UPDATE materials SET hw_version=?, sw_version=?, hw_description=?, "
        "sw_description=?, remarks=?, category_id=? WHERE sn=?",
        (new_hw, new_sw,
         data.get('hw_description', material['hw_description']),
         data.get('sw_description', material['sw_description']),
         data.get('remarks', material['remarks']),
         cat_id, sn)
    )

    change_desc = data.get('change_description', '').strip()
    if new_hw != old_hw:
        db.execute(
            "INSERT INTO version_changes (sn, change_time, change_type, "
            "old_version, new_version, description) VALUES (?,?,?,?,?,?)",
            (sn, now, '硬件', old_hw, new_hw, change_desc or '编辑时变更硬件版本'))
    if new_sw != old_sw:
        db.execute(
            "INSERT INTO version_changes (sn, change_time, change_type, "
            "old_version, new_version, description) VALUES (?,?,?,?,?,?)",
            (sn, now, '软件', old_sw, new_sw, change_desc or '编辑时变更软件版本'))
    db.commit()
    log_operation('edit_material', sn, f'编辑物料信息')
    return jsonify({'success': True, 'message': '物料信息更新成功', 'sn': sn})


@app.route('/api/outbound/<sn>', methods=['POST'])
def api_outbound(sn):
    """物料出库（带事务锁防止竞态）"""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        mat = db.execute("SELECT * FROM materials WHERE sn=?", (sn,)).fetchone()
        if not mat:
            msg = '物料不存在'
            code = 404
            db.rollback()
            return jsonify({'success': False, 'message': msg}), code
        if mat['status'] == '已出库':
            msg = '该物料已出库，不能二次出库'
            db.rollback()
            return jsonify({'success': False, 'message': msg}), 400
        if mat['status'] == '售后中':
            msg = '售后中的物料不能出库'
            db.rollback()
            return jsonify({'success': False, 'message': msg}), 400
        if mat['status'] == '故障中':
            msg = '故障中的物料不能出库'
            db.rollback()
            return jsonify({'success': False, 'message': msg}), 400
        if mat['status'] == '寄修中':
            msg = '寄修中的物料不能出库'
            db.rollback()
            return jsonify({'success': False, 'message': msg}), 400
        data = request.get_json(force=True)
        now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        return_status = data.get('return_status', '无需寄回')
        return_sn = data.get('return_sn', '')
        return_courier = data.get('return_courier', '')
        return_tracking = data.get('return_tracking', '')
        db.execute(
            "INSERT INTO outbound_records (sn, outbound_time, purpose, purpose_detail, "
            "courier_company, tracking_number, customer_name, customer_contact, "
            "customer_company, address, remarks, return_status, return_sn, "
            "return_courier, return_tracking) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sn, now, data.get('purpose', ''), data.get('purpose_detail', ''),
             data.get('courier_company', ''), data.get('tracking_number', ''),
             data.get('customer_name', ''), data.get('customer_contact', ''),
             data.get('customer_company', ''), data.get('address', ''),
             data.get('remarks', ''), return_status, return_sn,
             return_courier, return_tracking))

        purpose = data.get('purpose', '')
        if purpose == '故障出库' and data.get('supplier'):
            # 寄修模式：创建故障记录 + 标记为寄修中
            supplier = data.get('supplier', '')
            fault_courier = data.get('fault_courier', '')
            fault_tracking = data.get('fault_tracking', '')
            db.execute(
                "INSERT INTO fault_records (sn, created_time, fault_reason, status, "
                "previous_status, repair_type, supplier) VALUES (?,?,?,?,?,?,?)",
                (sn, now, data.get('purpose_detail', '').replace('故障: ', ''),
                 '寄修中', mat['status'], '寄修', supplier))
            db.execute(
                "UPDATE outbound_records SET courier_company=?, tracking_number=? "
                "WHERE sn=? AND outbound_time=? AND purpose='故障出库'",
                (fault_courier, fault_tracking, sn, now))
            db.execute("UPDATE materials SET status='寄修中' WHERE sn=?", (sn,))
            db.commit()
            log_operation('fault_create', sn,
                          f'故障出库寄修至 {supplier}，故障原因: {data.get("purpose_detail", "")}')
            log_operation('outbound', sn, f'故障出库寄修至 {supplier}')
            return jsonify({'success': True,
                            'message': f'{sn} 已故障出库至 {supplier}（寄修中，待寄回）'})
        else:
            # 普通出库
            db.execute("UPDATE materials SET status='已出库' WHERE sn=?", (sn,))
            db.commit()
            log_operation('outbound', sn, f'出库（{purpose}）')
            return jsonify({'success': True, 'message': '出库操作完成'})
    except Exception:
        db.rollback()
        raise


@app.route('/api/outbound/<int:record_id>', methods=['PUT'])
def api_outbound_edit(record_id):
    """编辑出库记录"""
    db = get_db()
    record = db.execute("SELECT * FROM outbound_records WHERE id=?", (record_id,)).fetchone()
    if not record:
        return jsonify({'success': False, 'message': '出库记录不存在'}), 404
    data = request.get_json(force=True)
    db.execute(
        "UPDATE outbound_records SET purpose=?, purpose_detail=?, courier_company=?, "
        "tracking_number=?, customer_name=?, customer_contact=?, "
        "customer_company=?, address=?, remarks=?, "
        "return_status=?, return_sn=?, return_courier=?, return_tracking=? WHERE id=?",
        (data.get('purpose', ''), data.get('purpose_detail', ''),
         data.get('courier_company', ''), data.get('tracking_number', ''),
         data.get('customer_name', ''), data.get('customer_contact', ''),
         data.get('customer_company', ''), data.get('address', ''),
         data.get('remarks', ''),
         data.get('return_status', '无需寄回'), data.get('return_sn', ''),
         data.get('return_courier', ''), data.get('return_tracking', ''),
         record_id))
    db.commit()
    log_operation('edit_outbound', record['sn'], f'编辑出库记录 #{record_id}')
    return jsonify({'success': True, 'message': '出库记录已更新'})


@app.route('/api/outbound/<int:record_id>', methods=['DELETE'])
def api_outbound_delete(record_id):
    """删除出库记录"""
    db = get_db()
    record = db.execute("SELECT * FROM outbound_records WHERE id=?", (record_id,)).fetchone()
    if not record:
        return jsonify({'success': False, 'message': '出库记录不存在'}), 404
    sn = record['sn']
    db.execute("DELETE FROM outbound_records WHERE id=?", (record_id,))
    # 如果该物料当前是「已出库」，判断是否需要恢复为「在库」
    mat = db.execute("SELECT status FROM materials WHERE sn=?", (sn,)).fetchone()
    restored = False
    if mat and mat['status'] == '已出库':
        remaining = db.execute("SELECT COUNT(*) FROM outbound_records WHERE sn=?", (sn,)).fetchone()[0]
        if remaining == 0:
            db.execute("UPDATE materials SET status='在库' WHERE sn=?", (sn,))
            restored = True
        else:
            # 检查被删记录是否是最新的：如果剩余记录都更早，说明已出库状态由被删记录设置
            latest_remaining = db.execute(
                "SELECT MAX(outbound_time) FROM outbound_records WHERE sn=?", (sn,)
            ).fetchone()[0]
            if not latest_remaining or record['outbound_time'] > latest_remaining:
                db.execute("UPDATE materials SET status='在库' WHERE sn=?", (sn,))
                restored = True
    db.commit()
    log_operation('delete_outbound', sn, f'删除出库记录 #{record_id}' + ('，物料恢复在库' if restored else ''))
    return jsonify({'success': True, 'message': '出库记录已删除'})


@app.route('/api/outbound/<int:record_id>/return', methods=['POST'])
def api_outbound_return(record_id):
    """标记售后出库的故障件已寄回"""
    db = get_db()
    record = db.execute("SELECT * FROM outbound_records WHERE id=?", (record_id,)).fetchone()
    if not record:
        return jsonify({'success': False, 'message': '出库记录不存在'}), 404
    if record['return_status'] == '已寄回':
        return jsonify({'success': False, 'message': '已标记为已寄回，无需重复操作'}), 400
    data = request.get_json(force=True)
    db.execute(
        "UPDATE outbound_records SET return_status='已寄回', return_sn=?, "
        "return_courier=?, return_tracking=? WHERE id=?",
        (data.get('return_sn', ''), data.get('return_courier', ''),
         data.get('return_tracking', ''), record_id))
    db.commit()
    log_operation('outbound_return', record['sn'], f'标记寄回完成 出库记录 #{record_id}')
    return jsonify({'success': True, 'message': '已标记为寄回完成'})


@app.route('/api/restock/<sn>', methods=['POST'])
def api_restock(sn):
    """物料重新入库（状态改回在库）"""
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn=?", (sn,)).fetchone()
    if not material:
        return jsonify({'success': False, 'message': '物料不存在'}), 404
    if material['status'] == '售后中':
        return jsonify({'success': False, 'message': '售后中的物料不能直接入库，请先完成售后'}), 400
    if material['status'] == '故障中':
        return jsonify({'success': False, 'message': '故障中的物料不能直接入库，请先修复故障'}), 400
    if material['status'] == '寄修中':
        return jsonify({'success': False, 'message': '寄修中的物料不能直接入库，请先修复故障'}), 400
    db.execute("UPDATE materials SET status='在库' WHERE sn=?", (sn,))
    db.commit()
    log_operation('restock', sn, '物料重新入库')
    return jsonify({'success': True, 'message': f'{sn} 已重新入库'})


@app.route('/api/aftersales/<sn>', methods=['POST'])
def api_aftersales(sn):
    """创建售后工单（带事务锁防止竞态）"""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        if not db.execute("SELECT sn FROM materials WHERE sn=?", (sn,)).fetchone():
            db.rollback()
            return jsonify({'success': False, 'message': '物料不存在'}), 404
        active = db.execute(
            "SELECT id FROM after_sales_records WHERE sn=? AND status='处理中'", (sn,)
        ).fetchone()
        if active:
            db.rollback()
            return jsonify({'success': False, 'message': f'该物料已有售后工单 #{active["id"]} 处理中，请先完成后再创建'}), 400
        data = request.get_json(force=True)
        now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        db.execute(
            "INSERT INTO after_sales_records (sn, created_time, return_courier, "
            "return_tracking, problem_description, status) VALUES (?,?,?,?,?,?)",
            (sn, now, data.get('return_courier', ''), data.get('return_tracking', ''),
             data.get('problem_description', ''), '处理中'))
        db.execute("UPDATE materials SET status='售后中' WHERE sn=?", (sn,))
        db.commit()
        log_operation('aftersales_create', sn, '创建售后工单')
        return jsonify({'success': True, 'message': '售后工单已创建'})
    except Exception:
        db.rollback()
        raise


@app.route('/api/aftersales/complete/<int:record_id>', methods=['POST'])
def api_aftersales_complete(record_id):
    db = get_db()
    record = db.execute("SELECT * FROM after_sales_records WHERE id=?", (record_id,)).fetchone()
    if not record:
        return jsonify({'success': False, 'message': '售后记录不存在'}), 404
    if record['status'] == '已完成':
        return jsonify({'success': False, 'message': '已完成，无需重复操作'}), 400
    data = request.get_json(force=True)
    now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        "UPDATE after_sales_records SET send_back_courier=?, send_back_tracking=?, "
        "status='已完成', completed_time=?, remarks=? WHERE id=?",
        (data.get('send_back_courier', ''), data.get('send_back_tracking', ''),
         now, data.get('remarks', ''), record_id))
    # 售后完成：根据 return_to_customer 决定设备去向
    return_to_customer = data.get('return_to_customer', True)  # 默认返还客户，保持向后兼容
    if return_to_customer:
        db.execute(
            "INSERT INTO outbound_records (sn, outbound_time, purpose, purpose_detail, "
            "courier_company, tracking_number, customer_name, customer_contact, "
            "customer_company, address, remarks) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (record['sn'], now, '售后返客户', '',
             data.get('send_back_courier', ''), data.get('send_back_tracking', ''),
             '', '', '', '', '售后工单 #' + str(record_id) + ' 完成后返还客户'))
        db.execute("UPDATE materials SET status='已出库' WHERE sn=?", (record['sn'],))
    else:
        db.execute("UPDATE materials SET status='在库' WHERE sn=?", (record['sn'],))
    db.commit()
    msg = '售后工单已完成，物料已出库返还客户' if return_to_customer else '售后工单已完成，物料已回库'
    log_operation('aftersales_complete', record['sn'], f'售后工单 #{record_id} 完成（{"返还客户" if return_to_customer else "回库"}）')
    return jsonify({'success': True, 'message': msg})


@app.route('/api/fault/<sn>', methods=['POST'])
def api_fault(sn):
    """创建故障记录（带事务锁防止竞态）。支持本地维修和寄修两种模式"""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        material = db.execute("SELECT * FROM materials WHERE sn=?", (sn,)).fetchone()
        if not material:
            db.rollback()
            return jsonify({'success': False, 'message': '物料不存在'}), 404
        if material['status'] == '售后中':
            db.rollback()
            return jsonify({'success': False, 'message': '售后中的物料不能创建故障记录，请先完成售后'}), 400
        if material['status'] == '寄修中':
            db.rollback()
            return jsonify({'success': False, 'message': '该物料已在寄修中，请先完成修复'}), 400
        active = db.execute(
            "SELECT id FROM fault_records WHERE sn=? AND status IN ('故障中','寄修中')", (sn,)
        ).fetchone()
        if active:
            db.rollback()
            return jsonify({'success': False, 'message': f'该物料已有故障记录 #{active["id"]} 处理中，请先修复后再创建'}), 400
        data = request.get_json(force=True)
        now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        repair_type = data.get('repair_type', '本地维修')
        preserve_status = material['status'] == '已出库'

        if repair_type == '寄修':
            supplier = data.get('supplier', '').strip()
            courier_company = data.get('courier_company', '').strip()
            tracking_number = data.get('tracking_number', '').strip()
            if not supplier or not courier_company or not tracking_number:
                db.rollback()
                return jsonify({'success': False, 'message': '寄修时供应商、快递公司和快递单号为必填'}), 400
            if preserve_status:
                db.rollback()
                return jsonify({'success': False, 'message': '已出库设备不能寄修，请先重新入库'}), 400
            db.execute(
                "INSERT INTO fault_records (sn, created_time, fault_reason, status, previous_status, repair_type, supplier) "
                "VALUES (?,?,?,?,?,?,?)",
                (sn, now, data.get('fault_reason', ''), '寄修中', material['status'], '寄修', supplier))
            db.execute(
                "INSERT INTO outbound_records (sn, outbound_time, purpose, purpose_detail, "
                "courier_company, tracking_number, customer_name, customer_company, remarks) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sn, now, '寄修出库', f'寄修至 {supplier}', courier_company, tracking_number,
                 supplier, '', data.get('fault_reason', '')))
            db.execute("UPDATE materials SET status='寄修中' WHERE sn=?", (sn,))
            db.commit()
            log_operation('fault_create', sn, f'创建寄修故障记录: {data.get("fault_reason", "")}，寄修至 {supplier}')
            return jsonify({'success': True, 'message': f'{sn} 已标记为寄修中，寄修出库至 {supplier}'})
        else:
            db.execute(
                "INSERT INTO fault_records (sn, created_time, fault_reason, status, previous_status) "
                "VALUES (?,?,?,?,?)",
                (sn, now, data.get('fault_reason', ''), '故障中', material['status']))
            if not preserve_status:
                db.execute("UPDATE materials SET status='故障中' WHERE sn=?", (sn,))
            db.commit()
            msg = f'{sn} 已记录故障工单' if preserve_status else f'{sn} 已标记为故障中'
            log_operation('fault_create', sn, f'创建故障记录: {data.get("fault_reason", "")}' + ('（已出库设备，状态不变）' if preserve_status else ''))
            return jsonify({'success': True, 'message': msg})
    except Exception:
        db.rollback()
        raise


@app.route('/api/fault/resolve/<int:record_id>', methods=['POST'])
def api_fault_resolve(record_id):
    """修复故障，恢复之前的状态。寄修故障支持登记寄回快递信息"""
    db = get_db()
    record = db.execute("SELECT * FROM fault_records WHERE id=?", (record_id,)).fetchone()
    if not record:
        return jsonify({'success': False, 'message': '故障记录不存在'}), 404
    if record['status'] == '已修复':
        return jsonify({'success': False, 'message': '已修复，无需重复操作'}), 400
    data = request.get_json(force=True)
    now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')

    # 同步更新出库记录：标记该故障关联的出库记录为已寄回
    ob = db.execute(
        "SELECT id FROM outbound_records WHERE sn=? AND purpose=? ORDER BY outbound_time DESC LIMIT 1",
        (record['sn'], '故障出库')).fetchone()
    if ob:
        db.execute(
            "UPDATE outbound_records SET return_status='已寄回', return_courier=?, return_tracking=? WHERE id=?",
            (data.get('return_courier', '').strip(), data.get('return_tracking', '').strip(), ob['id']))
    is_repair = record['repair_type'] == '寄修'
    if is_repair:
        return_courier = data.get('return_courier', '').strip()
        return_tracking = data.get('return_tracking', '').strip()
        db.execute(
            "UPDATE fault_records SET status='已修复', solution=?, resolved_time=?, "
            "return_courier=?, return_tracking=? WHERE id=?",
            (data.get('solution', ''), now, return_courier, return_tracking, record_id))
        # 寄修修复：恢复之前的状态（通常是恢复为在库）
        prev = record['previous_status'] if record['previous_status'] not in ('寄修中',) else '在库'
        db.execute("UPDATE materials SET status=? WHERE sn=?", (prev, record['sn']))
        db.commit()
        log_operation('fault_resolve', record['sn'], f'寄修故障 #{record_id} 已修复，状态恢复为「{prev}」')
        return jsonify({'success': True, 'message': f'{record["sn"]} 寄修故障已修复，物料已回库，状态恢复为「{prev}」'})
    else:
        db.execute(
            "UPDATE fault_records SET status='已修复', solution=?, resolved_time=? WHERE id=?",
            (data.get('solution', ''), now, record_id))
        # 恢复之前的状态（仅当物料当前仍是故障中；已出库设备创建故障时状态未变，无需恢复）
        current = db.execute("SELECT status FROM materials WHERE sn=?", (record['sn'],)).fetchone()
        if current and current['status'] == '故障中':
            prev = record['previous_status'] if record['previous_status'] not in ('故障中',) else '在库'
            db.execute("UPDATE materials SET status=? WHERE sn=?", (prev, record['sn']))
        else:
            prev = current['status'] if current else '在库'
        db.commit()
        log_operation('fault_resolve', record['sn'], f'故障 #{record_id} 已修复，状态恢复为「{prev}」')
    return jsonify({'success': True, 'message': f'{record["sn"]} 故障已修复，状态恢复为「{prev}」'})


@app.route('/api/material/delete/<sn>', methods=['POST'])
def api_delete_material(sn):
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn=?", (sn,)).fetchone()
    if not material:
        return jsonify({'success': False, 'message': '物料不存在'}), 404
    if material['status'] == '售后中':
        return jsonify({'success': False, 'message': '售后中的物料不能删除，请先处理售后工单'}), 400
    if material['status'] == '故障中':
        return jsonify({'success': False, 'message': '故障中的物料不能删除，请先修复故障'}), 400
    if material['status'] == '寄修中':
        return jsonify({'success': False, 'message': '寄修中的物料不能删除，请先完成修复'}), 400
    db.execute("DELETE FROM version_changes WHERE sn=?", (sn,))
    db.execute("DELETE FROM after_sales_records WHERE sn=?", (sn,))
    db.execute("DELETE FROM fault_records WHERE sn=?", (sn,))
    db.execute("DELETE FROM outbound_records WHERE sn=?", (sn,))
    db.execute("DELETE FROM materials WHERE sn=?", (sn,))
    db.commit()
    log_operation('delete_material', sn, f'删除物料 {sn}')
    return jsonify({'success': True, 'message': f'物料 {sn} 已删除'})


@app.route('/api/material/batch_delete', methods=['POST'])
def api_batch_delete():
    db = get_db()
    data = request.get_json(force=True)
    sns = data.get('sns', [])
    if not sns:
        return jsonify({'success': False, 'message': '未选择物料'}), 400
    existing = [r['sn'] for r in db.execute(
        "SELECT sn FROM materials WHERE sn IN ({})".format(','.join('?' * len(sns))), sns
    ).fetchall()]
    if not existing:
        return jsonify({'success': False, 'message': '物料不存在'}), 404
    # 检查状态：售后中/故障中的物料不能删除
    blocked = []
    rows = db.execute(
        "SELECT sn, status FROM materials WHERE sn IN ({})".format(','.join('?' * len(existing))), existing
    ).fetchall()
    for r in rows:
        if r['status'] == '售后中':
            blocked.append(f'{r["sn"]} 当前为售后中，请先处理售后工单')
        elif r['status'] == '故障中':
            blocked.append(f'{r["sn"]} 当前为故障中，请先修复故障')
    if blocked:
        return jsonify({'success': False, 'message': '以下物料无法删除：\n' + '\n'.join(blocked)}), 400
    for sn in existing:
        db.execute("DELETE FROM version_changes WHERE sn=?", (sn,))
        db.execute("DELETE FROM after_sales_records WHERE sn=?", (sn,))
        db.execute("DELETE FROM fault_records WHERE sn=?", (sn,))
        db.execute("DELETE FROM outbound_records WHERE sn=?", (sn,))
    db.execute("DELETE FROM materials WHERE sn IN ({})".format(','.join('?' * len(existing))), existing)
    db.commit()
    for sn in existing:
        log_operation('delete_material', sn, f'批量删除物料 {sn}')
    return jsonify({'success': True, 'message': f'已删除 {len(existing)} 个物料'})


@app.route('/api/check_sn/<sn>')
def api_check_sn(sn):
    db = get_db()
    cur = db.execute("SELECT sn FROM materials WHERE sn=?", (sn,)).fetchone()
    return jsonify({'exists': cur is not None})


def levenshtein_ratio(s1, s2):
    """计算两个字符串的相似度 (0.0 ~ 1.0)，基于编辑距离"""
    if not s1 or not s2:
        return 0.0
    len1, len2 = len(s1), len(s2)
    # 长度差超过 30% 直接判为不相似（性能优化）
    if abs(len1 - len2) > max(len1, len2) * 0.3:
        return 0.0
    prev = list(range(len2 + 1))
    curr = [0] * (len2 + 1)
    for i in range(1, len1 + 1):
        curr[0] = i
        for j in range(1, len2 + 1):
            curr[j] = prev[j - 1] if s1[i - 1] == s2[j - 1] else 1 + min(prev[j], curr[j - 1], prev[j - 1])
        prev, curr = curr, prev
    return 1.0 - prev[len2] / max(len1, len2)


@app.route('/api/similar_sn/<sn>')
def api_similar_sn(sn):
    """查找与给定 SN 最相似的已有物料，用于新增时自动填充信息"""
    db = get_db()
    if len(sn) < 4:
        return jsonify({'found': False, 'reason': 'SN too short'})
    all_sns = db.execute("SELECT sn FROM materials").fetchall()
    if not all_sns:
        return jsonify({'found': False, 'reason': 'no data'})

    THRESHOLD = 0.85  # 相似度阈值
    best_sn = None
    best_ratio = 0.0

    for row in all_sns:
        existing = row['sn']
        if existing == sn:
            continue
        ratio = levenshtein_ratio(sn, existing)
        if ratio > best_ratio:
            best_ratio = ratio
            best_sn = existing

    if best_sn and best_ratio >= THRESHOLD:
        material = db.execute(
            "SELECT m.*, c.id AS cat_id FROM materials m "
            "LEFT JOIN categories c ON m.category_id = c.id "
            "WHERE m.sn = ?", (best_sn,)
        ).fetchone()
        if not material:
            return jsonify({'found': False})
        return jsonify({
            'found': True,
            'similar_sn': best_sn,
            'similarity': round(best_ratio, 4),
            'category_id': material['cat_id'],
            'hw_version': material['hw_version'],
            'sw_version': material['sw_version'],
            'hw_description': material['hw_description'],
            'sw_description': material['sw_description'],
            'remarks': material['remarks']
        })

    return jsonify({'found': False})


@app.route('/api/category_sn_samples/<int:cat_id>')
def api_category_sn_samples(cat_id):
    """返回指定品类下不同命名模式的代表性 SN（各取最新一条）"""
    db = get_db()
    rows = db.execute(
        "SELECT sn FROM materials WHERE category_id = ? ORDER BY inbound_time DESC",
        (cat_id,)
    ).fetchall()
    if not rows:
        return jsonify({'samples': []})

    sns = [r['sn'] for r in rows]
    CLUSTER_THRESHOLD = 0.7  # 同类命名模式聚类阈值
    clusters = []  # [(representative, [members]), ...]

    for sn in sns:
        matched = False
        for rep, members in clusters:
            if levenshtein_ratio(sn, rep) >= CLUSTER_THRESHOLD:
                members.append(sn)
                matched = True
                break
        if not matched:
            clusters.append((sn, [sn]))

    # 每类取代表（已是该类最新 SN），最多 5 类
    samples = [rep for rep, _ in clusters[:5]]
    return jsonify({'samples': samples})


@app.route('/api/category_recent_sns/<int:cat_id>')
def api_category_recent_sns(cat_id):
    """返回指定品类下最近添加的物料（供新增时参考）"""
    db = get_db()
    rows = db.execute(
        "SELECT sn, hw_version, sw_version, hw_description, sw_description, "
        "inbound_time, remarks FROM materials WHERE category_id = ? "
        "ORDER BY inbound_time DESC LIMIT 10",
        (cat_id,)
    ).fetchall()
    return jsonify([{
        'sn': r['sn'],
        'hw_version': r['hw_version'],
        'sw_version': r['sw_version'],
        'hw_description': r['hw_description'],
        'sw_description': r['sw_description'],
        'inbound_time': r['inbound_time'],
        'remarks': r['remarks']
    } for r in rows])


# ==========================================================================
#                         批量出库
# ==========================================================================

@app.route('/batch-outbound')
def batch_outbound_page():
    """批量出库页面"""
    db = get_db()
    parents = db.execute("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY id").fetchall()
    categories = []
    for p in parents:
        subs = db.execute(
            "SELECT * FROM categories WHERE parent_id=? ORDER BY id", (p['id'],)
        ).fetchall()
        categories.append({'id': p['id'], 'name': p['name'], 'subs': [dict(s) for s in subs]})
    return render_template('batch_outbound.html', categories=categories)


@app.route('/api/search_sn')
def api_search_sn():
    """搜索 SN（返回 JSON），用于批量出库选择"""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        "SELECT m.sn, m.status, c.name AS cat_name, p.name AS parent_name "
        "FROM materials m "
        "LEFT JOIN categories c ON m.category_id = c.id "
        "LEFT JOIN categories p ON c.parent_id = p.id "
        "WHERE m.sn LIKE ? ESCAPE '\\' ORDER BY m.inbound_time DESC LIMIT 20",
        (f'%{escape_like(q)}%',)
    ).fetchall()
    return jsonify([{'sn': r['sn'], 'status': r['status'],
                     'cat_name': r['cat_name'], 'parent_name': r['parent_name']} for r in rows])


@app.route('/api/in_stock_sns')
def api_in_stock_sns():
    """返回指定小类下所有在库 SN（用于批量出库品类筛选）"""
    cat_id = request.args.get('cat', '').strip()
    if not cat_id:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        "SELECT sn, status, hw_version, sw_version FROM materials "
        "WHERE category_id=? AND status='在库' ORDER BY inbound_time DESC",
        (cat_id,)
    ).fetchall()
    return jsonify([{'sn': r['sn'], 'status': r['status'],
                     'hw_version': r['hw_version'], 'sw_version': r['sw_version']} for r in rows])


@app.route('/api/outbound/batch', methods=['POST'])
def api_outbound_batch():
    """批量出库：多个 SN 共用同一客户/快递/备注信息"""
    db = get_db()
    data = request.get_json(force=True)
    sns = data.get('sns', [])
    if not sns:
        return jsonify({'success': False, 'message': '请选择至少一个物料'}), 400
    # 去重
    sns = list(dict.fromkeys(sns))
    # 校验所有 SN 存在且为在库状态
    invalid = []
    for sn in sns:
        mat = db.execute("SELECT status FROM materials WHERE sn=?", (sn,)).fetchone()
        if not mat:
            invalid.append(f'{sn} 不存在')
        elif mat['status'] != '在库':
            invalid.append(f'{sn} 当前状态为「{mat["status"]}」')
    if invalid:
        return jsonify({'success': False, 'message': '以下物料无法出库：\n' + '\n'.join(invalid)}), 400

    now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    purpose = data.get('purpose', '')
    purpose_detail = data.get('purpose_detail', '')
    courier = data.get('courier_company', '')
    tracking = data.get('tracking_number', '')
    customer = data.get('customer_name', '')
    contact = data.get('customer_contact', '')
    company = data.get('customer_company', '')
    address = data.get('address', '')
    remarks = data.get('remarks', '')
    return_status = data.get('return_status', '无需寄回')
    return_sn = data.get('return_sn', '')
    return_courier = data.get('return_courier', '')
    return_tracking = data.get('return_tracking', '')

    # 寄修模式（仅故障出库支持）
    is_repair = purpose == '故障出库' and data.get('supplier')
    supplier = data.get('supplier', '')
    # 每个SN独立的故障原因（故障出库时前端传入）
    fault_details = data.get('fault_details', {})
    if not isinstance(fault_details, dict):
        fault_details = {}

    db.execute("BEGIN IMMEDIATE")
    try:
        for sn in sns:
            # 故障出库时优先使用该SN独立的故障原因，否则用共用purpose_detail
            sn_fault_reason = fault_details.get(sn, '').strip() if fault_details else purpose_detail
            db.execute(
                "INSERT INTO outbound_records (sn, outbound_time, purpose, purpose_detail, "
                "courier_company, tracking_number, customer_name, customer_contact, "
                "customer_company, address, remarks, return_status, return_sn, "
                "return_courier, return_tracking) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sn, now, purpose, sn_fault_reason, courier, tracking, customer, contact,
                 company, address, remarks, return_status, return_sn,
                 return_courier, return_tracking))
            if is_repair:
                db.execute(
                    "INSERT INTO fault_records (sn, created_time, fault_reason, status, "
                    "previous_status, repair_type, supplier) VALUES (?,?,?,?,?,?,?)",
                    (sn, now, sn_fault_reason,
                     '寄修中', '在库', '寄修', supplier))
                db.execute("UPDATE materials SET status='寄修中' WHERE sn=?", (sn,))
            else:
                db.execute("UPDATE materials SET status='已出库' WHERE sn=?", (sn,))
        db.commit()
    except Exception as e:
        db.execute("ROLLBACK")
        return jsonify({'success': False, 'message': f'批量出库失败，已回滚：{str(e)}'}), 500
    for sn in sns:
        log_operation('outbound', sn,
                      f'批量出库（{purpose}{"寄修至" + supplier if is_repair else ""}）')
    msg = f'已批量出库 {len(sns)} 个物料'
    if is_repair:
        msg += f'（寄修中，供应商：{supplier}）'
    return jsonify({'success': True, 'message': msg})


# ==========================================================================
#                       API：操作日志
# ==========================================================================

@app.route('/api/operation_logs')
def api_operation_logs():
    """查询操作日志，支持分页和筛选"""
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    op_type = request.args.get('type', '').strip()
    sn = request.args.get('sn', '').strip()

    conditions = []
    params = []
    if op_type:
        conditions.append('op_type = ?')
        params.append(op_type)
    if sn:
        conditions.append('sn LIKE ? ESCAPE \'\\\'')
        params.append(f'%{escape_like(sn)}%')

    where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
    total = db.execute(f"SELECT COUNT(*) FROM operation_logs{where}", params).fetchone()[0]

    offset = (page - 1) * per_page
    rows = db.execute(
        f"SELECT * FROM operation_logs{where} ORDER BY op_time DESC LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()

    return jsonify({
        'logs': [dict(r) for r in rows],
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': max(1, (total + per_page - 1) // per_page)
    })


# ==========================================================================
#                       API：数据导出 CSV
# ==========================================================================

import csv
import io
from urllib.parse import quote


def _sanitize_csv_cell(value):
    """Prevent CSV formula injection: prefix cells starting with =, +, -, @ with a single quote"""
    s = str(value) if value is not None else ''
    if s and s[0] in ('=', '+', '-', '@'):
        return "'" + s
    return s


def _make_csv_response(rows, filename, columns):
    """生成 CSV 文件响应，带 BOM 兼容 Excel 中文打开"""
    output = io.StringIO()
    output.write('﻿')  # UTF-8 BOM
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        if isinstance(row, dict):
            writer.writerow([_sanitize_csv_cell(row.get(c, '')) for c in columns])
        else:
            writer.writerow([_sanitize_csv_cell(row[c]) if c in row.keys() else '' for c in columns])
    resp = app.response_class(output.getvalue(), mimetype='text/csv; charset=utf-8')
    resp.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return resp


@app.route('/api/export/materials')
def api_export_materials():
    """导出物料清单 CSV"""
    db = get_db()
    rows = db.execute("""
        SELECT m.sn, m.hw_version, m.sw_version, m.hw_description, m.sw_description,
               m.status, m.inbound_time, m.remarks,
               c.name AS cat_name, p.name AS parent_name
        FROM materials m
        LEFT JOIN categories c ON m.category_id = c.id
        LEFT JOIN categories p ON c.parent_id = p.id
        ORDER BY m.inbound_time DESC
    """).fetchall()
    cols = ['sn', 'hw_version', 'sw_version', 'hw_description', 'sw_description',
            'status', 'inbound_time', 'cat_name', 'parent_name', 'remarks']
    return _make_csv_response(rows, '物料清单.csv', cols)


@app.route('/api/export/outbounds')
def api_export_outbounds():
    """导出出库记录 CSV"""
    db = get_db()
    rows = db.execute("""
        SELECT o.sn, o.outbound_time, o.purpose, o.purpose_detail,
               o.courier_company, o.tracking_number, o.customer_name,
               o.customer_contact, o.customer_company, o.address,
               o.return_status, o.return_sn, o.remarks
        FROM outbound_records o ORDER BY o.outbound_time DESC
    """).fetchall()
    cols = ['sn', 'outbound_time', 'purpose', 'purpose_detail',
            'courier_company', 'tracking_number', 'customer_name',
            'customer_contact', 'customer_company', 'address',
            'return_status', 'return_sn', 'remarks']
    return _make_csv_response(rows, '出库记录.csv', cols)


@app.route('/api/export/aftersales')
def api_export_aftersales():
    """导出售后工单 CSV"""
    db = get_db()
    rows = db.execute("""
        SELECT a.sn, a.created_time, a.problem_description, a.status,
               a.return_courier, a.return_tracking,
               a.send_back_courier, a.send_back_tracking,
               a.completed_time, a.remarks
        FROM after_sales_records a ORDER BY a.created_time DESC
    """).fetchall()
    cols = ['sn', 'created_time', 'problem_description', 'status',
            'return_courier', 'return_tracking',
            'send_back_courier', 'send_back_tracking',
            'completed_time', 'remarks']
    return _make_csv_response(rows, '售后工单.csv', cols)


@app.route('/api/export/faults')
def api_export_faults():
    """导出故障记录 CSV"""
    db = get_db()
    rows = db.execute("""
        SELECT f.sn, f.created_time, f.fault_reason, f.solution,
               f.status, f.previous_status, f.resolved_time,
               f.repair_type, f.supplier, f.return_courier, f.return_tracking
        FROM fault_records f ORDER BY f.created_time DESC
    """).fetchall()
    cols = ['sn', 'created_time', 'fault_reason', 'solution',
            'status', 'previous_status', 'resolved_time',
            'repair_type', 'supplier', 'return_courier', 'return_tracking']
    return _make_csv_response(rows, '故障记录.csv', cols)


@app.route('/api/export/operation_logs')
def api_export_operation_logs():
    """导出操作日志 CSV"""
    db = get_db()
    rows = db.execute("SELECT * FROM operation_logs ORDER BY op_time DESC").fetchall()
    cols = ['id', 'op_time', 'op_type', 'sn', 'detail', 'operator_ip']
    return _make_csv_response(rows, '操作日志.csv', cols)


# ==========================================================================
#                       API：客户信息复用
# ==========================================================================

@app.route('/api/customer_history')
def api_customer_history():
    """返回历史客户信息，供出库时下拉选择"""
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT customer_name, customer_contact, customer_company, address,
               COUNT(*) AS cnt, MAX(outbound_time) AS last_time
        FROM outbound_records
        WHERE customer_name != '' OR customer_company != ''
        GROUP BY COALESCE(NULLIF(customer_name,''), customer_company)
        ORDER BY last_time DESC LIMIT 50
    """).fetchall()
    return jsonify([{
        'name': r['customer_name'],
        'contact': r['customer_contact'],
        'company': r['customer_company'],
        'address': r['address'],
        'count': r['cnt'],
        'last_time': r['last_time']
    } for r in rows])


# ==========================================================================
#                       API：库存盘点
# ==========================================================================

@app.route('/api/inventory/check_records')
def api_check_records():
    """查询盘点记录"""
    db = get_db()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    total = db.execute("SELECT COUNT(*) FROM inventory_checks").fetchone()[0]
    offset = (page - 1) * per_page
    rows = db.execute(
        "SELECT * FROM inventory_checks ORDER BY check_time DESC LIMIT ? OFFSET ?",
        (per_page, offset)
    ).fetchall()
    return jsonify({
        'checks': [dict(r) for r in rows],
        'total': total,
        'page': page,
        'total_pages': max(1, (total + per_page - 1) // per_page)
    })


@app.route('/api/inventory/check', methods=['POST'])
def api_inventory_check():
    """执行盘点：标记物料在库/不在库"""
    db = get_db()
    data = request.get_json(force=True)
    sns = data.get('sns', [])
    notes = data.get('notes', '')
    if not sns:
        return jsonify({'success': False, 'message': '请选择至少一个物料'}), 400

    now = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    mismatch = []
    checked = 0
    for sn in sns:
        mat = db.execute("SELECT status FROM materials WHERE sn=?", (sn,)).fetchone()
        found_status = mat['status'] if mat else '不存在'
        if not mat:
            mismatch.append(f'{sn} — 系统中不存在')
        elif mat['status'] != '在库':
            mismatch.append(f'{sn} — 状态为「{mat["status"]}」，非"在库"')
        db.execute(
            "INSERT INTO inventory_checks (check_time, sn, found_status, notes) VALUES (?,?,?,?)",
            (now, sn, found_status, notes)
        )
        checked += 1
    db.commit()

    msg = f'已盘点 {checked} 个物料'
    if mismatch:
        msg += f'（{len(mismatch)} 个异常：\n' + '\n'.join(mismatch) + '）'
    log_operation('inventory_check', '', f'盘点 {checked} 个物料' + (f'，{len(mismatch)} 个异常' if mismatch else ''))
    return jsonify({'success': True, 'message': msg, 'mismatch': mismatch})


@app.route('/api/inventory/stats')
def api_inventory_stats():
    """库存盘点统计（按品类汇总）"""
    db = get_db()
    parents = db.execute("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY id").fetchall()
    result = []
    for p in parents:
        subs = db.execute("""
            SELECT c.*,
                COUNT(m.sn) AS total,
                SUM(CASE WHEN m.status='在库' THEN 1 ELSE 0 END) AS in_stock,
                SUM(CASE WHEN m.status='已出库' THEN 1 ELSE 0 END) AS outbound,
                SUM(CASE WHEN m.status='售后中' THEN 1 ELSE 0 END) AS after_sales,
                SUM(CASE WHEN m.status='故障中' THEN 1 ELSE 0 END) AS fault,
                SUM(CASE WHEN m.status='寄修中' THEN 1 ELSE 0 END) AS repair
            FROM categories c
            LEFT JOIN materials m ON m.category_id = c.id
            WHERE c.parent_id = ?
            GROUP BY c.id ORDER BY c.id
        """, (p['id'],)).fetchall()
        result.append({
            'name': p['name'],
            'subs': [{
                'name': s['name'],
                'total': s['total'] or 0,
                'in_stock': s['in_stock'] or 0,
                'outbound': s['outbound'] or 0,
                'after_sales': s['after_sales'] or 0,
                'fault': s['fault'] or 0,
            } for s in subs]
        })
    # 总计
    totals = db.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='在库' THEN 1 ELSE 0 END) AS in_stock,
            SUM(CASE WHEN status='已出库' THEN 1 ELSE 0 END) AS outbound,
            SUM(CASE WHEN status='售后中' THEN 1 ELSE 0 END) AS after_sales,
            SUM(CASE WHEN status='故障中' THEN 1 ELSE 0 END) AS fault
        FROM materials
    """).fetchone()
    return jsonify({
        'categories': result,
        'totals': dict(totals),
        'orphan': db.execute("SELECT COUNT(*) FROM materials WHERE category_id IS NULL").fetchone()[0]
    })


# ==========================================================================
#                       API：物料状态时间线
# ==========================================================================

@app.route('/api/timeline/<sn>')
def api_timeline(sn):
    """返回物料状态时间线（合并出库、售后、故障、版本变更、操作日志）"""
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn=?", (sn,)).fetchone()
    if not material:
        return jsonify({'success': False, 'message': '物料不存在'}), 404

    events = []

    # 入库事件
    events.append({
        'time': material['inbound_time'],
        'type': 'inbound',
        'label': '入库',
        'detail': f"SN: {material['sn']}，硬件: {material['hw_version']}，软件: {material['sw_version']}"
    })

    # 出库记录
    for r in db.execute(
        "SELECT * FROM outbound_records WHERE sn=? ORDER BY outbound_time", (sn,)
    ).fetchall():
        events.append({
            'time': r['outbound_time'],
            'type': 'outbound',
            'label': f"出库（{r['purpose'] or '未指定'}）",
            'detail': f"{r['customer_name']} {r['customer_company']} {r['courier_company'] or ''} {r['tracking_number'] or ''}".strip()
        })

    # 售后记录
    for r in db.execute(
        "SELECT * FROM after_sales_records WHERE sn=? ORDER BY created_time", (sn,)
    ).fetchall():
        events.append({
            'time': r['created_time'],
            'type': 'aftersales',
            'label': '创建售后工单',
            'detail': f"#{r['id']} — {r['problem_description']}"
        })
        if r['completed_time']:
            events.append({
                'time': r['completed_time'],
                'type': 'aftersales_done',
                'label': '售后完成',
                'detail': f"#{r['id']} — {r['remarks'] or ''}"
            })

    # 故障记录
    for r in db.execute(
        "SELECT * FROM fault_records WHERE sn=? ORDER BY created_time", (sn,)
    ).fetchall():
        events.append({
            'time': r['created_time'],
            'type': 'fault',
            'label': '故障记录',
            'detail': f"#{r['id']} — {r['fault_reason']}"
        })
        if r['resolved_time']:
            events.append({
                'time': r['resolved_time'],
                'type': 'fault_resolved',
                'label': '故障修复',
                'detail': f"#{r['id']} — {r['solution'] or ''}"
            })

    # 版本变更
    for r in db.execute(
        "SELECT * FROM version_changes WHERE sn=? ORDER BY change_time", (sn,)
    ).fetchall():
        events.append({
            'time': r['change_time'],
            'type': 'version_change',
            'label': f"版本变更（{r['change_type']}）",
            'detail': f"{r['old_version']} → {r['new_version']} — {r['description']}"
        })

    # 操作日志
    for r in db.execute(
        "SELECT * FROM operation_logs WHERE sn=? ORDER BY op_time", (sn,)
    ).fetchall():
        events.append({
            'time': r['op_time'],
            'type': 'log',
            'label': r['op_type'],
            'detail': r['detail']
        })

    events.sort(key=lambda e: e['time'])
    return jsonify({'sn': sn, 'events': events, 'current_status': material['status']})


# ==========================================================================
#                       API：仪表板汇总统计
# ==========================================================================

@app.route('/api/dashboard/stats')
def api_dashboard_stats():
    """仪表板统计数据（供 Chart.js 使用）"""
    db = get_db()
    # 各状态数量
    status_counts = db.execute("""
        SELECT status, COUNT(*) AS cnt FROM materials GROUP BY status
    """).fetchall()
    status_map = {r['status']: r['cnt'] for r in status_counts}

    # 每月入库趋势（最近12个月）
    month_in = db.execute("""
        SELECT substr(inbound_time, 1, 7) AS m, COUNT(*) AS cnt
        FROM materials GROUP BY m ORDER BY m DESC LIMIT 12
    """).fetchall()

    # 每月出库趋势
    month_out = db.execute("""
        SELECT substr(outbound_time, 1, 7) AS m, COUNT(*) AS cnt
        FROM outbound_records GROUP BY m ORDER BY m DESC LIMIT 12
    """).fetchall()

    # 品类分布 Top 10
    top_cats = db.execute("""
        SELECT c.name, COUNT(m.sn) AS cnt
        FROM materials m JOIN categories c ON m.category_id = c.id
        GROUP BY c.id ORDER BY cnt DESC LIMIT 10
    """).fetchall()

    # 出库用途分布
    purpose_dist = db.execute("""
        SELECT purpose, COUNT(*) AS cnt FROM outbound_records
        WHERE purpose != '' GROUP BY purpose ORDER BY cnt DESC
    """).fetchall()

    return jsonify({
        'status_counts': {r['status']: r['cnt'] for r in status_counts},
        'month_in': [{'month': r['m'], 'count': r['cnt']} for r in month_in],
        'month_out': [{'month': r['m'], 'count': r['cnt']} for r in month_out],
        'top_categories': [{'name': r['name'], 'count': r['cnt']} for r in top_cats],
        'purpose_dist': [{'purpose': r['purpose'], 'count': r['cnt']} for r in purpose_dist],
    })


@app.route('/api/dashboard/category_trend')
def api_category_trend():
    """品类+月份筛选的趋势数据，供新图表使用"""
    db = get_db()
    parent_id = request.args.get('parent_id', type=int)
    sub_id = request.args.get('subcategory_id', type=int)
    month_start = request.args.get('month_start', '')
    month_end = request.args.get('month_end', '')

    # 构建品类 WHERE 条件
    cat_clause = ''
    cat_params = []
    if sub_id:
        cat_clause = ' AND m.category_id = ?'
        cat_params.append(sub_id)
    elif parent_id:
        cat_clause = ' AND m.category_id IN (SELECT id FROM categories WHERE parent_id = ?)'
        cat_params.append(parent_id)

    # 月份条件
    month_clause = ''
    month_params = []
    if month_start:
        month_clause += ' AND substr(o.outbound_time,1,7) >= ?'
        month_params.append(month_start)
    if month_end:
        month_clause += ' AND substr(o.outbound_time,1,7) <= ?'
        month_params.append(month_end)

    # 1) 按月+目的统计出库数量
    outbounds = db.execute(
        'SELECT substr(o.outbound_time,1,7) AS month, o.purpose, COUNT(*) AS cnt'
        ' FROM outbound_records o JOIN materials m ON o.sn = m.sn'
        ' WHERE 1=1' + cat_clause + month_clause +
        ' GROUP BY month, o.purpose ORDER BY month',
        cat_params + month_params
    ).fetchall()

    # 2) 按月统计入库数量
    in_month_clause = ''
    in_month_params = []
    if month_start:
        in_month_clause += ' AND substr(inbound_time,1,7) >= ?'
        in_month_params.append(month_start)
    if month_end:
        in_month_clause += ' AND substr(inbound_time,1,7) <= ?'
        in_month_params.append(month_end)

    inbound_by_month = db.execute(
        'SELECT substr(inbound_time,1,7) AS month, COUNT(*) AS cnt'
        ' FROM materials m WHERE 1=1' + cat_clause + in_month_clause +
        ' GROUP BY month ORDER BY month',
        cat_params + in_month_params
    ).fetchall()

    # 收集所有月份
    all_months_set = set()
    for r in outbounds:
        all_months_set.add(r['month'])
    for r in inbound_by_month:
        all_months_set.add(r['month'])
    all_months = sorted(all_months_set)

    if not all_months:
        return jsonify({'months': [], 'series': {}})

    # 3) 计算每个月末的在库数量
    all_materials = db.execute(
        'SELECT m.sn, m.status, m.inbound_time,'
        ' (SELECT MAX(o2.outbound_time) FROM outbound_records o2 WHERE o2.sn = m.sn) AS last_out'
        ' FROM materials m WHERE 1=1' + cat_clause,
        cat_params
    ).fetchall()

    purposes = set()
    outbound_by_month_purpose = {}
    for r in outbounds:
        purposes.add(r['purpose'])
        key = (r['month'], r['purpose'])
        outbound_by_month_purpose[key] = r['cnt']

    purpose_order = sorted([p for p in purposes if p])
    # directly use all outbound purposes from data

    in_stock_by_month = {}
    for m in all_months:
        month_end_str = m + '-31'
        cnt = 0
        for mat in all_materials:
            if mat['inbound_time'] and mat['inbound_time'] <= month_end_str:
                if mat['last_out'] is None or mat['last_out'] > month_end_str:
                    cnt += 1
        in_stock_by_month[m] = cnt

    series = {}
    series['在库'] = [in_stock_by_month.get(m, 0) for m in all_months]
    for p in purpose_order:
        series[p] = [outbound_by_month_purpose.get((m, p), 0) for m in all_months]

    return jsonify({
        'months': all_months,
        'series': series,
        'purposes': purpose_order,
    })


# ==========================================================================
#                            启动入口
# ==========================================================================

def create_app():
    with app.app_context():
        try:
            init_db()
        except Exception as e:
            print(f"[ERROR] 数据库初始化失败: {e}")
        try:
            insert_sample_data()
        except Exception as e:
            print(f"[ERROR] 示例数据插入失败: {e}")
    return app


@app.route('/health')
def health():
    """健康检查接口 — PythonAnywhere 定时任务可用此接口防止休眠"""
    try:
        get_db().execute("SELECT 1")
        return jsonify({'status': 'ok', 'db': 'connected'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# 数据库同步密匙（从环境变量读取，与 sync_db.py 保持一致）
SYNC_TOKEN = os.environ.get('WLGZ_SYNC_TOKEN', '')
if not SYNC_TOKEN:
    print('[WARNING] WLGZ_SYNC_TOKEN 环境变量未设置，数据库同步接口已禁用')


@app.route('/api/db/download')
def api_db_download():
    """下载数据库文件（用于本地同步）"""
    token = request.args.get('token', '')
    if token != SYNC_TOKEN:
        return jsonify({'error': 'unauthorized'}), 403
    from flask import send_file
    return send_file(DATABASE, mimetype='application/octet-stream',
                     as_attachment=True, download_name='material.db')


@app.route('/api/db/upload', methods=['POST'])
def api_db_upload():
    """上传完整数据库文件（用于数据恢复/迁移）"""
    token = request.form.get('token', '')
    if token != SYNC_TOKEN:
        return jsonify({'success': False, 'error': 'unauthorized'}), 403
    file = request.files.get('db')
    if not file or not file.filename:
        return jsonify({'success': False, 'error': '未选择文件'}), 400
    # 备份旧数据库
    import shutil as _shutil
    bak = DATABASE + '.pre_upload.bak'
    _shutil.copy2(DATABASE, bak)
    # 写入新数据库
    file.save(DATABASE)
    # 校验新数据库可读
    try:
        test = sqlite3.connect(DATABASE)
        cnt = test.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
        test.close()
        return jsonify({'success': True, 'message': f'数据库已上传，物料 {cnt} 条'})
    except Exception as e:
        # 恢复旧数据库
        _shutil.copy2(bak, DATABASE)
        return jsonify({'success': False, 'error': f'数据库校验失败，已回滚: {str(e)}'}), 400


def auto_sync_from_cloud():
    """本地启动时自动双向同步，失败不阻塞启动"""
    import subprocess
    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync_db.py')
        subprocess.run([sys.executable, script], timeout=60)
    except Exception as e:
        print(f"[同步] 失败({e})，使用本地已有数据")


def start_background_sync(interval_minutes=30):
    """后台线程：每 interval_minutes 分钟自动执行一次双向同步"""
    import subprocess, threading, time as _time
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync_db.py')
    if not os.path.exists(script):
        print("[同步] sync_db.py 不存在，跳过后台同步线程")
        return

    def _sync_loop():
        _time.sleep(60)  # 启动后等 1 分钟再开始第一次同步
        while True:
            try:
                print(f"[同步] 开始定时同步...")
                subprocess.run([sys.executable, script], timeout=120)
            except Exception as e:
                print(f"[同步] 定时同步失败: {e}")
            _time.sleep(interval_minutes * 60)

    t = threading.Thread(target=_sync_loop, daemon=True, name="bg-sync")
    t.start()
    print(f"[同步] 后台同步线程已启动，每 {interval_minutes} 分钟同步一次")


if __name__ == '__main__':
    auto_sync_from_cloud()
    create_app()
    start_background_sync()
    print("=" * 60)
    print("  物料全流程追溯系统 v2.0")
    print("  本地访问: http://127.0.0.1:8080")
    print("  后台同步: 每 30 分钟自动同步一次")
    print("=" * 60)
    app.run(host='127.0.0.1', port=8080, debug=False, threaded=True)
else:
    create_app()
