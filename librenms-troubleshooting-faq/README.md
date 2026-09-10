# LibreNMS_F&Q_fork

本仓库记录在生产环境合并 [`librenms-ikuai-adapter`](../librenms-ikuai-adapter)
（iKuai 私有 MIB 适配 + snmptrapd 补全）改动过程中，实际遇到的问题、排查方法、根因和解决方案。
供后续在其他环境操作时参考，避免重复踩坑。

环境背景：宿主机 CentOS 7，LibreNMS 通过 docker-compose 部署
（`librenms` / `librenms-dispatcher` / `librenms-syslog` / `librenms-db` / `librenms-redis`），
23 台已纳管设备，告警通过 crontab 定时脚本 + LibreNMS API 实现。

---

## 目录

1. [合并 fork 改动的正确方式：不能直接覆盖 compose 文件](#1-合并-fork-改动的正确方式不能直接覆盖-compose-文件)
2. [CentOS 7 / Rocky 9.7 跨发行版是否存在兼容性问题](#2-centos-7--rocky-97-跨发行版是否存在兼容性问题)
3. [存量设备应用改动后 os 字段不会自动变更](#3-存量设备应用改动后-os-字段不会自动变更)
4. [私有 MIB 挂载了，但性能数据仍走通用 Linux/UCD 采集](#4-私有-mib-挂载了但性能数据仍走通用-linuxucd-采集)
5. [RRD 图表长期空白 / 断线：rrd.step 与实际轮询周期不匹配](#5-rrd-图表长期空白--断线rrdstep-与实际轮询周期不匹配)
6. [批量转换 RRD 文件时官方命令内存耗尽](#6-批量转换-rrd-文件时官方命令内存耗尽)
7. [SNMP Trap 端口映射存在，但实际未被任何进程监听](#7-snmp-trap-端口映射存在但实际未被任何进程监听)
8. [调研：加快轮询周期需要改哪些参数，对图表的影响](#8-调研加快轮询周期需要改哪些参数对图表的影响)
9. [GitLab 私有环境的一些操作经验](#9-gitlab-私有环境的一些操作经验)
10. [LibreNMS 通用采集能力边界：能拿到哪些模块的数据](#10-librenms-通用采集能力边界能拿到哪些模块的数据)

---

## 1. 合并 fork 改动的正确方式：不能直接覆盖 compose 文件

### 现象/需求
`zhangyu/LibreNMS_fork` 仓库提供了完整的 `docker-compose.yml`，但目标生产环境早已有一份内容完全不同、
承载了大量其他服务（Kafka、NetBox、Grafana 等）的 `docker-compose.yml`。

### 关键差异对比

| | 生产环境现状 | fork 仓库 |
|---|---|---|
| 项目名/容器名前缀 | `monitoring_` / `librenms-*` | `lab-librenms` / `lab-librenms-*` |
| `/data` 挂载方式 | **named volume** `monitoring_librenms-data` | **目录 bind mount** `./librenms:/data` |
| trap/syslog 组件 | 主容器直接开 162/514 端口 + 独立 `librenms-syslog` | 独立 `snmptrapd` 容器 |

### 结论
直接拿 fork 的 compose 文件替换会导致：
- 用不同容器名重新起一套**空的** LibreNMS（全新 DB、全新 `/data`），不会继承现有设备和历史数据。
- bind mount 在生产环境是空目录，容器会认为是全新安装重新初始化，现有 named volume 数据虽然不会
  被物理删除，但会因为挂载点不同而"看不到"。

### 正确做法
只合并**改动的增量部分**，不是整份文件替换：
1. 把 fork 仓库里的图标、OS 识别定义、私有 MIB 文件拷贝到生产环境一个新目录（如
   `/opt/monitoring/librenms-ikuai/`）。
2. 给现有 `librenms`、`librenms-dispatcher` 服务追加对应的 `volumes` 条目（挂载源换成拷贝后的绝对路径）
   和 `SNMP_EXTRA_MIB_DIRS` 环境变量。
3. `rrd.step`/`rrd.heartbeat` 等**动态配置**（存在数据库 `config` 表里的）不需要重启容器；
   volumes/environment 这类**静态配置**（写在 compose 文件里的）需要 `docker compose up -d` 重建对应容器才生效。
4. `docker compose up -d <service1> <service2>` 默认会沿依赖链重建关联服务（哪怕配置没变），
   如果不想波及无关容器，要加 `--no-deps`。

---

## 2. CentOS 7 / Rocky 9.7 跨发行版是否存在兼容性问题

### 结论：基本不存在，但发现一个真实的小问题

- 容器内运行的是镜像自带的 **Alpine Linux**，和宿主机是 CentOS 7 还是 Rocky 9.7 完全无关，
  bind mount 挂载的只是普通文件，不涉及跨发行版二进制兼容性。
- SELinux：如果是 **Permissive**（`getenforce` 确认），不会因为标签缺失拦截读取；
  如果是 **Enforcing**，需要给 bind mount 加 `:z`/`:Z` label 或者手动 `chcon`。
- **唯一真实问题**：fork 仓库里 `private-ikuai.mib` 文件用的是 **CRLF 换行符**（其余文件都是 LF）。
  net-snmp 的 MIB 解析器通常能兼容 CRLF，但不排除个别版本解析异常，建议统一转换：
  ```bash
  sed -i 's/\r$//' private-ikuai.mib
  ```

---

## 3. 存量设备应用改动后 os 字段不会自动变更

### 现象
挂载了 `os_detection/ikuai.yaml` 之后，已纳管的 iKuai 设备 `os` 字段仍显示 `linux`，
一度怀疑挂载没生效。

### 根因
LibreNMS 的 OS 识别只在 **discovery（设备发现）阶段**执行，不是每次 poll 都重新判定。
`docker compose up -d` 让挂载生效之后，**已经存在的设备不会自动触发一次新的 discovery**，
只会按原调度周期（通常每天一次）继续用旧的 `os` 值执行 poll。

### 验证/修复方法
```bash
# 注意：discovery.php 不能用 root 执行，必须用容器内 librenms 用户
docker exec -u librenms <container_name_librenms> php /opt/librenms/discovery.php -h <device_id> -d
```
执行后查询该设备 `os` 字段确认变为 `ikuai`、`icon` 变为 `ikuai.png`。

### 补充：确认了不存在"抢占"问题
查看 LibreNMS 源码 `LibreNMS\Modules\Core::detectOS()`：
```php
$generic_os = ['airos', 'freebsd', 'linux'];
// 遍历 os_defs 时，generic_os 里的这几个会被 deferred，放到所有其他厂商定义都不匹配后才兜底判定
```
所以新增的厂商专属定义（如 `ikuai`）理论上都会比 `linux`/`freebsd`/`airos` 优先命中，不存在互相抢占误判的问题，
唯一前提是要有一次新的 discovery 执行。

### 存量设备批量刷新的两种方式
- 等待下一次 dispatcher 自然调度周期的全量 discovery（无需人工介入，但生效时间不确定）。
- 按 `sysDescr like '%iKuai%'` 筛出具体设备列表，针对性手动执行上面的 discovery 命令。

---

## 4. 私有 MIB 挂载了，但性能数据仍走通用 Linux/UCD 采集

### 现象
`private-ikuai.mib`/`IKUAI-AP-MIB.mib` 已经挂载生效（`SNMP_EXTRA_MIB_DIRS` 配置正确），
但设备的 CPU/内存图表看到的还是官方 `UCD-RESOURCES-MIB`/`HOST-RESOURCES-MIB` 数据，
不是 iKuai 私有 MIB 里定义的专属指标（如 `sysCPUBusyGet`/`sysMemoryBusy`/`sysTemperature`，
OID 前缀 `1.3.6.1.4.1.27282.100.1.x`）。

### 根因
**私有 MIB 文件本身只是一个 OID 字典**（供 `snmpwalk`/`snmptranslate` 做名称转换用），
它不会让 LibreNMS 自动知道"该 walk 哪些私有 OID、按什么规则解析、写到哪张表"。
要让私有 MIB 里的指标真正被采集，必须有对应的 **discovery/polling 模块 PHP 代码**
（比如 `LibreNMS/OS/Ikuai.php`，实现 `ProcessorDiscovery`/传感器发现等接口），
这部分代码 fork 仓库里完全没有——`os_detection/ikuai.yaml` 只负责"贴标签"和"配图标"，
不负责"怎么采集"。

fork 仓库 README 里其实也提到了类似的缺失：
> 本仓库不包含 SNMP Trap 场景下的 iKuai 专属 Handler（CPU/内存/温度数值化处理），
> 那部分改动在测试环境中通过 docker cp 写入容器内部、未做持久化，已在容器重建后丢失

### 澄清：这不代表现有图表数据是假的
CPU/内存/接口流量图表能正常显示，是因为这台 iKuai 设备的 SNMP agent 本身是基于 net-snmp 的，
**标准 MIB（HOST-RESOURCES-MIB / UCD-SNMP-MIB / IF-MIB）本来就通着**，不需要任何私有 MIB 适配。
私有 MIB 里的数据目前只是"备而不用"，不影响标准图表的正确性。

### 可选后续方案（未实施，仅调研）
- **方案 A**：写一个真正的 `Ikuai.php` OS 类，覆盖私有 MIB 里的专属指标。工作量最大，最准确。
- **方案 B**：在 os yaml 里用 `snmpget`/`sensor` 定义方式直接声明单点 sensor，不用写 PHP 类。
  只能覆盖简单场景（如温度这种单值 sensor），复杂结构化数据覆盖不了。
- **方案 C**：放弃私有 MIB 集成，只保留"识别+图标"效果，性能数据继续走通用采集。

---

## 5. RRD 图表长期空白 / 断线：rrd.step 与实际轮询周期不匹配

这是本次排查中影响面最大的问题，**和 iKuai fork 合并完全无关**，是环境历史遗留的全局配置问题，
影响当时全部 23 台设备。记录下来因为排查方法和结论都有普适参考价值。

### 现象
- 数据库里 `processors`/`mempools`/`ports` 表的实时字段（如 `processor_usage`）看起来数值正常。
- 但图表（走 RRD 时序数据）却是空白或断线，`rrdtool fetch` 查看数据点全是 `-nan`。
- 不止 iKuai 设备，其他厂商设备（如 comware/vrp）也是同样现象——**证明是全局问题，不是私有 MIB 适配问题**。

### 排查方法（按此顺序，效率最高）
1. **先排除"没触发"的假象**：确认设备 `status=1`、`last_polled` 持续更新、`snmp_disable=0`。
2. **确认 SNMP 应答本身没问题**：手动 `snmpget`/`snmpwalk` 关键 OID，注意 HOST-RESOURCES-MIB 的
   `hrProcessorLoad` 用的 index 不是简单的 `.1`，而是较大的编号（如 `196608`），
   用错误 index 测试会得到"No Such Instance"的假阴性，容易误判。
3. **确认数据库落库正常**：查 `processors`/`mempools`/`ports` 表确认有实际数值和时间戳更新。
4. **单独检查 RRD 文件内部数据**：
   ```bash
   rrdtool info <file>.rrd | grep -E "step|heartbeat"
   rrdtool lastupdate <file>.rrd     # 看最新写入的真实值
   rrdtool fetch <file>.rrd AVERAGE -s <开始时间戳> -e <结束时间戳>   # 精确窗口，不要用相对时间，容易漏看最新点
   ```
5. **对比多台设备**：如果多台不同厂商设备表现一致，基本可以排除"这是某个特定 OS 定义/私有 MIB 的问题"，
   转向全局配置排查。
6. **核对真实轮询间隔 vs RRD step/heartbeat**：
   ```bash
   docker exec <dispatcher容器> tail -N /data/logs/librenms.log | grep "device:poll"
   # 记录连续几次poll的时间差，和 rrd info 里的 step/heartbeat 做对比
   ```

### 根因
```
config.rrd.step = 150       # 应该匹配轮询周期
config.rrd.heartbeat = 180  # 应该是 step 的约2倍
实际轮询周期 = 300秒（5分钟一轮，实测稳定在298~302秒）
```
RRD 的 `heartbeat` 定义了"两次数据写入之间允许的最大间隔"，超过这个间隔会把跨越的数据点标记为 UNKNOWN。
`180秒 < 实际300秒间隔`，导致**几乎每次真实数据写入都会触发心跳超时**，图表因此长期空白/断线。

官方文档 [1-Minute Polling](https://docs.librenms.org/Support/1-Minute-Polling/) 明确指出：
> Your polling MUST complete in the time you configure for the heartbeat step value.

社区也有真实案例（[Separate RRDcached processes](https://community.librenms.org/t/separate-rrdcached-processes/23561)）：
poller frequency 改变后没同步改 rrd step，导致图表直接不显示——现象和本次完全一致。

### 修复方案（两种，按需选择）

**方案A：官方迁移命令（保留历史数据，但要注意内存问题见下一节）**
```bash
# 1. 改数据库配置（不需要重启容器，动态生效）
UPDATE config SET config_value='300' WHERE config_name='rrd.step';
UPDATE config SET config_value='600' WHERE config_name='rrd.heartbeat';
# 2. 转换现有文件（会原地 dump/restore，不清空历史数据点，只改头部 step/heartbeat 元数据）
lnms maintenance:rrd-step all --confirm
```

**方案B：放弃历史数据一致性，直接删除重建（本次生产环境采用的方式）**
```bash
# 1. 务必先备份！
tar -czf rrd_backup_$(date +%Y%m%d_%H%M%S).tar.gz -C <volume路径> rrd
# 2. 改数据库配置（同上）
# 3. 删除所有.rrd文件（保留目录结构）
find /data/rrd -name '*.rrd' -delete
# 4. 等待自然轮询周期（至少2轮，约10分钟），LibreNMS会自动用新配置重建文件
```
方案B 会清空所有历史图表趋势线（只保留备份包里的旧数据，无法在线恢复），但操作简单、不会遇到方案A的内存问题。

### 对告警和 crontab 的影响：无
所有告警规则的 `builder` 条件全部基于数据库表字段（`processors.processor_usage`、
`mempools.mempool_perc`、`ports.ifOperStatus` 等实时快照值），完全不涉及 `.rrd` 文件。
crontab 告警脚本走 API + 直接查库，同样不涉及 RRD。这两条链路和 RRD 时序存储是**并行独立**的，
改 RRD 配置、删除 RRD 文件都不会影响告警判断逻辑或历史告警记录。

---

## 6. 批量转换 RRD 文件时官方命令内存耗尽

### 现象
执行 `lnms maintenance:rrd-step all --confirm` 处理到中途（约200多个文件后）报错：
```
PHP Fatal error: Allowed memory size of 268435456 bytes exhausted
```
即使把 `memory_limit` 从默认 256M 提高到 2048M（2GB），处理更多文件后依然会耗尽。

### 根因（推测）
`RrdProcess` 底层通过常驻子进程管道（`Symfony\Component\Process`）与 `rrdtool` 交互，
随着处理文件数增多，管道输出缓冲似乎没有被及时释放，内存占用随文件数线性增长，
不是简单调大 `memory_limit` 就能根治的架构性问题。

### 建议规避方式（未在生产环境验证到底）
- 按单个设备（`hostname` 而非 `all`）逐个执行，每次是独立进程，内存自然重置：
  ```bash
  for host in $(mysql ... -e "select hostname from devices"); do
    php artisan maintenance:rrd-step "$host" --confirm
  done
  ```
- 或者直接采用本文第5节的**方案B**（删除重建），规避这个命令。

---

## 7. SNMP Trap 端口映射存在，但实际未被任何进程监听

### 现象
`docker port librenms` 显示 `162/udp -> 0.0.0.0:162` 端口映射存在，
但容器内 `netstat`/`ss` 查不到任何进程在监听 162 端口。

### 根因
主 `librenms` 容器的 s6 服务列表里只有 `cron/nginx/php-fpm/snmpd/socklog`，**没有 `snmptrapd`**。
镜像本身支持 snmptrapd（`/etc/cont-init.d/08-svc-snmptrapd.sh` 初始化脚本、`/usr/sbin/snmptrapd`
二进制都存在），但需要显式设置环境变量 `SIDECAR_SNMPTRAPD=1` 才会被激活，当前 compose 配置里没有这个变量。

### 影响
**外部设备即使配置了往这台机器发 SNMP Trap，目前也会被静默丢弃**，不会进入 LibreNMS 事件系统。
端口映射的存在容易造成"trap 功能已经启用"的错觉。

### 后续方案（未实施，仅调研阶段）
fork 仓库 `docker-compose.yml` 里已经设计了独立的 `snmptrapd` 服务定义（`SIDECAR_SNMPTRAPD=1`，
与主容器共享 `/data` 和私有 MIB 挂载），可以参考它来补上这个能力，两种路径：
- **路径A**：给现有主容器加 `SIDECAR_SNMPTRAPD=1`，改动小，但主容器职责更重。
- **路径B**：按 fork 设计起一个独立 `snmptrapd` 容器，职责分离更彻底，但需要处理 162 端口迁移。

动手前还需确认：网络里是否真的有设备配置了 trap destination 指向这台机器；
现有告警规则是否有依赖 trap 事件类型的；私有 MIB 挂载是否需要同步给 trap 容器。

---

## 8. 调研：加快轮询周期需要改哪些参数，对图表的影响

（本节为纯调研内容，未在生产环境实施）

### 核心配置项（数据库 `config` 表，动态生效不需要重启容器）

| 配置项 | 作用 | 默认值 |
|---|---|---|
| `service_poller_frequency` | poll 轮询周期（秒），最终生效的配置项 | 300 |
| `poller_service_poll_frequency` | 旧版本向后兼容别名 | 300 |
| `service_poller_workers` | 并发 poll 的 worker 数 | 24 |
| `service_discovery_frequency` | discovery 周期（秒） | 21600（6小时） |
| `ping_rrd_step` | 纯 ICMP ping 检测周期 | 60 |

源码依据（`/opt/librenms/LibreNMS/service.py`）：
```python
poller = PollerConfig(24, 300)   # (workers, frequency)
self.poller.frequency = config.get("service_poller_frequency", ServiceConfig.poller.frequency)
```

### 关键结论：改轮询频率必须同步改 rrd.step/rrd.heartbeat，否则重现第5节的问题
如果只改 `service_poller_frequency`（比如改成 60 秒）而不同步改 `rrd.step`/`rrd.heartbeat`：
- 若心跳窗口仍然够宽松（比如没改，还是 600 秒），不会立刻出现 UNKNOWN，但会造成浪费——
  每分钟采集一次的数据，RRD 却按 5 分钟一个点归档存储，中间几次数据被静默丢弃只保留最后一次。
- 官方文档明确要求同步修改，比例关系一般是 `heartbeat = 2 × step`。

改成 1 分钟轮询的完整改动清单：
1. `service_poller_frequency` = `60`
2. `rrd.step` = `60`
3. `rrd.heartbeat` = `120`
4. 现有全部 `.rrd` 文件需要重建/转换（参考第5、6节的方法）
5. 评估 `service_poller_workers` 是否需要跟着调大（轮询频率提高，单位时间 SNMP 请求量、
   数据库/RRD 写入 IO 都会成倍增加）

### 对告警实时性的影响
不影响告警规则判断逻辑本身，但会让告警发现问题的延迟从"最多5分钟"缩短到"最多1分钟"，
这是加快轮询的直接收益。

### 更优的替代方案：SNMP Trap
相比"缩短轮询间隔换取更快感知"，SNMP Trap 是设备主动上报事件、接近零延迟，且不需要
持续高频轮询消耗资源。两者是互补关系：轮询适合采集性能趋势数据（CPU/内存/流量曲线），
Trap 适合感知离散的异常事件（链路 up/down、设备重启、温度越限等）。
本环境目前 Trap 接收链路实际是失效的（见第7节），这是比"提高轮询频率"更值得优先修复的方向。

---

## 9. GitLab 私有环境的一些操作经验

- 该 GitLab 实例是较新版本，`/session` API 已废弃（返回404），PAT（Personal Access Token）
  创建走前端 JS/GraphQL，直接摸 API 创建 PAT 未成功（GraphQL mutation 名称与官方 schema 不一致）。
- **账号密码可以直接用于 HTTPS Git 认证**：`git clone http://<user>:<password>@<gitlab-host>/<ns>/<repo>.git`，
  这是最简单可靠的免密替代方案，不需要额外申请 token。
- **Push-to-create 特性**：如果目标项目在 GitLab 上不存在，直接 `git push` 到一个新的项目路径，
  GitLab 会自动创建该项目（默认私有可见性），无需先在 Web UI 上手动新建：
  ```bash
  git remote add origin http://<user>:<password>@<gitlab-host>/<ns>/<new-repo>.git
  git push -u origin main   # 若项目不存在，GitLab会自动创建并返回创建成功提示
  ```
- SSH key 免密：如果需要，仍建议手动在 GitLab 网页 `Preferences → SSH Keys` 里添加，
  账号密码走 API 自动添加 SSH key（`/api/v4/user/keys`）在该版本下会返回 401（session cookie
  不能作为 API 认证凭据，必须用 PAT 或 SSH key 本身）。

---

## 10. LibreNMS 通用采集能力边界：能拿到哪些模块的数据

### 背景/需求
排查白牌 VRP 设备 CPU/内存缺失问题（详见另一独立仓库
[`librenms-manual-metric-patching`](../librenms-manual-metric-patching)）
的过程中，牵出一个更基础的问题：LibreNMS 到底能采集哪些数据，这个能力边界是不是随厂商变化。
本节基于容器内源码实测梳理（`LibreNMS/Modules/*.php`、`includes/discovery/*.inc.php`、
`includes/polling/*.inc.php`、`LibreNMS/OS/*.php`、`includes/html/pages/device/*` 全量列出后交叉比对），
不是查文档得出的结论。

### 结论：能力分三层，边界完全不同

**第一层：通用模块（约 50 个，厂商无关）**

这是 LibreNMS 核心代码对标准 SNMP MIB（BGP-MIB、OSPF-MIB、ENTITY-MIB、Q-BRIDGE-MIB 等 IETF/RFC
标准 MIB，以及 LLDP-MIB 这类事实标准）的统一实现，理论上对**任何**遵循这些标准 MIB 的设备都生效，
不区分厂商。按设备详情页 Tab 归类：

| 分类 | 模块 |
|---|---|
| 性能/资源 | Processors（CPU）、Mempools（内存）、Storage（磁盘/存储）、UcdDiskio（磁盘I/O）、HrDevice（标准HOST-RESOURCES-MIB）、Vminfo（虚拟内存）、Processes（进程列表） |
| 网络接口/二层 | Ports（端口流量/状态）、PortsStack（端口堆叠）、PortSecurity、Transceivers（光模块DDM）、Vlans、Stp（生成树）、MacAccounting、ArpTable、FdbTable |
| 三层/路由 | Routes、Ospf/Ospfv3、Isis、Mpls、Vrf、Ipv4Addresses/Ipv6Addresses、Ipv6Nd、BGP peers、IpSystemStats |
| 硬件健康 | Sensors（统一传感器框架，覆盖温度/风扇/电压/电流/功率/湿度/气流/信号强度/dBm/频率/压力/水流等30余种子类型）、EntityPhysical、EntityState |
| 服务质量/可用性 | Slas（Cisco IP SLA / 华为 NQA 等主动探测，含 ICMP echo/jitter/timestamp/RTT）、Qos、Availability、Netstats |
| 协议邻居发现 | discovery-protocols（LLDP/CDP）、Nac |
| 其他专用场景 | Wireless、Xdsl、LoadBalancer、Pseudowires、Mef、PrinterSupplies、Toner、Services（自定义TCP/UDP可用性检测） |
| 应用层 | Applications（84 种内置插件，需在被监控主机部署脚本，仅适用于可运行脚本的 Linux/BSD 主机） |
| 核心/基础 | Core（基础发现流程）、Os（操作系统识别） |

**第二层：OS 专属适配类（229 个文件，`LibreNMS/OS/*.php`）**

每个厂商/系统对应一个适配类（如 `Vrp.php` 对应华为 VRP、`Ios.php` 对应 Cisco IOS）。
关键认知：**这些类绝大多数不是"新增模块"，而是针对同一个通用模块覆写具体的 SNMP 查询逻辑**。
因为不同厂商即使实现同一功能（比如 CPU 使用率），用的 OID/MIB 结构可能不遵循标准
`HOST-RESOURCES-MIB`，而是走私有 MIB（华为走 `HUAWEI-ENTITY-EXTENT-MIB`），所以需要专门代码
把私有 OID"翻译"成通用模块能理解的数据。

这就是本仓库第 4 节 "iKuai 私有 MIB 挂载了但性能数据仍走通用 Linux/UCD 采集" 问题的通用化解释：
**私有 MIB 文件本身只是 OID 字典，不会让 LibreNMS 自动知道该 walk 哪些私有 OID、按什么规则解析**，
必须有对应的 OS 类（第二层）去消费它，只有 `os_detection` yaml 是不够的。

**第三层：厂商专属新增子模块（数十个，该厂商独有功能）**

和第二层不同，这些是其他厂商完全没有对应物的功能，比如 `cisco-otv`（Cisco 二层扩展协议）、
`cisco-qfp`（Cisco QFP转发芯片统计）、`aruba-controller`、`netscaler-vsvr`、`dell-powervault`
系列传感器。这些模块只对特定厂商设备生效，对华为/H3C 等设备完全用不上，属于正常现象，
不是缺失或 bug。

### 实际能不能触发某个 Tab，取决于三个条件（缺一不可）
1. 设备本身是否**开启**了对应功能（比如 NQA 测试要先在设备上配置，BGP 邻居要先建立）。
2. 该功能是否通过**标准 MIB 或已适配的私有 MIB** 暴露出来。
3. 如果走私有 MIB，是否存在对应的 **OS 专属适配类**（第二层）正确识别了这台设备的 OID 体系
   （企业号、sysObjectID 等）——白牌/OEM 设备最容易在这一步失败，因为适配类通常是按官方厂商
   企业号硬编码匹配的，OEM 改了企业号就完全识别不上，参见
   `zhangyu/librenms-manual-metric-patching` 仓库里的完整案例。

### 快速判断某台设备支持不支持某个模块
不要去猜 LibreNMS 支不支持，而是直接确认设备侧有没有暴露对应 MIB：
```bash
# BGP-MIB 根
snmpwalk -v2c -c <community> -t 3 -r 1 <device_ip> 1.3.6.1.2.1.15
# OSPF-MIB 根
snmpwalk -v2c -c <community> -t 3 -r 1 <device_ip> 1.3.6.1.2.1.14
```
有返回说明设备已开启且暴露了对应协议 MIB，下一次 discovery 会自动识别出来，不需要人工干预。
LibreNMS 支持的能力清单远比任何单台设备实际用上的多，瓶颈几乎总是在设备侧实际开启和暴露了什么，
不是 LibreNMS 这边的限制。

### 补充：约 1/3 的通用模块默认是全局关闭的，即使设备支持也不会自动采集

排查某台华为 VRP 设备路由表缺失时发现：即使设备本身完整暴露了路由表 MIB
（`snmpwalk` 能拿到 `ipCidrRouteTable`/`inetCidrRouteTable`/`ipRouteTable` 数据），
LibreNMS 依然不会自动采集，因为 `route` 模块的**全局默认开关就是关闭的**
（`resources/definitions/config_definitions.json` 里 `discovery_modules.route.default = false`）。

这不是 bug，是官方有意设计——部分模块默认关闭，是因为可能对存储/轮询性能有较大影响
（比如路由表在核心/边界设备上可能有成百上千条），需要用户按需显式打开。

**判断方法**（源码级实测，比猜测可靠）：
```bash
# 容器内执行,提取所有discovery_modules/poller_modules的默认开关状态
docker exec -u librenms <librenms容器> php -r "
require '/opt/librenms/vendor/autoload.php';
\$app = require '/opt/librenms/bootstrap/app.php';
\$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
\$json = json_decode(file_get_contents('/opt/librenms/resources/definitions/config_definitions.json'), true);
foreach (\$json['config'] as \$key => \$def) {
    if (strpos(\$key, 'discovery_modules.') === 0 && is_array(\$def) && (\$def['default'] ?? null) === false) {
        echo substr(\$key, strlen('discovery_modules.')) . PHP_EOL;
    }
}
"
```

**全局 discovery 默认关闭的模块（共17个，实测于 LibreNMS 26.6.1）：**
`applications`、`cisco-cef`、`cisco-otv`、`cisco-pw`、`cisco-qfp`、`discovery-arp`、
`entity-state`、`isis`、`junose-atm-vp`、`loadbalancers`、`mef`、`printer-supplies`、
**`route`（路由表）**、**`slas`（Cisco IP SLA / 华为NQA）**、`vminfo`、`vrf`、`xdsl`

**全局 poller 默认关闭的模块（共26个）：**
`arp-table`、`aruba-controller`、`cipsec-tunnels`、`cisco-ace-loadbalancer`、
`cisco-ace-serverfarms`、`cisco-cef`、`cisco-ipsec-flow-monitor`、`cisco-otv`、`cisco-qfp`、
`cisco-remote-access-monitor`、`cisco-vpdn`、`entity-state`、`ipv6-nd`、`isis`、
`junose-atm-vp`、`loadbalancers`、`mef`、`nac`、`netscaler-vsvr`、`port-security`、
`printer-supplies`、`slas`、`unix-agent`、`vlans`、`vminfo`、`xdsl`

**关键：不少 OS 定义会把某些全局关闭的模块重新打开**（在 `os_detection/<os>.yaml` 里
写 `discovery_modules: {xxx: true}` 覆盖全局值，优先级链 `manual > device > os > global`），
这解释了为什么同样默认关闭的 `slas` 模块，在 vrp/ios/junos 等设备上却有数据：

| 模块 | 被哪些 OS 重新打开 | 说明 |
|---|---|---|
| `slas` | 26个OS：Cisco系(ios/iosxe/iosxr/nxos等)、**vrp**、junos 等 | Cisco IP SLA / 华为 NQA 生态常用，官方帮开 |
| `cisco-cef`/`cisco-otv`/`cisco-pw` | 24~25个 Cisco 系 OS | Cisco 专属功能，仅 Cisco 系打开 |
| `vrf` | 29个OS：Cisco系 + junos + ericsson-ipos + srlinux + timos + vos | 运营商级设备常见，覆盖面最广 |
| `xdsl` | 15个OS：DSL厂商 + **vrp** + ios/iosxe | vrp 交换机场景实际不会有 DSL 数据，但开关是开的 |
| `applications`/`vminfo` | beagleboard/freebsd/linux/proxmox/vmware-esxi | 仅通用计算平台打开 |
| `printer-supplies` | 21个打印机厂商 OS | 仅打印机 |

**始终保持全局关闭、没有任何 OS 重新打开、需要手动开的模块**：
`route`、`isis`、`discovery-arp`、`entity-state`。`route` 就是本节开头的真实案例。

**如何手动开启（三种方式，影响面从大到小）：**
1. **Web UI 全局开**（Settings → Discovery/Poller → 对应 Discovery/Poller Modules 分组）：
   影响所有设备，等效于改数据库 `config` 表插入
   `discovery_modules.<module> = true` / `poller_modules.<module> = true`，不需要重启容器。
2. **按设备开**（写 `devices_attribs` 表 `(device_id, 'discover_<module>', '1')` 或
   `'poll_<module>'`）：只影响单台设备，优先级高于 OS/Global，最小影响面。
3. **命令行临时触发一次**（`lnms device:discover -m <module> <device_id>`）：注意模块名
   必须用代码里注册的**准确 key**（如 `route` 而非 `routes`/`routing`），否则命令会被
   静默忽略、回退执行其他模块而不报错，容易误判"没生效"。且这种方式只是一次性的，
   不会持续到下次自然调度周期，不能替代方案1/2。

### 补充：Discovery 和 Poller 是两套独立流程，别混为一谈

排查过程中容易把两者当成一回事，实际上职责、频率、开销完全不同：

| | Discovery（发现） | Poller（轮询） |
|---|---|---|
| 职责 | 找出设备上"有哪些可监控对象"（几块 CPU、几个端口、多少条 BGP 邻居） | 刷新已发现对象的"当前数值"（CPU用了多少%、端口流量多少） |
| 产出 | 往表里**插入/删除行**（决定"有没有这一行"），触发 `clean()`/`syncModels()` 清空未匹配记录 | 更新已有行的**数值字段**，写入 RRD 时序数据 |
| 默认周期 | `service_discovery_frequency`，默认 21600 秒（6小时） | `service_poller_frequency`，默认 300 秒（5分钟） |
| 开销 | 较重，需要重新 walk 大量 MIB 表找新增/消失对象 | 较轻，只查已知对象列表对应的数值 OID |

**两者的开关完全独立**（`devices_attribs` 里 `discover_<module>` 和 `poll_<module>`
是两个不同的 key），可以只关一边：比如只想让 discovery 不要清空手工插入的记录，
但让 poller 继续正常刷数值，只需要设 `discover_<module>=0`，不动 `poll_<module>`。

**不是所有模块都是"discover+poll"两段式**：路由表模块（`Routes.php`）的
`shouldPoll()` 直接硬编码 `return false`，因为路由表本身是"发现型"清单数据
（有哪些路由条目），没有需要持续刷新的实时数值，每次都是靠 discovery 全量重新 walk，
根本没有 poller 阶段。判断某个模块是否有 poll 阶段，同样建议直接翻源码
`LibreNMS/Modules/<Module>.php` 里的 `shouldPoll()` 方法确认，不要假设。

---

*本文档基于 2026-07-17 在生产环境实际排查、验证过程整理，所有结论均经过命令实测确认，
不是纯理论推测。第 10 节基于测试环境（`lab-librenms`）容器内源码交叉比对整理，同样经过实测验证。*
