
---

## 文件路径

`inventory/groups/README.md`

---

```markdown
# Groups 目录说明

本目录存放 Nornir 自动化框架中用于定义设备分组的 YAML 文件，通过 `build_inventory.py` 脚本自动合并并展开继承关系，生成 `groups.yaml` 供 Nornir 加载。

## 目录结构

```
inventory/groups/
├── vendors/                      # 厂商级基类
│   └── {vendor}.yaml             # 如 h3c.yaml, cisco.yaml
├── product_lines/                # 产线级中间类
│   └── {vendor}/
│       └── {product}.yaml        # 如 SR88.yaml, ASR.yaml
└── os_versions/                  # OS/版本级最终类（最终会输出到 groups.yaml）
    └── {vendor}/
        └── {os_version}.yaml     # 如 SR88_Comware_V7.yaml
```

## 设计原则

- **分层继承**：`vendors` → `product_lines` → `os_versions`，逐层细化
- **差异覆盖**：子类只声明与父类的差异部分，通用配置由父类提供
- **最终输出**：只有 `os_versions/` 目录下的 Group 会被输出到 `groups.yaml`
- **类型即安全**：子类必须保持与父类相同的数据类型结构，否则 `deep_merge` 会执行静默覆盖

---

## 类型约束

`build_inventory.py` 使用 `deep_merge` 函数进行递归合并。其核心判断逻辑是：

```python
if key in result and isinstance(result[key], dict) and isinstance(value, dict):
    result[key] = deep_merge(result[key], value)   # 递归合并
else:
    result[key] = deepcopy(value)                   # 直接覆盖
```

**只有当父子两层的值都是字典时，才会进入递归合并；否则直接覆盖。**

### 错误示例（静默覆盖）

```yaml
# vendors/h3c.yaml（父类）
h3c:
  connection_options:              # ← 字典类型
    netmiko:
      device_type: hp_comware
      timeout: 60
```

```yaml
# os_versions/.../R7171.yaml（子类）❌ 错误！
h3c_sr88_comware_v7_r7171:
  connection_options: "override"   # ← 字符串类型，直接覆盖整个字典
  # 结果：device_type 和 timeout 全部丢失，Netmiko 无法建立连接
```

### 正确做法（类型对齐）

```yaml
h3c_sr88_comware_v7_r7171:
  connection_options:              # ← 保持字典类型
    netmiko:                       # ← 保持字典类型
      timeout: 120
```

### 工具链协作指引

`pre-validate` 钩子用于确保 `groups.yaml` 结构可靠。在代码审查和日常开发中，需要关注以下几点：

- **类型一致性**：对于父类中定义为字典的字段（如 `connection_options`、`data`、`extras`），子类必须同样使用字典结构
- **嵌套字段审查**：通过 `git diff` 对比父子层级的字段类型变化
- **CI 自动检查**：`build_inventory.py` 执行过程中会输出相关日志，便于在 PR 阶段捕获异常

---

## 文件编写规范

### 1. 每个厂商必须有独立的基类

基类放在 `vendors/{vendor}.yaml`，不声明 `parents` 字段，作为继承链的终止节点。

```yaml
# vendors/h3c.yaml
h3c:
  platform: hp_comware
  connection_options:
    netmiko:
      port: 22
      extras:
        device_type: hp_comware
        timeout: 60
```

### 2. 产线类继承厂商基类

产线类放在 `product_lines/{vendor}/{product}.yaml`，声明 `parents` 指向对应的厂商基类。

```yaml
# product_lines/h3c/SR88.yaml
h3c_sr88:
  parents: [h3c]
  data:
    product_line: SR88
  connection_options:
    netmiko:
      extras:
        timeout: 90
```

### 3. 版本类继承产线类（或更高层级）

版本类放在 `os_versions/{vendor}/{os_version}.yaml`，最终会被输出到 `groups.yaml`。

```yaml
# os_versions/h3c/SR88_Comware_V7.yaml
h3c_sr88_comware_v7:
  parents: [h3c_sr88]
  data:
    os_version: "V7"
    supports_netconf: true
  connection_options:
    ncclient:
      port: 830
      extras:
        device_params:
          name: hpcomware
```

### 4. 补丁版本可平级追加

补丁版本与基础版本平级存放，通过 `parents` 指向基础版本。

```
os_versions/h3c/
├── SR88_Comware_V7.yaml
└── SR88_Comware_V7_R7171.yaml
```

```yaml
# os_versions/h3c/SR88_Comware_V7_R7171.yaml
h3c_sr88_comware_v7_r7171:
  parents: [h3c_sr88_comware_v7]
  data:
    os_patch: "R7171"
  connection_options:
    ncclient:
      extras:
        timeout: 120
```

---

## 禁止行为清单

### ❌ 禁止 1：在 `os_versions` 之外定义最终 Group

**后果**：合并后该 Group 被过滤，`groups.yaml` 中缺失，Nornir 启动报 `KeyError`。

```yaml
# ❌ 错误：将设备引用的 Group 放在 vendors/ 下
# vendors/h3c.yaml
h3c_sr88_comware_v7:
  parents: [h3c]
```

### ❌ 禁止 2：用非字典类型覆盖父类的字典结构

**后果**：`deep_merge` 执行直接覆盖，父类嵌套参数全部丢失，且脚本不报错。

```yaml
# ❌ 错误：connection_options 写成字符串
h3c_sr88_comware_v7:
  connection_options: "override all"
```

```yaml
# ❌ 错误：extras 写成列表
h3c_sr88_comware_v7:
  connection_options:
    netmiko:
      extras: ["timeout", "device_type"]
```

```yaml
# ✅ 正确：保持字典结构，只覆盖需要修改的字段
h3c_sr88_comware_v7:
  connection_options:
    netmiko:
      extras:
        timeout: 120
```

### ❌ 禁止 3：继承链引用不存在的父组

**后果**：`expand_inheritance` 抛出 `KeyError`，合并中断。

```yaml
# ❌ 错误
h3c_sr88_comware_v7:
  parents: [h3c_unknown]
```

### ❌ 禁止 4：跨文件使用 YAML 锚点

**后果**：`yaml.safe_load` 抛出 `ConstructorError`，脚本崩溃。

```yaml
# vendors/h3c.yaml
.h3c_defaults: &h3c_defaults
  timeout: 60

# os_versions/.../R7171.yaml ❌ 错误
h3c_r7171:
  <<: *h3c_defaults
```

### ❌ 禁止 5：不同厂商的 Group 互相继承

**后果**：设备获取到错误的 `device_type`，连接失败。

```yaml
# ❌ 错误：Cisco 继承了 H3C
cisco_ios:
  parents: [h3c]
```

### ❌ 禁止 6：循环继承

**后果**：`RecursionError`，脚本退出。

```yaml
# ❌ 错误
h3c:
  parents: [h3c_sr88]
h3c_sr88:
  parents: [h3c]
```

### ❌ 禁止 7：继承链缺少终止节点

**后果**：递归无法终止，或 `KeyError`。

```yaml
# ❌ 错误
h3c:
  parents: [base]
```

### ❌ 禁止 8：混用目录层级

**后果**：`group_source` 标记错误，导致 Group 被意外过滤。

```yaml
# ❌ 错误：版本类放在了 product_lines/ 下
# product_lines/h3c/V7.yaml（本该在 os_versions/ 下）
```

---

## 合规检查清单

| 检查项 | 合规标准 | 检查方法 |
| :--- | :--- | :--- |
| **目录位置** | 最终 Group 在 `os_versions/` 下 | 检查文件路径 |
| **parents 存在性** | `parents` 中的每个名称在内存中都有定义 | 运行 `build_inventory.py` |
| **类型一致性** | 子类字段类型与父类保持一致 | 对比 YAML 结构 |
| **无跨文件锚点** | 不使用 `<<: *anchor` | 搜索 `<<:` 关键字 |
| **厂商隔离** | 厂商间继承链完全独立 | 检查 `parents` 是否跨厂商 |
| **无循环继承** | 继承链为有向无环图 | 运行 `build_inventory.py` |
| **有基类终止节点** | 每个厂商至少有一个无 `parents` 的 Group | 检查 `vendors/` 下的文件 |
| **目录分层** | `vendors/` → `product_lines/` → `os_versions/` | 检查文件路径 |

---

## 常见问题

### Q1：补丁版本和基础版本是什么关系？

平级目录，通过 `parents` 字段建立逻辑继承关系。

### Q2：为什么最终只保留 `os_versions/` 下的 Group？

`build_inventory.py` 的输出目标是“版本层”Group，供 `hosts.yaml` 直接引用。`vendors/` 和 `product_lines/` 仅作为继承源，不直接输出。

### Q3：如何新增一个厂商？

1. `vendors/{vendor}.yaml` — 基类（无 `parents`）
2. `product_lines/{vendor}/{product}.yaml` — 产线类
3. `os_versions/{vendor}/{os_version}.yaml` — 版本类

### Q4：如何验证 Group 文件是否正确？

运行 `python scripts/build_inventory.py`，检查输出和生成的 `inventory/groups.yaml`。

### Q5：如何排查 `deep_merge` 类型覆盖问题？

1. 定位报错设备引用的 Group
2. 检查该 Group 的 `parents` 链
3. 逐级对比同一键的数据类型
4. 重点关注 `connection_options`、`data`、`extras` 等嵌套字段

---

*本文档与 `build_inventory.py` 合并逻辑保持同步，如有变更请同时更新。*


