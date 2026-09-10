# LibreNMS 手工补齐性能指标 —— 方法论与可复用模板

## 这个仓库是什么

当 LibreNMS 对某台设备的 discovery 机制识别不到某个性能指标（典型场景：白牌/OEM
设备沿用了某厂商的私有 MIB 结构，但把私有企业号换成了自己的），而你只是想补齐
**少数几个关键指标**（比如 CPU、内存、温度），并不想为这一类设备专门维护一个
PHP OS 类时，可以用本仓库记录的方法：**直接向 LibreNMS 数据库对应表插入一条
采集记录**，交给 LibreNMS 已有的轮询机制（dispatcher）去持续维护。

这不是一个能自动生效的代码补丁，而是一套 **调试方法论 + 可复用 SQL 模板**。
是否要为某类设备写完整的 OS 定义（yaml + PHP 类），还是用这套方法手工插值，
取决于该类设备的数量和重要程度——见文末"适用场景判断"。

本仓库目前覆盖了两个指标的完整实操验证：**CPU（`processors` 表）** 和
**内存（`mempools` 表）**。两者根因相同、修复方式相同，见下方对应章节。

## 核心结论（TL;DR）

- LibreNMS 用**符号名**（如 `hwEntityCpuUsage`）查询 SNMP OID 时，符号到数字 OID
  的翻译由本地 `.mib` 文件决定，与目标设备实际用的企业号无关。如果设备把某厂商的
  私有 MIB 结构原样复用、只换了企业号（如华为 `2011` → 白牌厂商 `56813`），
  discovery 用符号名查询必然查不到数据（会打到原厂商的企业号上）。
- `os_discovery/*.yaml` 里 `processors` 模块的定义 **不会被任何代码读取**——
  LibreNMS 目前没有 yaml 驱动的 processors discovery 机制（跟 `storage`/`mempools`
  不同，那两个模块有 `hasYamlDiscovery()` 判断分支，`processors` 没有）。
  真正驱动 CPU 采集的是各 OS 对应 PHP 类（如 `LibreNMS\OS\Vrp`）里硬编码的
  `discoverProcessors()` 方法，OID 企业号也是硬编码在 PHP 代码里的。
- 因此，遇到这类"MIB 结构相同、企业号不同"的白牌设备，如果不想改代码，
  最优雅的方式是：**手工 snmpwalk 验证出正确的数字 OID → 禁用该设备该模块的
  discovery（见下方"⚠️ 必读"） → 直接 INSERT 进 `processors` 表 → 交给现有
  轮询机制持续刷新**。

## ⚠️ 必读：插库前必须先禁用对应模块的 discovery，否则记录会被静默删除

**这是本方法论最容易踩的坑，务必先做这一步，再插入数据。**

`Processor::runDiscovery()` 最终会调用 `LibreNMS\Model::sync()` →
`clean($device_id, $valid_ids)`（`LibreNMS/Model.php`），逻辑等价于：
```sql
DELETE FROM processors WHERE device_id = ? AND processor_id NOT IN (<本轮 discovery 产出的有效 id 列表>)
```
如果目标设备的 `discoverProcessors()`（如 `LibreNMS\OS\Vrp` 里硬编码
的方法）因为企业号不匹配 **永远返回空数组**，这条 DELETE 会退化成
`DELETE FROM processors WHERE device_id = ?`——**清空这台设备在
processors 表的全部记录，不区分是不是手工插入的**。

这个删除动作发生在**每一次** `processors` discovery 执行时，与轮询
（poll）周期无关。触发时机包括：
- LibreNMS 默认的 discovery 调度周期（官方默认 21600 秒 / 6 小时一次）
- 任何人手动执行 `lnms device:discover <device_id>`（不带 `-m` 参数，跑全部模块）
- 任何人手动执行 `lnms device:discover -m processors <device_id>`

也就是说：**插入记录后，最快几秒内（如果有人手动跑了一次discovery）、
最慢 6 小时内（等自动调度周期），记录会被无声清空，UI 上图表会重新变回
空白，没有任何报错或告警提示**，容易被误判为"又出问题了"、需要重新排查。

### 解决方式：用 `devices_attribs` 表禁用该设备该模块的 discovery（不影响 poll）

LibreNMS 支持按设备单独覆盖模块的启用状态，判断优先级为
`manual 覆盖 > device 覆盖 > os 覆盖 > global 配置`（源码见
`LibreNMS/Util/Polling/ModuleStatus.php::isEnabled()`）。`device` 这一层
读取的正是 `devices_attribs` 表：

```sql
-- 禁用该设备的 processors 模块 discovery（值为 '0'/'1' 分别表示禁用/启用）
-- 这条只影响 discovery，完全不影响 poll ——poll 走的是独立的 poll_processors 键，
-- 该设备现有的手工记录会继续被 poll 正常轮询刷新数值。
INSERT INTO devices_attribs (device_id, attrib_type, attrib_value)
VALUES ({{DEVICE_ID}}, 'discover_processors', '0');
```

**已在测试环境实测验证**：写入这条 attrib 后，执行 `lnms device:discover
{{DEVICE_ID}}`（完整跑全部模块），日志里确认不再出现
`#### Load discovery module processors ####`，两条手工记录安然无损；
随后执行 `lnms device:poll -m processors -vv {{DEVICE_ID}}`，数值正常刷新、
RRD 正常更新——证明这条 attrib 精确只挡住了 discovery，不影响 poll。

**已知的例外情况（绕过这条禁用规则的场景）**：`manual` 覆盖优先级比
`device` 覆盖更高。如果有人执行 `lnms device:discover -m processors
<device_id>`（显式用 `-m` 参数强制指定要跑的模块列表），会绕过
`devices_attribs` 的禁用设置，模块依然会执行、依然会清空记录。这是
LibreNMS 官方判断逻辑本身的设计（命令行显式指定的优先级最高），不是
本方法论能规避的，需要团队内部约定：**对这类设备不要用 `-m processors`
强制触发discovery**，日常自动调度和不带 `-m` 的常规discovery都是安全的。

正确的完整操作顺序应为：

```sql
-- 第 1 步：先禁用 discovery（顺序很重要，务必在插入 processors 记录之前做）
INSERT INTO devices_attribs (device_id, attrib_type, attrib_value)
VALUES ({{DEVICE_ID}}, 'discover_processors', '0');

-- 第 2 步：再插入 processors 记录（见下方模板）
```

### 内存（`mempools` 表）同样适用，且验证过是同一套清空机制

内存模块虽然走的是 LibreNMS 较新的模块化架构（`LibreNMS\Modules\Mempools`，
实现了 `Module` 接口，而不是 `processors` 那种老式 `includes/discovery` +
静态方法直调的写法），但 `discover()` 方法内部同样调用了
`$this->syncModels($os->getDevice(), 'mempools', $mempools)`（同一个
`SyncsModels` trait），行为和 `processors` **完全一致**：只要 OS 类的
`discoverMempools()` 因企业号不匹配返回空集合，一次 discovery 就会清空
该设备 `mempools` 表的全部记录。禁用方式完全同构，只是 attrib key 换成
`discover_mempools`：

```sql
INSERT INTO devices_attribs (device_id, attrib_type, attrib_value)
VALUES ({{DEVICE_ID}}, 'discover_mempools', '0');
```

**内存模块的一个额外注意点**：`mempools` 表比 `processors` 多了
`mempool_used_oid` / `mempool_free_oid` / `mempool_total_oid` 三个可选
OID 字段，poll 阶段的 `defaultPolling()` 会尝试用这些 OID 逐个 SNMP 查询、
再用 `fillUsage()` 反推缺失值（比如只给 `total_oid` + `perc_oid`，
`used`/`free` 能被自动算出）。**如果这些 OID 字段全部留空，poll 阶段就
没有任何数据源可查，`mempool_used`/`mempool_free` 会一直停留在插入时手
填的初始值，不会被真实刷新**——这是本方法论第一次验证 CPU 时没遇到的坑
（`processors` 表没有类似的多 OID 拆分设计），插内存记录时至少要把
`mempool_perc_oid` 和 `mempool_total_oid` 填对，才能让 poll 真正生效。

## 排查思路（如何一步步定位到"该插哪个表、哪个 OID"）

以下是完整的排查顺序，适用于任何"某个指标图表画不出来"的场景：

1. **确认 discovery 有没有跑起来、跑的是哪个模块**
   ```bash
   lnms device:discover -m processors -vv <device_id>
   ```
   注意模块名要用 LibreNMS 当前版本的真实模块名（如 LLDP 相关模块在新版本里
   叫 `discovery-protocols` 而不是 `lldp`，用错名字会静默退化为"跑全部模块"，
   容易误判"没有任何反应=模块不存在"）。

2. **看 discovery 日志走的是哪个 SNMP 查询，返回了什么**
   如果返回 `No Such Object`/`No Such Instance`，说明这个 OID 路径在目标设备上
   确实查不到，不是权限或超时问题。

3. **确认设备的 `sysObjectID`，判断它到底属于哪个企业号**
   ```bash
   snmpget -v2c -c <community> <host> 1.3.6.1.2.1.1.2.0
   ```
   如果企业号和 LibreNMS 判定的 OS（如 `vrp`）常见企业号不一致，大概率是白牌/OEM。

4. **在私有企业号下手工探测，验证目标指标是否真实存在**
   用官方 MIB 文件里记录的符号名对应的 OID 路径（把企业号换成设备实际的），
   小范围 `snmpwalk`（**务必加 `-t`/`-r` 超时重试限制，避免大范围 walk 拖慢/
   打满设备 SNMP agent**）：
   ```bash
   timeout 10 snmpwalk -v2c -c <community> -t 2 -r 1 <host> <数字OID路径>
   ```

5. **确认这个指标对应哪张业务表，而不是笼统丢进 Custom OID**
   - `processors` 表 → 概览页 CPU 区块、CPU 告警规则唯一数据源
   - `mempools` 表 → 概览页内存区块、内存告警规则唯一数据源
   - `sensors` 表（`sensor_class` 区分 temperature/voltage/fan 等）→ 概览页
     温度/电压/风扇区块
   - `customoids` 表 → 只出现在独立的 "Custom Graphs" 页面，与概览页、
     告警模板完全不关联，**不要**为了图省事把 CPU/内存/温度这类标准指标
     塞进 `customoids`，否则概览页和告警规则都用不上。

6. **禁用该模块对这台设备的 discovery（务必在插入记录之前做，见上方"⚠️ 必读"）**

7. **按对应表的字段规范手工 INSERT，然后手动跑一次轮询验证**
   ```bash
   lnms device:poll -m processors -vv <device_id>
   ```
   确认 SNMP 请求真的发出去了、数值刷新进了表、RRD 文件生成。

## 可复用 SQL 模板（processors 表 —— CPU）

```sql
-- 插入一条 processor 采集记录
-- 把下面的占位符替换成实际值:
--   <DEVICE_ID>      目标设备在 devices 表里的 device_id
--   <ENT_PHYS_INDEX> 该 CPU/板卡对应的 entPhysicalIndex（用 ENTITY-MIB::entPhysicalName 反查确认，别猜）
--   <FULL_OID>       完整数字 OID（不要用符号名，符号名会被本地 MIB 文件翻译到错误的企业号）
--   <DESCR>          描述文字，建议和同型号/同角色设备的现有命名习惯保持一致（如 "MPU Board 0 Processor"）
INSERT INTO processors
    (entPhysicalIndex, hrDeviceIndex, device_id, processor_oid, processor_index,
     processor_type, processor_usage, processor_descr, processor_precision, processor_perc_warn)
VALUES
    (<ENT_PHYS_INDEX>, NULL, <DEVICE_ID>, '<FULL_OID>', '<ENT_PHYS_INDEX>',
     'manual', 0, '<DESCR>', 1, 75);
```

`processor_type` 建议统一填 `manual`（而不是复用某个 OS 名字），方便日后
`SELECT * FROM processors WHERE processor_type='manual'` 一键找出所有靠这套
方法手工补齐的记录，避免和 discovery 自动生成的记录混淆、误清理。

删除记录（设备下线/板卡更换后清理）：
```sql
DELETE FROM processors WHERE processor_id = <PROCESSOR_ID>;
```
对应 RRD 文件路径规律为 `<rrd_dir>/<device_hostname>/processor-<processor_type>-<processor_index>.rrd`，
删库记录后建议同步清掉对应 RRD 文件，避免僵尸文件堆积（不清也不影响功能，只是磁盘占用）。

## 可复用 SQL 模板（mempools 表 —— 内存）

```sql
-- 插入一条 mempool 采集记录
-- 把下面的占位符替换成实际值:
--   <DEVICE_ID>       目标设备在 devices 表里的 device_id
--   <ENT_PHYS_INDEX>  该内存/板卡对应的 entPhysicalIndex（同 processors 表的取值方式）
--   <PERC_OID>        内存使用率百分比的完整数字 OID
--   <TOTAL_OID>       内存总容量的完整数字 OID（单位需要跟 <TOTAL_BYTES> 一致，通常是 bytes）
--   <DESCR>           描述文字（如 "MPU Board 0 Memory"）
--   <USED_BYTES> / <FREE_BYTES> / <TOTAL_BYTES>
--                      先手工 snmpget 出实际数值填进去做初始值，插入后立即执行一次
--                      poll（见下方"验证"），LibreNMS 会用 <PERC_OID> + <TOTAL_OID>
--                      通过 fillUsage() 反推出准确的 used/free，覆盖这里的初始估算值
INSERT INTO mempools
    (mempool_index, entPhysicalIndex, mempool_type, mempool_class, mempool_precision,
     mempool_descr, device_id, mempool_perc, mempool_perc_oid,
     mempool_used, mempool_free, mempool_total, mempool_total_oid, mempool_perc_warn, mempool_deleted)
VALUES
    ('<ENT_PHYS_INDEX>', <ENT_PHYS_INDEX>, 'manual', 'system', 1,
     '<DESCR>', <DEVICE_ID>, 0, '<PERC_OID>',
     <USED_BYTES>, <FREE_BYTES>, <TOTAL_BYTES>, '<TOTAL_OID>', 90, 0);
```

**务必填 `mempool_total_oid`（至少这一个），否则 poll 阶段无 OID 可查，
`mempool_used`/`mempool_free` 会永远停留在插入时的初始估算值，不会被真实
刷新**（详见上方"内存（mempools 表）同样适用"小节）。

删除记录：
```sql
DELETE FROM mempools WHERE mempool_id = <MEMPOOL_ID>;
```
对应 RRD 文件路径规律为
`<rrd_dir>/<device_hostname>/mempool-<mempool_type>-<mempool_class>-<mempool_index>.rrd`。

## 生效方式说明：要不要重启/重建

**不需要重启任何服务，不需要重建任何容器。**

- INSERT 这条 SQL 执行完立刻生效，下一次 dispatcher 的轮询（poll）周期
  （默认几分钟一次）就会自动扫描到这条新记录并发起 SNMP 请求，**前提是
  已经按上方"⚠️ 必读"章节先禁用了该模块对这台设备的 discovery**，否则
  discovery 周期一到（或任何人手动触发一次），记录会被清空，poll 也就无从
  刷起。
- 如果想立刻看到效果不想等自然轮询周期，手动跑一次：
  ```bash
  lnms device:poll -m processors -vv <device_id>
  # 内存指标对应:
  lnms device:poll -m mempools -vv <device_id>
  ```
  这条命令跑完，概览页刷新即可看到数据（RRD 已经写入）。
- 这套方法**完全不涉及**修改 LibreNMS 代码目录、`resources/definitions` 下的
  yaml 文件、镶像重建——纯粹是数据库里多了几行记录（`processors` 表 +
  `devices_attribs` 表各一条/两条），LibreNMS 服务本身"感知不到"这是手工
  加的还是 discovery 自动生成的，poll 阶段一视同仁去轮询；discovery 阶段
  因为有 `devices_attribs` 的禁用规则挡着，也不会去动它。

### 容器重启/重建后，这条记录还在不在？

取决于 LibreNMS 数据库容器的数据目录是不是持久化挂载的，和 LibreNMS 应用
本身、镜像版本无关（`processors` 表数据完全独立于应用代码）：

| 操作 | 记录是否保留 | 说明 |
|---|---|---|
| `docker restart <db容器>` | 保留 | 只是重启进程，磁盘数据不受影响 |
| `docker compose down && docker compose up -d`（不带 `-v`） | 保留 | 容器被删除重建，但只要数据库的 bind mount / named volume 指向的宿主机目录没被删，新容器挂载的还是同一份数据 |
| LibreNMS 应用容器（`librenms`/`dispatcher`）单独重建、升级镜像版本 | 保留 | 这些容器不持有数据库数据，`processors` 表在独立的 db 容器里 |
| `docker compose down -v`，或手动删除数据库挂载的宿主机目录 | **丢失** | 这是唯一会真正清空数据的操作，属于高风险操作，执行前需确认是否真的要清空全部监控数据（不止这几条手工记录，所有设备的历史数据都会丢） |

**排查前建议先确认一下自己环境的持久化方式**：
```bash
docker inspect <db容器名> --format '{{json .Mounts}}'
```
如果 `Type` 是 `bind` 或 `volume`，且对应的宿主机路径/volume 是长期存在的，
这条手工记录就是安全的、持久化的，跟 discovery 自动生成的记录没有任何区别。
如果数据库跑在容器内部临时文件系统上（没有任何挂载），那么**所有**数据本来
就是易失的，这条手工记录的持久性问题只是整个环境本身设计缺陷的一个体现，
需要先解决数据库持久化，而不是单独担心这几条记录。

## 局限性（诚实说明，别指望它能干什么）

- **不会自动应用到新设备。** 这条 INSERT 语句绑定的是具体的 `device_id`，
  新增第二台同款设备，`device_id` 一定不同，需要重新走一遍第 3~6 步排查流程，
  手动插一条新记录。这套方法不是"让 LibreNMS 学会识别这类设备"，只是
  "给这一台设备的这一个指标做人工兜底"。
- **不会跟随硬件变化自动纠正。** 如果设备换板卡导致 `entPhysicalIndex` 变化，
  这条硬编码的 OID 记录会失效（要么查不到值，要么查到别的东西），需要人工
  重新探测、更新记录。
- 改设备 IP 不受影响（记录绑定的是 `device_id` 不是 IP），但换设备型号/更换
  设备本体（哪怕保留同一个 IP，LibreNMS 内部 `device_id` 不变的情况下）需要
  重新核实 OID 是否还对得上。
- **必须配合 `devices_attribs` 禁用规则一起用**（见上方"⚠️ 必读"），单独插入
  `processors`/`sensors` 表记录、不禁用对应模块 discovery，记录会在下一次
  discovery 执行时被清空，这不是小概率边缘情况，是必然发生的。
- `devices_attribs` 的禁用规则可以被 `lnms device:discover -m <module>
  <device_id>` 这种显式指定模块的命令绕过（命令行手动指定的优先级最高）。
  需要团队内部约定：**对插过手工记录的这类设备，避免用 `-m` 显式指定该
  模块去手动触发discovery**。

## 适用场景判断：什么时候该走这条路，什么时候该写 PHP 类

| 场景 | 建议方式 |
|---|---|
| 只有 1~2 台特殊设备，且未来不会大批量采购同款 | 本仓库方法：手工插库 |
| 同款设备已有 3 台以上，或预期后续会持续采购接入 | 应该写独立 OS 定义（yaml + PHP 类），
让 discovery 自动识别，一次投入、长期免维护 |
| 只是临时排障、验证一个假设，不追求长期稳定性 | 本仓库方法，事后随时可删 |
| 需要覆盖的指标很多（CPU + 温度 + 风扇 + 电压 + 光模块等一整套） | 手工插库的维护成本会
指数上升，超过 3~4 个指标就该考虑写 PHP 类了 |

## 附：本方法在测试环境的验证记录

- 环境：LibreNMS 26.6.1，设备为一台贴牌白牌交换机（`sysDescr` 含 "FutureMatrix"
  厂商信息，`sysObjectID` 企业号为 `56813`，本质是基于华为 VRP 系统的 OEM 产品）。
- 该设备原本 CPU 图表空白，discovery 日志显示按 `LibreNMS\OS\Vrp` 硬编码的
  `2011` 企业号 OID 查询返回 `No Such Object`。
- 手工验证华为标准 MIB 结构在该设备的 `56813` 企业号下原样存在（对比同型号
  华为设备的两块 MPU 主控板 entPhysicalIndex，验证数值均能正常返回）。
- 按本文档流程插入 2 条 `processors` 记录（对应两块 MPU 板），手动触发一次
  poll，确认 SNMP 请求成功、数值正常写入、RRD 文件生成，概览页 CPU 图表
  恢复正常显示。
- **踩坑记录（已修正进正文，此处留痕供参考）**：最初版本的文档遗漏了
  "discovery 会清空手工记录"这一风险，读源码（`LibreNMS/DB/SyncsModels.php`
  + `LibreNMS/Model.php` 的 `sync()`/`clean()` 方法）确认后，在测试环境
  实测复现：插入记录后手动执行一次 `lnms device:discover -m processors
  <device_id>`，日志出现 `Processor Removed: ...` 事件记录，两条手工记录
  被整体 DELETE。随后验证了 `devices_attribs` 表的 `discover_processors=0`
  方案（源码依据：`LibreNMS/Util/ModuleList.php::moduleStatus()` 读取
  `$device->getAttrib("discover_$module_name")`），实测确认：写入该 attrib
  后，完整 discovery 不再加载 processors 模块，记录不受影响；poll 模块不受
  该 attrib 影响，正常刷新数值。此后文档已补充为"先禁用 discovery，再插
  记录"的正确顺序。

### 内存指标（mempools 表）的验证记录

- 同一台设备，排查 CPU 问题的次日，发现该设备内存图表
  同样空白。先确认这**不是**修复 CPU 那次改动引入的新问题：查
  `eventlog` 表该设备历史记录，完全没有任何 `mempool` 相关的新增/删除
  事件，说明内存 discovery 从这台设备第一次被 LibreNMS 监控开始就从未
  产出过任何数据，和 CPU 问题同源、同时存在，只是分两次被发现。
- 根因验证：`LibreNMS\OS\Vrp::discoverMempools()` 同样用符号名
  `hwEntityMemUsage`（对应 `HUAWEI-ENTITY-EXTENT-MIB` 里
  `hwEntityStateEntry` 表的第 7 列）查询，被本地 MIB 文件翻译到 `2011`
  企业号，该设备真实企业号 `56813` 下查询返回 `No Such Object`。
- 手工在 `56813` 企业号下验证：内存使用率（第 7 列）、内存总量（第 9 列，
  单位 bytes）均正常返回数值；确认该设备不支持 `hwEntityMemSizeMega`
  （第 19 列，MB 单位的备选字段），只能用 bytes 单位的第 9 列。
- 按"先禁用 discovery，再插记录"的正确顺序操作：先插入
  `devices_attribs`（`discover_mempools=0`），再插入 2 条 `mempools`
  记录（对应两块 MPU 板，初始 `used`/`free` 用手工 snmpget 值估算）。
- 验证 poll：执行 `lnms device:poll -m mempools -vv`，日志确认 SNMP 请求
  发出、`fillUsage()` 反推出的 `used`/`free` 值与手工估算值几乎一致（仅
  1 字节的取整误差），RRD 文件正常创建。
- 验证 discovery 不清空：执行完整 `lnms device:discover -vv`（不带
  `-m`，跑全部模块），日志确认不再出现 `Load discovery module mempools`，
  也没有 `Mempool Removed` 事件，2 条记录数值完整保留。
- 与 CPU 验证过程唯一的差异点：内存插入记录时最初只填了
  `mempool_perc_oid`，漏填 `mempool_total_oid`，导致第一次 poll 验证
  used/free 没有被刷新（一直是插入时的初始值）；补上 `mempool_total_oid`
  后问题解决。该坑已写入上方"内存（mempools 表）同样适用"小节和 SQL
  模板注释里，避免后续照抄时重复踩坑。
