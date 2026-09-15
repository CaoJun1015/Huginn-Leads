# Huginn Leads

从 QQ 群和好友消息中筛选、整理并跟进业务线索的本地 Web 工作台。

Huginn Leads 通过 NapCatQQ 和 OneBot v11 接收消息，在本机完成来源授权、规则匹配、人工复核、线索管理和数据备份。项目默认不发送 QQ 消息、不调用云模型，也不会采集未明确启用的群或联系人。

> 当前处于早期版本，面向 Windows 本地单用户及个人非商业场景。

## 它能做什么

- 按 QQ 账号选择允许采集的群和好友联系人，新发现的来源默认关闭。
- 用业务模式和自定义字段创建采购、销售等关注任务。
- 将消息区分为命中、待核对、冲突和未命中，并保留判断依据。
- 搜索保留期内原文，人工补充遗漏线索并记录跟进状态。
- 查看 QQ 连接、处理积压和失败作业，管理保留期限与 SQLite 备份。
- 所有业务数据默认保存在本机，不下载聊天附件。

内置的“电脑经销商模式”可识别供求方向、配置、价格、数量和交付地区。自定义字段当前采用明确、可解释的文字包含规则，不使用 LLM 猜测缺失信息。

## 工作方式

```text
QQ / NapCatQQ
      │ OneBot v11 反向 WebSocket
      ▼
Huginn Leads
├─ NoneBot2：接收 QQ 事件
├─ 规则与任务处理
├─ FastAPI + WebUI
└─ SQLite + 本地备份
```

NapCatQQ 是独立的第三方 QQ 协议端，未包含在本仓库中。请从 [NapCatQQ 官方 Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载，并遵守其许可和使用要求。

## 快速开始

### 环境要求

- Windows 10/11
- Python 3.11
- Node.js 与 npm（仅首次安装和构建前端需要）
- 已安装并能登录 QQ 的 NapCatQQ

### 安装 Huginn Leads

```powershell
git clone <你的仓库地址>
cd huginn-leads
Copy-Item .env.example .env
```

打开 `.env`，将 `ONEBOT_ACCESS_TOKEN` 替换为你自己的长随机令牌，然后双击 `install.cmd`。安装完成后双击 `start.cmd`，浏览器将自动打开 <http://127.0.0.1:18080>。

首次启动时，根据终端显示的一次性初始化码设置至少 12 位的本地密码。

### 连接 NapCatQQ

在 NapCatQQ 中添加 OneBot v11 反向 WebSocket：

```text
URL: ws://127.0.0.1:18080/onebot/v11/ws
Token: 与 .env 中的 ONEBOT_ACCESS_TOKEN 相同
```

连接成功后，在 Huginn Leads 的首次配置向导中刷新群和联系人，并手动开启允许采集的来源。

## 数据与隐私

- 数据库：`data\huginn-leads.db`
- 备份：`backups\huginn-leads-*.db`
- 恢复：停止工作台后运行 `restore.cmd backups\备份文件名.db`

只有明确启用的群和好友联系人消息会写入数据库。陌生人、临时会话及未启用来源的正文不会保存；Token 不会返回给浏览器。数据库和普通备份默认未加密，请使用受控的 Windows 账户和磁盘保护。

## 当前限制

- 仅处理普通群消息和好友私聊文本，不进行 OCR 或附件下载。
- 自定义字段暂不理解同义词、否定表达、日期语义和数值区间。
- WebSocket 重连不会补回断线期间的历史消息。
- 当前是 Windows 本地单用户应用，不是公网或多租户服务。
- NapCatQQ 当前许可限制商业用途；商业化或对外分发前请单独核查授权。

完整说明见 [已知限制](docs/已知限制.md) 和 [第三方组件与许可边界](THIRD_PARTY.md)。

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

NoneBot2、API、后台处理和 WebUI 运行在同一个 Python 进程中。前端生产文件由 Vite 构建到 `src/huginn_leads/static`。

## 外部数据源

工作台可以从可配置的本机 SQLite 数据源只读导入通用业务记录，先保存到 Huginn 自己的数据库，再发布线索任务。业务字段由模式模板定义，Diaohuo 电脑库存只是内置示例，数据库路径、查询和字段映射都可替换。详见 [外部数据源](docs/外部数据源.md)。

## 项目状态

首版已通过后端自动测试、模拟规则样本、前端类型检查和生产构建。真实 QQ 多账号验证及长时间运行仍需继续测试，详见 [验收结果](docs/验收结果.md)。

本仓库尚未声明项目自身的开源许可证。在公开发布前，请根据预期的使用和贡献方式添加 `LICENSE`；第三方组件仍分别遵循其原始许可。
