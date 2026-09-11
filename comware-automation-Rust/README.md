# comware-automation-Rust

[`nornir-comware-automation`](../nornir-comware-automation)（Python + Nornir + Netmiko + ncclient）的 **Rust 重写实现**。

> **验证状态：编译通过、渲染输出与 Python 版逐字节一致、纯逻辑已离线验证。**
> **未做任何真机测试**（无测试环境）。所有涉及设备实际交互的路径均未验证。

## 文档

| 文档 | 内容 |
| :--- | :--- |
| [`RESEARCH.md`](./RESEARCH.md) | 前期成熟度调研：逐层核实 Rust 生态能否替代 Nornir / Jinja2 / Netmiko / ncclient |
| [`MIGRATION_PLAN.md`](./MIGRATION_PLAN.md) | 迁移企划：目标架构、crate 选型、分期计划、风险登记 |

## 快速开始

```bash
cargo build --release

# 展开 Group 继承（对应 build_inventory.py）
./target/release/cwa groups

# 渲染模板但不下发（对应 preview_bootstrap.py）
./target/release/cwa preview bootstrap -d H3C-SR88-01
./target/release/cwa preview netconf-cmd -d H3C-SR88-01

# 三阶段，均支持 --dry-run
./target/release/cwa console        --dry-run          # 阶段一：Console 开局
./target/release/cwa enable-netconf --dry-run          # 阶段二：SSH 使能 NETCONF
./target/release/cwa deploy-ip      --dry-run          # 阶段三：接口 IP
./target/release/cwa deploy-ospf    --dry-run          # 阶段三：OSPF

# 回退（基于快照）
./target/release/cwa rollback-ip
./target/release/cwa rollback-ospf
```

单二进制约 18 MB，无运行时依赖。五个 Python 脚本入口统一为一个二进制 + 子命令。

## 结构

```
crates/
├── inventory/     Group 继承合并 + Host/Group/defaults 三级解析
├── templating/    补丁提取 + 分层路径解析 + minijinja 渲染
├── transport/     CLI(SSH) / NETCONF / Console(Telnet) 三通道
├── atoms/         四阶段原子操作 + 快照存储
└── scenes/        场景编排 + cwa 二进制入口
templates/         从 Python 项目原样复制，未改一字
inventory/         同上（groups.yaml 不再需要，继承在内存中展开）
```

## 依赖选型

| 层 | 方案 | 说明 |
| :--- | :--- | :--- |
| 并发 | `tokio` + `Semaphore` | 替代 Nornir threaded runner |
| 模板 | `minijinja` + `minijinja-contrib` | `pycompat` 提供 `startswith` 等 Python 字符串方法 |
| NETCONF | `rustnetconf` `=0.17.0` | `error_option` 为一等参数 |
| CLI | `rneter` `=0.5.2` | 内置 `h3c_comware` 设备模板 |
| Console | `telnet` `=0.2.5` | 仅协议层，状态机自写 |
| XML | `roxmltree` | 只读解析 |
| YAML | `serde_yaml_ng` | `serde_yaml` 已 deprecated |

传输层三个 crate 锁定精确版本，且调用全部收敛在 `transport` crate 内部，上层不直接依赖其类型（供应链隔离，见 `MIGRATION_PLAN.md` 6.2）。

## 已验证 / 未验证

### 已验证（离线）

- 全 workspace 编译通过，`cargo clippy --all-targets` 零警告
- **Group 继承合并结果与 Python 版语义等价**（同一份 YAML，展开后逐键比对相同）
- **模板渲染与 Python 版逐字节一致**：
  - `cmd/bootstrap.j2` 5 层继承链（533 B，含 CRLF 行尾）
  - `cmd/netconf_cmd.j2`（286 B）
  - `netconf/ospf_xml.j2`（1003 B）
- `extends` / `block` / `super()` / `import as macros` / `startswith` 全部生效
- 独立块解析：正确切分 3 个 `#INDEPENDENT` 块与 1 个普通块
- XML 解析：BOM 剥离、命名空间、`Ipv4Address` 容器与同名叶子节点区分
- 回退计划推导：IP 两种快照语义、Loopback 是否本次创建、OSPF 逐层删除 XML
- 快照往返：原子写入 → 读回 → 删除
- 错误检测：正确区分 `% Unrecognized command`（报错）与 ` ip address 1.1.1.1`（正常配置行）
- 全部 9 个子命令可执行

### 未验证（需真机）

- Comware 在 `rustnetconf` `GenericVendor` profile 下的 hello 能力协商
- `commit` 时序、`rollback-on-error` 在 Comware 上的实际行为
- `rneter` h3c 提示符正则对真实 SR88 R7171 输出的匹配度
- 80 字符换行问题（模板中 `sysname` 仍置于末行以保留 Python 版的规避手法）
- Console Telnet 的 ZTP 中断时序、`Press ENTER` 交互
- 所有真实的下发与回退路径

## 相对 Python 版补齐的能力

以下均为 `nornir-comware-automation/need_to_discuss_prob.md` 中记录为"待讨论 / 未实现"的项，在本实现中已完成。**Python 项目未做任何改动。**

| 开放问题 | 本实现的处理 |
| :--- | :--- |
| 延伸① OSPF 未走 Atom 抽象 | 新增 `NetconfOspfAtom`，获得幂等判断、回退前依赖检查 |
| 延伸① 两条路线快照语义不一致 | 统一为 `Snapshot` 只记变更前状态、`Desired` 记期望状态，两者分离 |
| 延伸② `NetconfLoopbackAtom` 半成品 | 明确废弃纯 NETCONF 建接口路线，统一走"CLI 建接口 + NETCONF 配 IP"并纳入 Atom |
| 延伸② `post_check` 的 `desired` 语义不一致 | `Desired` 提升为 trait 关联类型，由 `pre_check` 产出、`post_check` 消费 |
| 延伸③ cmd 下发无错误检测 | `check_output_for_errors()` + `rneter` 模板双重校验，任一命令报错即中止 |
| 延伸③ 缺"快照 → 回退步骤"映射 | 新增 `RollbackPlan` / `plan_rollback()`，回退步骤可推导、可预览、可审计 |
| 延伸⑥ `<config>` 外壳两种包法 | 查 ncclient 源码确认 Python 侧 `atoms/base.py` 那条分支会抛 `XMLError`；本实现统一传裸片段并自动剥除误传外壳 |
| 新发现① `PathResolver` 硬编码 SR88 | 改为从 Group 名动态推导厂商/产线/OS 版本坐标，不再绑定单一产线 |
| 新发现③ `logs/` 混放调试与业务快照 | 拆为 `state/snapshots/`（业务，原子写入）与 `logs/debug/`（可清理） |
| `pre_validate_groups` 空实现 | 补上跨厂商继承隔离与父子类型一致性两项校验 |

另外补充了 Python 版没有的几处：

- **`discard_changes()`**：Python 版 `edit_config` 成功但 `commit` 失败时会残留脏 candidate，本实现在失败路径自动丢弃
- **Loopback 存在性真实查询**：Python 版 `get_cli_interface()` 是 `return None` 空实现，导致 `pre_check` 永远认为接口不存在
- **OSPF 回退依赖检查**：若进程在变更前已存在（非本次创建），拒绝删除。Python 版会无条件删除整个进程
- **规划表外部化**：Python 版 `test_ospf_deploy_full.py` 反向 `import DEVICE_CONFIG`，形成场景脚本间耦合；本实现改为 `plan.yaml`（不存在时回落内置默认值，数值与 Python 版一致）

### 保持与 Python 版一致的"未实现"

`atoms/utils.py` 中 `get_interface_dependencies()` / `check_cli_dependencies()` 是 `return []` 空实现，即 IP 与 Loopback 的回退依赖检查从未真正生效。本实现**保持同样行为（始终放行）**，但在代码中显式记录这一事实，而不是伪装成已检查。补齐它需要先确定"哪些配置算依赖 IP"，属设计决策，留待讨论。

## 测试

按要求**未编写测试代码**。上述"已验证"项通过 `examples/` 下两个可运行示例与临时探针完成，探针已清理，保留：

```bash
cargo run -p cwa-inventory  --example dump_inventory -- .   # Group 展开 + Host 解析
cargo run -p cwa-templating --example render_probe           # 分层查找 + 渲染
```

与 Python 版做逐字节比对的方法记录在 `MIGRATION_PLAN.md` 第 3 章（黄金文件比对）。
