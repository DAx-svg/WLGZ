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
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, g
import sqlite3

app = Flask(__name__)
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
DATABASE = os.path.join(DATA_DIR, 'material.db')


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
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
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS outbound_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sn              TEXT NOT NULL,
            outbound_time   TEXT NOT NULL,
            courier_company TEXT DEFAULT '',
            tracking_number TEXT DEFAULT '',
            customer_name   TEXT DEFAULT '',
            customer_contact TEXT DEFAULT '',
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

        CREATE INDEX IF NOT EXISTS idx_outbound_sn ON outbound_records(sn);
        CREATE INDEX IF NOT EXISTS idx_aftersales_sn ON after_sales_records(sn);
        CREATE INDEX IF NOT EXISTS idx_version_sn ON version_changes(sn);
        CREATE INDEX IF NOT EXISTS idx_materials_category ON materials(category_id);
    """)
    # 兼容旧数据库：category_id 列如果不存在则添加
    try:
        db.execute("ALTER TABLE materials ADD COLUMN category_id INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
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

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
            "tracking_number, customer_name, customer_contact, remarks) "
            "VALUES (?,?,?,?,?,?,?)", o
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


# ==========================================================================
#                            页面路由
# ==========================================================================

@app.route('/')
def index():
    """首页：品类导航 + 物料列表"""
    db = get_db()
    search = request.args.get('search', '').strip()
    sub_cat_id = request.args.get('cat', '').strip()

    # 精确 SN 搜索 → 直接跳转
    if search:
        exact = db.execute("SELECT sn FROM materials WHERE sn = ?", (search,)).fetchone()
        if exact:
            return redirect(url_for('detail', sn=search))
        materials = db.execute(
            "SELECT * FROM materials WHERE sn LIKE ? ORDER BY inbound_time DESC",
            (f'%{search}%',)
        ).fetchall()
    elif sub_cat_id:
        materials = db.execute(
            "SELECT * FROM materials WHERE category_id = ? ORDER BY inbound_time DESC",
            (sub_cat_id,)
        ).fetchall()
    else:
        materials = db.execute(
            "SELECT * FROM materials ORDER BY inbound_time DESC"
        ).fetchall()

    # 品类树：大类和旗下小类（带统计）
    parents = db.execute("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY id").fetchall()
    categories = []
    for p in parents:
        subs = db.execute("""
            SELECT c.*,
                COUNT(m.sn) AS total,
                SUM(CASE WHEN m.status='在库' THEN 1 ELSE 0 END) AS in_stock,
                SUM(CASE WHEN m.status='已出库' THEN 1 ELSE 0 END) AS outbound,
                SUM(CASE WHEN m.status='售后中' THEN 1 ELSE 0 END) AS after_sales
            FROM categories c
            LEFT JOIN materials m ON m.category_id = c.id
            WHERE c.parent_id = ?
            GROUP BY c.id
            ORDER BY c.id
        """, (p['id'],)).fetchall()

        # 每个小类取前 20 个 SN
        sns_for_subs = {}
        for s in subs:
            rows = db.execute(
                "SELECT sn FROM materials WHERE category_id=? ORDER BY inbound_time DESC LIMIT 20",
                (s['id'],)
            ).fetchall()
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
                'sns': sns_for_subs.get(s['id'], [])
            } for s in subs]
        })

    # 总计数
    total_all = sum(
        sum(s['total'] for s in cat['subs']) for cat in categories
    )
    # 未归类物料数
    orphan = db.execute(
        "SELECT COUNT(*) AS cnt FROM materials WHERE category_id IS NULL"
    ).fetchone()['cnt']

    return render_template('index.html',
                           categories=categories,
                           materials=materials,
                           search=search,
                           current_cat=sub_cat_id,
                           total_all=total_all,
                           orphan=orphan)


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
    version_changes = db.execute(
        "SELECT * FROM version_changes WHERE sn=? ORDER BY change_time DESC", (sn,)
    ).fetchall()

    # 所有小类（给编辑弹窗用）
    cats = db.execute(
        "SELECT c.*, p.name AS parent_name FROM categories c "
        "LEFT JOIN categories p ON c.parent_id=p.id "
        "WHERE c.parent_id IS NOT NULL ORDER BY p.id, c.id"
    ).fetchall()

    return render_template('detail.html',
                           material=material,
                           outbounds=outbounds,
                           aftersales=aftersales,
                           version_changes=version_changes,
                           cats=cats)


@app.route('/add')
def add_page():
    db = get_db()
    cats = db.execute(
        "SELECT c.*, p.name AS parent_name FROM categories c "
        "LEFT JOIN categories p ON c.parent_id=p.id "
        "WHERE c.parent_id IS NOT NULL ORDER BY p.id, c.id"
    ).fetchall()
    return render_template('add.html', cats=cats)


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
    return jsonify({'success': True, 'message': f'品类已更名为"{name}"'})


@app.route('/api/category/<int:cid>', methods=['DELETE'])
def api_delete_category(cid):
    db = get_db()
    # 删除该品类及其子品类，关联的物料置为 NULL
    db.execute("UPDATE materials SET category_id=NULL WHERE category_id IN "
               "(SELECT id FROM categories WHERE id=? OR parent_id=?)", (cid, cid))
    db.execute("DELETE FROM categories WHERE id=? OR parent_id=?", (cid, cid))
    db.commit()
    return jsonify({'success': True, 'message': '品类已删除，关联物料已取消分类'})


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
    if db.execute("SELECT sn FROM materials WHERE sn=?", (sn,)).fetchone():
        return jsonify({'success': False, 'message': f'SN "{sn}" 已存在'}), 400

    cat_id = data.get('category_id')
    if cat_id:
        try:
            cat_id = int(cat_id)
        except (ValueError, TypeError):
            cat_id = None

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        "INSERT INTO materials (sn, hw_version, sw_version, hw_description, "
        "sw_description, remarks, inbound_time, status, category_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (sn, data.get('hw_version', ''), data.get('sw_version', ''),
         data.get('hw_description', ''), data.get('sw_description', ''),
         data.get('remarks', ''), now, '在库', cat_id)
    )
    db.commit()
    return jsonify({'success': True, 'message': f'物料 {sn} 添加成功'})


@app.route('/api/material/batch_add', methods=['POST'])
def api_batch_add():
    db = get_db()
    data = request.get_json(force=True)
    sns_text = data.get('sns', '')
    sns = re.split(r'[,;\n\r]+', sns_text)
    sns = [s.strip() for s in sns if s.strip()]
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

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
    return jsonify({'success': True, 'message': msg})


@app.route('/api/material/edit/<sn>', methods=['POST'])
def api_edit_material(sn):
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn=?", (sn,)).fetchone()
    if not material:
        return jsonify({'success': False, 'message': '物料不存在'}), 404

    data = request.get_json(force=True)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    old_hw, old_sw = material['hw_version'], material['sw_version']
    new_hw = data.get('hw_version', old_hw)
    new_sw = data.get('sw_version', old_sw)

    cat_id = data.get('category_id')
    if cat_id:
        try:
            cat_id = int(cat_id)
        except (ValueError, TypeError):
            cat_id = None

    db.execute(
        "UPDATE materials SET hw_version=?, sw_version=?, hw_description=?, "
        "sw_description=?, remarks=?, status=?, category_id=? WHERE sn=?",
        (new_hw, new_sw,
         data.get('hw_description', material['hw_description']),
         data.get('sw_description', material['sw_description']),
         data.get('remarks', material['remarks']),
         data.get('status', material['status']),
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
    return jsonify({'success': True, 'message': '物料信息更新成功'})


@app.route('/api/outbound/<sn>', methods=['POST'])
def api_outbound(sn):
    db = get_db()
    if not db.execute("SELECT sn FROM materials WHERE sn=?", (sn,)).fetchone():
        return jsonify({'success': False, 'message': '物料不存在'}), 404
    data = request.get_json(force=True)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        "INSERT INTO outbound_records (sn, outbound_time, courier_company, "
        "tracking_number, customer_name, customer_contact, remarks) "
        "VALUES (?,?,?,?,?,?,?)",
        (sn, now, data.get('courier_company', ''), data.get('tracking_number', ''),
         data.get('customer_name', ''), data.get('customer_contact', ''),
         data.get('remarks', '')))
    db.execute("UPDATE materials SET status='已出库' WHERE sn=?", (sn,))
    db.commit()
    return jsonify({'success': True, 'message': '出库操作完成'})


@app.route('/api/aftersales/<sn>', methods=['POST'])
def api_aftersales(sn):
    db = get_db()
    if not db.execute("SELECT sn FROM materials WHERE sn=?", (sn,)).fetchone():
        return jsonify({'success': False, 'message': '物料不存在'}), 404
    data = request.get_json(force=True)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        "INSERT INTO after_sales_records (sn, created_time, return_courier, "
        "return_tracking, problem_description, status) VALUES (?,?,?,?,?,?)",
        (sn, now, data.get('return_courier', ''), data.get('return_tracking', ''),
         data.get('problem_description', ''), '处理中'))
    db.execute("UPDATE materials SET status='售后中' WHERE sn=?", (sn,))
    db.commit()
    return jsonify({'success': True, 'message': '售后工单已创建'})


@app.route('/api/aftersales/complete/<int:record_id>', methods=['POST'])
def api_aftersales_complete(record_id):
    db = get_db()
    record = db.execute("SELECT * FROM after_sales_records WHERE id=?", (record_id,)).fetchone()
    if not record:
        return jsonify({'success': False, 'message': '售后记录不存在'}), 404
    if record['status'] == '已完成':
        return jsonify({'success': False, 'message': '已完成，无需重复操作'}), 400
    data = request.get_json(force=True)
    db.execute(
        "UPDATE after_sales_records SET send_back_courier=?, send_back_tracking=?, "
        "status='已完成', remarks=? WHERE id=?",
        (data.get('send_back_courier', ''), data.get('send_back_tracking', ''),
         data.get('remarks', ''), record_id))
    db.execute("UPDATE materials SET status='在库' WHERE sn=?", (record['sn'],))
    db.commit()
    return jsonify({'success': True, 'message': '售后工单已完成'})


@app.route('/api/check_sn/<sn>')
def api_check_sn(sn):
    db = get_db()
    cur = db.execute("SELECT sn FROM materials WHERE sn=?", (sn,)).fetchone()
    return jsonify({'exists': cur is not None})


# ==========================================================================
#                            启动入口
# ==========================================================================

def create_app():
    with app.app_context():
        init_db()
        insert_sample_data()
    return app


if __name__ == '__main__':
    create_app()
    print("=" * 60)
    print("  物料全流程追溯系统 v2.0")
    print("  本地访问: http://127.0.0.1:8080")
    print("=" * 60)
    app.run(host='0.0.0.0', port=8080, debug=True, threaded=True)
else:
    create_app()
