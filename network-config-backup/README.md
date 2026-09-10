# Network Config Backup & Availability Monitor

三个协同工作的定时脚本：每日采集网络设备运行配置、按周期归档到 Git 仓库形成变更历史、每 5 分钟检测设备可达性并推送钉钉告警。三者共用同一套 crontab，组成一条完整的"配置留档 + 故障发现"链路。

## 解决的问题

网络设备的配置变更往往缺乏版本管理：谁改的、什么时候改的、改了什么，出问题后很难追溯。同时，设备离线（掉电、链路中断、被误操作重启）如果不能第一时间发现，会导致故障发现滞后。这套脚本用最简单的方式解决这两个问题：定时拉取配置 + 定期归档进 Git + 高频可达性探测。

## 架构设计

```
┌────────────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│ collect_configs.py │     │    git_configs.sh     │     │   ping_monitor.py   │
│  每天02:00执行       │     │   每28天04:00执行      │     │    每5分钟执行        │
│                     │     │                       │     │                     │
│ Nornir并发登录设备   │ --> │  rsync同步当日配置到   │     │  icmplib并发Ping     │
│ 按厂商执行对应命令   │     │  git工作区，清理超过   │     │  监控清单中的设备     │
│ 获取running-config  │     │  保留期的历史目录，    │     │  不可达则汇总推送     │
│ 与版本信息          │     │  commit+push到远端     │     │  钉钉告警            │
│                     │     │  仓库                  │     │                     │
│ 按日期/站点归档为txt │     │                       │     │                     │
└────────────────────┘     └──────────────────────┘     └─────────────────────┘
        │                                                          │
        ▼                                                          ▼
 /home/network_device_configs/                              钉钉群机器人告警
   YYYY-MM-DD/<site>/*.txt
```

- **collect_configs.py**：基于 Nornir + Netmiko 并发连接设备清单中的全部主机，按平台（Cisco / 华为 / H3C）自动选择对应的 `show running-config` / `display current-configuration` 及版本查询命令，解析出软件版本号后，将配置写入 `日期/站点/设备名_IP_型号_版本_时间.txt`，每份文件头部附带采集元信息，方便脱离脚本直接检索。
- **git_configs.sh**：定期（默认每 28 天，基准日与周期均可调整）将本地积累的按日期归档目录整体同步进一个独立的 Git 工作区并推送到远端仓库，形成可 diff、可追溯的配置变更历史；用 `flock` 防止并发执行，用日期比较自动清理超过保留期（默认 90 天）的历史目录，避免仓库无限增长。
- **ping_monitor.py**：对配置中的目标 IP 列表做高频（5 分钟一次）并发 ICMP 探测，检测到不可达设备时汇总成一条 Markdown 消息推送到钉钉群机器人，避免逐台发送刷屏。

## 关键工程实践

- **凭据与资产信息完全外置**：设备清单（IP、账号密码、厂商分组）通过 Nornir 的 `hosts.yaml` / `defaults.yaml` 管理，脚本本身不包含任何真实资产信息；钉钉 Webhook、Git 仓库地址等均通过环境变量注入。
- **按厂商差异化适配**：同一套采集脚本通过 `platform` 字段自动切换 Cisco / 华为 / H3C 三种设备的命令语法与版本号正则，新增厂商只需扩展 `if/elif` 分支。
- **归档粒度设计**：文件名固定包含设备名、IP、型号、版本、采集时间，任何一次配置文件都能脱离目录结构单独定位设备与时间点。
- **Git 备份的幂等与安全性**：`git diff --cached --quiet` 判断本次是否有实际变更，无变更时直接跳过 commit，避免产生空提交；`flock` 防止 cron 重叠调度导致的并发写冲突。
- **告警降噪**：Ping 监控采用"只在有不可达设备时才发送一条汇总消息"的策略，而不是每台设备单独告警，避免批量故障时刷屏。

## 环境要求

- Python 3.6+（`ping_monitor.py` 生产环境使用 3.9，但代码本身兼容 3.6+）
- 待采集设备已开启 SSH，且账号具备只读的配置查看权限
- 一个可访问的 Git 仓库（GitHub / GitLab 私有仓库均可）用于配置备份
- 钉钉自定义机器人 Webhook

## 安装

```bash
git clone <your-repo-url>
cd network-config-backup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 Git 仓库地址、钉钉 Webhook、监控目标清单

cp config/hosts.yaml.example config/hosts.yaml
cp config/defaults.yaml.example config/defaults.yaml
# 编辑 hosts.yaml / defaults.yaml，填入真实设备清单与登录凭据
```

## 使用

```bash
# 采集全部设备当前配置
python3 collect_configs.py

# 将本地配置归档目录同步到 Git 远端仓库
./git_configs.sh
# 或带变更说明手动触发
./git_configs.sh --manual "核心交换机上联链路调整后备份"

# 单次可达性检测
python3 ping_monitor.py
```

## 定时执行

参考 `crontab.example`，按需修改脚本路径后加入 crontab：

```cron
# 每天凌晨2点采集设备配置
0 2 * * * python3 /opt/network-config-backup/collect_configs.py >> /home/network_device_configs/cron.log 2>&1

# 每28天凌晨4点全量Git备份（基准日按部署时间调整）
0 4 * * * [ $(( ($(date +\%j) - 107) \% 28 )) -eq 0 ] && /opt/network-config-backup/git_configs.sh >> /home/network_device_configs/git_backup.log 2>&1

# 每5分钟检测设备可达性
*/5 * * * * /usr/bin/python3.9 /opt/network-config-backup/ping_monitor.py >> /var/log/ping_monitor/monitor.log 2>&1
```

## 免责声明

本项目为个人网络自动化作品集示例，脱敏自实际生产环境使用的脚本（已移除真实设备清单、账号密码、Git 仓库凭据与钉钉 Webhook）。使用前请替换为自己的设备清单与环境配置，并在非工作时间段测试后再投入生产使用。
