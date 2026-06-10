# 物料全流程追溯系统 — 项目状态摘要

> 生成日期：2026-06-10  
> 版本：v2.0  
> 部署地址：https://daxsvg.pythonanywhere.com

---

## 部署架构

```
这台电脑(开发) ──git push──▶ GitHub ──git pull──▶ PythonAnywhere
                   (代码)        DAx-svg/WLGZ       daxsvg

本地启动 ──sync_db.py──▶ https://daxsvg.pythonanywhere.com/api/db/download
                (数据库双向同步，全自动)
```

### 代码更新流程
1. 本机修改 → `git push origin master`
2. PythonAnywhere 网页 → Bash Console → `git pull origin master`
3. PythonAnywhere → Web 标签 → **Reload**

不需要在这台电脑安装任何 PythonAnywhere 工具，不需要登录。

---

## 已确认的 Bug

### 🔴 Bug 1：编辑 API 可绕过状态流转（严重）
- **位置**：`app.py:835` `api_edit_material()`
- **问题**：直接接受 `status` 参数写入数据库，可把"售后中"改为"在库"，跳过完整流程
- **后果**：售后工单变成孤儿记录，数据不一致

### 🟡 Bug 2：删除物料无状态检查（中等）
- **位置**：`app.py:1066-1077` `api_delete_material()`  
- **问题**：可直接删除"售后中"/"故障中"的物料，其他操作（出库、入库）都有检查
- **影响范围**：单个删除 + 批量删除

### 🟡 Bug 3：故障创建不检查已出库状态（中等）
- **位置**：`app.py:1021-1042` `api_fault()`
- **问题**：已出库的物料可创建故障记录，虽然修复后会恢复状态但业务上不合理

### 🟢 Bug 4：品类 ID 无存在性校验（低）
- **位置**：`app.py:728-732`
- **问题**：`category_id=99999` 也被接受，仅做了类型转换

---

## 逻辑设计问题

1. **售后完成强制自动出库** — 修好的设备不一定都要返给客户，缺少选择
2. **一维状态机** — 无法表达"已出库+售后中"等组合状态
3. **删除出库记录恢复状态不严谨** — 删除非最后一条记录也会恢复为"在库"
4. **批量出库非原子操作** — 部分成功部分失败时不回滚

---

## 建议新增功能

### 高优先级
- [ ] 操作日志（审计追溯）
- [ ] 数据导出 Excel
- [ ] 客户信息复用（出库时下拉选择历史客户）

### 中优先级
- [ ] 售后完成时选择"返还客户"或"回库"
- [ ] 物料状态历史时间线
- [ ] 库存盘点

### 低优先级
- [ ] 附件上传（售后/故障照片）
- [ ] 仪表板图表（Chart.js）
- [ ] SN 条码打印
- [ ] API 鉴权（目前仅 `/api/db/download` 有 token）

---

## 技术栈

- **后端**：Python Flask + SQLite
- **前端**：Bootstrap 5.3 + Bootstrap Icons（CDN: BootCDN）
- **数据库**：SQLite，journal_mode=DELETE（兼容 NFS）
- **部署**：PythonAnywhere + GitHub
- **依赖**：flask>=2.3.0, gunicorn>=21.2.0, tzdata

## 数据表

| 表名 | 用途 |
|------|------|
| categories | 二级品类管理（大类→小类） |
| materials | 物料主表（SN、版本、状态、品类） |
| outbound_records | 出库记录（快递、客户、寄回追踪） |
| after_sales_records | 售后工单 |
| fault_records | 故障记录 |
| version_changes | 软硬件版本变更历史 |

## 物料状态流转

```
在库 → 已出库 / 售后中 / 故障中
              ↑        ↑
              └────────┘  (修复/完成后恢复)
```
