# 数据恢复操作指南

> 写给另一台电脑的 AI — 请严格按步骤执行，不要跳过任何一步

---

## 一、发生了什么

2026-06-11，主电脑运行 `sync_db.py` 时云端数据库是空的（刚重新部署），同步脚本把空云端当成了「权威源」，
将本地 24 条真实物料全部删除、所有出库/售后/故障/版本变更记录永久丢失。

**你那台电脑是恢复全部数据的唯一希望。**

---

## 二、当前云端状态（已确认）

- `https://daxsvg.pythonanywhere.com` 的下载 token 已被改为 `wlgz-sync-2026-BLOCKED`
- 同步脚本连上云端会收到 HTTP 403，**自动跳过，不碰本地数据**
- 云端现存：5 条示例数据 + 23 条仅 SN 的恢复数据（无关联记录）

---

## 三、你到公司后第一步（最紧急）

### 3.1 断开网络（最保险）

到了公司先把那台电脑的网线拔了或关 WiFi，确保不会自动连上云端。

### 3.2 立即备份

```bash
cd 项目目录
copy material.db material.db.SAFE_20260611
```

先确认备份里有完整数据：

```bash
python -c "import sqlite3; db=sqlite3.connect('material.db.SAFE_20260611'); db.row_factory=sqlite3.Row; print('物料:', db.execute('SELECT COUNT(*) FROM materials').fetchone()[0], '条'); print('出库:', db.execute('SELECT COUNT(*) FROM outbound_records').fetchone()[0], '条'); print('售后:', db.execute('SELECT COUNT(*) FROM after_sales_records').fetchone()[0], '条'); print('故障:', db.execute('SELECT COUNT(*) FROM fault_records').fetchone()[0], '条'); print('版本变更:', db.execute('SELECT COUNT(*) FROM version_changes').fetchone()[0], '条')"
```

如果物料数量远大于 5，且出库/售后记录不为 0，说明数据完好。

### 3.3 删除同步状态文件

```bash
del sync_state.json
```

这是关键一步！这个文件记录着「上次云端有哪些 SN」。删掉后，同步脚本会把本地独有的数据视为「本地新增」推送到云端，而不是「云端已删」同步删除本地。

---

## 四、恢复云端 token

在你那台电脑上，用浏览器登录 [pythonanywhere.com](https://www.pythonanywhere.com)，
账号跟你主电脑用的是同一个。

1. 顶部点 **Consoles** → 点已有的 Bash console（或新建一个）
2. 执行：

```bash
cd ~/WLGZ
sed -i "s/wlgz-sync-2026-BLOCKED/wlgz-sync-2026/" app.py
grep "wlgz-sync" app.py   # 确认变回了 wlgz-sync-2026
```

3. 顶部点 **Web** → 绿色 **Reload** 按钮

---

## 五、从你那台电脑推送数据到云端

确认网络已恢复、云端 token 已改回后：

```bash
cd 项目目录
python sync_db.py
```

正常情况下你会看到：

```
本地 XX 条 · 云端 28 条
发现本地新增 XX 条，推送到云端...
  ✓ SN-xxx
  ✓ SN-yyy
  ...
已推送 XX 条，重新拉取...
同步完成
```

这说明你的完整数据（含出库记录、售后工单、版本变更等）已经推送到云端了。

---

## 六、推送后验证

在浏览器打开 `https://daxsvg.pythonanywhere.com`：

1. 首页物料数量应该是你的真实数据量
2. 点进几个物料详情，确认出库记录、售后工单都在
3. 确认品类归属正确
4. 顶栏应该能看到「导出」「操作日志」「库存盘点」等新按钮

---

## 七、主电脑恢复

云端数据完整后，主电脑这边执行：

```bash
cd d:\WLGZ\WLGZ
del sync_state.json
python sync_db.py
```

就会把完整数据从云端拉回本地。

---

## 八、注意事项

| # | 事项 |
|---|------|
| 1 | **绝对不要在主电脑上再跑 sync_db.py**，直到你那台电脑先推送完毕 |
| 2 | sync_state.json 是罪魁祸首 — 推送前一定先删掉 |
| 3 | 推送完确认数据正确后，建议在云端手动跑一次备份：`python ~/WLGZ/backup.py` |
| 4 | 以后部署更新用 `git pull`，不要再重新 clone，避免云端数据库被重置 |
| 5 | 主电脑这边已经修了 4 个 Bug + 加了 6 个新功能（操作日志、CSV导出、客户复用、时间线、盘点、售后返还选择），你那台 pull 最新代码就能用 |

---

## 九、如果连你那台电脑的数据也有问题

立即停止一切操作，联系我排查。不要把唯一数据源连上云端。

---

生成时间：2026-06-11 01:00
