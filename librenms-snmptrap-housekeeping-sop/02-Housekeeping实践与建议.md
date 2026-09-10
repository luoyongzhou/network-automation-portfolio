# Housekeeping 配置实践与建议（基于当前环境真实核验，2026-07-17）

> 本文档记录当前环境housekeeping机制的真实运作方式、今日新增的docker日志滚动改动，
> 以及后续建议。所有配置值和日志证据均来自容器内实际执行结果，不是凭记忆或官方文档默认值推测。

## 1. LibreNMS官方housekeeping机制（数据库层）

### 1.1 调度入口

由容器内crond驱动，真实crontab（`librenms`用户）：

```
15 0 * * * cd /opt/librenms/ && bash daily.sh
* * * * * php /opt/librenms/artisan schedule:run --no-ansi --no-interaction > /dev/null 2>&1
```

每天0:15执行一次`daily.sh cleanup`。真实执行日志见`/opt/librenms/logs/daily.log`
（容器内路径，对应宿主机`./librenms/logs/daily.log`），当前环境最近一次运行确认发生在2026-07-17 00:15。

### 1.2 daily.sh cleanup 具体清理项（真实源码）

```bash
cleanup)
    options=("refresh_alert_rules"
                   "refresh_device_groups"
                   "recalculate_device_dependencies"
                   "eventlog"
                   "authlog"
                   "callback"
                   "purgeusers"
                   "bill_data"
                   "alert_log"
                   "rrd_purge"
                   "ports_fdb"
                   "ports_nac"
                   "route"
                   "ports_purge")
    call_daily_php "${options[@]}"
;;
```

每个选项调用`php daily.php -f <选项>`，核心清理逻辑用`lock_and_purge()`函数，SQL模式为：

```sql
DELETE FROM <table> WHERE <时间字段> < DATE_SUB(NOW(), INTERVAL ? DAY)
```

`?`处的天数来自对应配置项，真实取值（当前环境，均为官方默认值，未做定制）：

| 数据类型 | 表 | 配置项 | 当前值 |
|---|---|---|---|
| 事件日志 | `eventlog` | `eventlog_purge` | 30天 |
| Syslog | `syslog` | `syslog_purge` | 30天 |
| 认证日志 | `authlog` | `authlog_purge` | 30天 |
| 路由表历史 | `route` | `route_purge` | 10天 |
| 端口FDB表 | `ports_fdb` | `ports_fdb_purge` | 10天 |
| 端口NAC表 | `ports_nac` | `ports_nac_purge` | 10天 |
| **告警日志** | `alert_log` | `alert_log_purge` | **365天** |
| RRD文件 | 文件系统 | `rrd_purge` | 0（未启用，官方默认也不启用） |

### 1.3 另一个独立的清理命令：孤儿记录清理

`app/Console/Commands/MaintenanceDatabaseCleanup.php`（`maintenance:cleanup-database`），
真实源码逻辑是清理`alerts`/`alert_log`表里`rule_id`指向已被删除的`alert_rules`记录，
**这不是按天数过期清理，是数据一致性清理**，与上面的定时purge机制是两套独立逻辑。

### 1.4 当前环境数据库实际数据跨度（2026-07-17查询结果）

| 表 | 记录数 | 最早记录 | 最新记录 |
|---|---|---|---|
| `eventlog` | 5524 | 2026-07-14 | 2026-07-17 |
| `alerts` | 5 | 2026-07-14 | 2026-07-17 |
| `alert_log` | 28 | 2026-07-14 | 2026-07-17 |

环境部署至今仅3天，尚未有任何数据触及30天/365天清理阈值，housekeeping尚未真实执行过purge动作
（daily.log里能看到`alert_log`相关的"Deleting..."提示语句，但因为数据都不满365天，实际删除行数为0）。

**结论：当前配置下，`eventlog`可审计窗口上限30天，`alert_log`（告警历史）可审计窗口上限365天，
这是官方默认设计的一致性差异（事件日志量大且价值随时间快速衰减，告警历史需要更长期保留做趋势分析），
不是本环境的定制。**

## 2. 新发现问题：docker容器日志爆炸（与数据库housekeeping无关的另一层风险）

### 2.1 问题现象（2026-07-17发现时的真实数据）

```
lab-librenms-snmptrapd 容器日志: 729 MB, 378万行
其余4个容器: 1-2 MB级别
```

### 2.2 根因（真实日志内容核验）

`snmptrapd`进程每收到一条trap，都会重新遍历解析`-M`参数指定的全部MIB目录
（覆盖官方内置MIB库 + iKuai私有MIB + H3C私有MIB共上千个文件）。由于iKuai和H3C的私有MIB
本身存在已知的`notifications`节点定义缺陷（在部分版本下解析异常，属于MIB文件本身的历史遗留问题），
每次重新解析都会打印几十行`Cannot resolve OID`/`Did not find`警告。

实测trap到达频率约每100秒一条（持续性、有规律的流量，不是异常风暴），
但由于每条trap都触发一次"全量MIB重新解析+打印警告"的放大效应，日志增长速度远超实际trap数量
（实测最近1小时新增约10.9万行）。

**⚠️ 规范要求：这是docker层面的日志驱动问题（`json-file`驱动默认没有大小限制），
和数据库housekeeping是完全独立的两套机制，排查"环境是否有数据/日志堆积风险"时必须分别检查，
不能因为数据库housekeeping配置正常就假设日志文件也没问题。**

### 2.3 已执行的处理（本次会话，止血措施，未修复MIB本身）

按照"不改动MIB节点解析逻辑本身，后续测试验证后再按需添加"的决定，
本次只对docker日志驱动做了滚动限制，`docker-compose.yml`所有5个服务新增：

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"   # snmptrapd服务单独设置为 "100m"，因其日志基数更大
    max-file: "5"
```

执行`docker compose up -d`后5个容器全部重建生效（真实验证`docker inspect`输出确认配置已生效），
旧的729MB日志随容器重建清除。重建后功能核验：snmptrapd进程正常拉起，
iKuai/H3C的MIB路径挂载和设备识别定义文件均确认未受影响。

**MIB本身的`notifications`节点解析缺陷未修复**——这是有意的决定：
不同net-snmp版本对这类节点定义的容错程度不同，贸然"补全"私有MIB的写法风险未知，
后续应采用逐个OID测试验证、只对确认感兴趣且需要告警的OID做修复或注册专属Handler，
而不是一次性全量处理。

## 3. 后续建议

1. **docker日志滚动配置已生效，需要定期观察**：`max-size 100m × max-file 5`意味着
   `snmptrapd`日志占用上限约500MB，如果MIB解析警告的产生速率显著高于预期
   （比如上线更多高频发trap的设备），应重新评估这个上限是否足够，
   或者考虑升级方案（见下方第4点）。

2. **`eventlog_purge`（当前30天）是否需要调整，取决于实际审计需求**：
   如果需要比对更长时间的告警前后事件上下文，可以用
   `lnms config:set eventlog_purge <天数>`调大，但要评估数据库增长量
   （当前3天已产生5524条eventlog记录，量级不小，调大保留期前建议先观察一段稳定期后的真实增长率）。

3. **中长期建议：从"打印警告"升级为"根治MIB解析问题"**：
   按你的决定，后续会逐个测试验证感兴趣的OID节点，建议这部分工作产出一份独立的
   "私有MIB质量清单"，记录哪些节点在当前net-snmp版本下能正确解析、哪些不能、
   处理方式是什么（修MIB文件本身 vs 忽略 vs 注册专属Handler绕过），
   避免"全量莽撞添加"重演，也避免每次都要重新调查同样的问题。

4. **可选的进一步止血方案（本次未采用，供后续参考）**：
   如果发现`max-size 100m`仍然不够用（比如接入更多高频trap设备后warning日志量继续膨胀），
   可以考虑给`snmptrapd`单独配置日志级别过滤（net-snmp本身支持`-Le`/`-Ls`等日志目标和级别参数），
   把这类"MIB解析警告"从stdout（会被docker捕获）改到独立文件+logrotate，
   实现更精细的日志分级管理，而不是简单粗暴的整体截断。这个方案改动更大，
   需要单独评估对现有排障能力（依赖stdout看实时日志）的影响，本次未实施。

## SOP标准动作清单

1. ⚠️ 判断环境是否有"数据堆积"风险，必须分别检查数据库housekeeping状态和docker日志文件大小，
   两者是独立机制，不能只查一个就下结论
2. ⚠️ 调整任何`*_purge`天数配置前，先查当前表的真实记录数和增长速率，评估调大保留期后的存储成本
3. ⚠️ 遇到日志异常增长，先定位是应用逻辑问题（比如本例的MIB重复解析）还是纯粹的流量问题，
   前者应该找根因（哪怕暂不修复也要记录清楚），后者才是单纯调整日志滚动参数
4. 🔍 排障参考：怀疑housekeeping没生效，先看`/opt/librenms/logs/daily.log`确认daily.sh是否按时运行，
   再用`lnms config:get <配置项>`核实真实生效的天数配置，最后查表的真实最早记录时间反推
