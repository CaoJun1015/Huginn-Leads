# Huginn Leads 消息工作台

Windows 本地、单用户的 QQ 群与联系人消息线索工作台。NapCat 通过 OneBot v11 反向 WebSocket 连接本项目；NoneBot、API、后台处理和 WebUI 在同一个 Python 进程内运行。

## 安装

1. 确认 `D:\Software\nonebota2` 保留为回滚副本，且没有正在运行。
2. 双击 `install.cmd`。脚本在本目录创建新的 `.venv`，安装固定依赖并构建前端。
3. 将原实例 `.env` 复制到本目录；保留原 `ONEBOT_ACCESS_TOKEN`，并将 `LOG_LEVEL` 设为 `WARNING`。
4. 双击 `start.cmd`；窗口会打印可点击的 <http://127.0.0.1:18080>，服务就绪后也会自动打开浏览器。
5. 首次启动时，从黑色启动窗口读取一次性初始化码，在网页中设置至少 12 位的本地密码。
6. 按网页配置向导检查 Huginn Leads、NoneBot2、NapCatQQ 和 QQ 登录状态，然后刷新并开启允许采集的群或联系人。向导可以暂时跳过，并可从“运行与数据”重新打开。

NapCat 继续使用：`ws://127.0.0.1:18080/onebot/v11/ws`。首次刷新到的群和联系人默认关闭，只有手动开启后才会保存新消息。网页只显示 `ONEBOT_ACCESS_TOKEN` 是否已配置，不读取或返回令牌。

命令行只输出已启用来源的连接与消息保存摘要；未启用联系人的私聊正文不会写入数据库或日志。完整的接收记录请在 WebUI 的“原文与复查”查看。

## 业务模式

内置“电脑经销商模式”保留方向、配置、价格、数量和交付地区判断；采购任务中的交付地区指目标收货地，销售任务中指可交付地。业务模式可新增、复制、编辑和停用，内置模式还可重置。模板更新不自动改变旧任务，需在编辑任务时主动升级。

自定义字段支持单行文本、数字、单选、日期和开关。参与匹配的字段只做正文文字包含判断：出现即满足，未出现进入待核对。本版本不接入 LLM。

## 数据与恢复

- 数据库：`data\huginn-leads.db`
- 备份：`backups\huginn-leads-*.db`
- 恢复：先关闭工作台，再运行 `restore.cmd backups\备份文件名.db`

恢复工具会先校验备份，并把当前数据库保存成 `backups\before-restore-*.db`。不要同时启动本项目和旧 `D:\Software\nonebota2`，两者使用相同端口。

## 开发与验证

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\evaluate_samples.py
.\.venv\Scripts\python.exe scripts\benchmark.py
cd frontend
npm run typecheck
npm run build
```

项目不发送 QQ 消息、不下载附件、不调用云模型。只有用户明确开启的群和好友联系人消息会保存；陌生人、临时会话及未开启来源的正文不会入库，业务诊断也不保存这些正文或 Token。

交付记录见 [验收结果](docs/验收结果.md)、[已知限制](docs/已知限制.md) 和 [第三方组件与许可边界](THIRD_PARTY.md)。
