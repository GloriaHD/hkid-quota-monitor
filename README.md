# 香港身份证预约配额看板

监控香港入境处六大人事登记办事处的智能身份证预约配额，约 5 分钟检测一次，
放号时通过邮件 / 飞书群提醒订阅者。**第三方公益工具，非入境处官方服务；只做监控提醒，不做任何代抢代约。**

## 看板入口

- GitHub Pages：https://chen1111-a.github.io/hkid-quota-monitor/
- 内地手机入口（免翻墙）：https://cdn.jsdelivr.net/gh/chen1111-a/hkid-quota-monitor@main/index.html
  （该镜像按纯文本返回，微信内置浏览器等国内手机浏览器会自动按网页渲染；桌面 Chrome/Edge 会显示源码，请用 Pages 链接）

## 它怎么工作

```
cron-job.org（每5分钟）──▶ GitHub Actions
                            │  python -m quota_monitor.run
                            │  ├─ 抓入境处公开配额接口（只读，一次一请求）
                            │  ├─ 与上一轮快照 diff → 放号事件
                            │  └─ commit data/ + 刷新 jsDelivr 缓存
                            ▼
        index.html 看板（手机优先，5分钟自动刷新）
        邮件 + 飞书通知（放号事件触发，带防抖冷却）
```

- 接口结构：[docs/api-notes.md](docs/api-notes.md)
- 5 分钟触发配置：[docs/cron-setup.md](docs/cron-setup.md)

## 相比同类工具的改进

- **手机优先**：日期做行纵向滚动，6 办事处一屏放下，不用横向拖表格
- **色盲友好**：状态格颜色+文字双通道（有/少/满）
- **防通知轰炸**：官方接口存在负载均衡数据抖动（实测同一分钟内 304↔346 格波动），通知层带单格冷却期，不会每 5 分钟轰炸一次
- **内地直连**：看板镜像与订阅链路均不需要科学上网
- 深浅双主题、摘要卡直接回答「最早哪天能约」

## 声明

- 数据来自入境处公开配额查询页同源接口，抓取频率低于官方页面自身的自动刷新强度
- 仅供学习交流，禁止商用；请以[入境处官网](https://www.immd.gov.hk/hks/services/hkid/registration_appointment_booking.html)为准
- 订阅者邮箱加密存储，退订随时生效
- 运营上限：QQ 个人邮箱日发送额度有限（数百封/天量级），订阅规模接近该量级时应改用企业邮箱或专业发信服务
