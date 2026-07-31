# 香港身份证预约配额看板

![monitor](https://github.com/chen1111-a/hkid-quota-monitor/actions/workflows/monitor.yml/badge.svg)

监控香港入境处六大人事登记办事处的智能身份证预约配额，约 2 分钟检测一次，
放号时通过邮件 / 飞书群提醒订阅者。**第三方公益工具，非入境处官方服务；只做监控提醒，不做任何代抢代约。**

## 看板入口

- 看板：https://chen1111-a.github.io/hkid-quota-monitor/
  （内地网络对 github.io 时通时断，打不开时请配合加速器；jsDelivr/raw 直链带 nosniff 头只会显示源码，不要当网页入口用）
- 内地免翻墙的完整体验走**邮件订阅 + 飞书群**——通知链路全程国内直连，放号第一时间推到手机，看板只是辅助
- 想要稳定的内地网页入口：可将本仓库镜像到 Gitee 并开启 Gitee Pages（需实名认证，见自部署一节）

## 它怎么工作

```
cron-job.org（每2分钟）──▶ GitHub Actions
                            │  python -m quota_monitor.run
                            │  ├─ 抓入境处公开配额接口（只读，一次一请求）
                            │  ├─ 与上一轮快照 diff → 放号事件
                            │  └─ commit data/ + 刷新 jsDelivr 缓存
                            ▼
        index.html 看板（手机优先，90 秒自动刷新）
        邮件 + 飞书通知（放号事件触发，带防抖冷却）
```

- 接口结构：[docs/api-notes.md](docs/api-notes.md)
- 2 分钟触发配置：[docs/cron-setup.md](docs/cron-setup.md)

## 相比同类工具的改进

- **手机优先**：日期做行纵向滚动，6 办事处一屏放下，不用横向拖表格
- **色盲友好**：状态格颜色+文字双通道（有/少/满）
- **防通知轰炸**：官方接口存在负载均衡数据抖动（实测同一分钟内 304↔346 格波动），通知层带单格冷却期，不会每轮都轰炸一次
- **内地直连**：邮件订阅与飞书通知链路全程国内直达，无需科学上网
- 深浅双主题、摘要卡直接回答「最早哪天能约」

## 个性化订阅

订阅邮件里写上需求即可（看板「个性化订阅」按钮有预填模板）：

```
订阅 只看湾仔 长沙湾 2026-10-15之前
```

- 办事处：写官方地区名（湾仔/长沙湾/将军澳/火炭/屯门/元朗，旧称港岛/九龙也认），没写 = 全部
- 截止日期：支持 2026-10-15 / 2026/10/15 / 2026年10月15日，没写 = 不限
- 重发一封订阅邮件 = 更新偏好；确认信会回显系统解析到的范围，可核对
- 放号提醒只推你范围内的名额，范围外不打扰

## 分级提醒

编辑仓库根目录的 [config.json](config.json)（GitHub 网页上点铅笔图标即可）设置两条日期线：

**监测窗口 `monitor_before`**：只关心这天之前的名额，之后的（如 10 月、9 月下旬）
既不推送也不在看板高亮——实测这类占放号总量约三成，全是噪声。

- 名额日期早于 `urgent_before` → **大提醒**：🚨 邮件红头 + 飞书 @所有人 + 看板红色横幅
- 早于 `notice_before` → **小提醒**：🔔 前缀 + 看板黄色横幅
- 其余（仍在监测窗口内）→ 常规 🎫 提醒

改完无需重新部署：Pages 链路即时生效；jsDelivr 手机镜像随下一次数据提交刷新（最长约 20 分钟）。
注意 `urgent_before` 应早于 `notice_before`（填反会自动对调）；日期必须是 `YYYY-MM-DD` 格式（格式不对该行自动失效，不会误报）。

## 一键自部署（拥有一套自己的监控）

整套系统零服务器、零费用，6 步搭一套：

1. **Fork 本仓库**（右上角 Fork 按钮）
2. Fork 后进入自己仓库的 **Actions** 页 → 点绿色按钮启用 workflow（GitHub 对 fork 默认停用）
3. **Settings → Pages** → Source 选 `main` 分支根目录 → 保存，几分钟后看板就在 `https://你的用户名.github.io/hkid-quota-monitor/`（看板会自动识别你的仓库，无需改代码）
4. （要通知才需要）**Settings → Secrets and variables → Actions** 添加：
   | Secret | 内容 |
   |---|---|
   | `QQ_SMTP_USER` | 发信 QQ 邮箱地址 |
   | `QQ_SMTP_PASS` | QQ 邮箱 SMTP/IMAP 授权码（设置→账号→开启服务获取） |
   | `ADMIN_EMAIL` | 你自己的收件邮箱 |
   | `SUBSCRIBER_KEY` | Fernet 密钥，本地跑 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成 |
   | `FEISHU_WEBHOOK` | （可选）飞书群自定义机器人 webhook |
5. （要 2 分钟级才需要）照 [docs/cron-setup.md](docs/cron-setup.md) 配 cron-job.org；不配则走 15 分钟兜底调度
6. （开放订阅才需要）改 `index.html` 顶部三个常量：`OWNER_REPO` 改成你的 `用户名/仓库名`，`SUBSCRIBE_EMAIL` 填收件 QQ 邮箱，`FEISHU_GROUP_URL` 填飞书群链接。
   ⚠️ 不改 `OWNER_REPO` 时订阅入口会自动隐藏（防止 fork 的用户误把订阅信发给原作者），这是有意设计

## 声明

- 数据来自入境处公开配额查询页同源接口，抓取频率低于官方页面自身的自动刷新强度
- 仅供学习交流，禁止商用；请以[入境处官网](https://www.gov.hk/tc/residents/immigration/idcard/hkic/bookregidcard.htm)为准
- 订阅者邮箱加密存储，退订随时生效
- 运营上限：QQ 个人邮箱日发送额度有限（数百封/天量级），订阅规模接近该量级时应改用企业邮箱或专业发信服务
