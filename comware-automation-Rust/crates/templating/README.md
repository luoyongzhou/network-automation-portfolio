# cwa-templating

补丁版本提取 + 分层模板路径解析 + minijinja 渲染。

对应 Python 版 [`scripts/preview_bootstrap.py`](../../../nornir-comware-automation/scripts/preview_bootstrap.py) 的 `PatchExtractor` / `PathResolver` / `TemplateRenderer` / `SceneAPI` 四组抽象。

## 概述

模板渲染分三步，每步一个可替换的抽象：

```
Host（含 groups、data）
   │
   │ ① PatchExtractor  —— 从 Group 名提取补丁段  h3c_sr88_comware_v7_r7171 → "R7171"
   ▼
   │ ② PathResolver    —— 生成 5 级候选路径，按优先级从高到低
   ▼
   │ ③ TemplateRenderer —— 探测存在性 → minijinja 渲染
   ▼
配置文本（CLI 命令行 / NETCONF XML 片段）
```

`SceneApi` 在三者之上包一层场景命名入口（`bootstrap()` / `ssh_bootstrap()` / `direct_render()`）。

**模板文件从 Python 项目原样复制，未改一个字符**——这是黄金文件比对的前提。

## 使用方法

```rust
use cwa_templating::{SceneApi, TemplateRenderer, ctx};

// 默认：autoescape 关闭
let renderer = TemplateRenderer::new("templates")?;

// 与 Python 版逐字节对齐时：开启 autoescape
let renderer = TemplateRenderer::with_autoescape("templates")?;

let scene = SceneApi::new(renderer);

// 分层查找（走 5 级继承链）
let text = scene.bootstrap(host)?;          // cmd/bootstrap.j2
let text = scene.ssh_bootstrap(host)?;      // cmd/netconf_cmd.j2

// 直接指定路径（不做分层查找）
let context = ctx([
    ("ifindex",      serde_json::json!(1235)),
    ("ipv4_address", serde_json::json!("11.11.11.11")),
    ("ipv4_mask",    serde_json::json!("255.255.255.255")),
    ("operation",    serde_json::json!("merge")),
]);
let xml = scene.direct_render(host, cwa_templating::paths::NC_IP_ADDRESS_ATOM, Some(&context))?;
```

命令行验证：

```bash
cargo run -p cwa-templating --example render_probe
```

## 补丁版本提取（`PatchExtractor`）

```rust
pub trait PatchExtractor: Send + Sync {
    fn extract(&self, host: &Host) -> Option<String>;
}
```

三个实现，与 Python 版一一对应：

| 实现 | 正则 | 输出示例 |
| :--- | :--- | :--- |
| `H3cPatchExtractor` | 分支 `(?i)_r([0-9]+)`、热补丁 `(?i)_h([0-9]+)` | `R7171` 或 `R7171/H02` |
| `CiscoPatchExtractor` | `(?i)_V([\d.]+)` | `15.2` |
| `DefaultPatchExtractor` | `_([\d]+)$` | `01` |

**所有实现都优先读 `host.data["patch"]`**，存在且非空时直接返回，用于绕过正则做显式指定。

H3C 的组合逻辑：

```
branch=Some, hotfix=Some  →  "R7171/H02"
branch=Some, hotfix=None  →  "R7171"
其他                       →  None
```

`get_patch_extractor(platform_or_group)` 按关键字选择实现：含 `h3c`/`hp_comware` → H3C，含 `cisco`/`ios` → Cisco，其余 → Default。

> Python 版的 `CiscoPatchExtractor` 写好了但从未被任何 `PathResolver` 使用（`need_to_discuss_prob.md` 新发现②）。本 crate 的 `LayeredPathResolver` 是产线无关的，把它接进去只需换一个 extractor。

## 分层路径解析（`PathResolver`）

```rust
pub trait PathResolver: Send + Sync {
    fn resolve(&self, host: &Host, template_name: &str) -> Vec<String>;
}
```

### 候选路径优先级

`LayeredPathResolver` 生成的候选，从高到低：

| 级别 | 路径模式 | H3C SR88 R7171 实例 |
| :--- | :--- | :--- |
| ① 补丁级 | `os_versions/{vendor}/{os_version}/{patch}/{tpl}` | `os_versions/h3c/SR88_Comware_V7/R7171/H02/cmd/bootstrap.j2` |
| ①b 分支级 | `os_versions/{vendor}/{os_version}/{branch}/{tpl}` | `os_versions/h3c/SR88_Comware_V7/R7171/cmd/bootstrap.j2` |
| ② OS 版本级 | `os_versions/{vendor}/{os_version}/{tpl}` | `os_versions/h3c/SR88_Comware_V7/cmd/bootstrap.j2` |
| ③ 产线级 | `product_lines/{vendor}/{product_line}/{tpl}` | `product_lines/h3c/SR88/cmd/bootstrap.j2` |
| ④ 厂商级 | `vendors/{vendor}/{tpl}` | `vendors/h3c/cmd/bootstrap.j2` |
| ⑤ 兜底 | `_base/{tpl}` | `_base/cmd/bootstrap.j2` |

**①b 是本 crate 新增的**。Python 版当 `patch == "R7171/H02"` 时只生成补丁级路径，若 `H02/` 目录下没有该模板，会直接跳到 OS 版本级，**丢掉分支级 `R7171/` 那一层**。本实现在检测到 `patch` 含 `/` 时补上分支级候选。

### 分层坐标推导（与 Python 版的关键差异）

Python 版 `H3CBootstrapPathResolver.resolve()` 把路径硬编码在方法体内：

```python
rel = f"os_versions/h3c/SR88_Comware_V7/{patch}/{template_name}"
rel = f"product_lines/h3c/SR88/{template_name}"
```

即使 `inventory/groups/product_lines/h3c/` 下新增了 `ASR.yaml`，解析器也不会自动支持（`need_to_discuss_prob.md` 新发现①）。

本 crate 改为从 Group 名**动态推导**：

```rust
pub struct LayerCoords {
    pub vendor: String,        // h3c
    pub product_line: String,  // SR88
    pub os_version: String,    // SR88_Comware_V7
}
```

推导规则，以 `h3c_sr88_comware_v7_r7171` 为例：

```
h3c_sr88_comware_v7_r7171
        │ ① strip_version_suffixes() 剥除末尾的 _r7171 / _h02
        ▼
h3c_sr88_comware_v7
        │ ② 首段为厂商（小写）
        ▼
vendor = "h3c"，剩余 [sr88, comware, v7]
        │ ③ normalize_segment() 逐段规范化
        ▼
[SR88, Comware, V7]
        │ ④ 首段为产线，全部拼接为 OS 版本
        ▼
product_line = "SR88"，os_version = "SR88_Comware_V7"
```

`normalize_segment()` 规则：

| 输入特征 | 处理 | 示例 |
| :--- | :--- | :--- |
| 同时含字母和数字 | 整段大写 | `sr88` → `SR88`、`v7` → `V7` |
| 其他 | 首字母大写，其余小写 | `comware` → `Comware` |

### 显式覆盖

自动推导规则不适用时，可在 `host.data` 中显式指定：

```yaml
H3C-CUSTOM-01:
  hostname: 10.0.0.1
  groups: [some_irregular_group_name]
  data:
    template_vendor: h3c
    template_product_line: SR88
    template_os_version: SR88_Comware_V7
```

三个键**必须同时提供**才生效（避免部分覆盖导致的隐式行为）。

### `DirectPathResolver`

模板名即路径，不做分层查找。对应 Python 版 `scenes/test_*.py` 里各自重复定义的 `DirectPathResolver`。

## 渲染引擎（`TemplateRenderer`）

### minijinja 配置

```rust
env.set_trim_blocks(true);      // 与 Python 版一致
env.set_lstrip_blocks(true);    // 与 Python 版一致
env.set_loader(minijinja::path_loader(&templates_root));
env.set_unknown_method_callback(minijinja_contrib::pycompat::unknown_method_callback);
```

### 关键差异 ①：Python 字符串方法

minijinja **不实现任何 Python 方法**。而 `templates/vendors/h3c/cmd/_macros.j2` 的 `interface_config` 宏依赖 `startswith`：

```jinja
{% if name.startswith('GigabitEthernet') or name.startswith('Ten-GigabitEthernet') or ... %}
 port link-mode route
{% endif %}
```

解法是注册 `minijinja-contrib` 的 `pycompat` callback，它提供 `startswith` / `endswith` / `upper` / `lower` / `strip` / `split` / `replace` / `items` 等分支，**覆盖本项目所需**。

### 关键差异 ②：autoescape 必须全程关闭

Python 版的配置是：

```python
autoescape=select_autoescape(['xml', 'j2'])
```

字面上对所有 `.j2` 启用 HTML 转义。**但不能照搬**，原因是两个引擎的转义字符集不兼容：

```python
>>> from markupsafe import escape
>>> str(escape('a&b<c>d"e/f'))
'a&amp;b&lt;c&gt;d&#34;e/f'          # Jinja2 不转义 /
```

minijinja 的 HTML 转义**额外转义 `/`**，会把 `GigabitEthernet0/0/0` 变成 `GigabitEthernet0&#x2f;0&#x2f;0`，直接破坏配置。

而实测 Python 版渲染 `bootstrap.j2` 的输出里 `/` 确实没被转义。所以：

| 构造方式 | autoescape | 用途 |
| :--- | :--- | :--- |
| `TemplateRenderer::new()` | 关闭 | **正常使用** |
| `TemplateRenderer::with_autoescape()` | 开启 | 保留但一般不用，见下 |

**关闭才是与 Python 版一致的正确选择。** 当前模板与数据中不含 `& < > " '`，关闭后输出与 Python 版逐字节一致（已验证三处模板）。

附带的好处：消除了 Python 版的一个隐患。若将来配置值里出现 `&`（例如密码含 `&`），Python 版会转成 `&amp;` 并下发到设备，本实现不会。

`with_autoescape()` 保留仅用于将来可能出现的、确实需要 HTML 转义的模板场景。用它渲染现有的 CLI / XML 模板会产生错误输出。

### 渲染两阶段

与 Python 版一致：

```
① 探测存在性：过滤出 templates_root/rel 确实是文件的候选
      │
      │ 全部不存在 → TemplateError::NoTemplateFound { searched: 全部候选 }
      ▼
② 逐个尝试渲染：返回首个成功结果
      │
      │ 全部失败 → TemplateError::RenderFailed（携带最后一个错误）
```

两阶段分离的意义：错误信息能区分"没找到模板"和"找到了但渲染报错"，前者列出所有搜索路径便于排查。

### 基础上下文（`build_base_context`）

与 Python 版 `build_base_context()` 字段完全一致：

| 键 | 来源 | 默认值 |
| :--- | :--- | :--- |
| `sysname` | `host.name` | — |
| `mgmt_ip` | `host.hostname` | — |
| `mgmt_interface` | `host.data` | `"Vlan-interface 100"` |
| `mgmt_mask` | `host.data` | `"255.255.255.0"` |
| `gateway` | `host.data` | `null` |
| `username` | `host.username` | — |
| `password` | `host.password` | — |
| `enable_secret` | `host.data` | `null` |

`context_override` 中的键会覆盖同名基础键。

## 模板路径常量（`paths`）

Python 版把模板路径散落在各 Atom 类的类属性里（`TEMPLATE` / `CREATE_TEMPLATE` / `IP_TEMPLATE` / ...）。本 crate 集中到 `scene_api::paths`：

```rust
pub mod paths {
    pub const CMD_INTERFACE_CREATE: &str   = "product_lines/h3c/SR88/cmd/interface_create_cmd.j2";
    pub const CMD_INTERFACE_ROLLBACK: &str = "product_lines/h3c/SR88/cmd/interface_create_rollback_cmd.j2";
    pub const CMD_IP_ADDRESS: &str         = "product_lines/h3c/SR88/cmd/ip_address_cmd.j2";
    pub const CMD_IP_ROLLBACK: &str        = "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2";
    pub const NC_IP_ADDRESS_ATOM: &str     = "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2";
    pub const NC_INTERFACE_ATOM: &str      = "product_lines/h3c/SR88/netconf/_fragments/interface_atom.j2";
    pub const NC_OSPF_XML: &str            = "product_lines/h3c/SR88/netconf/ospf_xml.j2";
}
```

这些仍是 SR88 特定路径（走 `DirectPathResolver`），因为原子模板本身就写在产线层。分层查找只用于 `bootstrap.j2` / `netconf_cmd.j2` 这类有继承链的场景模板。

## 强约束：绝对禁止行为

### 禁止 1：修改 `templates/` 下的任何模板文件

模板从 Python 项目原样复制，是黄金文件比对的基准。改动模板会使"渲染输出与 Python 版一致"这一验证失效。

如需调整模板，应先在 Python 项目侧确认，再同步过来。

### 禁止 2：在渲染出的 NETCONF 片段里包 `<config>` 外壳

`rustnetconf` 的 `edit_config` 内部会 `wrap_config()`。模板只输出裸片段。详见 [`../transport/README.md`](../transport/README.md)。

### 禁止 3：绕过 `PathResolver` 直接拼路径

```rust
// 错误
let path = format!("os_versions/h3c/SR88_Comware_V7/{}/{}", patch, tpl);

// 正确
let candidates = resolver.resolve(host, tpl);
```

硬编码路径正是 Python 版新发现①要解决的问题，不要在 Rust 侧重新引入。

### 禁止 4：在 `LayerCoords::derive()` 中只提供部分显式覆盖

`template_vendor` / `template_product_line` / `template_os_version` 必须三者齐备。只给一两个时会走自动推导，产生难以预期的混合结果。

## 弱约束：推荐做法

### 推荐 1：新增场景优先复用 `SceneApi` 而非直接用 `TemplateRenderer`

`SceneApi` 已经持有 resolver 实例，避免每次调用都重新构造。

### 推荐 2：新增分层场景模板时，在 `SceneApi` 上加命名方法

而不是让调用方到处写 `layered_render(host, "cmd/xxx.j2", None)`。命名方法起到文档作用。

### 推荐 3：`ctx()` 辅助函数构造上下文

```rust
let context = ctx([("ifname", json!("LoopBack1"))]);
```

比手写 `JsonMap::new()` + 多次 `insert()` 清晰。

## 常见问题

### Q1：为什么渲染结果里有 `\r\n`？

`templates/` 下的 `.j2` 文件本身是 CRLF 行尾（从 Python 项目原样复制）。minijinja 与 Jinja2 都原样保留模板中的行尾，所以输出含 `\r\n`。

这是**正确行为**——两边逐字节一致的验证就包含了这一点。下发到设备时，CLI 通道会按行 trim 处理。

### Q2：`bootstrap.j2` 渲染结果里为什么有大量空行？

模板继承链上每一级的 `{% block custom_config %}` 都可能只有注释和空行。`trim_blocks` / `lstrip_blocks` 会移除块标签自身的换行，但保留模板正文里的空行。

Python 版输出完全相同（533 B 逐字节一致），所以不是缺陷。若要清理需改模板，而改模板会破坏比对基准。

### Q3：为什么不提供"与 Python 版对齐 autoescape"的开关？

因为做不到。Jinja2（markupsafe）的转义集是 `& < > " '`，minijinja 额外转义 `/`。开启 autoescape 会把接口名 `GigabitEthernet0/0/0` 变成 `GigabitEthernet0&#x2f;0&#x2f;0`，反而与 Python 版不一致。

关闭 autoescape 才是对齐的正确做法——已验证三处模板逐字节一致。详见上文"关键差异 ②"。

### Q4：如何让某台设备使用完全不同的模板路径？

三种方式，按侵入性从低到高：

1. `host.data` 里设 `patch`，走补丁级路径
2. `host.data` 里设三个 `template_*` 键，显式指定分层坐标
3. 实现自定义 `PathResolver`，通过 `SceneApi::render_with()` 传入

### Q5：新增一个厂商（如华为）需要改什么？

1. `templates/vendors/huawei/` 下放模板
2. `inventory/groups/vendors/huawei.yaml` 定义厂商 Group
3. Group 命名遵循 `huawei_<产线>_<os>_<版本>` 约定，`LayerCoords::derive()` 即可自动推导
4. 若华为的补丁命名规则与 H3C 不同，新增一个 `PatchExtractor` 实现并在 `get_patch_extractor()` 里加分支

**不需要**像 Python 版那样新写一个 `PathResolver`——`LayeredPathResolver` 是产线/厂商无关的。

### Q6：`startswith` 之外还有哪些 Python 方法可用？

`minijinja-contrib` 的 `pycompat` 提供的分支包括 `upper` / `lower` / `strip` / `replace` / `split` / `startswith` / `endswith` / `items` 等。完整列表见 [pycompat.rs](https://github.com/mitsuhiko/minijinja/blob/main/minijinja-contrib/src/pycompat.rs)。

不在列表内的方法（如 `format`）会报错。此时应改用 minijinja 的 filter 语法。
