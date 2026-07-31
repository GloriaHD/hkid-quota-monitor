# 入境处配额接口笔记（2026-07-30 逆向）

## 接口
```
GET https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation?svcId=579&t=<毫秒时间戳>
```
- `svcId=579` = 智能身份证预约（ROP booking），来源：官方配额预览页 `eservices.es2.immd.gov.hk/es/quota-enquiry-client/?appId=579` 的 script.js 中 `enquiryURL` 常量
- 无需 Cookie / Token，直接 GET 即可；建议带 UA 和 Referer
- 官方前端自身 15 分钟自刷新（`refreshTime=9e5`），失败重试 3 次——我们 5 分钟一次远低于人肉 F5 强度
- 内地网络实测直连可达（无需代理）

## 响应结构（约 58KB）
| 字段 | 含义 |
|---|---|
| `data[]` | 576 行 = 6 办事处 × 96 天；每行 `{date: "MM/DD/YYYY", officeId, quotaR, quotaK}` |
| `quotaR` | 一般服务时段状态 CSS class：`quota-g` 充足 / `quota-y` 少量 / `quota-r` 已满 |
| `quotaK` | 延长服务时段，同上；`no-quotaK` = 该日不开放延长时段 |
| `office[]` | 6 个办事处元数据（三语名称/地址/交通提示/电话/服务时间） |
| `lastUpdateTime` | 官方数据更新时间 `MM/DD/YYYY HH:MM:SS`（港时） |
| `label` / `scheme` / `col` / `css` / `afterRender` | 前端渲染配置，可忽略 |

## 办事处
RHK 湾仔 / RKO 长沙湾 / RTK 将军澳 / FTO 火炭 / TMO 屯门 / YLO 元朗
（显示名取接口 district 字段，与官方预约系统一致；officeName 是「港岛办事处」这类内部称谓，勿用）

## 快照规范化（data/quota.json）
状态压缩为单字符：`g`充足 `y`少量 `r`已满 `x`不开放；
`quota[officeId][YYYY-MM-DD] = {R, K}`。样例原始响应见 docs/sample_response.json。
