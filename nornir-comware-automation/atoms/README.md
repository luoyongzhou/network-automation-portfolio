# 原子操作（Atom）开发范式约束

本目录存放所有"配置变更原子操作"的实现。每个 Atom 封装一次**具备完整生命周期**的设备配置变更：幂等检查 → 下发 → 校验 → 回退，是本框架实现"可靠变更"的核心抽象层。

## 目录结构（厂商 + 协议分层）

```
atoms/
├── base.py                        # 抽象基类：Atom / CmdAtom / NetconfAtom
├── utils.py                       # 工具函数：NETCONF 会话、ifindex/IP 查询、CLI 查询占位
├── __init__.py
├── h3c/                            # H3C 原子操作实现（当前唯一有实现的厂商）
│   ├── cmd/                        # 基于 Netmiko（CLI）的原子操作
│   │   ├── interface_ip_address.py
│   │   └── interface_loopback.py
│   └── netconf/                    # 基于 ncclient（NETCONF）的原子操作
│       ├── interface_ip_address.py
│       └── interface_loopback.py
├── huawei/                         # 预留：华为原子操作（cmd/、netconf/ 均为空目录）
│   ├── cmd/
│   └── netconf/
└── cisco/                          # 预留：Cisco 原子操作（cmd/、netconf/ 均为空目录）
    ├── cmd/
    └── netconf/
```

**目录对称原则**：与 `templates/` 保持同构——厂商级子目录下按协议分为 `cmd/`（CLI）与 `netconf/`（NETCONF XML），每种协议下按"场景"（接口 IP、Loopback、OSPF……）拆分独立文件，一个场景一个 Atom 类。`huawei/`、`cisco/` 当前是空目录，表示协议分层结构已预留，具体 Atom 实现待补充。

## 设计原则

### 1. 生命周期四阶段（模板方法模式）

`Atom`（`base.py`）是所有原子操作的抽象基类，定义了固定的执行骨架 `execute()`，子类只需实现四个抽象方法：

```
execute()
  ├─ 1. pre_check()   幂等检查 + 生成快照（变更前的状态）
  ├─ 2. deploy()      下发配置（依赖 pre_check 返回的快照）
  ├─ 3. post_check()  校验配置是否真正生效
  └─ 4. rollback()    出错时基于快照回退（仅当 auto_rollback=True 时自动触发）
```

`pre_check()` 的返回状态决定后续流程走向：

| `pre_check()` 返回状态 | `execute()` 行为 |
| :--- | :--- |
| `blocked` | 直接返回 `failed`，不执行 `deploy` |
| `skipped` | 直接返回 `skipped`（幂等：目标状态已存在，无需变更） |
| `ready` | 携带 `snapshot` 继续执行 `deploy` → `post_check` |

`deploy()` 或 `post_check()` 失败时，若调用 `execute(host, auto_rollback=True)`，会自动调用 `rollback()` 并返回 `rolled_back` 状态；否则直接返回 `failed`，由上层场景脚本决定是否手动回退。

### 2. 快照驱动回退（Snapshot-driven Rollback）

`pre_check()` 必须在 `snapshot` 中记录**变更前的原始状态**（如 `current_ip`），`rollback()` 完全依赖这份快照决定动作，而不是重新查询当前状态。典型分支逻辑（见 `h3c/netconf/interface_ip_address.py::_rollback_impl`）：

```python
if snapshot.get("current_ip"):
    # 变更前已有 IP → 回退 = 恢复原 IP
else:
    # 变更前没有 IP（本次是新建）→ 回退 = 删除
```

这种设计使回退行为具备确定性：无论 `deploy()` 执行到哪一步失败，`rollback()` 都能依据同一份快照给出正确的补偿动作。

### 3. 协议层封装（`CmdAtom` / `NetconfAtom`）

`Atom` 之下有两个协议专属的中间基类，封装了各自协议"渲染模板 + 下发"的公共逻辑，具体 Atom 子类只需指定模板路径和上下文，不需要重复写渲染/发送代码：

| 中间基类 | 封装的公共方法 | 底层依赖 |
| :--- | :--- | :--- |
| `CmdAtom` | `_render_and_send()`：渲染 CLI 模板 → `net_connect.send_command()` | Netmiko |
| `NetconfAtom` | `_edit_config()`：渲染 XML 片段 → `session.edit_config(target="candidate")` | ncclient |

**注意**：`NetconfAtom._edit_config()` 传递给 `edit_config()` 的 XML **不能**包含外层 `<config>` 标签（ncclient 会自动包装），这是本框架在 `need_to_discuss_prob.md` 中提到的"ncclient 不是哑管道"问题的具体体现——直接影响模板设计（`netconf/_fragments/*.j2` 只写片段，不写 `<config>` 外壳）。

### 4. 回退前的依赖检查（Rollback Safety Gate）

删除类回退操作（如删除 Loopback 接口、删除 IP）执行前，会先做依赖检查，防止误删仍被其他业务（如路由协议）引用的配置：

```python
def rollback(self, host, snapshot, net_connect, **kwargs):
    rollback_check = self._rollback_pre_check_impl(host, snapshot, **kwargs)
    if rollback_check["status"] == "blocked":
        return {"status": "blocked", "reason": rollback_check.get("reason")}
    return self._rollback_impl(host, snapshot, net_connect, **kwargs)
```

`check_cli_dependencies()` / `get_interface_dependencies()`（`utils.py`）是这一机制的钩子，当前为占位实现（返回空列表），预留给后续接入"查询该接口是否被 OSPF/ACL 等其他配置引用"的真实逻辑。

### 5. `pre_check` 参数签名由场景自行定义

`Atom.pre_check(self, host, **kwargs)` 只约束了 `host` 是必需参数，具体需要哪些业务参数（`ifname`、`ipv4_address`、`ifindex`……）由每个子类自行声明。这是有意为之的松耦合设计：不同场景的输入差异很大（IP 地址操作需要 `ifname`+`ipv4_address`+`ipv4_mask`，Loopback 创建需要 `ifindex`+`description`），强行统一签名反而会引入大量无意义的可选参数。

## 现有 Atom 一览（H3C）

| 文件 | 类名 | 场景 | 协议 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| `h3c/cmd/interface_ip_address.py` | `CmdIPAddressAtom` | 接口配置 IPv4 地址 | CLI（Netmiko） | 完整实现（四阶段 + 依赖占位） |
| `h3c/cmd/interface_loopback.py` | `CmdLoopbackAtom` | 创建 Loopback 接口（可选带 IP） | CLI（Netmiko） | 完整实现（含回退前依赖检查） |
| `h3c/netconf/interface_ip_address.py` | `NetconfIPAddressAtom` | 接口配置 IPv4 地址 | NETCONF（ncclient） | 完整实现（四阶段 + 依赖检查钩子） |
| `h3c/netconf/interface_loopback.py` | `NetconfLoopbackAtom` | 创建 Loopback 接口（含真实 ifindex 二次查询） | NETCONF（ncclient） | **不完整**：只实现了 `deploy_pre_check()` / `deploy()`，未实现标准的 `pre_check()` / `post_check()` / `rollback()`，方法名与 `Atom` 抽象基类不一致，当前无法通过 `execute()` 统一调度，需要在扩展新场景时对齐 |

## 强约束：绝对禁止行为

### ❌ 约束 1：禁止在 `deploy()` 中跳过快照直接操作

`deploy()` 的第二个参数必须是 `pre_check()` 产出的 `snapshot`，不允许在 `deploy()` 内部重新查询"当前状态"后直接下发——这会破坏"快照驱动回退"的一致性保证，导致 `rollback()` 时依据的状态与实际变更前状态不符。

### ❌ 约束 2：禁止 `rollback()` 跳过依赖检查直接执行删除

任何会删除/清空配置的回退动作，必须先经过 `_rollback_pre_check_impl()`（或等价的依赖检查钩子），返回 `blocked` 时必须终止，不能强行继续删除。

### ❌ 约束 3：禁止跨协议复用模板路径

`CmdAtom` 子类的模板路径必须指向 `templates/.../cmd/` 下的文件，`NetconfAtom` 子类必须指向 `templates/.../netconf/` 下的文件。两种协议的模板内容格式完全不同（文本 vs XML 片段），混用会导致渲染出的内容无法被对应协议正确下发。

### ❌ 约束 4：禁止在 `NetconfAtom._edit_config()` 的模板中包裹 `<config>` 外层标签

`ncclient` 的 `session.edit_config()` 会自动包装 `<config>` 标签，模板（`netconf/_fragments/*.j2`）只应渲染标签内部的 XML 片段。重复包裹会导致 XML 结构错误。

## 弱约束：推荐做法

### 推荐 1：新增厂商时先补 `cmd/` 和 `netconf/` 空目录再实现

参照 `huawei/`、`cisco/` 当前的预留方式，新增厂商时先建好协议分层的空目录结构，再逐个补充具体场景的 Atom 实现，保持目录结构在任何开发阶段都是自解释的。

### 推荐 2：新场景优先参考 `interface_ip_address.py` 的完整实现

`h3c/cmd/interface_ip_address.py` 和 `h3c/netconf/interface_ip_address.py` 是当前四阶段生命周期实现最完整、最规范的参考样例（幂等检查、快照、校验、回退全部到位），新增 Atom 时建议以它们为模板，而不是 `netconf/interface_loopback.py`（如上表所述，该文件的生命周期实现不完整）。

### 推荐 3：`utils.py` 中的占位函数在接入真实逻辑前保持返回空值

`get_interface_dependencies()`、`get_cli_interface()`、`check_cli_dependencies()` 等函数当前返回 `None` / `[]` 占位。在没有接入真实的依赖查询逻辑之前，不要为了让流程"看起来通过"而返回伪造的非空数据——这会让回退安全门（约束 2）失效。

## 常见问题

### Q1：`Atom` 和 `CmdAtom` / `NetconfAtom` 是什么关系？

`Atom` 是协议无关的抽象基类，只定义生命周期骨架。`CmdAtom` / `NetconfAtom` 继承 `Atom`，是"协议专属的半成品基类"——封装了各自协议的渲染+下发公共方法（`_render_and_send` / `_edit_config`），但仍然是抽象类（`pre_check`/`deploy`/`post_check`/`rollback` 未实现），具体场景的 Atom 类需要再继承其中一个并补全四个抽象方法。

### Q2：为什么 `execute()` 里 `post_check` 的第三个参数传的是 `kwargs` 而不是 `desired`？

见 `base.py` 中 `self.post_check(host, snapshot, kwargs, **kwargs)` 这一行——这是当前实现中的一个可辨认的设计随意点：`post_check` 的形参名叫 `desired`（期望状态），但 `execute()` 实际传入的是原始 `kwargs`（调用参数字典），而不是一个明确构造的"期望状态"对象。目前各 Atom 子类的 `_post_check_impl` 并未使用 `desired` 参数（校验逻辑改为直接从 `snapshot` 里取期望值，如 `snapshot["ipv4_address"]`），所以没有暴露出问题，但如果后续要用到 `desired` 参数，需要注意它当前的真实取值。

### Q3：一个 Atom 只能对应一个模板吗？

不是。`CmdLoopbackAtom` 就是反例——它在 `deploy()` 中依次渲染并下发了两个模板（`CREATE_TEMPLATE` 创建接口，再 `IP_TEMPLATE` 配置 IP），回退时也对称地依次调用 `ROLLBACK_IP_TEMPLATE` 和 `ROLLBACK_INTERFACE_TEMPLATE`。一个 Atom 对应"一个可独立幂等验证、独立回退的业务变更单元"，可能涉及多个模板的顺序编排。

### Q4：新增一个 H3C 的场景（如 ACL），应该怎么组织代码？

1. 在 `atoms/h3c/cmd/`（或 `netconf/`）下新建 `acl.py`
2. 定义 `class CmdAclAtom(CmdAtom)`（或 `NetconfAclAtom(NetconfAtom)`）
3. 声明该场景用到的模板路径类属性（如 `DEPLOY_TEMPLATE = "product_lines/h3c/SR88/cmd/acl_cmd.j2"`），确认对应模板文件已存在于 `templates/` 下（见 [`templates/README.md`](../templates/README.md)）
4. 实现 `pre_check` / `deploy` / `post_check` / `rollback` 四个方法，参考 `interface_ip_address.py` 的实现模式
5. 在场景脚本（`scenes/*.py`）中实例化该 Atom 并调用 `execute()`

### Q5：为什么 `huawei/`、`cisco/` 目录是空的？

当前项目的实际验证环境是 H3C SR88（见根目录 `实验topo.jpg` 和 `inventory/hosts.yaml`），华为、Cisco 的原子操作尚未开发。保留空目录是为了让协议分层的目录结构（`cmd/`/`netconf/`）在多厂商场景下保持一致，方便后续按同样的范式补充实现，而不是等到真正开发时再决定目录怎么摆。

---

*本文档描述的是当前代码的真实状态（包括已知的不一致之处，如 Q2、`NetconfLoopbackAtom` 的生命周期缺口），不是理想化的设计规范。补全或修改 `atoms/` 下的实现后，请同步更新本文档对应章节。*
