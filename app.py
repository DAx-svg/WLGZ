"""
物料全流程追溯系统
====================
基于 Flask + SQLite 的公司内部物料追溯管理系统。
支持物料入库、出库、售后全流程追踪，以及硬件/软件版本变更记录。

运行方式：
  1. pip install -r requirements.txt
  2. python app.py
  3. 浏览器访问 http://127.0.0.1:8080
  4. 局域网内其他设备访问 http://<本机IP>:8080
  5. 若端口被占用，修改 app.py 底部的 port=8080 为其他端口

首次运行会自动创建数据库并插入示例数据。
"""

import os
import re
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, g
import sqlite3

# ---------------------------------------------------------------------------
# Flask 应用初始化
# ---------------------------------------------------------------------------
app = Flask(__name__)

# 数据库路径：本地用项目目录，部署平台通过环境变量指定持久化目录
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
DATABASE = os.path.join(DATA_DIR, 'material.db')


# ---------------------------------------------------------------------------
# 数据库连接管理
# ---------------------------------------------------------------------------
def get_db():
    """获取当前请求的数据库连接（每个请求独立连接）。
    启用 WAL 模式以支持并发读写，设置 busy_timeout 防止写入冲突时报错。
    """
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row  # 让查询结果可以用列名访问
        g.db.execute("PRAGMA journal_mode=WAL")      # Write-Ahead Logging，支持并发
        g.db.execute("PRAGMA busy_timeout=5000")      # 写入锁等待 5 秒
        g.db.execute("PRAGMA foreign_keys=ON")        # 启用外键约束
    return g.db


@app.teardown_appcontext
def close_db(exception):
    """请求结束时自动关闭数据库连接"""
    db = g.pop('db', None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# 数据库初始化（首次运行时自动建表）
# ---------------------------------------------------------------------------
def init_db():
    """创建所有数据表（如不存在）"""
    db = get_db()
    db.executescript("""
        -- 物料主表：每件物料一条记录，SN 全局唯一
        CREATE TABLE IF NOT EXISTS materials (
            sn              TEXT PRIMARY KEY,       -- 全局唯一序列号
            hw_version      TEXT DEFAULT '',        -- 硬件版本号
            sw_version      TEXT DEFAULT '',        -- 软件版本号
            hw_description  TEXT DEFAULT '',        -- 硬件功能描述
            sw_description  TEXT DEFAULT '',        -- 软件功能描述
            status          TEXT DEFAULT '在库',    -- 当前状态：在库/已出库/售后中
            inbound_time    TEXT NOT NULL,          -- 入库时间
            remarks         TEXT DEFAULT ''         -- 备注
        );

        -- 出库记录表：每次出库一条记录，一个物料可多次出库
        CREATE TABLE IF NOT EXISTS outbound_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sn              TEXT NOT NULL,          -- 物料SN
            outbound_time   TEXT NOT NULL,          -- 出库时间
            courier_company TEXT DEFAULT '',        -- 快递公司
            tracking_number TEXT DEFAULT '',        -- 快递单号
            customer_name   TEXT DEFAULT '',        -- 客户名称
            customer_contact TEXT DEFAULT '',       -- 客户联系方式
            remarks         TEXT DEFAULT '',        -- 出库备注
            FOREIGN KEY (sn) REFERENCES materials(sn)
        );

        -- 售后记录表：每次售后一条记录
        CREATE TABLE IF NOT EXISTS after_sales_records (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            sn               TEXT NOT NULL,         -- 物料SN
            created_time     TEXT NOT NULL,         -- 售后发起时间
            return_courier   TEXT DEFAULT '',       -- 寄回快递公司
            return_tracking  TEXT DEFAULT '',       -- 寄回快递单号
            send_back_courier TEXT DEFAULT '',      -- 寄出快递公司（返回给客户）
            send_back_tracking TEXT DEFAULT '',     -- 寄出快递单号
            problem_description TEXT DEFAULT '',    -- 售后问题描述
            status           TEXT DEFAULT '处理中', -- 处理状态：处理中/已完成
            remarks          TEXT DEFAULT '',       -- 处理备注
            FOREIGN KEY (sn) REFERENCES materials(sn)
        );

        -- 版本变更记录表：每次硬件或软件版本变更一条记录
        CREATE TABLE IF NOT EXISTS version_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sn          TEXT NOT NULL,              -- 物料SN
            change_time TEXT NOT NULL,              -- 变更时间
            change_type TEXT NOT NULL,              -- 变更类型：硬件/软件
            old_version TEXT DEFAULT '',            -- 旧版本
            new_version TEXT DEFAULT '',            -- 新版本
            description TEXT DEFAULT '',            -- 变更说明
            FOREIGN KEY (sn) REFERENCES materials(sn)
        );

        -- 索引：加速按 SN 查询各记录表
        CREATE INDEX IF NOT EXISTS idx_outbound_sn ON outbound_records(sn);
        CREATE INDEX IF NOT EXISTS idx_aftersales_sn ON after_sales_records(sn);
        CREATE INDEX IF NOT EXISTS idx_version_sn ON version_changes(sn);
    """)
    db.commit()


# ---------------------------------------------------------------------------
# 示例数据（首次运行自动插入，方便即刻体验）
# ---------------------------------------------------------------------------
def insert_sample_data():
    """首次运行时插入示例物料、出库、售后、版本变更记录"""
    db = get_db()
    cur = db.execute("SELECT COUNT(*) AS cnt FROM materials")
    if cur.fetchone()['cnt'] > 0:
        return  # 已有数据，跳过

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ---- 示例物料 ----
    materials = [
        ('SN-2024-001', 'V2.1', 'FW-3.4.1',
         '4G通信模组，支持GPS定位',
         '支持远程OTA升级，心跳包上报周期可配置',
         '在库', '2024-01-10 09:30:00', '首批试产样品'),
        ('SN-2024-002', 'V2.1', 'FW-3.4.1',
         '4G通信模组，支持GPS定位',
         '支持远程OTA升级，心跳包上报周期可配置',
         '已出库', '2024-01-15 10:30:00', '已发给客户A现场测试'),
        ('SN-2024-003', 'V2.2', 'FW-3.4.2',
         '4G通信模组，支持GPS+北斗双模定位',
         '支持远程OTA升级，新增断点续传功能',
         '已出库', '2024-02-20 14:00:00', '改进版，定位精度提升'),
        ('SN-2024-004', 'V2.2', 'FW-3.4.2',
         '4G通信模组，支持GPS+北斗双模定位',
         '支持远程OTA升级，新增断点续传功能',
         '售后中', '2024-02-20 14:05:00', '客户反馈开机异常'),
        ('SN-2024-005', 'V3.0', 'FW-4.0.0',
         '5G通信模组，支持GPS+北斗+GLONASS三模定位',
         '全新架构，支持边缘计算与本地数据预处理',
         '在库', '2024-03-10 09:00:00', '新一代旗舰产品'),
    ]
    for m in materials:
        db.execute(
            "INSERT INTO materials (sn, hw_version, sw_version, hw_description, "
            "sw_description, status, inbound_time, remarks) VALUES (?,?,?,?,?,?,?,?)", m
        )

    # ---- 示例出库记录 ----
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

    # ---- 示例售后记录 ----
    aftersales = [
        ('SN-2024-004', '2024-05-01 16:00:00', '中通快递', 'ZT1111111111',
         '', '', '设备上电后指示灯不亮，无法正常开机', '处理中',
         '收到后初步排查为电池接口虚焊'),
    ]
    for a in aftersales:
        db.execute(
            "INSERT INTO after_sales_records (sn, created_time, return_courier, "
            "return_tracking, send_back_courier, send_back_tracking, "
            "problem_description, status, remarks) VALUES (?,?,?,?,?,?,?,?,?)", a
        )

    # ---- 示例版本变更记录 ----
    version_changes = [
        ('SN-2024-003', '2024-02-20 14:00:00', '硬件', 'V2.1', 'V2.2',
         'GPS模组升级为GPS+北斗双模，定位精度由5m提升至2.5m'),
        ('SN-2024-003', '2024-02-20 14:05:00', '软件', 'FW-3.4.1', 'FW-3.4.2',
         '新增固件断点续传功能，修复偶发的心跳包丢失问题'),
        ('SN-2024-005', '2024-03-10 09:00:00', '硬件', 'V2.2', 'V3.0',
         '核心芯片更换为5G方案，新增GLONASS支持，整体功耗降低15%'),
        ('SN-2024-005', '2024-03-10 09:00:00', '软件', 'FW-3.4.2', 'FW-4.0.0',
         '固件架构重构，引入边缘计算框架，支持本地数据过滤与预处理'),
    ]
    for v in version_changes:
        db.execute(
            "INSERT INTO version_changes (sn, change_time, change_type, "
            "old_version, new_version, description) VALUES (?,?,?,?,?,?)", v
        )

    db.commit()
    print(f"[示例数据] 已插入 {len(materials)} 个物料、{len(outbounds)} 条出库记录、"
          f"{len(aftersales)} 条售后记录、{len(version_changes)} 条版本变更记录")


# ===========================================================================
#                            页面路由
# ===========================================================================

@app.route('/')
def index():
    """首页：物料列表 + SN 搜索"""
    db = get_db()
    search = request.args.get('search', '').strip()

    if search:
        # 先尝试精确匹配：输入完整SN直接跳转详情页
        exact = db.execute("SELECT sn FROM materials WHERE sn = ?", (search,)).fetchone()
        if exact:
            return redirect(url_for('detail', sn=search))
        # 模糊搜索
        materials = db.execute(
            "SELECT * FROM materials WHERE sn LIKE ? ORDER BY inbound_time DESC",
            (f'%{search}%',)
        ).fetchall()
    else:
        materials = db.execute(
            "SELECT * FROM materials ORDER BY inbound_time DESC"
        ).fetchall()

    return render_template('index.html', materials=materials, search=search)


@app.route('/detail/<sn>')
def detail(sn):
    """物料详情页：基本信息 + 出库/售后/版本变更标签页"""
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn = ?", (sn,)).fetchone()
    if not material:
        return render_template('404.html', sn=sn), 404

    outbounds = db.execute(
        "SELECT * FROM outbound_records WHERE sn = ? ORDER BY outbound_time DESC", (sn,)
    ).fetchall()

    aftersales = db.execute(
        "SELECT * FROM after_sales_records WHERE sn = ? ORDER BY created_time DESC", (sn,)
    ).fetchall()

    version_changes = db.execute(
        "SELECT * FROM version_changes WHERE sn = ? ORDER BY change_time DESC", (sn,)
    ).fetchall()

    return render_template('detail.html',
                           material=material,
                           outbounds=outbounds,
                           aftersales=aftersales,
                           version_changes=version_changes)


@app.route('/add')
def add_page():
    """添加物料页面"""
    return render_template('add.html')


# ===========================================================================
#                           API 路由
# ===========================================================================

@app.route('/api/material/add', methods=['POST'])
def api_add_material():
    """添加单个物料"""
    db = get_db()
    data = request.get_json(force=True)
    sn = data.get('sn', '').strip()

    if not sn:
        return jsonify({'success': False, 'message': 'SN 不能为空'}), 400

    existing = db.execute("SELECT sn FROM materials WHERE sn = ?", (sn,)).fetchone()
    if existing:
        return jsonify({'success': False, 'message': f'SN "{sn}" 已存在，请使用其他SN'}), 400

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        "INSERT INTO materials (sn, hw_version, sw_version, hw_description, "
        "sw_description, remarks, inbound_time, status) VALUES (?,?,?,?,?,?,?,?)",
        (sn,
         data.get('hw_version', ''),
         data.get('sw_version', ''),
         data.get('hw_description', ''),
         data.get('sw_description', ''),
         data.get('remarks', ''),
         now,
         '在库')
    )
    db.commit()
    return jsonify({'success': True, 'message': f'物料 {sn} 添加成功'})


@app.route('/api/material/batch_add', methods=['POST'])
def api_batch_add():
    """批量添加物料：支持换行/逗号/分号分隔多个SN"""
    db = get_db()
    data = request.get_json(force=True)
    sns_text = data.get('sns', '')

    # 按换行、逗号、分号拆分
    sns = re.split(r'[,;\n\r]+', sns_text)
    sns = [s.strip() for s in sns if s.strip()]

    if not sns:
        return jsonify({'success': False, 'message': '未提供有效的SN'}), 400

    existing_sns = []
    new_sns = []
    for sn in sns:
        cur = db.execute("SELECT sn FROM materials WHERE sn = ?", (sn,)).fetchone()
        if cur:
            existing_sns.append(sn)
        else:
            new_sns.append(sn)

    if not new_sns:
        return jsonify({
            'success': False,
            'message': f'所有SN均已存在: {", ".join(existing_sns)}'
        }), 400

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for sn in new_sns:
        db.execute(
            "INSERT INTO materials (sn, hw_version, sw_version, hw_description, "
            "sw_description, remarks, inbound_time, status) VALUES (?,?,?,?,?,?,?,?)",
            (sn,
             data.get('hw_version', ''),
             data.get('sw_version', ''),
             data.get('hw_description', ''),
             data.get('sw_description', ''),
             data.get('remarks', ''),
             now,
             '在库')
        )
    db.commit()

    msg = f'成功添加 {len(new_sns)} 个物料'
    if existing_sns:
        msg += f'（{len(existing_sns)} 个已存在跳过）'
    return jsonify({'success': True, 'message': msg})


@app.route('/api/material/edit/<sn>', methods=['POST'])
def api_edit_material(sn):
    """编辑物料基本信息，版本变更时自动记录"""
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn = ?", (sn,)).fetchone()
    if not material:
        return jsonify({'success': False, 'message': '物料不存在'}), 404

    data = request.get_json(force=True)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    old_hw = material['hw_version']
    old_sw = material['sw_version']
    new_hw = data.get('hw_version', old_hw)
    new_sw = data.get('sw_version', old_sw)

    # 更新物料基本信息
    db.execute(
        "UPDATE materials SET hw_version=?, sw_version=?, hw_description=?, "
        "sw_description=?, remarks=?, status=? WHERE sn=?",
        (new_hw, new_sw,
         data.get('hw_description', material['hw_description']),
         data.get('sw_description', material['sw_description']),
         data.get('remarks', material['remarks']),
         data.get('status', material['status']),
         sn)
    )

    # 自动记录版本变更
    change_desc = data.get('change_description', '').strip()
    if new_hw != old_hw:
        db.execute(
            "INSERT INTO version_changes (sn, change_time, change_type, "
            "old_version, new_version, description) VALUES (?,?,?,?,?,?)",
            (sn, now, '硬件', old_hw, new_hw,
             change_desc or '编辑基本信息时变更硬件版本')
        )
    if new_sw != old_sw:
        db.execute(
            "INSERT INTO version_changes (sn, change_time, change_type, "
            "old_version, new_version, description) VALUES (?,?,?,?,?,?)",
            (sn, now, '软件', old_sw, new_sw,
             change_desc or '编辑基本信息时变更软件版本')
        )

    db.commit()
    return jsonify({'success': True, 'message': '物料信息更新成功'})


@app.route('/api/outbound/<sn>', methods=['POST'])
def api_outbound(sn):
    """执行出库操作"""
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn = ?", (sn,)).fetchone()
    if not material:
        return jsonify({'success': False, 'message': '物料不存在'}), 404

    data = request.get_json(force=True)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    db.execute(
        "INSERT INTO outbound_records (sn, outbound_time, courier_company, "
        "tracking_number, customer_name, customer_contact, remarks) "
        "VALUES (?,?,?,?,?,?,?)",
        (sn, now,
         data.get('courier_company', ''),
         data.get('tracking_number', ''),
         data.get('customer_name', ''),
         data.get('customer_contact', ''),
         data.get('remarks', ''))
    )
    db.execute("UPDATE materials SET status = '已出库' WHERE sn = ?", (sn,))
    db.commit()
    return jsonify({'success': True, 'message': '出库操作完成，物料状态已更新为"已出库"'})


@app.route('/api/aftersales/<sn>', methods=['POST'])
def api_aftersales(sn):
    """发起售后工单"""
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE sn = ?", (sn,)).fetchone()
    if not material:
        return jsonify({'success': False, 'message': '物料不存在'}), 404

    data = request.get_json(force=True)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    db.execute(
        "INSERT INTO after_sales_records (sn, created_time, return_courier, "
        "return_tracking, problem_description, status) VALUES (?,?,?,?,?,?)",
        (sn, now,
         data.get('return_courier', ''),
         data.get('return_tracking', ''),
         data.get('problem_description', ''),
         '处理中')
    )
    db.execute("UPDATE materials SET status = '售后中' WHERE sn = ?", (sn,))
    db.commit()
    return jsonify({'success': True, 'message': '售后工单已创建，物料状态已更新为"售后中"'})


@app.route('/api/aftersales/complete/<int:record_id>', methods=['POST'])
def api_aftersales_complete(record_id):
    """完成售后工单：记录寄回快递信息，恢复物料为在库"""
    db = get_db()
    record = db.execute(
        "SELECT * FROM after_sales_records WHERE id = ?", (record_id,)
    ).fetchone()
    if not record:
        return jsonify({'success': False, 'message': '售后记录不存在'}), 404

    if record['status'] == '已完成':
        return jsonify({'success': False, 'message': '该售后工单已完成，无需重复操作'}), 400

    data = request.get_json(force=True)
    db.execute(
        "UPDATE after_sales_records SET send_back_courier = ?, "
        "send_back_tracking = ?, status = '已完成', remarks = ? WHERE id = ?",
        (data.get('send_back_courier', ''),
         data.get('send_back_tracking', ''),
         data.get('remarks', ''),
         record_id)
    )
    # 售后完成 → 物料恢复为在库
    db.execute("UPDATE materials SET status = '在库' WHERE sn = ?", (record['sn'],))
    db.commit()
    return jsonify({'success': True, 'message': '售后工单已完成，物料状态已恢复为"在库"'})


@app.route('/api/check_sn/<sn>')
def api_check_sn(sn):
    """检查 SN 是否已存在（前端 AJAX 校验用）"""
    db = get_db()
    cur = db.execute("SELECT sn FROM materials WHERE sn = ?", (sn,)).fetchone()
    return jsonify({'exists': cur is not None})


# ===========================================================================
#                            启动入口
# ===========================================================================

def create_app():
    """应用工厂：初始化数据库并返回 app 实例"""
    with app.app_context():
        init_db()
        insert_sample_data()
    return app


if __name__ == '__main__':
    create_app()

    print("=" * 60)
    print("  物料全流程追溯系统")
    print("  本地访问: http://127.0.0.1:8080")
    print("  局域网访问: http://<本机IP>:8080")
    print("  按 Ctrl+C 停止服务器")
    print("=" * 60)

    app.run(host='0.0.0.0', port=8080, debug=True, threaded=True)
else:
    # gunicorn 导入时自动初始化
    create_app()
