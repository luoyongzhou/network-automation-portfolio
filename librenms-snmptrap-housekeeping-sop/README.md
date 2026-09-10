# LibreNMS SNMP Trap 接收链路 / Housekeeping / 私有MIB接入 SOP

> 本仓库整理自对当前 `lab-librenms` 测试环境的真实核验结果（2026-07-17），
> 不是凭空编写的通用教程。涉及具体配置值、代码片段、日志证据的地方，
> 均来自容器内实际执行命令得到的真实输出。
>
> 与 [`librenms-ikuai-adapter`](../librenms-ikuai-adapter)
> 仓库的关系：`librenms-ikuai-adapter`存放的是**可直接应用到其他环境的配置文件**
> （`docker-compose.yml`、MIB文件、图标等）；本仓库存放的是**原理性的SOP文档**，
> 解释这些配置为什么这样设计、背后的机制是什么、遇到问题该怎么排查。

## 目录

1. [SNMP Trap接收数据链路](./01-SNMPTrap接收数据链路.md) —— 从设备发trap到落库/触发告警的完整链路，
   包含`snmptrapd`进程参数、`Dispatcher.php`路由逻辑、Handler匹配机制的真实代码级说明
2. [Housekeeping实践与建议](./02-Housekeeping实践与建议.md) —— 数据库层的定时清理机制（`eventlog`/
   `alert_log`等表的保留策略），以及今日新发现并处理的docker容器日志爆炸问题
3. [私有MIB接入建议与SOP](./03-私有MIB接入建议与SOP.md) —— 新厂商私有MIB接入的可执行清单，
   基于iKuai、H3C两个真实案例整理，包含持久化方式选择、Trap Handler开发的正确姿势

## 使用建议

- 本仓库文档假定读者已经了解Docker Compose基本操作，重点是LibreNMS自身的机制和本环境的实践经验
- 标注"⚠️ 规范要求"的地方是团队必须遵守的操作规范，标注"🔍 排障参考"的地方是遇到问题时的诊断思路
- 如果发现文档描述的状态与实际环境不一致（比如配置被后续操作修改过），
  请以实际核验结果为准，并考虑更新本文档——这些文档的价值在于"当前真实状态"，
  过时不更新的SOP比没有SOP更危险
