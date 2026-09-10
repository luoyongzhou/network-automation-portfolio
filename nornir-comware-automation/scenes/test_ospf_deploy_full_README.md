# test_ospf_deploy_full.py

## 概述

`test_ospf_deploy_full.py` 是 **H3C SR88 OSPF 配置的 NETCONF 原子化下发与精确回退** 测试脚本，是 `test_ip_deploy_full.py` 之后的下一个业务层：先用 IP 脚本把 Loopback 和物理接口的 IP 落地，再在这些已有 IP 的接口上启用 OSPF。

它负责：
1. 基于 `test_ip_deploy_full.DEVICE_CONFIG` 中的接口 IP 规划，为每台设备构建 OSPF 配置（进程 + 区域 + 接口三层结构）
2. 使用 H3C 私有 YANG 模型（非 OpenConfig）通过 NETCONF 下发，并启用 `rollback-on-error` 保证单次下发的原子性
3. 回退时逐层精确删除（接口 → 区域 → 进程），补偿 H3C YANG 模型"删除不做级联"的限制
4. 多设备通过 Nornir 并发执行

这个脚本背后完整的调试过程、报错信息、根因分析和方案演进，已经写成了一份约 42000 字的独立文档：[`summary.md`](./summary.md)。本 README 只总结代码层面"现在是怎么工作的"，遇到 XML 报错、模型选型疑问时请优先查阅 `summary.md`。

## 使用方法

```bash
# 预览 OSPF 配置 XML（不下发）
python scenes/test_ospf_deploy_full.py preview [device]

# 下发 OSPF 配置（merge + rollback-on-error）
python scenes/test_ospf_deploy_full.py test-ospf [device]

# 回退 OSPF 配置（逐层删除，continue-on-error）
python scenes/test_ospf_deploy_full.py rollback-ospf [device]
```

`[device]` 可省略（默认对 `DEVICE_CONFIG` 中列出的全部设备执行）。

## 依赖前置：为什么必须先跑 `test_ip_deploy_full.py`

本脚本没有自己的 IP 规划表，而是直接 `from scenes.test_ip_deploy_full import DEVICE_CONFIG` 复用 IP 测试脚本的配置：

```python
from scenes.test_ip_deploy_full import DEVICE_CONFIG
```

`build_ospf_config()` 会读取 `DEVICE_CONFIG` 中的 Loopback IP（作为 OSPF Router ID）和物理接口列表（作为 OSPF 区域内的接口），并通过 `get_ifindex_map()` 把接口名转换为 OSPF 模型所需的 `ifindex`。如果这些接口尚未配置 IP（`test_ip_deploy_full.py` 还没跑过），OSPF 下发本身不会报错（OSPF 模型不检查 IP 是否存在），但业务上是没有意义的——没有 IP 的接口跑 OSPF 邻居发现不了对端。两个脚本的执行顺序是：

```
test_ip_deploy_full.py test-netconf   (先把 IP 落地)
        ↓
test_ospf_deploy_full.py test-ospf    (再在这些接口上启用 OSPF)
```

## OSPF 配置模型：三层结构

`build_ospf_config()` 生成的配置字典严格对应 H3C 私有 YANG 模型（命名空间 `http://www.h3c.com/netconf/config:1.0`）的三层结构，与 `templates/product_lines/h3c/SR88/netconf/ospf_xml.j2` 一一对应：

```
Instances（OSPF 进程）
  └─ Name: "1"，RouterId: <Loopback IP>
Areas（OSPF 区域）
  └─ Name: "1"（关联进程），AreaId: "0.0.0.0"，AreaType: 0（Normal）
Interfaces（参与 OSPF 的接口）
  ├─ Loopback（若 ifindex_map 中存在）：仅加入区域，不设 NetworkType
  └─ 每个物理接口（若 ifindex_map 中存在）：加入区域，NetworkType = 3（P2P）
```

**为什么物理接口固定用 P2P（NetworkType=3）**：脚本中的测试拓扑是路由器间的点对点直连链路（见根目录 `实验topo.jpg`），P2P 网络类型不需要 DR/BDR 选举，邻居建立更快、更适合这种拓扑。如果目标网络是多路访问网段（如接了交换机的以太网段），需要在 `build_ospf_config()` 里为对应接口去掉 `network_type` 字段或改为其他值。

**为什么选择 H3C 私有模型而不是 OpenConfig**：详见 `summary.md` 第四章，结论是当前测试设备的固件版本对 OpenConfig OSPF 模型支持不完整（下发直接报 `The data model is not supported`），私有模型是当前唯一验证通过的路径。

## 下发：merge + rollback-on-error

```python
session.edit_config(
    config=config_xml,
    target="candidate",
    error_option="rollback-on-error"
)
session.commit()
```

- 使用 `merge` 而非 `create`：因为 OSPF 进程可能已因为之前的测试或手工配置存在，`create` 会报 `Configuration already exists`，`merge` 天然幂等（存在则更新，不存在则创建）
- `error_option="rollback-on-error"`：只保证**单次 `edit_config` 调用内部**的原子性——如果这次提交的 XML 里任何一个元素校验失败，整个 candidate 会话自动丢弃，恢复到调用前状态。这个保证**不会**跨越多次独立的 RPC 调用，也**不会**跨越多台设备（Nornir 并发下发时，一台设备失败不会导致其他设备回滚），详见 `summary.md` 第八章"事务边界"表格
- 下发成功后立即保存完整的 `ospf_config` 字典到 `logs/snapshots_<device>_ospf.json`——注意这里保存的是**期望配置的结构化数据**，不是 XML 文本本身，回退时会依据这份数据重新构造删除 XML

## 回退：逐层精确删除 + 仅指定索引列

回退函数 `task_rollback_ospf` 手工拼接删除 XML（没有走 Jinja2 模板），是本脚本中最能体现"H3C YANG 模型删除语义"的部分：

```
<config>
  <OSPF xmlns="http://www.h3c.com/netconf/config:1.0">
    <Interfaces>
      <Interface nc:operation="delete"><IfIndex>132</IfIndex></Interface>   ← 仅索引列 IfIndex
      <Interface nc:operation="delete"><IfIndex>2</IfIndex></Interface>
    </Interfaces>
    <Areas>
      <Area nc:operation="delete">
        <Name>1</Name>            ← 索引列 1
        <AreaId>0.0.0.0</AreaId>  ← 索引列 2（Area 是复合索引）
      </Area>
    </Areas>
    <Instances>
      <Instance nc:operation="delete"><Name>1</Name></Instance>   ← 仅索引列 Name
    </Instances>
  </OSPF>
</config>
```

背后的硬性规则（`summary.md` 第七章有完整的报错演进记录，这里只记结论）：

1. **删除操作只能携带索引列**，携带任何非索引列字段（如给 `Interface` 删除时附带 `IfEnable`）都会报 `data cannot be assigned to non-index columns`
2. **没有级联删除**：`Instances`、`Areas`、`Interfaces` 在模型中是平级表，删除进程（`Instance`）不会自动清理关联的接口 OSPF 配置，必须三层都显式删除
3. **删除顺序在这个模型下不敏感**（不同于很多关系型数据的外键约束），但脚本仍按"接口 → 区域 → 进程"从末端到根的顺序删除，符合直觉、便于阅读

删除操作使用 `error_option="continue-on-error"`（而不是下发时的 `rollback-on-error`），因为回退场景下"某一项本来就不存在"是可接受的正常情况（比如之前的回退已经删过、或者本来就没配置成功），不应该因为这类错误中断整个回退流程。

## 快照与幂等的局限性

与 `test_ip_deploy_full.py` 不同，本脚本的下发逻辑**没有做"目标配置是否已存在"的幂等检查**——`merge` 操作本身对重复下发是安全的（不会报错），但脚本不会在下发前查询"OSPF 是否已经按预期配置"，也就是说：

- 重复执行 `test-ospf` 是安全的（`merge` 幂等），但每次都会重新走一遍完整的 `edit_config` + `commit`，不会打印"已存在，跳过"这类提示
- 如果只想确认当前 OSPF 状态是否符合预期，需要用 `preview` 命令查看即将下发的 XML，或直接登录设备执行 `display ospf peer` / `display current-configuration` 交叉验证（`summary.md` 第九章有实际验证时的设备侧配置和邻居状态输出示例）

## 与 `atoms/` 目录的关系

本脚本和 `test_ip_deploy_full.py` 都是**独立的场景脚本**，直接调用 `atoms/utils.py` 的工具函数（如 `get_netconf_session`、`get_ifindex_map`）和 `scripts/preview_bootstrap.py` 的渲染引擎，但**没有**通过 `atoms/base.py` 中定义的 `Atom` / `NetconfAtom` 类来组织下发逻辑（对比 `atoms/h3c/netconf/interface_ip_address.py` 里 `NetconfIPAddressAtom` 的四阶段实现）。这意味着本脚本没有复用 `Atom` 抽象提供的"幂等检查 → 下发 → 校验 → 回退"标准骨架和依赖检查安全门，而是把这些逻辑直接写在 `task_deploy_ospf` / `task_rollback_ospf` 函数体内。这是当前代码的真实状态，如果要把 OSPF 场景重构为符合 `atoms/` 范式的 `NetconfOspfAtom`，需要重新设计快照结构（当前直接存整个 `ospf_config` 字典，而不是像 IP 场景那样存"变更前的原始状态"），详见 [`atoms/README.md`](../atoms/README.md) 中"快照驱动回退"一节的设计原则。
