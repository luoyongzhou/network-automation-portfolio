# preview_bootstrap.py

## 概述

`preview_bootstrap.py` 是本框架的**模板渲染引擎核心实现**，提供了模板路径解析（`PathResolver`）、补丁版本提取（`PatchExtractor`）、Jinja2 渲染（`TemplateRenderer`）、场景入口封装（`SceneAPI`）四层抽象。它不仅是一个可以独立运行的 CLI 预览工具，更是全项目其他脚本（`scenes/*.py`、`atoms/h3c/**/*.py`）实际导入复用的渲染引擎所在文件——`templates/README.md` 里描述的"目录对称"、"继承链路"、"补丁路径提取"等设计原则，都是本文件里的类具体实现的。

它负责：
1. 根据设备所属的 Group（`h3c_sr88_comware_v7_r7171` 这类名称）反推出补丁路径段（`R7171`），进而定位到该设备应该使用哪一层级的模板文件
2. 按"补丁级 → 分支级/OS版本级 → 产线级 → 厂商级 → 全局兜底"的优先级顺序查找模板，找到第一个存在的文件即停止（模板存在性由文件系统决定，不需要额外配置清单）
3. 用 Jinja2 渲染找到的模板，返回渲染结果或结构化的错误信息
4. 作为 CLI 工具，可以直接预览任意设备在任意场景下会渲染出什么配置，不需要连接真实设备

## 使用方法

```bash
# 渲染所有设备的 bootstrap 配置（默认场景）
python scripts/preview_bootstrap.py

# 渲染指定场景（NETCONF 初始化 CLI 命令）
python scripts/preview_bootstrap.py --scene ssh_bootstrap

# 渲染 ACL CLI 命令
python scripts/preview_bootstrap.py --scene ssh_acl

# 渲染 ACL 的 NETCONF XML
python scripts/preview_bootstrap.py --scene netconf_acl

# 只渲染指定设备
python scripts/preview_bootstrap.py H3C-SR88-01
python scripts/preview_bootstrap.py --scene ssh_bootstrap H3C-SR88-01
```

内置场景（`SceneAPI` 中已注册的方法）：`bootstrap`、`ssh_bootstrap`、`ssh_acl`、`ssh_qos`、`netconf_acl`、`netconf_qos`。新增场景需要同时在 `SceneAPI` 类中新增方法、在 `main()` 的 `scene_methods` 字典中注册（做法见 `templates/README.md` "场景扩展指南"章节，两处文档描述的是同一套注册机制）。

## 四层核心抽象

### 1. `PatchExtractor`：从 Group 名称反推补丁版本

```
H3CPatchExtractor:   从形如 "..._r7171" 提取 "R7171"，"..._h02" 提取 "H02"，组合为 "R7171/H02"
CiscoPatchExtractor:  从形如 "..._V15.2" 提取 "15.2"
DefaultPatchExtractor: 从形如 "..._7171"（纯数字结尾）提取 "7171"
```

`get_patch_extractor(platform_or_group)` 根据关键字（`h3c`/`hp_comware`、`cisco`/`ios`）选择对应的提取器实现，这是一处**厂商专属正则规则**的集中点——新增厂商时，如果该厂商的 Group 命名规则里补丁版本的表示方式与现有三种都不同，需要在这里新增一个 `PatchExtractor` 子类，而不是散落地在别处写正则匹配。

`host.data.get("patch")` 拥有最高优先级：如果设备的 `data` 字典里显式声明了 `patch` 字段，会直接使用该值，完全跳过从 Group 名称正则提取的逻辑——这是给"Group 命名不规范、但仍想手动指定补丁路径"的设备留的后门。

### 2. `PathResolver`：候选模板路径生成

`H3CBootstrapPathResolver.resolve()` 对每次模板查找请求，按固定优先级生成一份**候选路径列表**（不是只返回一个路径）：

```python
candidates = [
    f"os_versions/h3c/SR88_Comware_V7/{patch}/{template_name}",  # 补丁级，仅当 patch 存在时加入
    f"os_versions/h3c/SR88_Comware_V7/{template_name}",          # OS 版本级
    f"product_lines/h3c/SR88/{template_name}",                    # 产线级
    f"vendors/h3c/{template_name}",                                # 厂商级
    f"_base/{template_name}",                                      # 全局兜底
]
```

`TemplateRenderer.render()` 拿到这份列表后，先过滤出文件系统中**真实存在**的路径，再按顺序尝试渲染，第一个渲染成功的直接返回——这意味着"路径存在"和"路径能被 Jinja2 成功渲染（如 `{% extends %}` 指向的父模板也存在）"是两次独立的检查，如果补丁级模板文件存在但其 `extends` 指向了一个不存在的父模板，会渲染失败并**继续尝试下一个候选路径**，而不是直接报错终止。

**当前实现只有 H3C 一种 `PathResolver`**：`CiscoPatchExtractor`、`DefaultPatchExtractor` 已经写好了补丁提取逻辑，但目前没有对应的 `CiscoPathResolver` 类——也就是说这两个 Extractor 目前实际上还没有被任何 PathResolver 使用到，是为未来扩展 Cisco/其他厂商预先搭好的一半骨架（另一半是 `atoms/cisco/`、`atoms/huawei/` 的空目录，两者体现的是同一个"预留但未实现"的扩展点）。

### 3. `TemplateRenderer`：渲染引擎本体

```python
class TemplateRenderer:
    def __init__(self, templates_root=None):
        self.templates_root = templates_root or Path.cwd() / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(self.templates_root)),
            autoescape=select_autoescape(['xml', 'j2']),
            trim_blocks=True,
            lstrip_blocks=True,
        )
```

- `autoescape=select_autoescape(['xml', 'j2'])`：对 `.xml`/`.j2` 后缀的模板启用自动转义（防止渲染出的 XML 因为特殊字符如 `<`、`&` 而破坏结构）。所有本项目模板都是 `.j2` 后缀，因此**全部模板默认都开启了自动转义**，包括生成纯文本 CLI 命令的模板——这在实践中通常无影响（CLI 命令文本里很少出现需要转义的字符），但如果未来某个 CLI 模板的变量值恰好包含 `&`、`<` 等字符，输出会被转义，需要留意。
- `trim_blocks` + `lstrip_blocks`：去除 Jinja2 控制语句（`{% %}`）本身占用的空行和前导空白，让渲染出的 CLI 命令/XML 不会因为模板里的 `{% for %}`/`{% if %}` 缩进产生多余空行。
- `build_base_context(host)`：所有场景渲染前都会自动注入的"基础上下文"（`sysname`、`mgmt_ip`、`username`、`password` 等，直接从 Nornir `Host` 对象取值），场景专属的额外变量通过 `context_override` 参数合并进来，**后者覆盖前者**（`context.update(context_override)`）。

### 4. `SceneAPI`：场景入口的薄封装

`SceneAPI` 的每个方法都是"选定一个 `PathResolver` + 调用 `renderer.render()` + 指定模板名"的固定模式，本身不包含业务逻辑，纯粹是给外部调用者（`atoms/`、`scenes/`、本文件的 `main()`）提供一组语义化的入口名字（`ssh_bootstrap` 比直接写 `render(host, "cmd/netconf_cmd.j2")` 更易读）。`custom()` 方法是逃生舱——不在预定义场景里的任意模板名和路径解析器组合，都可以通过它直接调用（`atoms/base.py` 中 `CmdAtom._render_and_send()` 和 `NetconfAtom._edit_config()` 内部就是通过 `self.scene_api.custom(...)` 调用的，说明 `atoms/` 层没有走 `SceneAPI` 的具名方法，而是统一走 `custom()` 逃生舱，具体原因是 `atoms/` 里的模板路径来自 Atom 类自己的 `DEPLOY_TEMPLATE` 等类属性，不是 `SceneAPI` 里硬编码的固定几个场景名）。

## `main()`：CLI 入口的参数解析

命令行参数解析是手写的（没有用 `argparse`），逻辑分三步：

```
1. 若第一个参数是 --scene=xxx 或 --scene xxx，取出场景名，从 args 中移除
2. 剩余的第一个参数（若有）作为设备名
3. 若指定了设备名，只渲染该设备；否则遍历 Inventory 中全部设备
```

渲染结果的汇总统计（成功/失败设备数、失败设备列表）只在"渲染全部设备"模式下打印，指定单个设备时只打印该设备的渲染结果或错误详情——这是刻意的行为差异：单设备调试时想直接看到配置内容，不需要看汇总表格。

## 与 `preview_bootstrap_bak.py` 的关系

`preview_bootstrap_bak.py` 是本文件重构前的历史版本，保留在同目录下作为演进记录，**不参与任何脚本的导入**（`scenes/*.py`、`atoms/*.py` 全部 import 的是 `scripts.preview_bootstrap`，没有任何地方 import `preview_bootstrap_bak`）。两者最主要的差异：

| 维度 | `preview_bootstrap_bak.py`（旧） | `preview_bootstrap.py`（当前） |
| :--- | :--- | :--- |
| 模板路径 | 不含协议前缀，如 `bootstrap.j2` | 含 `cmd/`/`netconf/` 前缀，如 `cmd/bootstrap.j2` |
| CLI 渲染实现 | 有一套独立的 `render_bootstrap_config()` 函数，硬编码只处理 `netconf_cmd.j2` 场景，带大量调试 `print` | 完全统一走 `SceneAPI` + `TemplateRenderer.render()`，不区分场景特殊处理 |
| 代码重复度 | `render_bootstrap_config()` 和 `TemplateRenderer.render()` 的候选路径生成逻辑几乎重写了一遍 | 消除重复，`main()` 统一通过 `scene_methods` 字典分发到 `SceneAPI` 方法 |

如果需要理解"为什么模板目录会有 `cmd/`/`netconf/` 这一层协议分层"，对比这两个文件的 diff 是最直接的历史证据——协议分层是在某次重构中引入的，不是项目最初的设计。

## 与其他模块的关系

- 依赖 [`inventory/README.md`](../inventory/README.md) 描述的 Group 分层结构（通过 `host.groups` 反推补丁版本）
- 依赖 [`templates/README.md`](../templates/README.md) 描述的模板目录结构（`PathResolver` 的候选路径必须与目录结构吻合）
- 被 [`atoms/README.md`](../atoms/README.md) 中描述的各 Atom 类通过 `SceneAPI.custom()` 间接调用
- 被 `scenes/test_ip_deploy_full_README.md`、`scenes/test_ospf_deploy_full_README.md` 中描述的场景脚本直接 `import TemplateRenderer, PathResolver` 使用（并各自定义了 `DirectPathResolver`，绕过 H3C 专属的补丁路径推导，直接使用调用方传入的完整路径——这是场景脚本明确知道自己要用的模板路径，不需要再走一遍补丁提取和多级候选查找）
