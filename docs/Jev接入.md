# Jev 消息分类 API

Huginn Leads 的现有 FastAPI 服务提供 Jev 接口，无需另起服务或安装 SDK。
目前仅在明确调用分类接口时发送本次请求的文本、上下文和分类标准到 TypeSafe。
后台 QQ 消息处理仍使用本地规则；接口不会读取历史消息、自动写入线索或保存分类结果。

## 配置

在项目根目录 `.env` 中添加以下配置，然后重启 Huginn Leads：

```dotenv
TYPESAFE_API_KEY=你的TypeSafe密钥
TYPESAFE_MODEL=jev-1.13.0
```

API Key 只由后端读取，不会返回前端；请勿提交 `.env`。系统环境变量优先于 `.env`，
将 `TYPESAFE_API_KEY` 清空后重启可停用。默认固定模型版本以避免分类行为随别名升级而变化。

## 接口

两个接口均要求现有 Huginn 登录会话。

- `GET /api/ai/jev/status`：返回 `configured` 和 `model`，不调用上游。`configured` 仅表示密钥已填写，不代表连接成功。
- `POST /api/ai/jev/classify`：执行一次消息分类。须携带会话 Cookie、与服务地址一致的 `Origin` 和 `X-CSRF-Token`。

最小请求：

```json
{"text":"收两台 ThinkBook 14，有货的报个价"}
```

完整请求示例（可使用自己的标签和描述）：

```json
{
  "text": "收两台 ThinkBook 14，有货的报个价",
  "context": "",
  "categories": {
    "purchase": "发布者求购商品，想买入",
    "sell": "发布者出售商品，想卖出",
    "other": "其他内容或无法判断"
  },
  "confidence_threshold": 0.8
}
```

`text` 必填，去除首尾空白后最多 8,000 字符；`context` 可选，最多 4,000 字符。
`categories` 可选，支持 2–50 个类别，标签最多 64 字符，描述最多 500 字符，均不能为空。
默认类别为 `purchase`（求购）、`sell`（出售）、`both`（兼有供求）、`other`（其他）、`uncertain`（信息不足）。
自定义分类建议保留兜底类别；一条消息选择一个类别，多意图需定义相应类别。

响应示例（仅展示格式，不是实测结果）：

```json
{
  "model": "jev-1.13.0",
  "category": "purchase",
  "probabilities": {"purchase": 0.96, "sell": 0.02, "other": 0.02},
  "confidence": 0.94,
  "needs_review": false,
  "usage": {"input_tokens": 300, "output_tokens": 0}
}
```

置信度低于请求中的阈值，或选中 `uncertain` 时，`needs_review` 为 `true`。
0.8 只是初始阈值，需用真实中文消息评估；置信度不是业务准确率，接口不会自动执行后续动作。

## 在 Huginn 页面中试用

登录工作台后，在同一页面的浏览器开发者控制台运行以下代码。
这将把示例文本发送至 TypeSafe；浏览器无需也不应获得 TypeSafe API Key。

```javascript
const auth = await fetch('/api/auth/status').then(r => r.json());
const response = await fetch('/api/ai/jev/classify', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-CSRF-Token': auth.csrf_token,
  },
  body: JSON.stringify({text: '收两台 ThinkBook 14，有货的报个价'}),
});
console.log(response.status, await response.json());
```

服务端脚本接入时，先向 `/api/auth/login` 提交 `{"password":"本地工作台密码"}`，
携带正确 `Origin`，保存响应 Cookie 及 `csrf_token`，再调用分类接口。
无需初始化第二个账号；不要把本地工作台 API 暴露到公网。

## 错误与验证

错误沿用 `{"code":"...","message":"..."}` 结构：

| HTTP | code | 含义 |
|---|---|---|
| 401 / 403 | 现有认证错误码 | 未登录、Origin 或 CSRF 无效 |
| 422 | validation_error | 文本、类别或阈值格式不正确 |
| 503 | jev_not_configured | 未配置密钥 |
| 502 | jev_auth_failed | 上游密钥无效或无访问权限 |
| 429 | jev_rate_limited | 上游限流 |
| 503 | jev_unavailable | 上游暂时不可用 |
| 504 | jev_timeout | 上游网络操作超时 |
| 502 | jev_connection_failed / jev_upstream_error / jev_invalid_response | 网络、其他上游错误或响应格式异常 |

网络操作超时为 20 秒；同步 HTTP 调用在线程中执行，不阻塞事件循环。
一次请求只尝试一次上游调用；限流或暂时失败后由调用方退避重试，避免隐式重复计费。
上游错误正文不返回客户端。自动化测试模拟上游，不使用真实密钥、不发送真实 QQ 消息。

协议依据：[TypeSafe 官方 API](https://docs.typesafe.ai/api)、[模型规格](https://docs.typesafe.ai/models)。
