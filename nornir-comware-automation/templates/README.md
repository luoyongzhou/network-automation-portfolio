

```markdown
# Jinja2 模板开发范式约束

本目录存放所有用于生成设备配置的 Jinja2 模板文件。模板目录结构与 `inventory/groups/` 的继承链保持严格对称，实现“厂商 → 产线 → OS → 分支 → 热补丁”的分层差异化管理。

## 目录结构（嵌套结构 + 协议分层）

```
templates/
├── _base/                                    # 全局兜底（极少使用）
│   ├── cmd/                                  # CLI 命令模板（兜底）
│   │   ├── bootstrap.j2
│   │   └── netconf_cmd.j2
│   └── netconf/                              # NETCONF XML 模板（兜底）
├── vendors/                                  # 厂商级
│   └── h3c/
│       ├── cmd/                              # H3C CLI 命令模板
│       │   ├── bootstrap.j2
│       │   ├── netconf_cmd.j2
│       │   └── _macros.j2                    # 厂商级宏定义
│       └── netconf/                          # H3C NETCONF XML 模板
│           └── _fragments/
│               ├── interface_atom.j2
│               └── ip_address_atom.j2
├── product_lines/                            # 产线级
│   └── h3c/
│       └── SR88/
│           ├── cmd/                          # SR88 CLI 模板
│           │   ├── bootstrap.j2
│           │   └── netconf_cmd.j2
│           └── netconf/                      # SR88 NETCONF 模板
│               └── interfaces_xml.j2
└── os_versions/                              # OS/版本级
    └── h3c/
        └── SR88_Comware_V7/                  # OS 版本级
            ├── cmd/                          # V7 CLI 模板
            │   ├── bootstrap.j2
            │   └── netconf_cmd.j2
            ├── netconf/                      # V7 NETCONF 模板
            │   └── interfaces_xml.j2
            └── R7171/                        # 分支版本 R7171（嵌套）
                ├── cmd/                      # R7171 CLI 模板
                │   ├── bootstrap.j2
                │   └── netconf_cmd.j2
                ├── netconf/                  # R7171 NETCONF 模板
                └── H02/                      # 热补丁 H02（嵌套在分支下）
                    ├── cmd/                  # H02 CLI 模板
                    │   └── netconf_cmd.j2
                    └── netconf/              # H02 NETCONF 模板
```

## 设计原则

### 1. 协议分层原则

模板按**下发协议**分为两个顶层子目录：

| 子目录 | 协议 | 下发方式 | 文件命名示例 |
| :--- | :--- | :--- | :--- |
| `cmd/` | CLI 命令 | Netmiko（SSH/Telnet/Console） | `bootstrap.j2`、`netconf_cmd.j2` |
| `netconf/` | NETCONF XML | ncclient（端口 830） | `interfaces_xml.j2`、`ospf_xml.j2` |

**目录对称原则**：每个层级（`_base/`、`vendors/`、`product_lines/`、`os_versions/`）都同时包含 `cmd/` 和 `netconf/` 子目录，确保 CLI 和 NETCONF 模板物理隔离。

### 2. 目录对称原则（嵌套版本）

模板目录采用**嵌套结构**，与 Group 继承链的层级关系对应：

| Group 层级 | 模板目录 | 对应 Group 示例 |
| :--- | :--- | :--- |
| 厂商级 | `vendors/{vendor}/{protocol}/` | `h3c/cmd/`、`h3c/netconf/` |
| 产线级 | `product_lines/{vendor}/{product}/{protocol}/` | `h3c/SR88/cmd/`、`h3c/SR88/netconf/` |
| OS 版本级 | `os_versions/{vendor}/{os_name}/{protocol}/` | `h3c/SR88_Comware_V7/cmd/`、`h3c/SR88_Comware_V7/netconf/` |
| 分支版本级 | `os_versions/{vendor}/{os_name}/{branch}/{protocol}/` | `h3c/SR88_Comware_V7/R7171/cmd/` |
| 热补丁级 | `os_versions/{vendor}/{os_name}/{branch}/{hotfix}/{protocol}/` | `h3c/SR88_Comware_V7/R7171/H02/cmd/` |

**路径拼接规则**：
- CLI 模板：`{层级}/{protocol}/cmd/{template_name}`
- NETCONF 模板：`{层级}/{protocol}/netconf/{template_name}`

### 3. 继承链路原则

每一层的模板文件必须通过 `{% extends %}` 指向其直接父层，路径中**必须包含 `cmd/` 或 `netconf/` 前缀**：

```
os_versions/.../V7/R7171/H02/cmd/netconf_cmd.j2
    extends → os_versions/.../V7/R7171/cmd/netconf_cmd.j2
        extends → os_versions/.../V7/cmd/netconf_cmd.j2
            extends → product_lines/.../SR88/cmd/netconf_cmd.j2
                extends → vendors/.../h3c/cmd/netconf_cmd.j2
                    imports → vendors/h3c/cmd/_macros.j2
```

### 4. 差异覆盖原则

子模板只声明与父模板的**差异部分**。通用命令由父模板提供，子模板通过 `{{ super() }}` 保留父级内容。

### 5. 单场景多模板原则

同一场景（如开局配置、NETCONF 初始化、ACL 命令）使用相同的文件名分布在各个层级目录中。

`TemplateRenderer` 通过 `PathResolver` 按优先级查找，找到第一个存在的模板即停止。

### 6. 补丁路径提取原则

`H3CPatchExtractor` 从 Group 名称中提取补丁路径段：

| Group 名称 | 提取结果 | 最终模板路径 |
| :--- | :--- | :--- |
| `h3c_sr88_comware_v7_r7171` | `R7171` | `os_versions/h3c/SR88_Comware_V7/R7171/cmd/netconf_cmd.j2` |
| `h3c_sr88_comware_v7_r7171_h02` | `R7171/H02` | `os_versions/h3c/SR88_Comware_V7/R7171/H02/cmd/netconf_cmd.j2` |
| `h3c_sr88_comware_v7` | `None` | `os_versions/h3c/SR88_Comware_V7/cmd/netconf_cmd.j2` |

## 宏定义规范

### 1. 宏的定位

宏（Macro）是 Jinja2 模板语言中的 **“函数”**，用于将重复的 CLI 命令片段封装为可复用的代码块。

在框架中，宏承担 **“原子命令库”** 的职责：
- **输入**：参数（如接口名、IP、描述）
- **处理**：逻辑判断（如判断是否为物理接口，是否需要 `port link-mode route`）
- **输出**：渲染后的命令文本

宏与模板继承（`{% extends %}`）分工明确：
- **继承**：解决 **“文件结构”** 的复用，决定模板骨架
- **宏**：解决 **“代码片段”** 的复用，生成具体命令

### 2. 宏的存放位置

- **厂商级宏文件**：必须存放在 `templates/vendors/{vendor}/cmd/_macros.j2`。
- **跨厂商禁止引用**：不同厂商的宏文件相互独立，禁止交叉引用。

### 3. 宏的编写规范

#### 3.1 命名规范
- 宏名采用小写加下划线（如 `interface_config`、`vlan_config`），语义清晰。
- 参数名采用小写加下划线，与变量命名一致。

#### 3.2 单一职责
- 每个宏只完成一项功能（例如 `interface_config` 只负责生成接口配置，不涉及 VLAN）。

#### 3.3 参数化
- 所有可变内容必须通过参数传递，宏本身不硬编码任何具体值。
- 为常用参数设置默认值（如 `desc=''`、`role='network-admin'`），减少调用时的冗余。

#### 3.4 注释
- 每个宏上方必须包含注释，说明用途、参数含义和示例用法。

### 4. 现有宏说明（H3C）

| 宏名 | 用途 | 关键逻辑 | 示例调用 |
| :--- | :--- | :--- | :--- |
| `interface_config(name, ip, mask, desc='')` | 配置接口（含物理口自动切三层） | 自动判断是否为物理接口，追加 `port link-mode route` | `{{ macros.interface_config("GigabitEthernet0/0/0", "172.12.1.11", "255.255.255.0", "Management") }}` |
| `vlan_config(id, name)` | 创建 VLAN | 纯命令拼接 | `{{ macros.vlan_config(100, "Management") }}` |
| `static_route(dest, mask, next_hop)` | 配置静态路由 | 纯命令拼接 | `{{ macros.static_route("0.0.0.0", "0", "172.12.1.254") }}` |
| `ssh_user(username, password, role='network-admin')` | 配置 SSH 用户 | 使用 `password simple` 接受明文密码 | `{{ macros.ssh_user("admin", "Ssh@Pass123") }}` |

### 5. 调用方式

在厂商级模板中通过 `{% import %}` 导入宏文件：

```jinja2
{% import 'vendors/h3c/cmd/_macros.j2' as macros %}
```

## 强约束：绝对禁止行为

### ❌ 约束 1：父模板必须定义 `{% block custom_config %}`

如果父模板中没有定义 `custom_config` 块，子模板通过 `{% block custom_config %}` 添加的任何内容都会被**静默丢弃**（不报错、不显示）。

```jinja2
{# ✅ 正确：厂商级模板必须定义 custom_config 块 #}
netconf ssh server enable
netconf ssh server port 830

{# 子模板可在此插入差异命令 #}
{% block custom_config %}{% endblock %}

quit
save force
```

### ❌ 约束 2：禁止创造新的 `{% block %}` 名称

父模板中只定义了一个可供子模板覆盖的块：**`custom_config`**。

```jinja2
{# ❌ 错误：父模板没有定义 sr88_specific 块，内容会被静默丢弃 #}
{% block sr88_specific %}
system-working-mode standard
{% endblock %}
```

```jinja2
{# ✅ 正确：使用唯一的 custom_config 块 #}
{% block custom_config %}
system-working-mode standard
{{ super() }}
{% endblock %}
```

### ❌ 约束 3：禁止在 `{% extends %}` 中使用绝对路径或 `../`

`{% extends %}` 中的路径必须相对于 `templates/` 根目录，且**必须包含 `cmd/` 或 `netconf/` 前缀**。

```jinja2
{# ❌ 错误 #}
{% extends '/absolute/path/to/vendors/h3c/bootstrap.j2' %}
{% extends '../product_lines/h3c/SR88/bootstrap.j2' %}
{% extends 'vendors/h3c/bootstrap.j2' %}          # 缺少 cmd/ 前缀
```

```jinja2
{# ✅ 正确 #}
{% extends 'vendors/h3c/cmd/bootstrap.j2' %}
{% extends 'product_lines/h3c/SR88/cmd/bootstrap.j2' %}
```

### ❌ 约束 4：禁止跨厂商引用宏文件

宏文件（`_macros.j2`）是厂商级原子命令库，不同厂商的命令语法不同，禁止交叉引用。

```jinja2
{# ❌ 错误：H3C 模板引用了 Cisco 的宏 #}
{% import 'vendors/cisco/cmd/_macros.j2' as macros %}
```

```jinja2
{# ✅ 正确：引用本厂商的宏 #}
{% import 'vendors/h3c/cmd/_macros.j2' as macros %}
```

### ❌ 约束 5：补丁路径必须使用嵌套结构

```
# ✅ 正确：嵌套结构（当前框架标准）
os_versions/h3c/
└── SR88_Comware_V7/
    ├── cmd/
    └── R7171/
        ├── cmd/
        └── H02/
            └── cmd/

# ❌ 错误：平级结构（当前框架不支持）
os_versions/h3c/
├── SR88_Comware_V7/
│   └── cmd/
└── SR88_Comware_V7_R7171/
    └── cmd/
```

### ❌ 约束 6：禁止补丁目录命名与 Group 补丁标识不一致

```yaml
# Group 名称
h3c_sr88_comware_v7_r7171      # 提取结果：R7171
h3c_sr88_comware_v7_r7171_h02  # 提取结果：R7171/H02
```

```
# ❌ 错误：目录名不匹配
os_versions/h3c/SR88_Comware_V7/Patch7171/

# ✅ 正确：目录名与提取结果一致
os_versions/h3c/SR88_Comware_V7/R7171/
os_versions/h3c/SR88_Comware_V7/R7171/H02/
```

### ❌ 约束 7：禁止在父模板中写死子类特有的命令

厂商级模板只应包含所有该厂商设备通用的命令。子类特有的命令应放在产线级或更低层级。

### ❌ 约束 8：禁止不同场景的模板混放在同一目录

每个场景使用独立的文件名，禁止将不同场景的命令混写在同一个模板文件中。

## 弱约束：推荐做法

### 推荐 1：宏文件按功能拆分

当 `_macros.j2` 变得庞大时，建议按功能拆分为多个文件：

```
vendors/h3c/cmd/_macros/
├── interface.j2
├── vlan.j2
├── route.j2
└── ssh.j2
```

### 推荐 2：模板中增加版本标识注释

在每个模板文件头部注明适用版本、作者、修改日期：

```jinja2
{# ============================================================ #}
{# 模板: os_versions/h3c/SR88_Comware_V7/R7171/cmd/netconf_cmd.j2  #}
{# 适用: H3C SR88 Comware V7 R7171 分支                        #}
{# 作者: admin                                                  #}
{# 最后修改: 2026-09-03                                         #}
{# ============================================================ #}
```

### 推荐 3：`custom_config` 块中调用 `{{ super() }}`

子模板在追加内容时，**必须**调用 `{{ super() }}` 保留父模板的内容：

```jinja2
{% block custom_config %}
{# 本层特有命令 #}
netconf ssh server keepalive 30

{# 保留父级内容 #}
{{ super() }}
{% endblock %}
```

### 推荐 4：同一场景的所有层级模板使用相同的 `extends` 深度

补丁模板 → 分支模板 → OS 模板 → 产线模板 → 厂商模板，每一级都指向直接父级，保持继承链的连续性。

## 场景扩展指南

### 如何新增一个场景（如 QoS CLI 命令）

1. **确定场景名称**：如 `qos_cmd`

2. **在所有目录层级中创建同名模板文件**：
   ```
   templates/
   ├── _base/cmd/qos_cmd.j2
   ├── vendors/h3c/cmd/qos_cmd.j2
   ├── product_lines/h3c/SR88/cmd/qos_cmd.j2
   └── os_versions/h3c/SR88_Comware_V7/cmd/qos_cmd.j2
       └── R7171/cmd/qos_cmd.j2
           └── H02/cmd/qos_cmd.j2
   ```

3. **建立继承链**：每个 `qos_cmd.j2` 通过 `{% extends %}` 指向直接父层，路径中包含 `cmd/` 前缀

4. **在 `SceneAPI` 中注册新场景**：
   ```python
   def ssh_qos_cmd(self, host):
       resolver = H3CBootstrapPathResolver(get_patch_extractor("h3c"))
       return self.renderer.render(host, "cmd/qos_cmd.j2", path_resolver=resolver)
   ```

5. **在命令行入口注册**：
   ```python
   scene_methods = {
       ...
       "ssh_qos": scene.ssh_qos_cmd,
   }
   ```

## 合规检查清单

| 检查项 | 合规标准 | 检查方法 |
| :--- | :--- | :--- |
| **块名称** | 只使用 `custom_config` | 搜索 `{% block` 检查所有块名 |
| **父模板定义 block** | 每一层父模板都有 `{% block custom_config %}` | 检查父模板是否存在 `custom_config` 块 |
| **子模板调用 super** | 子模板中调用 `{{ super() }}` | 搜索 `super()` |
| **extends 路径** | 相对于 `templates/` 根目录，且包含 `cmd/` 或 `netconf/` | 检查 `{% extends %}` 路径 |
| **宏导入** | 同厂商引用，且包含 `cmd/` 前缀 | 检查 `{% import %}` 路径 |
| **补丁路径** | 嵌套结构，命名一致 | 检查目录名与补丁标识是否匹配 |
| **厂商级模板** | 不含产线/OS 特有命令 | Code Review |
| **场景隔离** | 不同场景使用不同文件名 | 检查模板文件命名 |
| **目录深度** | 与 Group 继承链一致 | 对比目录结构和 Group 层级 |
| **协议分层** | CLI 模板在 `cmd/`，NETCONF 模板在 `netconf/` | 检查文件路径 |

## 常见问题

### Q1：如何新增一个厂商的模板？

1. 在 `vendors/{vendor}/cmd/` 下创建 `_macros.j2` 和各场景模板文件
2. 在 `product_lines/{vendor}/{product}/cmd/` 下创建同名模板文件
3. 在 `os_versions/{vendor}/{os_name}/cmd/` 下创建同名模板文件
4. 如需 NETCONF 模板，对应放置在 `netconf/` 子目录
5. `PathResolver` 自动适配嵌套目录结构

### Q2：如何新增一个补丁版本？

在 `os_versions/{vendor}/{os_name}/{branch}/` 下创建热补丁目录（如 `H02/`），并在其中创建 `cmd/`（或 `netconf/`）子目录，放入对应的模板文件，通过 `{% extends %}` 指向分支模板。

### Q3：为什么我的新命令没有出现在渲染结果中？

按顺序排查：
1. 检查模板中的 `{% block %}` 名称是否是 `custom_config`
2. 检查父模板是否定义了 `custom_config` 块
3. 检查子模板是否调用了 `{{ super() }}`
4. 检查 `{% extends %}` 路径是否包含 `cmd/` 或 `netconf/` 前缀

### Q4：如何判断我应该用哪个场景的模板？

| 场景 | 模板文件 | 用途 | 下发方式 |
| :--- | :--- | :--- | :--- |
| 开局配置 | `cmd/bootstrap.j2` | 设备首次上电初始化 | Console/SSH |
| NETCONF 初始化 | `cmd/netconf_cmd.j2` | 开启 NETCONF 服务 | SSH (CLI) |
| ACL 命令 | `cmd/acl_cmd.j2` | ACL 命令行配置 | SSH (CLI) |
| ACL XML | `netconf/acl_xml.j2` | ACL XML 配置 | NETCONF |

### Q5：不同的场景可以共享同一个 `custom_config` 块吗？

**不可以。** `custom_config` 块是在父模板中定义的，不同场景的父模板是独立的文件，它们各自拥有独立的 `custom_config` 块。

### Q6：补丁版本（如 H02）的模板如何组织？

使用**嵌套结构**：`os_versions/h3c/SR88_Comware_V7/R7171/H02/cmd/{template}.j2`，通过 `{% extends '.../R7171/cmd/{template}.j2' %}` 继承分支模板。

### Q7：`PathResolver` 和 `{% extends %}` 的区别是什么？

- **`PathResolver`**：在 Python 层面决定“**用哪个模板文件**”（文件发现），按优先级查找
- **`{% extends %}`**：在 Jinja2 层面决定“**模板内容的继承关系**”（内容继承）

### Q8：分支版本（如 R7753）如何与 R7171 并行？

在 `os_versions/h3c/SR88_Comware_V7/` 下创建 `R7753/` 目录，与 `R7171/` 平级，各自包含 `cmd/` 和 `netconf/` 子目录。

```
os_versions/h3c/SR88_Comware_V7/
├── R7171/
│   └── cmd/
│       └── netconf_cmd.j2
└── R7753/
    └── cmd/
        └── netconf_cmd.j2
```

### Q9：为什么 CLI 和 NETCONF 模板要分开放？

CLI 模板（`cmd/`）通过 Netmiko 下发文本命令，NETCONF 模板（`netconf/`）通过 ncclient 下发 XML。两者协议不同、文件内容不同、目录结构显式区分可以避免混淆，也便于未来扩展 RESTCONF 等其他协议（可新增 `restconf/` 目录）。


*本文档与 `preview_bootstrap.py` 渲染逻辑保持同步，如有变更请同时更新。*

## 与 `atoms/` 目录的关系

`templates/` 只负责"生成配置文本/XML"，不负责"什么时候生成、生成后怎么校验、失败了怎么回退"——这部分职责在 `atoms/` 目录。两者的分工边界：

| 目录 | 职责 | 产出 |
| :--- | :--- | :--- |
| `templates/` | 声明式地描述"配置长什么样"（CLI 命令文本 / NETCONF XML 片段） | 渲染后的字符串 |
| `atoms/` | 命令式地描述"配置变更的完整生命周期"（幂等检查 → 下发 → 校验 → 回退） | 一次变更操作的执行结果 |

调用链路（以 H3C NETCONF 下发 IP 地址为例）：

```
atoms/h3c/netconf/interface_ip_address.py (NetconfIPAddressAtom)
    └─ deploy() 阶段调用 self._edit_config(host, TEMPLATE, context, session)
        └─ NetconfAtom._edit_config()（定义在 atoms/base.py）
            └─ self.scene_api.custom(host, template_name, context_override=context)
                └─ SceneAPI.custom() → TemplateRenderer.render()（定义在 scripts/preview_bootstrap.py）
                    └─ 按 H3CBootstrapPathResolver 的优先级顺序查找并渲染 templates/ 下的 .j2 文件
```

也就是说，`atoms/` 里的每一个 Atom 子类，`TEMPLATE` / `DEPLOY_TEMPLATE` / `ROLLBACK_TEMPLATE` 等类属性指向的路径，必须是本目录（`templates/`）中真实存在的模板文件相对路径（相对于 `templates/` 根目录，含 `cmd/` 或 `netconf/` 前缀），例如：

```python
# atoms/h3c/cmd/interface_ip_address.py
DEPLOY_TEMPLATE = "product_lines/h3c/SR88/cmd/ip_address_cmd.j2"
ROLLBACK_TEMPLATE = "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2"
```

新增一个 Atom 时，务必同步确认它引用的模板路径符合本文档的目录对称原则和 `PathResolver` 搜索顺序；反之，模板目录的重命名/迁移也需要同步检查 `atoms/` 下所有引用该路径的 Atom 类属性。`atoms/` 目录自身的设计原则、原子操作生命周期、回退与依赖检查机制，见 [`atoms/README.md`](../atoms/README.md)。

