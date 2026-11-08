# 中转站浏览器来源防护

现状：公开报名端点已有固定来源 CORS；AI `/gotc/v1/` 的 OPTIONS 默认拒绝跨域，但实际请求与 WebSocket 升级没有统一 Origin 校验。只依靠浏览器不允许读取响应，无法保证请求不会发送或计费。

采用应用入口统一校验：路径完成规范化后、API Key 认证与读取上传正文之前，允许缺少 Origin 的 SDK 或精确匹配 `GOTC_PUBLIC_ORIGIN` 的网页请求。拒绝空值、`null`、重复或其他 Origin；出现 Sec-Fetch-Site 时仅允许 `same-origin`、`none`。不信任 Host 或转发头推导可信来源。

合法预检不需要密钥，仅允许 GET、HEAD、POST 及有限请求头；返回精确来源、Vary: Origin，不使用通配符或 Cookie credentials。实际请求仍必须通过现有子密钥认证、团队权限、到期和限流校验。同一入口覆盖聊天、SSE、媒体、异步任务及 WebSocket；报名、管理 API 原有规则保持原样。

这限制浏览器跨站调用，不能防止已拿到密钥的人用 curl/SDK 省略或伪造 Origin。无 Origin 的 WorkBuddy 保持可用；第三方网页与 Origin:null 的 WebView 会被拒绝。若需要对外网页客户端，应单独配置明确可信来源，不能开放任意来源。

验证使用合成数据库和假上游：拒绝请求不进入上游、不新增计费；同源与无 Origin 正常；预检、错误响应、编码路径、重复头、流式及 WebSocket 前置拒绝；保留报名与管理接口回归。生产发布以当前线上源码为基线，只叠加 app.py 的入口变更与新 helper，保留其他聊天已发布的功能和当前上游 Key。
