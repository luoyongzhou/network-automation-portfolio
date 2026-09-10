# LibreNMS Alert Notifier

自动化脚本：轮询 LibreNMS 告警 API，结合数据库查询补充设备详情，通过钉钉机器人推送结构化告警通知，并对特定规则（链路 up→down）做增量/减量检测与分级重复提醒。

## 解决的问题

LibreNMS 自带的告警渠道信息量有限（缺少机房位置等业务上下文），且默认没有"持续告警周期性提醒"与"故障接口增量提醒"的能力，容易导致长时间未处理的严重告警被运维忽略。本脚本作为 LibreNMS 与钉钉之间的中间层，补充上下文并优化提醒策略。

## 核心逻辑

```
LibreNMS API 拉取活动告警
        │
        ▼
  与本地状态文件(state)比对
        │
   ┌────┴────┬─────────────┬──────────────┐
   ▼         ▼             ▼              ▼
 新告警    已恢复告警    增量/减量检测    周期性重复提醒
(立即推送) (发恢复通知)  (Rule 13专用)   (仅critical, 间隔10分钟)
```

- **告警状态机**：本地维护 `alert_state.json`，记录每条告警的首次触发时间、最后提醒时间。每轮执行都会 diff 当前活动告警与历史状态，分类为"新告警 / 持续告警 / 已恢复告警"。
- **增量/减量检测（Rule 13：链路 up→down）**：对于接口批量故障的场景，不是简单地重复推送同一条告警，而是持续对比"当前故障接口集合"与"上次记录的故障接口集合"，只在有新增故障接口或有接口恢复时才主动推送，并在消息中明确列出【新增故障】【持续故障】【已恢复接口】，附带每个接口的首次检测时间与故障时长。
- **周期性重复提醒**：仅对 `critical` 级别告警生效，按告警从首次触发起的持续时间计算，每 10 分钟提醒一次，避免长时间挂起的严重告警被刷屏的普通告警淹没。
- **数据库直查补充上下文**：LibreNMS API 返回的告警字段有限，脚本通过 `docker exec` 直接查询 MySQL 容器内的 `devices`/`locations`/`ports` 表，补充设备名称、机房位置、接口详情，不依赖 API 版本是否暴露这些字段。

## 环境要求

- Python 3.6+
- LibreNMS（已启用 API，并配置好告警规则）
- LibreNMS 数据库运行在 Docker 容器中（`docker exec` 方式查询；如非容器部署需自行调整 `DatabaseQuery` 的连接方式）
- 钉钉自定义机器人 Webhook

## 安装

```bash
git clone <your-repo-url>
cd librenms-alert-notifier
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入实际的 LibreNMS Token / 数据库密码 / 钉钉 Webhook
```

## 使用

```bash
python alert_notify.py
```

日志输出到 `logs/alert.log`，告警状态持久化在 `data/alert_state.json`。

## 定时执行

建议每分钟轮询一次：

```cron
* * * * * /usr/bin/python3 /opt/librenms-alert-notifier/alert_notify.py >> /opt/librenms-alert-notifier/logs/cron.log 2>&1
```

## 关于 Rule ID

脚本中的 `DETAIL_RULE_IDS = [13]` 对应作者生产环境里"链路状态 up→down"的告警规则 ID，规则 ID 因 LibreNMS 实例配置不同而不同，使用前请替换为自己环境中对应的规则 ID。

## 免责声明

本项目为个人网络自动化作品集示例，脱敏自实际生产环境使用的脚本（已移除真实的 API Token、数据库密码与 Webhook 地址）。使用前请替换为自己的环境配置。
