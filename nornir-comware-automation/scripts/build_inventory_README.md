# build_inventory.py

## 概述

`build_inventory.py` 是 **Nornir Inventory 分层继承展开引擎**，负责把 `inventory/groups/` 目录下按"厂商 → 产线 → OS 版本"分层拆散存放的 Group 定义文件，合并、展开继承关系，最终生成 Nornir 直接加载的单一文件 `inventory/groups.yaml`。

它负责：
1. 递归扫描 `inventory/groups/` 下的全部 `.yaml` 文件（`vendors/`、`product_lines/`、`os_versions/` 三层）
2. 解析每个 Group 的 `parents` 字段，递归展开继承链（深度合并父子配置）
3. 检测循环继承、缺失父组等结构性错误
4. 只保留来自 `os_versions/` 目录的 Group 输出到 `groups.yaml`（`vendors/`、`product_lines/` 仅作继承源，不直接给 Nornir 用）

`inventory/groups/` 目录的分层组织原则、类型约束、禁止行为清单，见 [`inventory/README.md`](../inventory/README.md)（该文档描述的正是本脚本要处理的输入数据结构）。本文档聚焦"这个合并引擎本身是怎么工作的"。

## 使用方法

```bash
python scripts/build_inventory.py
```

无命令行参数。脚本会自动定位到项目根目录（通过 `Path(__file__).parent.parent`），扫描 `inventory/groups/`，输出 `inventory/groups.yaml`。这一步是 `config.yaml` 中 `SimpleInventory` 插件加载 Nornir Inventory 的**前置依赖**——`groups.yaml` 不是手写维护的文件，是本脚本的生成产物。

其他脚本（`scripts/preview_bootstrap.py`、`scenes/*.py`）在自己的 `main()` 里都会先调用本脚本的 `merge_group_files()` 函数（通过 `ensure_groups_up_to_date()` 包装），所以正常使用流程中很少需要手动单独运行本脚本——只有在调试合并逻辑本身、或者想单独查看展开结果时才需要直接执行。

## 核心流程

```
1. 扫描 inventory/groups/ 下所有 .yaml 文件（按路径深度、文件名排序，保证展开顺序确定）
2. 逐文件加载，记录每个 Group 名称属于哪个顶层目录（vendors / product_lines / os_versions）
   → 检测重复定义的 Group 名（后加载的覆盖前面的，并打印警告）
3. 执行预校验 Hook（pre_validate_groups，当前为空实现，见下文）
4. 展开继承关系（expand_inheritance）：
   对每个 Group，递归解析其 parents 列表，深度合并父级配置到子级
5. 过滤：只保留 group_source 记录为 "os_versions" 的 Group
6. 写出 inventory/groups.yaml
```

## 深度合并算法（`deep_merge`）

```python
def deep_merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)   # 递归合并
        else:
            result[key] = deepcopy(value)                   # 直接覆盖
    return result
```

判断规则只有一条：**父子两层的同名字段都必须是字典类型才会递归合并，否则子级直接整体覆盖父级**。这条规则简单但后果重大——如果子类不小心把一个原本是字典的字段写成字符串或列表，`deep_merge` 不会报错，而是静默丢弃父级该字段下的所有嵌套内容（`inventory/README.md` 中"类型约束"章节和"禁止行为清单"里的错误示例 2 就是这个陷阱的具体案例）。理解这一行为是排查"Group 合并后连接参数丢失但脚本不报错"这类问题的关键。

## 继承展开算法（`expand_inheritance`）

```python
def resolve_group(name):
    if name in expanded:      return expanded[name]      # 已展开，直接复用（记忆化）
    if name in processing:    raise RecursionError(...)   # 正在展开中又被引用 → 循环继承
    if name not in groups:    raise KeyError(...)          # parents 引用了不存在的组

    processing.add(name)
    parents = groups[name].get("parents", [])
    if not parents:
        result = groups[name]                              # 无父组，自身即终止节点
    else:
        merged = {}
        for parent_name in parents:                         # 支持多重继承
            merged = deep_merge(merged, resolve_group(parent_name))
        merged = deep_merge(merged, groups[name])            # 自身配置覆盖所有父级
        result = merged
    processing.remove(name)
    expanded[name] = result
    return result
```

几个值得注意的实现细节：

- **`processing` 集合是循环继承检测的核心**：一个 Group 在"正在被展开"的过程中如果again被requested，说明继承链回到了自己，直接抛 `RecursionError`，而不是无限递归到 Python 调用栈溢出
- **`expanded` 字典是记忆化缓存**：同一个父组被多个子组继承时（如 `h3c` 被 `h3c_sr88` 继承，`h3c_sr88` 又被多个 OS 版本继承），只会真正展开一次
- **支持多重继承**（`parents` 是列表）：多个父组按列表顺序依次 `deep_merge`，越靠后的父组优先级越高（会覆盖前面父组的同名字段），最后自身配置再覆盖所有父组结果——这一点在 `inventory/README.md` 中没有单独强调，是阅读代码后才能确认的行为细节
- 展开完成后会 `pop("parents", None)`——最终写入 `groups.yaml` 的 Group 定义不包含 `parents` 字段，因为已经是完全展开后的"扁平"配置，Nornir 加载时不需要（也不支持）再次处理继承

## 预校验 Hook（`pre_validate_groups`）

```python
def pre_validate_groups(raw_groups, group_source):
    errors, warnings = [], []
    # ===== 未来扩展示例（注释） =====
    # 1. 目录层级校验...
    # 2. 类型一致性校验...
    return errors, warnings
```

**当前是空实现**——函数签名、调用位置、错误/警告的处理流程都已经搭好（`merge_group_files()` 中会打印警告、遇到 errors 会终止合并），但函数体内没有任何实质性校验逻辑，只留了两段注释掉的示例代码。这是一个**预留的扩展点**，不是遗漏的 bug：`inventory/README.md` 中"禁止行为清单"列出的 8 条约束（如跨厂商继承隔离、YAML 锚点禁用），目前全部靠人工 Code Review 遵守，没有一条被本脚本自动校验。如果要把这些约束变成自动化检查，就应该在这个函数里实现，而不是新建其他校验入口。

## 排序与确定性

```python
yaml_files = sorted(
    groups_root.rglob("*.yaml"),
    key=lambda p: (len(p.relative_to(groups_root).parents), p.name),
)
```

文件按"路径深度、文件名"排序后再加载，保证同名 Group 出现在多个文件时，"后加载覆盖先加载"的行为是**确定性的、可复现的**，而不是依赖文件系统遍历的随意顺序。文件名以 `_` 开头的文件会被跳过（视为模板/草稿文件，不参与合并）。

## 常见问题

### Q：为什么 `vendors/`、`product_lines/` 里定义的 Group 名字不会出现在 `groups.yaml` 里？

因为最后一步做了 `group_source.get(name) == "os_versions"` 过滤。`vendors/`、`product_lines/` 里的 Group（如 `h3c`、`h3c_sr88`）只是**继承链上的中间节点**，专门设计为不直接分配给设备使用（`hosts.yaml` 里也不会引用它们），Nornir 最终只需要"扁平化后的版本层" Group。

### Q：如果两个文件定义了同名 Group 会怎样？

脚本会打印 `⚠️  重复 Group 'xxx'，将被覆盖`，并按加载顺序（见上文"排序与确定性"）让后加载的定义生效，不会报错终止。这是宽松处理，不是强校验——如果想让重复定义直接报错，需要修改脚本逻辑或在 `pre_validate_groups` 里补充检测。

### Q：`build_inventory.py` 会校验 `hosts.yaml` 引用的 Group 是否存在吗？

不会。本脚本只处理 `inventory/groups/` 目录，完全不读取 `hosts.yaml`。如果 `hosts.yaml` 里某台设备引用了一个不存在于最终 `groups.yaml` 的 Group 名，本脚本不会报错，报错会发生在**下一步** Nornir 加载 Inventory 的时候（`InitNornir()` 调用时）。
