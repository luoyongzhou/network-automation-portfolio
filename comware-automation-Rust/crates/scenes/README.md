# cwa-scenes

场景编排与 `cwa` 命令行入口。

对应 Python 版 [`scenes/`](../../../nornir-comware-automation/scenes/) 四个脚本 + `scripts/preview_bootstrap.py` 的 CLI 部分。

## 概述

把五个 Python 脚本入口统一为**一个二进制 + 子命令**：

| Python 脚本 | `cwa` 子命令 |
| :--- | :--- |
| `scripts/build_inventory.py` | `cwa groups` |
| `scripts/preview_bootstrap.py --scene X` | `cwa preview X` |
| `scenes/bootstrap_step1.py console\|preview` | `cwa console [--dry-run]` |
| `scenes/bootstrap_step2.py deploy\|preview` | `cwa enable-netconf [--dry-run]` |
| `scenes/test_ip_deploy_full.py test-netconf` | `cwa deploy-ip` |
| `scenes/test_ip_deploy_full.py rollback-netconf` | `cwa rollback-ip` |
| `scenes/test_ip_deploy_full.py preview` | `cwa deploy-ip --dry-run` |
| `scenes/test_ospf_deploy_full.py test-ospf` | `cwa deploy-ospf` |
| `scenes/test_ospf_deploy_full.py rollback-ospf` | `cwa rollback-ospf` |
| `scenes/test_ospf_deploy_full.py preview` | `cwa deploy-ospf --dry-run` |

> **CMD 路线未实现。** Python 版 `test_ip_deploy_full.py` 有 `test-cmd` / `rollback-cmd` 两个纯 CLI 命令，是与 NETCONF 路线并列的备选实现。本 crate 只实现了实际验证过的混合路线（CLI 建接口 + NETCONF 配 IP）。若需要纯 CLI 路线，`CmdLoopbackAtom` 已具备基础，补一个 CLI IP Atom 即可。

## 使用方法

```bash
# 全局选项，所有子命令可用
--root <PATH>        项目根目录，默认 "."
-d, --device <NAME>  指定设备，可重复；缺省为全部
--workers <N>        并发数，默认 10


# Group 继承展开
cwa groups

# 模板渲染预览
cwa preview bootstrap    -d H3C-SR88-01
cwa preview netconf-cmd  -d H3C-SR88-01

# 三阶段（均支持 --dry-run）
cwa console        --dry-run
cwa enable-netconf --dry-run
cwa deploy-ip      --dry-run
cwa deploy-ospf    --dry-run

# 回退
cwa rollback-ip
cwa rollback-ospf
```

日志级别通过环境变量控制：

```bash
RUST_LOG=debug cwa deploy-ip --dry-run
```

## 并发模型

对应 Python 版 Nornir 的 threaded runner（`config.yaml` 里 `num_workers: 10`）。

Python 版对 runner 的使用极浅——只有 `nr.run(task=...)` 和 worker 数配置，所以用 `tokio` + `Semaphore` 即可等价覆盖，且是真并发，不受 GIL 约束。

```rust
let sem = Arc::new(Semaphore::new(max_workers.max(1)));
for host in inventory.hosts.values().cloned() {
    handles.push(tokio::spawn(async move {
        let _permit = sem.acquire().await?;   // 并发上限
        task(host).await
    }));
}
```

### 各场景的并发策略

| 场景 | 策略 | 理由 |
| :--- | :--- | :--- |
| `console` | **串行** | 多台设备共用一台串口服务器，并发连接容易互相干扰；且开局是低频操作 |
| `enable-netconf` | 并发（`--workers`） | 各设备独立 SSH |
| `deploy-ip` / `deploy-ospf` | 顺序（当前实现） | 需要 `&SceneApi` 引用，跨线程传递需重构；设备规模上升后可优化 |
| `rollback-*` | 顺序 | 同上 |

> 已知的可优化点：IP / OSPF 场景目前是顺序执行。要并发化需要让 `SceneApi` 满足 `Send + Sync + 'static`（用 `Arc` 包装）。当前 3 台设备规模下顺序执行是可接受的，且顺序执行的日志更易读。

### 日志聚合

与 Python 版一致：并发执行期间各设备日志缓冲在 `TaskLog` 里，全部结束后统一输出，避免多设备日志交错。

```rust
pub struct TaskLog { lines: Vec<String> }

log.header("IP 下发: H3C-SR88-01");   // 加 ==== 分隔的标题
log.section("Loopback1 创建");         // 加 -- xxx -- 的小节
log.line("  已创建");
```

结果按设备名排序（`results.sort_by(...)`），保证输出顺序稳定可比对。

## 规划表（`plan.rs`）

### 与 Python 版的差异

Python 版把 IP 规划**硬编码在场景脚本里**：

```python
# scenes/test_ip_deploy_full.py
DEVICE_CONFIG = {
    "H3C-SR88-01": {"loopback": {...}, "interfaces": [...]},
    ...
}
```

而 `test_ospf_deploy_full.py` 通过 `from scenes.test_ip_deploy_full import DEVICE_CONFIG` **反向依赖它**，形成场景脚本之间的耦合——改 IP 场景会影响 OSPF 场景。

本 crate 改为外部化：

```
plan.yaml（可选）→ 不存在时回落内置默认值
```

内置默认值的数值与 Python 版 `DEVICE_CONFIG` **完全一致**，所以不提供 `plan.yaml` 时行为等价。

### `plan.yaml` 格式

```yaml
H3C-SR88-01:
  loopback:
    number: 1
    ip: 11.11.11.11
    mask: 255.255.255.255
  interfaces:
    - ifname: GigabitEthernet0/0/1
      ip: 10.0.0.0
      mask: 255.255.255.254
    - ifname: GigabitEthernet0/0/2
      ip: 10.0.0.2
      mask: 255.255.255.254
```

解析失败时产出 warning 并回落内置默认值，不中断——避免手写 YAML 的笔误导致整个流程失败。

## 阶段一：Console 开局（`step1_console.rs`）

对应 `scenes/bootstrap_step1.py`。

### 流程

```
渲染 cmd/bootstrap.j2（主线程完成）
        │
        │ 进入 spawn_blocking（ConsoleSession 是同步阻塞的）
        ▼
① ConsoleSession::connect(host)  ← 读 netmiko_oob 连接选项
② wake()             发 \r 唤醒
③ interrupt_ztp()    连发两次 \x03，再 \r，读一次吸收横幅
④ enter_system_view()
⑤ send_config_lines(&commands)   ← 逐行下发，含错误检测
⑥ finish()           发 end + quit
```

渲染在**主线程**完成后才进 `spawn_blocking`，因为 `SceneApi` 不是 `Send`。

### 为什么串行

多台设备通常共用一台串口服务器（`hosts.yaml` 里三台设备的 `netmiko_oob.hostname` 都是同一个地址，只有端口不同）。并发连接同一台串口服务器容易触发其连接数限制或产生串扰。

## 阶段二：SSH 使能 NETCONF（`step2_netconf.rs`）

对应 `scenes/bootstrap_step2.py`。

### 执行单元切分

模板渲染结果先经 `parse_preserving_order()` 切成执行单元，**严格保持模板原始顺序**：

```
渲染 cmd/netconf_cmd.j2
        ▼
① Independent(3 行): ["public-key local create rsa", "y", "512"]
② Independent(3 行): ["public-key local create dsa", "y", "512"]
③ Independent(2 行): ["public-key local create ecdsa secp256r1", "y"]
④ Normal(3 条):      ["netconf ssh server enable", "netconf ssh server port 830", "end"]
```

### 两种单元的处理差异

| 单元类型 | 连接 | 错误处理 |
| :--- | :--- | :--- |
| `Normal` | 每块一个新连接 | 任一条报错即中止整个场景 |
| `Independent` | 每块一个独立连接 | **忽略单行错误**（"发送即忘"） |

独立块忽略错误是必要的：`y` 和 `512` 这两行本身不是合法命令，作为独立命令发送必然产生非预期回显。Python 版用 `expect_string=r".*"` 达到同样效果。

```rust
match session.run(mode::CONFIG, line).await {
    Ok(_) => {}
    Err(e) => tracing::debug!(line = %line, error = %e, "独立块行执行返回异常（预期内）"),
}
```

## 阶段三之一：接口 IP（`step3_ip.rs`）

对应 `scenes/test_ip_deploy_full.py` 的 NETCONF 路线。

### 混合路径

H3C 的 NETCONF 不支持直接创建 Loopback 接口，所以：

```
① CLI（CmdLoopbackAtom）      建 Loopback 接口
② NETCONF 查询 ifindex        ← 接口刚创建，需拿真实索引
③ NETCONF（IpAddressAtom）    配 Loopback IP
④ NETCONF（IpAddressAtom）    配物理接口 IP
⑤ 保存场景快照
```

这不是设计洁癖上的"纯 NETCONF"方案，而是真实设备调试确认的工程取舍。

### 场景快照结构

把三个 Atom 的快照聚合成一个：

```rust
pub struct IpSceneSnapshot {
    pub loopback: Option<LoopbackSnapshot>,       // 接口本身
    pub loopback_ip: Option<IpAddressSnapshot>,   // Loopback 的 IP
    pub interfaces: Vec<IpAddressSnapshot>,       // 物理接口的 IP
}
```

落盘到 `state/snapshots/<device>_ip.json`。

### 回退顺序

```
① 物理接口 IP（逆序）  ← 与下发顺序相反
② Loopback IP
③ Loopback 接口本身   ← 仅当 existed_before == false
```

**物理接口只删 IP，不改 admin 状态**（幂等），与 Python 版一致——物理接口的 up/down 不是本次变更引入的，不该由回退改动。

### 回退失败时保留快照

```rust
if errors.is_empty() {
    store.remove(&host.name, SCOPE)?;   // 全部成功才删快照
} else {
    log.line("  快照保留，可修正后重试回退");
}
```

## 阶段三之二：OSPF（`step3_ospf.rs`）

对应 `scenes/test_ospf_deploy_full.py`。

### 配置构造

`build_ospf_config()` 与 Python 版 `build_ospf_config()` 逻辑一致：

| 项 | 取值 |
| :--- | :--- |
| `instance_name` | 固定 `"1"` |
| `router_id` | Loopback IP |
| 区域 | 单个 `area 0.0.0.0`，`AreaType = 0` |
| Loopback 接口 | 不设 `network_type`（用设备默认 broadcast） |
| 物理接口 | `network_type = 3`（P2P） |

### 下发时序

```
① NETCONF 连接 + 查询 ifindex
② build_ospf_config()
③ pre_check     ← 幂等判断（Python 版没有）
④ 保存快照       ← 先落盘再下发
⑤ deploy        ← merge + rollback-on-error
⑥ post_check    ← 查询进程是否存在
```

**先落盘再下发**：即使下发成功后进程被杀，快照仍在，可以回退。

### 回退时序

```
① 读快照 → 无则跳过
② NETCONF 连接
③ rollback_guard  ← 依赖检查（Python 版没有）
                     进程变更前已存在 → 阻止
④ plan_rollback   ← 纯函数，先打印计划
⑤ apply_rollback  ← continue-on-error
⑥ 删快照
```

### 预览含回退计划

`cwa deploy-ospf --dry-run` 会同时展示下发 XML 和回退计划：

```
  -- 回退计划（若需回退将执行） --
  1. 逐层删除 OSPF 进程 1（3 个接口、1 个区域、1 个进程），仅发索引列
```

预览时 ifindex 用占位值（9000 / 9001 / 9002），因为真实索引需要连设备。输出里有明确提示。

## 事务边界（重要）

`rollback-on-error` 只保证**单设备单次 RPC** 的原子性。

**不保证**："三台设备中两台成功一台失败"时自动回滚已成功的两台。

当前的处理与 Python 版一致：各设备独立执行，结果汇总在末尾。若出现部分成功，需要人工判断是否对已成功的设备执行 `rollback-*`。

汇总输出会明确标出每台设备的状态：

```
  [ OK ] H3C-SR88-01: SUCCESS
  [ OK ] H3C-SR88-02: SUCCESS
  [FAIL] H3C-SR88-03: FAILED
         原因: ...

  总计: 2/3 通过
```

进程退出码：全部成功返回 0，否则返回 1（便于脚本判断）。

应用层两阶段提交（先全部 `validate` 再统一 `commit`）是 `MIGRATION_PLAN.md` 里记录的待讨论项，未实现。`rustnetconf` 提供了 `validate()`，具备实现基础。

## 强约束：绝对禁止行为

### 禁止 1：在 `spawn_blocking` 外调用 `ConsoleSession`

它是同步阻塞的，会卡住 tokio 工作线程。

### 禁止 2：把 `SceneApi` 跨线程传递

它不是 `Send`（持有 `minijinja::Environment`）。渲染必须在主线程完成，再把结果字符串传进任务。

若要并发化 IP / OSPF 场景，需先用 `Arc` 包装并确认 `Environment` 的线程安全性。

### 禁止 3：回退失败时删除快照

快照是唯一的回退依据。失败时必须保留，让使用者能修正后重试。

### 禁止 4：下发后才保存快照

必须先落盘。`step3_ospf.rs` 是正确示范。若下发成功后进程被杀而快照未落盘，设备上的配置就没有回退依据了。

### 禁止 5：让独立块的错误中止整个场景

`y` / `512` 这类交互应答行必然产生非预期回显。中止会导致密钥生成永远无法完成。

## 弱约束：推荐做法

### 推荐 1：新场景先实现 `--dry-run` 再实现下发

预览路径不接触设备，可以先把渲染、配置构造、回退计划推导都验证一遍。

### 推荐 2：用 `TaskLog` 而非直接 `println!`

保证并发时日志不交错，且便于将来改为结构化输出。

### 推荐 3：场景的 `SCOPE` 常量集中定义

```rust
pub const SCOPE: &str = "ip";      // step3_ip
pub const SCOPE: &str = "ospf";    // step3_ospf
```

避免快照文件名在多处硬编码。

## 常见问题

### Q1：为什么 IP / OSPF 场景是顺序执行而不是并发？

因为它们需要 `&SceneApi`（渲染引擎），而 `SceneApi` 不是 `Send`。并发化需要用 `Arc` 包装并验证线程安全性。

当前 3 台设备规模下顺序执行可接受，且日志更易读。若设备规模上升成为瓶颈，这是明确的优化点。

`enable-netconf` 之所以能并发，是因为渲染在主线程完成后只传字符串进任务。

### Q2：`--workers` 对哪些场景有效？

只对 `enable-netconf`。`console` 是串行，IP / OSPF 是顺序执行。

### Q3：为什么没有 `test-cmd` / `rollback-cmd`（纯 CLI 路线）？

Python 版的 CMD 路线是与 NETCONF 路线并列的备选实现。本次重写只实现了实际验证过的混合路线。

若需要，`CmdLoopbackAtom` 已具备接口创建/删除能力，补一个 CLI IP Atom（用 `paths::CMD_IP_ADDRESS` / `CMD_IP_ROLLBACK` 模板）即可。

### Q4：预览模式的 ifindex 占位值会不会误导？

输出里有明确提示：`注意: 以下 ifindex 为占位值，实际下发时从设备查询`。

占位值选 9000+ 是为了明显区别于真实 ifindex（通常是小数字）。

### Q5：部分设备成功、部分失败时该怎么办？

汇总输出会标出每台的状态，退出码为 1。需要人工判断：

- 若失败设备未产生任何配置（如连接失败），无需处理
- 若失败设备产生了部分配置，对该设备执行 `cwa rollback-ip -d <device>`
- 已成功的设备是否回退，取决于业务上是否要求全部或全不
### Q6：怎么验证场景层？

`--dry-run` 路径完全不接触设备，可以验证渲染、配置构造、回退计划推导。见测试用例 T2.1 / T2.2 / T2.10。

真实下发与回退需要设备，见 T3 系列。
