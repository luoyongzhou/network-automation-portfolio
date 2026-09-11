# Rust 重构可行性调研报告

> 调研对象：[`nornir-comware-automation`](../nornir-comware-automation)（Python + Nornir + Netmiko + ncclient）
> 调研问题：用 Rust 重写能否达到等同功能与效果
> 调研日期：2026-09-11
> 调研方式：阅读现有项目全部源码 + 查询 crates.io 元数据 + 阅读候选 crate 源码
> **未做真机测试**（无测试环境）。凡涉及设备实际行为的判断，文中均显式标注为未验证。

---

## 0. 结论摘要

**能，但不是等价移植。**

| 层次 | 结论 |
| :--- | :--- |
| 上层（inventory 继承、模板渲染、并发调度、快照、XML 解析） | 现成 crate 成熟度足够，可平移 |
| NETCONF 协议层 | 有可用 crate（`rustnetconf`），**不需要自己实现** |
| CLI over SSH 层 | 有内置 H3C Comware 模板的 crate（`rneter`），可用但需自行验证 |
| **Console 转 Telnet 开局** | **无任何对等物，唯一必须从零实现的部分** |

真正的成本不在语言转换，而在两点：**传输层 crate 的生态年龄**（下载量三位数到四位数、2026 年新项目），以及 **Telnet 通道要自己写状态机**。

---

## 1. 现有项目的实际构成

读完 `scripts/`、`atoms/`、`scenes/`、`templates/`、`inventory/` 全部源码后，项目的骨架其实是**两个引擎 + 两条通道**：

### 1.1 继承合并引擎（`scripts/build_inventory.py`）

递归展开 Group 的 `parents` 链，`deep_merge` 递归深合并字典，`processing` 集合做循环继承检测，最后按来源目录过滤，只输出 `os_versions` 层的 Group 到 `inventory/groups.yaml`。

纯数据变换，不接触设备。**这是整个项目最容易移植的部分**——因为 Python 版本本身就是手写的，没有依赖 Nornir 的任何内部机制。

### 1.2 模板路径解析 + 渲染引擎（`scripts/preview_bootstrap.py`）

三个抽象叠在一起：

- `PatchExtractor`：从 Group 名称正则提取补丁段。`H3CPatchExtractor` 匹配 `_r([0-9]+)` → `R7171`、`_h([0-9]+)` → `H02`，组合成 `R7171/H02`；`host.data["patch"]` 可直接覆盖
- `PathResolver`：生成 5 级候选路径，优先级从高到低为 补丁级 → OS 版本级 → 产线级 → 厂商级 → `_base` 兜底
- `TemplateRenderer`：逐个探测候选路径的文件存在性，第一个存在且渲染成功的即返回

`SceneAPI` 在此之上包了一层场景方法（`bootstrap`/`ssh_bootstrap`/`custom` 等）。

### 1.3 CLI 通道

Netmiko `hp_comware` / `hp_comware_telnet`，核心手法是 `send_command(cmd, expect_string=r"\[.*?\]")` 驱动 `system-view` 视图切换。

`bootstrap_step2.py` 里还自己实现了一个解析器 `parse_preserving_order()`，识别模板中的 `#INDEPENDENT:START` / `#INDEPENDENT:END` 标记 —— 因为 `public-key local create rsa` 这类命令需要交互输入（`y`、`512`），且必须用独立 SSH 连接、采用"发送即忘"策略。

### 1.4 NETCONF 通道

ncclient + `device_params={"name": "hpcomware"}`，`edit_config(target="candidate")` + `commit()`。

- OSPF 下发用 `error_option="rollback-on-error"` 保证单次 RPC 原子性
- OSPF 回退用 `error_option="continue-on-error"`，且删除时只发索引列（`IfIndex` / `Name` / `AreaId`）
- lxml 带命名空间 XPath 解析 ifindex 映射和接口当前 IP

### 1.5 抽象落地的实际进度

`atoms/` 的四阶段模板方法（`pre_check → deploy → post_check → rollback`）只在 H3C 的 IP/Loopback 场景落地。OSPF 与实际跑通的 IP 下发都是在 `scenes/` 里手写的。这一点项目自己的 `need_to_discuss_prob.md` 已如实记录。

### 1.6 读码发现的一个事实（对开放问题⑥的直接回答）

`need_to_discuss_prob.md` 延伸⑥提出："`atoms/base.py` 传裸片段、`scenes/*.py` 手动包 `<config>`，两种方式哪个是对的？"

查 ncclient master 源码 `ncclient/operations/edit.py`：

```python
if format == 'xml':
    node.append(validated_element(config, ("config", qualify("config"))))
```

而 `ncclient/xml_.py` 的 `validated_element()` 在根元素 tag 不在允许列表时抛 `XMLError`。

**即 `edit_config(config=...)` 要求根元素必须是 `config`。`scenes/` 里手动拼 `<config>{frag}</config>` 才是正确用法；`atoms/base.py::NetconfAtom._edit_config()` 传裸片段的那条分支从未被真正执行过**（因为 `NetconfIPAddressAtom` 未被任何场景实例化调用），一旦被调用就会抛异常。

这个结论与语言选择无关，是当前 Python 代码里的一个待修缺陷。

---

## 2. 逐层 Rust 方案与成熟度

下载量与版本号为 2026-09-11 查询 crates.io API 所得。

### 2.1 可直接替换、成熟度充分的层

| 用途 | Python 现状 | Rust 方案 | 版本 | 累计下载 | 判断 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 并发调度 | Nornir threaded runner（10 workers） | `tokio` + `Semaphore` | 1.53.1 | 9.56 亿 | 只用到 `nr.run()` + `num_workers`，几十行即可覆盖 |
| 并发（CPU 密集备选） | — | `rayon` | 1.12.0 | 5.38 亿 | 备选 |
| 模板渲染 | Jinja2 | `minijinja` | 3.0.0-alpha / 2.24 稳定 | 3328 万 | 见 2.2 |
| Python 方法兼容 | — | `minijinja-contrib`（`pycompat`） | 同上 | 787 万 | 见 2.2 |
| YAML | PyYAML | `serde_yaml_ng` 或 `serde_norway` | 0.10.0 / 0.9.42 | 1124 万 / 1104 万 | `serde_yaml` 本体已标记 deprecated（0.9.34+deprecated，2024-03 停更），不要选它 |
| XML 解析 | lxml + XPath | `roxmltree` 或 `quick-xml` | 0.21.1 / 0.42.0 | 7210 万 / 4.08 亿 | 项目只有 3 处查询，含一个 `[h3c:IfIndex='x']` 谓词，手写过滤即可 |
| JSON 快照 | json | `serde_json` | — | 十亿级 | 无风险 |

**并发这一层不需要等 nornir-rust。** 项目对 Nornir runner 的使用极浅——只有 `nr.run(task=...)` 和 `num_workers: 10`，外加 `nr.filter(F(name=...))` 和手动替换 `nr.inventory.hosts` 字典。用 `tokio::spawn` + `Semaphore` 限流即可复现，且是真并发，不受 GIL 约束。

### 2.2 minijinja 的具体差异（迁移时必须处理）

模板继承体系可对等：`{% extends %}`、`{% block %}`、`{{ super() }}`、`{% import ... as ... %}`、`trim_blocks` / `lstrip_blocks` 均有对应实现。据 minijinja 官方兼容性文档，`extends` / `block` / `macro` / `import` 与 Jinja2 特性齐平。

但有两处必须显式处理：

**① Python 方法不被支持**

`templates/vendors/h3c/cmd/_macros.j2` 的 `interface_config` 宏里用了：

```jinja
{% if name.startswith('GigabitEthernet') or name.startswith('Ten-GigabitEthernet') or ... %}
 port link-mode route
{% endif %}
```

minijinja 明确不实现任何 Python 方法。解法有两个：
- 引入 `minijinja-contrib` 的 `pycompat` unknown_method_callback。已核对 `minijinja-contrib/src/pycompat.rs` 源码，其中包含 `startswith`、`endswith`、`upper`、`lower`、`strip`、`split`、`replace`、`items` 等分支，**覆盖本项目所需**
- 或改写为自定义 filter / test

**② autoescape 行为必须对齐**

当前 `TemplateRenderer` 的配置是：

```python
autoescape=select_autoescape(['xml', 'j2'])
```

即所有 `.j2` 模板都在做 HTML 转义。目前 CLI 命令文本里没有 `<`、`>`、`&`，所以问题没有暴露；但 NETCONF 的 XML 模板是走同一个 Environment 的。迁移时必须显式决定是否保留这个行为，否则渲染输出会与 Python 版产生差异。

**这两点都能通过离线的黄金文件比对（见企划书第 3 章）暴露出来，不需要设备。**

### 2.3 NETCONF 层：`rustnetconf`

crates.io 上有多个同类 crate，容易选错，先列清楚：

| Crate | 版本 | 下载 | 最后更新 | 评估 |
| :--- | :--- | :--- | :--- | :--- |
| **`rustnetconf`** | 0.17.0 | 2441 | 2026-09-01 | **推荐**。见下 |
| `netconf-rs` | 0.2.6 | 13011 | 2023-11 | **不可用**。下载量最高但已停更，`src/lib.rs` 公开 API 仅 `Connection::new` / `get_config`，无 `edit_config` |
| `netconf-rust` | 0.5.0 | 105 | 2026-03 | 作者自述 EXTREMELY experimental |
| `netconf-async` | 0.1.0 | 17 | 2026-08 | 单版本，过新 |
| `netgauze-netconf-proto` | 0.13.0 | 607 | 2026-07 | 低层协议实现，非客户端 |

**`rustnetconf`（[fastrevmd-lab/rustnetconf](https://github.com/fastrevmd-lab/rustnetconf)）的实际能力**，来自阅读源码：

- `src/session.rs` 4857 行，`src/rpc/reply/parser.rs` 11.8 万字节，`src/client.rs` 8.5 万字节
- 基于 tokio + russh + rustls，纯 Rust，不链接 OpenSSL / libssh2
- `edit_config` 把 `error_option` 作为一等参数：

```rust
pub async fn edit_config(
    &mut self, target: Datastore, config: &str,
    default_operation: Option<DefaultOperation>,
    test_option: Option<TestOption>,
    error_option: Option<ErrorOption>,
) -> Result<(), NetconfError>
```

`ErrorOption` 枚举为 `StopOnError` / `ContinueOnError` / `RollbackOnError`。**本项目 OSPF 下发依赖的 `rollback-on-error` 与回退依赖的 `continue-on-error` 均可直接使用。**

- 其余可用操作：`commit`、`validate`、`lock` / `unlock`、`discard_changes`、`confirmed_commit` / `confirming_commit`、`cancel_commit`、`get`、`get_config`、`get_config_xpath`、`copy_config`、`delete_config`、`close_session`、`kill_session`、`partial_lock` / `partial_unlock`、`lock_or_kill_stale`、`create_subscription`
- 附加能力：`candidate_dirty()` 候选态脏标记跟踪、连接池（`src/pool/`）、RFC 6242 chunked framing（`src/framing/chunked.rs`）、fuzz target、以及对 vSRX 24.4R1.9 的真机集成测试

**一个语义翻转，迁移时必须注意：**

`rustnetconf::edit_config` 内部执行 `self.vendor_profile.wrap_config(config)`，**由库负责包 `<config>` 外壳，调用方传裸片段**。这与 ncclient 的约定（要求根元素必须是 `config`，见 1.6）正好相反。

好的一面是，这把 `need_to_discuss_prob.md` 延伸⑥那个歧义在架构层面消除了 —— 包不包外壳不再是调用方的选择。

**短板：** `src/vendor/` 只有 `junos` 和 `generic` 两个 profile，`detect_vendor()` 对非 Junos 设备一律回落到 `GenericVendor`。

对本项目而言这**可能**是够的，理由是：项目所有 XML 都在模板里自己写全了 `xmlns="http://www.h3c.com/netconf/config:1.0"`；而 ncclient 的 `hpcomware` handler 源码实际只做两件事——注入 nsmap（`data:1.0` / `config:1.0` 前缀）、注册 `cli_display` / `cli_config` / `action` / `rollback` / `save` 五个私有 RPC，**这五个 RPC 本项目一个都没用**。

即：**换语言不会丢失厂商适配逻辑，只会丢失协议栈的生态年龄。**

但"可能够"不等于"验证过"。Comware 在 `GenericVendor` 下的 hello 能力协商与 commit 时序，无真机无法确认。

### 2.4 CLI over SSH 层：`rneter`

| Crate | 下载 | 最后更新 | H3C 支持 | Telnet |
| :--- | :--- | :--- | :--- | :--- |
| **`rneter`** | 799 | 2026-09-07 | **内置 `h3c_comware` / `hp_comware`** | 无 |
| `ferrissh` | 113 | 2026-03 | 无（仅 arista / juniper / nokia_sros / arrcus_arcos） | 无 |
| `rauto` | 277 | 2026-08 | 基于 rneter 的 CLI 工具 | — |

`rneter`（[demohiiiii/rneter](https://github.com/demohiiiii/rneter)）的 `src/templates/network/h3c.rs` 内容出乎预期地贴合本项目需求：

- **提示符规则**区分 Enable `<...>` 与 Config `[...]`，正则中专门处理了 `RBM_P` / `RBM_S`（IRF 主备前缀），且 Config 那条正则刻意排除 `[Y/N]` 形式，避免把确认框误判为配置态提示符
- **`error_regex`** 包含 `.+%.+` 与 `.+\^.+`，以及 `Permission denied\.`、`Failed to apply .+` 等 —— **这正是 `need_to_discuss_prob.md` 延伸③要求的"对返回文本做错误关键字匹配"，当前 `CmdAtom._render_and_send()` 缺失的那一层**
- **`more_regex`** 处理 `---- More ----` 分页
- **`after_connect` hook** 自动下发 `screen-length disable`
- **`input_rule`** 处理保存确认、"保留原文件名请回车"、密码过期提示等交互场景
- **`src/session/transaction.rs`** 提供 `RollbackPolicy::{None, PerStep, WholeResource}`、`plan_rollback()`、`workflow_rollback_order()`、`failed_block_rollback_summary()` —— 比当前手写的回退编排更结构化
- **`src/testkit/`** 提供 `FakeSshDevice` + `DevicePersona::builtin("h3c_comware")`，仓库内 `tests/builtin_templates_e2e/network/h3c.rs` 已有针对 h3c 的分页采集、自动识别等 E2E 测试。**这意味着可以在无真机条件下做端到端验证**

**两个硬伤：**

1. **799 次下载、2026 年项目。** 对比 Netmiko 十余年积累的 `hp_comware` 驱动，边角行为未必覆盖。一个具体例子：当前 `templates/vendors/h3c/cmd/bootstrap.j2` 刻意把 `sysname {{ sysname }}` 放在模板最后一行，注释写明是"避免过早修改设备名称，触发 80 字符换行，导致 netmiko 原生命令校验失败"。这类经验性规避在新 crate 上是否仍然必要、或是否会以别的形式复现，无真机无法确认。

2. **完全不支持 Telnet。** 已检索整个仓库文件树，无任何 telnet / jump / proxy 相关文件。

### 2.5 Console 转 Telnet：唯一必须从零实现的部分

`scenes/bootstrap_step1.py` 依赖的能力：

- Netmiko `hp_comware_telnet` / `generic_telnet` 驱动（两级 fallback）
- `write_channel("\x03")` 连发两次中断 ZTP
- `read_until_prompt()` 后检测 `Press ENTER` 并响应
- Group 里配置的 `global_delay_factor: 3`、`timeout: 120` 等 Comware Console 慢速特性适配

Rust 侧只有 `telnet` crate（0.2.5，14.7 万下载，2026-07 更新），是"a simple implementation of telnet protocol"，**协议层可用，但提示符状态机、ZTP 中断时序、交互应答全部要自己写**。

这部分是整个迁移里风险最高、且最依赖真机验证的一块。

---

## 3. 风险对称性提示

必须指出一个容易被忽略的代价：

`rustnetconf`（2441 下载）和 `rneter`（799 下载）都是 2026 年的项目。它们在功能上填补了当前 Python 实现的缺口（CLI 错误检测、回退编排），但：

- 作者可能随时变更 API 或停止维护
- `rustnetconf` 的 README 自述为"Unofficial / community project"
- 从版本历史看迭代很快（`rustnetconf` 半年内从 0.14 走到 0.17，且 0.17.0 的变更说明里明确提到因加密后端切换而做的非补丁级版本号跳跃）

而当前依赖的 ncclient / Netmiko 虽有功能缺口，但十年内不会消失。

**用新 crate 换来的能力，是用供应链稳定性付的账。** 这不是反对迁移，而是这个决策必须被显式承担。

---

## 4. 一个反直觉的观察

`rneter` 的 `error_regex`（`%` / `^` 前缀检测）与 `transaction.rs` 的 `RollbackPolicy`，恰好对应 `need_to_discuss_prob.md` 中列为"待讨论、尚未实现"的两件事：

- 延伸③：cmd 下发后对返回文本做错误关键字匹配
- 延伸③深层：快照如何映射到回退时该执行哪些具体步骤

即 Rust 生态在 CLI 错误检测与回退编排上，反而已把当前缺失的那层做进了库里。

但这不构成迁移的充分理由 —— 这两个问题本质是**设计层面**的，与语言无关，在 Python 里同样可以实现。见企划书第 5 章的决策建议。

---

## 5. 无法验证的事项清单

以下均为无真机环境无法确认，不应作为已确定的结论使用：

1. Comware 在 `rustnetconf` `GenericVendor` profile 下的 hello 能力协商是否正常
2. `commit` 的时序行为、以及 `rollback-on-error` 在 Comware 上经由 `rustnetconf` 下发时是否与 ncclient 表现一致
3. `rneter` 的 `h3c_comware` 提示符正则对实际 SR88 R7171 输出的匹配度
4. 80 字符换行问题在 `rneter` 上是否需要同样的规避手法
5. Console Telnet 通道的 ZTP 中断时序

其中 1、3 可用 `rneter` 的 `FakeSshDevice` 和 `rustnetconf` 的测试夹具做**离线逼近**，但离线逼近不等于真机验证。

---

## 附：引用来源

- [fastrevmd-lab/rustnetconf](https://github.com/fastrevmd-lab/rustnetconf) — NETCONF 客户端库
- [demohiiiii/rneter](https://github.com/demohiiiii/rneter) — SSH 会话管理与 H3C 模板
- [joshbenz/ferrissh](https://github.com/joshbenz/ferrissh) — CLI scraper（无 H3C 支持）
- [jiegec/netconf-rs](https://github.com/jiegec/netconf-rs) — 已停更的 NETCONF crate
- [mitsuhiko/minijinja](https://github.com/mitsuhiko/minijinja) — 模板引擎，含 [COMPATIBILITY.md](https://github.com/mitsuhiko/minijinja/blob/main/COMPATIBILITY.md) 与 [pycompat.rs](https://github.com/mitsuhiko/minijinja/blob/main/minijinja-contrib/src/pycompat.rs)
- [ncclient/ncclient](https://github.com/ncclient/ncclient) — `operations/edit.py`、`xml_.py`、`devices/hpcomware.py`
- crates.io API — 各 crate 版本与下载量元数据

部分内容为符合来源许可要求已改写表述。
