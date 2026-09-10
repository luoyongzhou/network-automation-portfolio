# test_ip_deploy_full.py

## 概述

`test_ip_deploy_full.py` 是 **H3C SR88 接口 IP 地址下发与回退** 的完整测试脚本，是 `bootstrap_step1/2` 完成开局与 NETCONF 初始化之后的下一个环节：设备已经可以通过 SSH（CLI）和 NETCONF 双通道访问，本脚本在此基础上验证"业务配置层"的下发与回退能力。

它负责：
1. 按 `DEVICE_CONFIG` 中预定义的 IP 规划，为每台设备创建 Loopback 接口并配置物理接口 IP
2. 同时验证两条技术路线：**纯 CLI（Netmiko）** 与 **CLI 创建接口 + NETCONF 配置 IP 的混合路线**
3. 每次下发后落地 JSON 快照，回退时基于快照精确还原到变更前状态
4. 多设备通过 Nornir 并发执行，日志按设备聚合后统一输出

## 使用方法

```bash
# 预览配置（不下发，同时展示 CMD 和 NETCONF 两种渲染结果）
python scenes/test_ip_deploy_full.py preview [device]

# CMD 路线下发（Netmiko 创建接口 + 配置 IP，不回退）
python scenes/test_ip_deploy_full.py test-cmd [device]

# NETCONF 路线下发（CLI 创建接口 + NETCONF 配置 IP，不回退）
python scenes/test_ip_deploy_full.py test-netconf [device]

# CMD 路线全量回退（基于 logs/snapshots_<device>_cmd.json）
python scenes/test_ip_deploy_full.py rollback-cmd [device]

# NETCONF 路线全量回退（基于 logs/snapshots_<device>_netconf.json）
python scenes/test_ip_deploy_full.py rollback-netconf [device]
```

`[device]` 可省略（默认对 `DEVICE_CONFIG` 中列出的全部设备执行），也可指定一个或多个设备名进行范围限定。

## IP 规划（`DEVICE_CONFIG`）

脚本内置了三台设备的静态 IP 规划表，作为本次测试的"期望状态"输入：

```python
DEVICE_CONFIG = {
    "H3C-SR88-01": {
        "loopback": {"number": 1, "ip": "11.11.11.11", "mask": "255.255.255.255"},
        "interfaces": [
            {"ifname": "GigabitEthernet0/0/1", "ip": "10.0.0.0", "mask": "255.255.255.254"},
            {"ifname": "GigabitEthernet0/0/2", "ip": "10.0.0.2", "mask": "255.255.255.254"},
        ]
    },
    # H3C-SR88-02 / 03 同构，IP 段依次递增
}
```

这份配置同时被 `test_ospf_deploy_full.py` 通过 `from scenes.test_ip_deploy_full import DEVICE_CONFIG` 直接复用（OSPF 需要在这些接口上启用），是两个场景之间的**唯一耦合点**——修改 IP 规划会同时影响两个脚本的行为，两者共享同一份"接口拓扑事实"。

## 核心流程

### CMD 路线（`test-cmd` / `rollback-cmd`）

```
1. 构建 groups.yaml，加载 Nornir Inventory
2. 逐设备：
   a. 创建 Loopback 接口（渲染 interface_create_cmd.j2，Netmiko 下发）
   b. 配置 Loopback IP（渲染 ip_address_cmd.j2，Netmiko 下发）
   c. 逐个物理接口：undo shutdown → 配置 IP
   d. 保存快照到 logs/snapshots_<device>_cmd.json
3. 回退时按相反顺序：先删物理接口 IP，再删 Loopback IP，最后删 Loopback 接口本身
   （物理接口本身不会被删除，只清除 IP——因为物理接口是设备固有资源，不是脚本创建的）
```

### NETCONF 路线（`test-netconf` / `rollback-netconf`）

```
1. 构建 groups.yaml，加载 Nornir Inventory
2. 通过 NETCONF 获取全部设备的 ifindex 映射（get_ifindex_map）
3. 逐设备：
   a. 若 Loopback 不存在：先用 CLI 创建（NETCONF 无法直接创建 Loopback 逻辑接口本身），
      再重新拉取一次 ifindex 映射以获得新建接口的真实 ifindex
   b. 用 NETCONF edit_config 配置 Loopback 和物理接口的 IP（merge 操作）
   c. 保存快照到 logs/snapshots_<device>_netconf.json（记录 ifindex，而非接口名）
4. 回退时通过 NETCONF edit_config（delete 操作）逐个删除 IP，
   Loopback 接口本身仍需通过 CLI 删除（同上，NETCONF 侵删接口本身超出本脚本封装范围）
```

## 关键设计：CLI 与 NETCONF 的分工边界

本脚本没有做到"纯 NETCONF"，而是刻意采用了混合路线，原因记录在根目录 `need_to_discuss_prob.md` 和本文档中：

| 操作 | 使用协议 | 原因 |
| :--- | :--- | :--- |
| 创建 / 删除 Loopback**接口本身** | CLI（Netmiko） | H3C 的 NETCONF `Ifmgr` 模型在测试环境中未验证支持"创建"逻辑接口，用 CLI 更可靠 |
| 配置 / 删除接口 **IP 地址** | NETCONF（ncclient） | IP 地址在 H3C 私有 YANG 模型（`IPV4ADDRESS` 表）中有明确的 merge/delete 语义，适合 NETCONF 的结构化操作 |
| 物理接口 `undo shutdown` | CLI（Netmiko） | 物理接口的存在性和 admin 状态用 CLI 直接确认最直观 |
| 获取接口索引（ifindex） | NETCONF（`get_ifindex_map`） | ifindex 是 NETCONF 操作 IP/OSPF 等业务配置时的必需索引，CLI 没有直接对应的查询命令 |

这个分工不是理论设计的产物，而是在实际调试中逐步确定的组合——`get_ifindex_map` 内部会把每次查询到的原始 XML 落盘到 `logs/ifindex_<device>.xml`，这是调试 H3C NETCONF 命名空间和 XPath 解析问题时留下的排障习惯，脚本本身也保留了这个行为。

## 快照结构与幂等性

两条路线的快照文件结构略有差异，但设计思路一致：**快照即"回退所需的全部信息"**，回退函数不重新查询设备当前状态，只依据快照决定动作。

```json
// logs/snapshots_<device>_cmd.json（按接口名索引）
{
  "loopback": {"ifname": "LoopBack1", "ipv4_address": "11.11.11.11", "ipv4_mask": "255.255.255.255"},
  "interfaces": [
    {"ifname": "GigabitEthernet0/0/1", "ipv4_address": "10.0.0.0", "ipv4_mask": "255.255.255.254"}
  ]
}

// logs/snapshots_<device>_netconf.json（按 ifindex 索引，因为 NETCONF 删除操作需要 ifindex）
{
  "loopback": {"ifindex": 132, "ipv4_address": "11.11.11.11", "ipv4_mask": "255.255.255.255", "logical_number": 1},
  "interfaces": [
    {"ifindex": 2, "ipv4_address": "10.0.0.0", "ipv4_mask": "255.255.255.254"}
  ]
}
```

下发前会先判断"目标 IP 是否已经配置"（`get_netconf_ip(host, ifindex) == ip`），已存在则跳过，这是本脚本层面的幂等保护——与 `atoms/` 目录中 `pre_check()` 返回 `skipped` 状态是同一设计思路的两种实现（`atoms/` 是封装为可复用类，本脚本是以场景脚本内联函数的形式实现，见根目录 [`atoms/README.md`](../atoms/README.md) 了解两者的关系）。

## 回退的安全边界

回退**只删除脚本自己创建/修改过的 IP 和 Loopback 接口**，对物理接口：
- 只清除 IP 地址
- **不会** 把物理接口改回 `shutdown` 状态

这是有意为之的保守策略——物理接口的 up/down 状态在真实网络中通常由其他因素（对端设备、光模块、业务需要）决定，回退脚本如果连带改变 admin 状态，容易造成超出预期的业务影响。日志中会显式打印"接口 IP 已删除，接口状态保持不变"来提醒这一点。
