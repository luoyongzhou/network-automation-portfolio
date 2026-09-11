# cwa-inventory

分层继承的设备清单加载与 Group 继承合并。

对应 Python 版 [`scripts/build_inventory.py`](../../../nornir-comware-automation/scripts/build_inventory.py) + Nornir 的 Inventory 语义。

## 概述

本 crate 承担两件事：

1. **Group 继承合并**：扫描 `inventory/groups/**/*.yaml`，递归展开 `parents` 链，深度合并，最终只保留 `os_versions` 层的 Group
2. **Host 三级解析**：按 `defaults → groups（声明顺序）→ host` 的优先级合并，产出可直接使用的 `Host` 结构

这是整个项目**唯一完全不接触设备**的一层，因此也是最容易验证的一层——同一份 YAML 输入，Python 版与 Rust 版的输出可以做逐键比对。

## 使用方法

```rust
use cwa_inventory::{build_groups, dump_groups_yaml, Inventory};
use std::path::Path;

let root = Path::new(".");

// 方式一：只做 Group 继承展开（对应 build_inventory.py）
let (groups, report) = build_groups(&root.join("inventory/groups"))?;
for w in &report.warnings {
    eprintln!("警告: {w}");
}
print!("{}", dump_groups_yaml(&groups)?);

// 方式二：加载完整 Inventory（Group 展开 + Host 解析）
let inv = Inventory::load(root)?;
let host = inv.hosts.get("H3C-SR88-01").unwrap();
println!("{} {} {}", host.name, host.hostname, host.username);
println!("NETCONF 端口: {}", host.netconf_port());

// 按名称过滤，对应 Python 版 nr.filter(F(name=...))
let filtered = inv.filter_names(&["H3C-SR88-01".to_string()])?;
```

命令行验证：

```bash
cargo run -p cwa-inventory --example dump_inventory -- .
```

## 核心流程

```
inventory/groups/**/*.yaml
        │
        │ ① 递归扫描，跳过 _ 前缀文件
        │ ② 按 (路径深度, 文件名) 排序 —— 保证重复 Group 覆盖顺序可复现
        ▼
   raw_groups: BTreeMap<String, RawGroup>
        │      （RawGroup 携带 source_dir，用于最终过滤）
        │
        │ ③ pre_validate_groups() 结构预校验
        ▼
        │ ④ expand_inheritance() 递归展开 parents 链
        │    processing 集合检测循环继承
        ▼
   expanded: BTreeMap<String, Mapping>
        │
        │ ⑤ 过滤 source_dir == "os_versions"
        ▼
   版本层 Group（供 Host 引用）
        │
        │ ⑥ defaults → groups → host 三级合并
        ▼
   Inventory { hosts: BTreeMap<String, Host> }
```

## 深度合并算法（`deep_merge`）

与 Python 版行为一致：**仅当双方同一 key 都是映射时才递归合并，否则由 override 整体替换**。

```rust
pub fn deep_merge(base: &Mapping, override_map: &Mapping) -> Mapping
```

```yaml
# base（父组）
connection_options:
  netmiko:
    port: 22
    extras:
      timeout: 60
      device_type: hp_comware

# override（子组）
connection_options:
  netmiko:
    extras:
      timeout: 90        # 只想改这一项

# 合并结果 —— device_type 被保留
connection_options:
  netmiko:
    port: 22
    extras:
      timeout: 90
      device_type: hp_comware
```

**列表不做元素级合并**，整体替换。这与 Python 版一致，也是有意的：接口列表这类数据做元素级合并会产生难以预测的结果。

## 继承展开算法（`expand_inheritance`）

```rust
fn resolve_group(name, raw, expanded, processing) -> Result<Mapping, InventoryError>
```

三个状态集合协同工作：

| 集合 | 作用 |
| :--- | :--- |
| `expanded` | 已完成展开的 Group，命中即直接返回（记忆化，避免重复计算） |
| `processing` | 当前递归栈上的 Group，**再次进入即为循环继承** |
| `raw` | 原始定义，缺失即报 `UndefinedGroup` |

多父继承按**声明顺序**依次合并，最后叠加自身定义，并从结果中移除 `parents` 键：

```
merged = {}
for parent in parents:            # 按 YAML 中的声明顺序
    merged = deep_merge(merged, resolve_group(parent))
merged = deep_merge(merged, group_def)   # 自身定义优先级最高
merged.remove("parents")
```

即：**后声明的父组覆盖先声明的，子组覆盖所有父组**。

## 结构预校验（`pre_validate_groups`）

Python 版的 `pre_validate_groups()` 是空实现，只保留了 Hook 位置，注释里列了"未来扩展"的几项。本 crate 补上了其中两项：

### ① 跨厂商继承隔离（warning）

厂商段取 Group 名的第一个下划线分段。若子组与父组的厂商段不同，产出警告。

```yaml
# 触发警告：cisco_xxx 继承了 h3c_xxx
cisco_asr_ios_v15:
  parents:
    - h3c            # 厂商段不一致
```

设为 warning 而非 error，是因为存在合法的例外场景（比如某厂商设备沿用另一厂商的 CLI 方言），不应硬性阻断。

### ② 父子类型一致性（error）

父子同名 key 的 YAML 类型必须一致。这直接对应 `inventory/README.md` 里"禁止 2：用非字典类型覆盖父类的字典结构"那条约束。

```yaml
# vendors/h3c.yaml
h3c:
  connection_options:      # mapping
    netmiko:
      port: 22

# os_versions/.../R7171.yaml —— 触发 error
h3c_sr88_comware_v7_r7171:
  parents: [h3c_sr88_comware_v7]
  connection_options: "netmiko"    # string，类型不一致
```

设为 error 是因为这种覆盖会**静默丢掉整棵子树**，Python 版不会报错，后果是运行时才发现连接参数丢失。

### 引用完整性（error）

`parents` 引用了不存在的 Group 时报错。注意 Python 版的 `build_inventory.py` 也会在 `expand_inheritance` 阶段抛 `KeyError`，但**不校验 `hosts.yaml` 引用的 Group 是否存在**。本 crate 的 `Inventory::load()` 对 Host 引用未定义 Group 的情况产出 warning 而不中断，与 Python 版的宽松行为保持一致。

## 排序与确定性

文件加载顺序按 `(路径深度, 文件名)` 排序，与 Python 版的排序键一致：

```rust
files.sort_by_key(|path| {
    let depth = path.strip_prefix(groups_root).map(|r| r.components().count()).unwrap_or(0);
    let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    (depth, name)
});
```

这保证了两件事：

- 浅层（`vendors/`）先于深层（`os_versions/.../`）加载
- 同层内按文件名字典序，重复 Group 的覆盖结果可复现

内部全程使用 `BTreeMap` 而非 `HashMap`，输出顺序稳定，便于与 Python 版做 diff。

## 与 Python 版的差异

| 项 | Python 版 | 本 crate |
| :--- | :--- | :--- |
| 中间产物 | 每次运行都重写 `inventory/groups.yaml`，下游从磁盘读 | **继承在内存中展开**，不写盘，无中间产物依赖 |
| 预校验 | `pre_validate_groups()` 空实现 | 补上跨厂商继承与类型一致性校验 |
| 输出顺序 | dict 插入顺序 | `BTreeMap` 字典序（内容语义等价，key 顺序可能不同） |

`dump_groups_yaml()` 仍然提供，用于与 Python 版产物做比对。

## 数据结构

```rust
pub struct Host {
    pub name: String,
    pub hostname: String,
    pub username: String,
    pub password: String,
    pub platform: Option<String>,
    pub port: Option<u16>,
    pub groups: Vec<String>,                                    // 按 hosts.yaml 声明顺序
    pub data: Mapping,                                          // 合并后的 data 段
    pub connection_options: BTreeMap<String, ConnectionOptions>,
}
```

便捷访问方法，把 Python 版散落在各处的默认值逻辑收敛到一处：

| 方法 | 解析优先级 |
| :--- | :--- |
| `netconf_port()` | `ncclient.port` → 830 |
| `netconf_timeout()` | `ncclient.extras.timeout` → 60 |
| `ssh_port()` | `netmiko.port` → `host.port` → 22 |
| `data_str(key)` | `data` 段中的字符串项 |
| `connection(name)` | 命名连接，如 `netmiko_oob` / `ncclient` |

## 强约束：绝对禁止行为

### 禁止 1：绕过 `deep_merge` 直接覆盖 Mapping

```rust
// 错误：整体替换会丢掉父组的其他键
result.insert(key, value.clone());

// 正确：先判断双方是否都是 Mapping
match (result.get(key), value) {
    (Some(Value::Mapping(b)), Value::Mapping(o)) => deep_merge(b, o),
    _ => value.clone(),
}
```

### 禁止 2：在 `resolve_group` 中省略 `processing` 的插入/移除配对

`processing.insert()` 与 `processing.remove()` 必须严格配对。漏掉 `remove` 会导致同一 Group 被两个不同的子组引用时**误报循环继承**。

### 禁止 3：用 `HashMap` 替换 `BTreeMap`

会破坏输出顺序的确定性，使与 Python 版的比对失效。

### 禁止 4：把 Host 解析的优先级顺序写错

必须是 `defaults → groups（声明顺序）→ host`。Nornir 的语义就是这个顺序，写反会导致 Group 里的参数覆盖掉 Host 上的显式设置。

## 常见问题

### Q1：为什么 `vendors/`、`product_lines/` 里定义的 Group 不出现在最终结果里？

它们只作为**中间继承源**。最终过滤条件是 `source_dir == "os_versions"`，即只有放在 `inventory/groups/os_versions/` 下的 Group 才会保留，供 `hosts.yaml` 引用。

这与 Python 版行为一致，设计意图是强制"设备只引用版本层 Group"，避免设备直接挂到厂商层导致版本差异无处安放。

### Q2：如果两个文件定义了同名 Group 会怎样？

后加载的覆盖先加载的，并产出 warning（`tracing::warn!`）。加载顺序由 `(路径深度, 文件名)` 决定，因此结果是确定的，但仍应避免——warning 存在的意义就是提示你这是个意外。

### Q3：为什么不再生成 `inventory/groups.yaml`？

Python 版每个脚本入口都会先调 `merge_group_files()` 重写这个文件，属于"每次运行都产生副作用"。本 crate 在内存中完成展开，`groups.yaml` 不再是必需的中间产物。

若需要与 Python 版比对，用 `cwa groups` 输出到文件即可。

### Q4：类型一致性校验会不会误报？

只在**父子都定义了同名 key** 时才比较。子组新增父组没有的 key 不会触发。目前的实现比较的是 YAML 类型（mapping / sequence / string / number / bool / null），不做更细的结构校验。

一个已知的宽松点：`null` 与其他类型比较会报错。若确实需要用 `null` 显式清空父组的某个值，当前会被判为类型不一致——这种场景尚未出现，若出现应调整为白名单放行。

### Q5：`Inventory::load()` 遇到 Host 引用了不存在的 Group 会怎样？

产出 warning 后继续，该 Group 的配置不参与合并。这与 Python 版一致（Nornir 的 `SimpleInventory` 也不会因此中断）。

如果因此导致 `hostname` 缺失，才会报 `MissingHostField` 错误。

### Q6：如何验证本 crate 的输出与 Python 版一致？

见 [`../../TESTING.md`](../../TESTING.md) 的用例 T1.1 / T1.2。核心思路是同一份 YAML 分别用两边展开，再做逐键比对（而非逐字节，因为 key 顺序不同）。
