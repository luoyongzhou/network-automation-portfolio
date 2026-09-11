# cwa-atoms

原子化配置变更单元：`pre_check → deploy → post_check → rollback` 四阶段生命周期。

对应 Python 版 [`atoms/`](../../../nornir-comware-automation/atoms/) 目录。

## 概述

一个 Atom 封装"单一资源的一次可逆变更"。四阶段的意义：

| 阶段 | 职责 | 缺失的后果 |
| :--- | :--- | :--- |
| `pre_check` | 幂等判断 + 采集变更前快照 | 重复下发、无快照可回退 |
| `deploy` | 下发变更 | — |
| `post_check` | 校验变更是否真的生效 | 报成功但设备上没生效 |
| `rollback` | 基于快照精确回退 | 失败后只能人工登录清理 |

本 crate 相对 Python 版做了三处**语义修正**和三处**能力补齐**，下面逐条说明。

## 使用方法

```rust
use cwa_atoms::{Atom, AtomStatus, SnapshotStore};
use cwa_atoms::h3c::{NetconfIpAddressAtom, IpAddressParams};

let atom = NetconfIpAddressAtom::new(&scene);
let params = IpAddressParams {
    ifindex: 1235,
    ipv4_address: "11.11.11.11".into(),
    ipv4_mask: "255.255.255.255".into(),
};

// 方式一：完整生命周期编排（含自动回退）
match atom.execute(&host, &mut nc, &params, true).await? {
    AtomStatus::Success                    => println!("成功"),
    AtomStatus::Skipped { reason }         => println!("幂等跳过: {reason}"),
    AtomStatus::Failed { reason }          => println!("失败: {reason}"),
    AtomStatus::RolledBack { reason, .. }  => println!("已回退: {reason}"),
    AtomStatus::RollbackBlocked { deps }   => println!("回退被阻止: {deps:?}"),
}

// 方式二：手动展开，以便拿到快照落盘
match atom.pre_check(&host, &mut nc, &params).await? {
    PreCheckOutcome::Ready { snapshot, desired } => {
        store.save(&host.name, "ip", &snapshot)?;   // 先落盘再下发
        atom.deploy(&host, &mut nc, &desired).await?;
        atom.post_check(&host, &mut nc, &desired).await?;
    }
    PreCheckOutcome::Skipped { reason } => { /* ... */ }
    PreCheckOutcome::Blocked { reason } => { /* ... */ }
}

// 预览回退计划（纯函数，不接触设备）
let plan = atom.plan_rollback(&host, &snapshot)?;
println!("{}", plan.describe());
```

## Atom trait

```rust
pub trait Atom {
    type Snapshot: Debug + Clone + Send;   // 变更前的原始状态
    type Desired:  Debug + Clone + Send;   // 期望达成的状态
    type Channel;                          // CliSession 或 NetconfSession
    type Params:   Debug + Clone + Send;   // 调用参数

    fn name(&self) -> &'static str;

    async fn pre_check(&self, host, channel, params)
        -> Result<PreCheckOutcome<Self::Snapshot, Self::Desired>, AtomError>;
    async fn deploy(&self, host, channel, desired) -> Result<(), AtomError>;
    async fn post_check(&self, host, channel, desired) -> Result<(), AtomError>;
    async fn rollback_guard(&self, host, channel, snapshot) -> Result<RollbackGuard, AtomError>;

    fn plan_rollback(&self, host, snapshot) -> Result<RollbackPlan, AtomError>;   // 纯函数
    async fn apply_rollback(&self, host, channel, plan) -> Result<(), AtomError>;

    // 默认实现：完整生命周期编排
    async fn execute(&self, host, channel, params, auto_rollback) -> Result<AtomStatus, AtomError>;
}
```

## 修正 ①：`desired` 参数语义

### Python 侧的问题

`atoms/base.py::execute()`：

```python
post_result = self.post_check(host, snapshot, kwargs, **kwargs)
```

形参名叫 `desired`（期望状态），实际传入的是 `kwargs`（原始调用参数字典），语义不一致（`need_to_discuss_prob.md` 延伸②）。

问题目前没暴露，是因为各 `_post_check_impl` 都改用 `snapshot` 里存的期望值做校验，没真正用这个参数。但这是隐藏的技术债——一旦有场景真需要一个语义清晰的"期望状态"对象，就得先决定它是什么、由谁构造。

### 本 crate 的处理

把"期望状态"提升为 trait 的**关联类型** `Desired`：

- 由 `pre_check` **产出**（与 `Snapshot` 一起返回）
- 由 `deploy` / `post_check` **消费**
- 不再复用参数字典

这样 `desired` 的语义在类型层面就被固定了，且编译器保证三个阶段拿到的是同一个类型。

## 修正 ②：快照语义统一

### Python 侧的问题

两条路线的快照语义不一致（延伸①）：

| 场景 | 快照内容 |
| :--- | :--- |
| IP（`test_ip_deploy_full.py`） | 变更前的原始状态（`current_ip` 可能为 `None`） |
| OSPF（`test_ospf_deploy_full.py`） | **期望配置全量**（整个 `ospf_config` 字典） |

两种语义混在同一个概念名下，若要合并到同一套框架必须先统一。

### 本 crate 的处理

**`Snapshot` 只记变更前状态，`Desired` 记期望状态，两者分离。**

以 IP Atom 为例：

```rust
pub struct IpAddressSnapshot {
    pub ifindex: i64,
    pub previous_ip: Option<String>,   // 变更前的 IP，None = 原本没配
    pub applied_ip: String,            // 本次下发的，回退时需据此删除
    pub applied_mask: String,
}
```

`applied_ip` 看起来像"期望状态"，但它在快照里的作用是**"回退时要删哪个"**——属于变更历史，不是期望。这个区分决定了 `plan_rollback` 能推导出正确的动作。

OSPF 同理：

```rust
pub struct OspfSnapshot {
    pub instance_existed_before: bool,   // 变更前进程是否已存在 ← 真正的"变更前状态"
    pub applied: OspfConfig,             // 本次下发的，用于推导删除步骤
}
```

`instance_existed_before` 是 Python 版完全没有采集的信息，而它恰好是回退安全性的判据（见补齐③）。

## 修正 ③：编译期强制完整实现

### Python 侧的问题

`atoms/h3c/netconf/interface_loopback.py` 的 `NetconfLoopbackAtom` 只实现了 `deploy_pre_check()` 和 `deploy()`，缺 `pre_check` / `post_check` / `rollback`，方法签名也与基类不一致。

因为 Python 的 `ABC` **只在实例化时**检查抽象方法，而这个类从未被实例化，所以这个半成品静默存在，不报任何错（延伸②）。

### 本 crate 的处理

Rust trait 在**编译期**强制所有方法实现。`NetconfLoopbackAtom` 那种状态**不可能编译通过**。

同时明确废弃纯 NETCONF 建 Loopback 的路线（Comware 不支持），统一走"CLI 建接口 + NETCONF 配 IP"混合路径，并把这条路径纳入 Atom（Python 版是散落在 `scenes/test_ip_deploy_full.py` 里手写的）。

## 补齐 ①：回退计划（`RollbackPlan`）

Python 版**只有快照，没有"快照 → 回退步骤"的映射**（延伸③深层）。回退步骤硬编码在 `_rollback_impl()` 里，既无法预览也无法审计。

### 数据结构

```rust
pub struct RollbackPlan { pub steps: Vec<RollbackStep> }

pub struct RollbackStep {
    pub description: String,      // 人类可读，用于预览与日志
    pub action: RollbackAction,   // 具体动作
}

pub enum RollbackAction {
    CliCommands(Vec<String>),
    NetconfFragment { fragment: String, continue_on_error: bool },
}
```

`plan_rollback()` 是**纯函数**（不接触设备），所以可以在 preview 模式下调用，展示"如果回退，将会执行什么"：

```bash
$ cwa deploy-ospf --dry-run -d H3C-SR88-01
...
  -- 回退计划（若需回退将执行） --
  1. 逐层删除 OSPF 进程 1（3 个接口、1 个区域、1 个进程），仅发索引列
```

### 快照 → 步骤的推导逻辑

IP Atom 的两个分支，正是"为什么需要这一层"的最好例证：

| 快照状态 | 推导出的动作 | `continue_on_error` |
| :--- | :--- | :--- |
| `previous_ip = Some("9.9.9.9")` | 恢复原 IP（`operation=merge`） | `false`（必须成功） |
| `previous_ip = None` | 删除本次下发的（`operation=delete`） | `true`（可能已被他人删除） |

Loopback Atom：

| 快照状态 | 推导出的动作 |
| :--- | :--- |
| `existed_before = false` | 删除接口（是本次创建的） |
| `existed_before = true` | **空计划**（不是本次创建的，不能删） |

`RollbackPlan::is_empty()` 让调用方能区分"无需回退"和"回退失败"。

## 补齐 ②：OSPF 纳入 Atom

Python 版 OSPF 完全绕过 Atom 抽象，手写在场景脚本的两个函数里，因此缺少：

- `pre_check` 幂等判断（merge 本身幂等，但脚本不会告诉你"其实什么都不用做"）
- 回退前依赖检查
- 与 IP 场景一致的快照语义

`NetconfOspfAtom` 补齐了这三项。

### 回退删除 XML

遵循 `scenes/summary.md` 第七章 v4 方案的两条硬性结论：

1. **只指定索引列**——多发字段会导致删除失败
2. **顺序必须是 接口 → 区域 → 进程**——反之会因对象仍被引用而失败

```xml
<OSPF xmlns="http://www.h3c.com/netconf/config:1.0">
  <Interfaces>
    <Interface xmlns:nc="..." nc:operation="delete">
      <IfIndex>9000</IfIndex>          <!-- 仅索引列 -->
    </Interface>
  </Interfaces>
  <Areas>
    <Area xmlns:nc="..." nc:operation="delete">
      <Name>1</Name>                    <!-- Name + AreaId 为索引列 -->
      <AreaId>0.0.0.0</AreaId>
    </Area>
  </Areas>
  <Instances>
    <Instance xmlns:nc="..." nc:operation="delete">
      <Name>1</Name>
    </Instance>
  </Instances>
</OSPF>
```

XML 由 `build_delete_xml()` 手工拼接而非模板渲染，因为删除结构与创建结构差异太大，共用模板会让模板里塞满 `{% if operation == 'delete' %}` 分支。

## 补齐 ③：OSPF 回退依赖检查

Python 版回退时**无条件删除整个 OSPF 进程**。若该进程在本次变更前就已存在（比如设备上原本就跑着 OSPF），回退会误删既有业务配置。

本 crate 用 `instance_existed_before` 做判据：

```rust
async fn rollback_guard(&self, _host, _channel, snapshot) -> Result<RollbackGuard, AtomError> {
    if snapshot.instance_existed_before {
        return Ok(RollbackGuard::Blocked {
            dependencies: vec![format!(
                "OSPF 进程 {} 在本次变更前已存在，非本次创建，拒绝删除",
                snapshot.applied.instance_name
            )],
        });
    }
    Ok(RollbackGuard::Safe)
}
```

下发时若检测到进程已存在，会提前告知：

```
注意: OSPF 进程 1 在本次变更前已存在，回退时将拒绝删除
```

## 保持与 Python 一致的"未实现"

`atoms/utils.py` 中三个函数是空实现：

```python
def get_interface_dependencies(host, ifindex, session=None) -> List[str]:
    return []
def get_cli_interface(host, ifname) -> Optional[Dict]:
    return None
def check_cli_dependencies(host, ifname) -> List[str]:
    return []
```

即 IP 与 Loopback 的**回退依赖检查从未真正生效**。

本 crate 的处理是分开的：

| 函数 | 处理 | 理由 |
| :--- | :--- | :--- |
| `get_cli_interface` → Loopback 存在性 | **补上真实查询**（`display interface X brief`） | 空实现导致 `pre_check` 永远认为接口不存在，幂等判断完全失效，这是明确的缺陷 |
| `get_interface_dependencies` / `check_cli_dependencies` → 依赖检查 | **保持始终放行**，但代码里显式记录"尚未实现" | 补齐它需要先定义"哪些配置算依赖某个 IP"，属设计决策，不应由重写单方面决定 |

```rust
async fn rollback_guard(&self, ...) -> Result<RollbackGuard, AtomError> {
    tracing::debug!("IP 回退依赖检查尚未实现（Python 版同为空实现），当前始终放行");
    Ok(RollbackGuard::Safe)
}
```

不伪装成已检查，是为了避免"看到有 `rollback_guard` 就以为安全"的误判。

## 快照持久化

### 目录分离

Python 版把两类文件混放在 `logs/`（新发现③）：

| 文件 | 性质 | 混放的风险 |
| :--- | :--- | :--- |
| `ifindex_<device>.xml` | 调试产物，每次查询覆盖写 | — |
| `snapshots_<device>_<mode>.json` | **回退的唯一依据** | 做日志清理时可能被连带误删 |

本 crate 按生命周期分离：

```
state/snapshots/<device>_<scope>.json    ← 业务状态，需纳入备份策略
logs/debug/<name>                        ← 调试产物，可随时清理
```

### 原子写入

Python 版直接 `open(..., "w")` 写入，进程中断会留下截断的 JSON——而这个文件是回退的唯一依据。

本 crate 写临时文件后原子重命名：

```rust
let tmp_path = final_path.with_extension("json.tmp");
std::fs::write(&tmp_path, json)?;
std::fs::rename(&tmp_path, &final_path)?;   // 同文件系统内的 rename 是原子操作
```

### API

```rust
let store = SnapshotStore::new(project_root);
store.save(device, scope, &snapshot)?;              // 返回实际路径
let s: Option<T> = store.load(device, scope)?;      // 不存在返回 None
let removed: bool = store.remove(device, scope)?;   // 不存在返回 false
```

`scope` 当前有两个值：`"ip"`（`step3_ip::SCOPE`）与 `"ospf"`（`step3_ospf::SCOPE`）。

## 现有 Atom 一览

| Atom | 通道 | 快照 | 幂等 | 回退计划 | 依赖检查 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `NetconfIpAddressAtom` | NETCONF | `previous_ip` / `applied_ip` | 已是目标 IP 则跳过 | 恢复原值 或 删除 | 未实现（同 Python） |
| `CmdLoopbackAtom` | CLI | `existed_before` | 接口已存在则跳过 | 删除 或 空计划 | 未实现（同 Python） |
| `NetconfOspfAtom` | NETCONF | `instance_existed_before` | 无接口则阻止 | 逐层删除 | **已实现** |

对比 Python 版的一览表（见 `atoms/README.md`），`NetconfLoopbackAtom` 已废弃，`NetconfOspfAtom` 是新增的。

## 强约束：绝对禁止行为

### 禁止 1：在 `deploy()` 里跳过 `pre_check` 直接操作

`pre_check` 是快照的唯一来源。没有快照就没有回退依据。

### 禁止 2：让 `plan_rollback()` 接触设备

它必须是纯函数。preview 模式依赖这一点来展示回退计划。若需要设备信息，应在 `pre_check` 阶段采集进快照。

### 禁止 3：在 `apply_rollback()` 里跳过 `rollback_guard`

`execute()` 的默认实现已经保证了顺序（guard → plan → apply）。手动展开生命周期时必须自己保证，见 `scenes/step3_ospf.rs::rollback()`。

### 禁止 4：把期望状态塞进 `Snapshot`

这正是 Python 版 OSPF 场景的问题。`Snapshot` 只放变更前状态和"本次改了什么"，期望状态放 `Desired`。

### 禁止 5：跨通道执行回退动作

`RollbackAction::CliCommands` 只能由 `CliSession` 执行，`NetconfFragment` 只能由 `NetconfSession` 执行。两个 Atom 的 `apply_rollback` 都对错配的情况返回 `AtomError::Internal`。

### 禁止 6：把"未实现"伪装成"已检查"

`rollback_guard` 若尚未实现真实逻辑，必须在代码里显式记录（`tracing::debug!`），不能静默返回 `Safe`。

## 弱约束：推荐做法

### 推荐 1：下发前先落盘快照

`scenes/step3_ospf.rs::deploy()` 的做法：先 `store.save()` 再 `atom.deploy()`。这样即使下发成功后进程被杀，快照仍在，可以回退。

### 推荐 2：新增 Atom 时先写 `plan_rollback` 再写 `deploy`

先想清楚"怎么撤回"，往往能发现快照该记什么。反过来容易漏字段。

### 推荐 3：`RollbackStep::description` 写得足够具体

它会直接出现在 preview 输出和日志里。`"删除 ifindex 1234 上本次下发的 IP 11.11.11.11"` 比 `"删除 IP"` 有用得多。

## 常见问题

### Q1：`execute()` 和手动展开四阶段，该用哪个？

`execute()` 适合"下发一个资源、失败就自动回退"的简单场景。

手动展开适合需要在中途做额外事情的场景，比如：
- 拿到快照后落盘（`step3_ospf.rs`）
- 把多个 Atom 的快照聚合成一个场景快照（`step3_ip.rs`）

### Q2：为什么 `Channel` 是关联类型而不是 trait object？

CLI 与 NETCONF 的会话 API 差异太大（一个是命令行文本，一个是 XML 片段），抽象成统一 trait 会产生大量"该方法在此通道不适用"的空实现。关联类型让编译器在类型层面保证 Atom 只能配对正确的通道。

代价是不能把不同通道的 Atom 放进同一个 `Vec<Box<dyn Atom>>`。当前场景不需要，若将来需要可以在场景层做枚举分发。

### Q3：`AtomStatus::RolledBack` 里 `rollback_detail` 是什么？

回退本身的结果描述。回退成功时是 `"已执行 N 个回退步骤"`，回退失败时是 `"回退失败: <错误>"`。

**回退失败仍然返回 `RolledBack` 而非 `Failed`**，因为原始的失败原因（`reason`）需要保留。调用方应同时关注两个字段。

### Q4：一个 Atom 能对应多个模板吗？

能。`CmdLoopbackAtom` 用了两个（创建 / 删除），`NetconfIpAddressAtom` 用同一个模板配不同的 `operation` 参数。

路径常量集中在 `cwa_templating::paths`，不再像 Python 版那样散落在各 Atom 的类属性里。

### Q5：新增一个场景（如 ACL）该怎么组织？

1. 在 `crates/atoms/src/h3c/` 下新建 `acl.rs`
2. 定义 `AclParams` / `AclSnapshot` / `AclDesired` 三个结构
3. 实现 `Atom` trait —— 编译器会强制你实现全部方法，包括 `plan_rollback`
4. 在 `h3c/mod.rs` 里 `pub mod acl;` 并 re-export
5. 模板路径加到 `cwa_templating::scene_api::paths`
6. 在 `crates/scenes/` 下新建场景编排

### Q6：怎么在没有设备的情况下验证这一层？

`plan_rollback()` 是纯函数，可以构造快照直接调用并检查产出的 XML / 命令。`SnapshotStore` 的往返也可以离线验证。见测试用例 T2.7 ~ T2.9。

`pre_check` / `deploy` / `post_check` 需要设备。
