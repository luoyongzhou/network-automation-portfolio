# 迁移重构企划书：nornir-comware-automation → Rust

> 前置阅读：[`RESEARCH.md`](./RESEARCH.md)（成熟度调研与选型依据）
> 编写日期：2026-09-11
> 状态：**企划草案，尚未动工**
> 约束：**当前无测试环境**，本企划把"可离线验证"与"必须真机验证"作为分期切口

---

## 1. 目标与非目标

### 1.1 目标

用 Rust 实现与现有 Python 项目**功能等价**的自动化框架，保持：

- 四层抽象不变：分层继承 inventory、对称模板继承树、原子化变更单元、场景化入口
- 三阶段闭环不变：Console 开局 → SSH 使能 NETCONF → 业务配置下发/回退
- 每个场景的三种模式不变：预览（不下发）、下发、回退
- **模板文件与 inventory YAML 原样复用，不做任何改写**（见 3.2，这是黄金文件比对的前提）

### 1.2 非目标

- 不追求 API 层面与 Nornir 兼容
- 不在本次迁移中顺带解决 `need_to_discuss_prob.md` 里的开放设计问题（理由见第 5 章）
- 不实现 ncclient `hpcomware` handler 里那五个未被使用的私有 RPC（`cli_display` / `cli_config` / `action` / `rollback` / `save`）

### 1.3 迁移的实际收益

诚实列举，避免自我说服：

| 收益 | 成立程度 |
| :--- | :--- |
| 单二进制分发，跳板机无需 Python 环境与 pip 依赖 | **确实成立**，且是最主要收益 |
| 真并发，不受 GIL 约束 | 成立，但当前 3 台设备规模下感知不到 |
| 编译期类型检查，减少 `atoms/` 抽象方法签名不一致这类问题 | 成立。Python 的 `ABC` 只在实例化时检查，`NetconfLoopbackAtom` 的半成品状态就是被这个特性掩盖的；Rust 的 trait 在编译期就会拒绝 |
| 性能 | **不成立**。瓶颈在设备响应与网络往返，不在本地计算 |

---

## 2. 目标架构与 crate 选型

### 2.1 workspace 布局

```
comware-automation-Rust/
├── Cargo.toml                    # workspace 根
├── crates/
│   ├── inventory/                # Group 继承合并 + Host/Group 数据模型
│   ├── templating/               # PatchExtractor + PathResolver + minijinja 渲染
│   ├── transport/                # CLI(SSH) / NETCONF / Console(Telnet) 三通道
│   ├── atoms/                    # 四阶段原子操作 trait 与 H3C 实现
│   └── scenes/                   # 三阶段场景编排 + CLI 入口
├── inventory/                    # 从 Python 项目原样复制
├── templates/                    # 从 Python 项目原样复制，不改写
├── tests/
│   └── golden/                   # 黄金文件（Python 版渲染输出快照）
└── docs/
```

### 2.2 依赖选型

| 用途 | crate | 备注 |
| :--- | :--- | :--- |
| 异步运行时 | `tokio` | 含 `Semaphore` 做并发限流 |
| 模板 | `minijinja` + `minijinja-contrib` | contrib 用于 `pycompat`，覆盖 `startswith` |
| YAML | `serde_yaml_ng` 或 `serde_norway` | **不要用 `serde_yaml`**，已 deprecated |
| JSON | `serde_json` | 快照读写 |
| XML | `roxmltree` | 只读解析，够用且比 `quick-xml` 简单 |
| NETCONF | `rustnetconf` | 锁定精确版本，见 6.2 |
| CLI over SSH | `rneter` | 内置 `h3c_comware` 模板，锁定精确版本 |
| Telnet | `telnet` | 仅提供协议层，状态机自写 |
| 正则 | `regex` | PatchExtractor 用 |
| CLI 参数 | `clap` | 替代现在的手写 `sys.argv` 解析 |
| 日志 | `tracing` | 替代现在散落的 `print()` |

### 2.3 与 Python 版的关键语义差异（必须在代码里显式处理）

| 差异点 | Python 现状 | Rust 侧行为 | 处理方式 |
| :--- | :--- | :--- | :--- |
| `<config>` 外壳 | `scenes/` 手动拼 `<config>{frag}</config>`；ncclient 要求根元素为 `config` | `rustnetconf::edit_config` 内部 `wrap_config()`，要求传**裸片段** | 渲染层统一输出裸片段，禁止在模板或调用方拼外壳。在 `transport` crate 的文档注释里写明此约定 |
| 模板中的 Python 方法 | `_macros.j2` 用 `name.startswith(...)` | minijinja 不支持任何 Python 方法 | 注册 `minijinja-contrib::pycompat` 的 unknown_method_callback |
| autoescape | `select_autoescape(['xml','j2'])`，所有 `.j2` 都转义 | minijinja 需显式配置 | 第一期先**完全对齐 Python 行为**（即保持转义），确保黄金比对通过；是否改为不转义作为后续独立决策 |
| 厂商 profile | ncclient `hpcomware` handler | `rustnetconf` 回落 `GenericVendor` | 接受。理由见 RESEARCH.md 2.3 |

---

## 3. 分期计划

分期切口刻意设在**"能否离线验证"**这条线上。

### 第一期：离线可完全验证（约占总工作量 40%，风险低）

**范围**

1. `inventory` crate
   - YAML 加载、`deep_merge` 递归深合并、`parents` 链递归展开、循环继承检测
   - 按来源目录过滤，只输出 `os_versions` 层
   - Host / Group / defaults 三级解析，`connection_options` 查找（含 `netmiko_oob`、`ncclient` 等命名连接）
   - `pre_validate_groups` 的 Hook 位置保留（Python 版当前是空实现）

2. `templating` crate
   - `PatchExtractor` trait + `H3C` / `Cisco` / `Default` 三个实现
   - `PathResolver` trait + `H3CBootstrapPathResolver`（5 级候选路径）+ `DirectPathResolver`
   - minijinja Environment 配置：`trim_blocks`、`lstrip_blocks`、autoescape 对齐、`pycompat` 注册
   - 候选路径存在性探测 + 逐个尝试渲染的 fallback 逻辑

3. 快照读写（`serde_json`，路径与文件命名与 Python 版一致）

4. 全部 `preview` 子命令

**验证方式：黄金文件比对**

这是第一期能够无设备验证的关键，具体做法：

```bash
# Step 1 —— 在现有 Python 项目里，对全部场景 × 全部设备生成基准输出
python scripts/preview_bootstrap.py --scene bootstrap            > golden/bootstrap.all.txt
python scripts/preview_bootstrap.py --scene ssh_bootstrap        > golden/ssh_bootstrap.all.txt
python scenes/bootstrap_step2.py preview                         > golden/step2_preview.txt
# IP / OSPF 的 preview 依赖 ifindex_map，用固定的假 map 注入以保证可重放

# Step 2 —— Rust 版产出同样的输出，逐字节 diff
cargo test --test golden
```

同时把 `python scripts/build_inventory.py` 生成的 `inventory/groups.yaml` 作为继承合并的黄金文件。

**这一期能顺带逼出的问题**：autoescape 差异、`startswith` 差异、以及模板文件里的 CRLF 行尾（现有 `.j2` 文件是 CRLF，读码时已确认）在两个引擎下的处理是否一致。

**第一期不需要任何设备，不需要任何模拟器。**

### 第二期：必须真机收尾（约占 60%，风险集中于此）

**范围**

1. `transport::netconf` —— 基于 `rustnetconf`
   - 会话建立、`edit_config` + `commit`、`error_option` 传递
   - ifindex 映射查询、接口 IP 查询（`roxmltree` 解析 + 命名空间处理）
   - **注意**：Python 版 `_safe_parse_xml()` 有剥离 BOM（`\ufeff`）的处理，Rust 侧需保留等价逻辑

2. `transport::cli` —— 基于 `rneter`
   - `system-view` 视图切换、命令下发
   - **新增能力**：接入 `rneter` 的 `error_regex`，补上当前 Python 版缺失的下发结果校验（`need_to_discuss_prob.md` 延伸③）
   - `#INDEPENDENT:START/END` 独立块解析器移植（纯字符串处理，本身可离线单测）

3. `transport::console` —— 基于 `telnet` crate **自写**
   - ZTP 中断（`\x03` ×2）、`Press ENTER` 检测与应答、提示符状态机
   - Comware Console 慢速特性适配（对应 Group 里的 `global_delay_factor: 3`）

4. `atoms` crate —— 四阶段 trait
   - **借此机会统一 OSPF 与 IP 两条路线**（Rust 的 trait 在编译期强制方法签名一致，`NetconfLoopbackAtom` 那种半成品状态不可能编译通过）
   - 修正 `post_check(host, snapshot, kwargs, **kwargs)` 那个 `desired` 参数语义不一致的问题——trait 签名必须先定义清楚 `desired` 是什么

5. `scenes` crate —— 三阶段编排 + `tokio` 并发 + `clap` 入口

**第二期可离线做的部分**

- RFC 6242 framing 用录制帧做单测
- edit-config XML 构造用 `scenes/summary.md` 里记录的报文样例做断言（该文档第 12 章有完整的成功/回退输出）
- `rneter` 的 `FakeSshDevice` + `DevicePersona::builtin("h3c_comware")` 可做 CLI 通道的 E2E 离线验证
- 独立块解析器、快照序列化等纯逻辑部分

**第二期必须真机才能确认的部分**

- Comware 在 `GenericVendor` 下的 hello 能力协商
- `commit` 时序、`rollback-on-error` 的实际行为
- 提示符正则对真实 SR88 R7171 输出的匹配度
- 80 字符换行规避是否仍然必要
- Console Telnet 的 ZTP 中断时序

---

## 4. 工作量估算

| 模块 | Rust 行数（估） | 对应 Python | 备注 |
| :--- | :--- | :--- | :--- |
| `inventory` | 600–800 | ~250 行 | 类型定义占比高 |
| `templating` | 500–700 | ~300 行 | |
| `transport::netconf` | 400–600 | ~120 行（utils.py） | 有 `rustnetconf` 兜底，比原估计的 1000–1500 行大幅下降 |
| `transport::cli` | 400–600 | ~100 行 | 有 `rneter` 兜底 |
| `transport::console` | 500–800 | ~80 行 | **自写状态机，行数放大最明显** |
| `atoms` | 600–900 | ~350 行 | 顺带统一两条路线 |
| `scenes` | 800–1200 | ~900 行 | |
| 测试 | 600–1000 | 目前无测试 | 黄金比对 + 单测 |
| **合计** | **约 4400–6600** | **约 2100** | |

时间量级：**周级别，不是天级别**。第一期约占 40%，且可独立交付。

---

## 5. 决策建议

这一章是企划书里最重要的部分。

### 5.1 建议：先不要迁移

理由不是 Rust 做不到，而是**迁移与当前真正的瓶颈无关**。

`need_to_discuss_prob.md` 里未解决的问题，按重要性排列：

| 开放问题 | 与语言的关系 |
| :--- | :--- |
| cmd 下发后无错误检测（延伸③） | **无关**，Python 里加正则匹配即可 |
| 快照如何映射到回退步骤（延伸③深层） | **无关**，纯设计问题 |
| 多设备变更的两阶段提交（延伸④） | **无关**，分布式事务设计问题 |
| `atoms/` 抽象只落地一半（延伸①） | **无关**，重构 Python 即可 |
| `_edit_config` 传裸片段走不通（延伸⑥） | **无关**，且 RESEARCH.md 1.6 已给出答案，改几行即可 |
| `PathResolver` 硬编码 SR88（新发现①） | **无关** |
| `logs/` 混放调试产物与业务快照（新发现③） | **无关** |

**没有一条是靠换语言解决的。**

而如果先迁移，这些设计问题就要在一个**未经真机验证的传输层**上调试——同时面对两类不确定性（我的设计对不对 + 底层 crate 行为对不对），排障难度是乘法而非加法。

### 5.2 推荐路径

```
阶段 A（现在做，纯 Python）
  ├── 修正 atoms/base.py::NetconfAtom._edit_config() 的 <config> 包裹问题
  ├── 给 CmdAtom._render_and_send() 加错误关键字检测
  │     （可直接借鉴 rneter h3c.rs 的 error_regex 列表：% / ^ / doesn't exist / Permission denied 等）
  ├── 统一 OSPF 与 IP 两条路线到同一套 Atom 抽象，明确 desired 的语义
  ├── 设计"快照 → 回退步骤"的映射
  └── 决定 logs/ 是否拆分为 debug/ 与 state/snapshots/

阶段 B（设计稳定后，评估是否需要）
  └── 若确实需要单二进制部署形态，再启动 Rust 迁移
        此时第一期（inventory + templating + preview）是纯机械翻译，风险最低
```

### 5.3 什么情况下应该立刻迁移

如果出现以下任一条件，5.1 的建议就不再适用：

- 跳板机 / 生产环境**不允许安装 Python 或 pip 依赖**，必须单二进制分发
- 设备规模从 3 台上升到几十台以上，Python 线程池成为实际瓶颈
- 需要把这套框架作为库嵌入到其他 Rust 服务里

前者是最可能成立的现实理由。

---

## 6. 风险登记

| 风险 | 等级 | 缓解措施 |
| :--- | :--- | :--- |
| Console Telnet 通道无对等库，自写状态机 | **高** | 放到第二期最后做；先用 `telnet` crate 做协议层，逐步补状态机；无真机时无法收尾，需如实标注为未完成 |
| `rustnetconf` / `rneter` 是 2026 年新项目，可能变更 API 或停更 | **高** | 6.2 |
| Comware 在 `GenericVendor` profile 下行为未知 | 中 | 需真机验证；若不通，可提 issue 或自行贡献 Comware profile（vendor trait 是公开的） |
| minijinja 与 Jinja2 的渲染差异 | 中 | 第一期黄金比对可完整暴露 |
| `rneter` h3c 提示符正则与实际设备不匹配 | 中 | 先用 `FakeSshDevice` 离线逼近，真机再校准 |
| 80 字符换行等经验性规避在新 crate 上的表现 | 中 | 保留 Python 版的模板写法（`sysname` 置于末行），真机验证后再决定是否可放开 |

### 6.2 供应链风险的具体缓解

必须显式承担这一点（RESEARCH.md 第 3 章已说明）：

- `Cargo.toml` 里**锁定精确版本**（`=0.17.0` 形式），不用 caret range
- `Cargo.lock` 提交进仓库
- 把对 `rustnetconf` / `rneter` 的调用**收敛到 `transport` crate 内部**，通过自定义 trait 隔离。上层 `atoms` / `scenes` 不直接依赖这两个 crate 的类型 —— 若将来需要更换实现或自己接手维护，改动面被限制在一个 crate 内
- 记录 fork 预案：两者均为 MIT / Apache-2.0，最坏情况可 vendor 进仓库自行维护

---

## 7. 首个可交付里程碑

若决定启动，建议第一个里程碑定为：

**"`cargo run -- preview --scene bootstrap H3C-SR88-01` 的输出与 Python 版逐字节一致"**

理由：

- 完全不需要设备
- 一次性打通 YAML 加载 → Group 继承合并 → PatchExtractor → PathResolver → minijinja 渲染 全链路
- 会立即暴露 autoescape、`startswith`、CRLF 行尾三个已知差异点
- 失败成本极低，若这一步就发现 minijinja 无法对齐，可以立刻停止，损失可控

**验收标准**：`diff <(cargo run -- preview --scene bootstrap H3C-SR88-01) golden/bootstrap.H3C-SR88-01.txt` 无输出。

---

*本企划书基于对现有 Python 项目全部源码的阅读、以及对候选 crate 源码的实际核查编写。所有涉及设备实际行为的判断均已标注为未验证 —— 当前无测试环境，未执行任何真机测试。*
