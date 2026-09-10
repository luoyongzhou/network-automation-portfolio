# Network Automation Portfolio

个人网络自动化脚本作品集，收录日常网络运维中实际使用的自动化工具，脱敏后开源展示。

## 项目列表

| 项目 | 说明 | 技术栈 |
|---|---|---|
| [vsphere-ip-validator](./vsphere-ip-validator) | 定时同步 vCenter 虚机与网络扫描结果到 NetBox IPAM，自动维护 IP 地址生命周期状态 | Python, pyVmomi, pynetbox, ThreadPoolExecutor |
| [librenms-alert-notifier](./librenms-alert-notifier) | LibreNMS 告警转钉钉通知中间层，支持增量故障检测与分级重复提醒 | Python, LibreNMS API, MySQL, DingTalk Webhook |
| [network-config-backup](./network-config-backup) | 网络设备配置每日采集 + 定期 Git 归档 + 高频可达性监控告警 | Python, Nornir, Netmiko, icmplib, Git, DingTalk Webhook |
| [librenms-ikuai-adapter](./librenms-ikuai-adapter) | LibreNMS 补全独立 snmptrapd 容器 + iKuai 私有 MIB 设备识别/轮询适配 | Docker Compose, LibreNMS, SNMP |
| [librenms-troubleshooting-faq](./librenms-troubleshooting-faq) | LibreNMS 生产环境真实排障案例集（10个案例，含根因与代码级分析） | LibreNMS, Docker, SNMP, RRD |
| [librenms-manual-metric-patching](./librenms-manual-metric-patching) | 白牌设备性能指标手工补齐方法论 + 可复用 SQL 模板 | LibreNMS, MySQL |
| [librenms-snmptrap-housekeeping-sop](./librenms-snmptrap-housekeeping-sop) | SNMP Trap 接收链路 / Housekeeping / 私有 MIB 接入 SOP 文档 | LibreNMS, SNMP, Docker |

## 关于脱敏

以上项目均已移除：

- 生产环境的 API Key / Token / 数据库密码（改为通过 `.env` 环境变量注入，仓库内仅保留 `.env.example` 占位模板）
- 真实的内网 IP 地址与网段（改为示例网段，如 `10.0.0.0/24`、`vcenter.example.local`）
- 内部系统的实际访问地址

各子项目的 `.env.example` 列出了运行所需的完整配置项，克隆后按需填写即可在自己的环境中运行。

## License

MIT
