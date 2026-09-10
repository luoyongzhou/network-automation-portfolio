# 新增厂商接入 Checklist（以接入 Huawei / Cisco 为例）

## 这份文档是什么

`atoms/huawei/`、`atoms/cisco/`、以及未来可能的其他厂商目录，目前都是"预留但未实现"的空壳。`templates/README.md` 里已经有一份"如何新增一个厂商的模板"的 FAQ，但那份 FAQ **只覆盖模板层**。实际上新增一个厂商需要同步触碰六个维度的代码/配置，任何一处遗漏都会导致新厂商"看起来接进来了，但实际用不起来"或者"部分场景能跑，部分场景静默失败"。

这份 checklist 把六个维度按依赖顺序排列，逐项列出"要做什么"、"参照 H3C 的哪个文件"、"最容易漏掉的坑"。**用途是防止遗漏步骤，不是要求现在就去实现**——在真正需要接入华为或 Cisco 设备之前，这只是一份参考清单；真正接入时，建议按顺序逐项过一遍，而不是想起哪个做哪个。

## 前提假设

以下步骤假设新厂商是"华为 VRP"（`atoms/huawei/` 已预留）或"Cisco IOS"（`atoms/cisco/` 已预留），且至少要支持一个具体产线/型号（类比 H3C 的 SR88）。如果新厂商完全没有 NETCONF 能力、只支持 CLI，第 3、4 步中 NETCONF 相关的部分可以跳过，但仍需要在文档里明确写出"该厂商不支持 NETCONF"，而不是留空不提。

---

## 第 1 步：Inventory 分层 Group 定义

**要做什么**：在 `inventory/groups/` 三层目录下分别新增该厂商的定义文件。

```
inventory/groups/
├── vendors/huawei.yaml                              # 新增：厂商级基类
├── product_lines/huawei/<product>.yaml              # 新增：产线级（如 huawei/CE6800.yaml）
└── os_versions/huawei/<os_version>.yaml             # 新增：OS版本级（最终被 groups.yaml 输出）
```

**参照**：`inventory/groups/vendors/h3c.yaml`、`inventory/groups/product_lines/h3c/SR88.yaml`、`inventory/groups/os_versions/h3c/SR88_Comware_V7.yaml` 的三层写法，具体的字段规范（`parents`、`connection_options`、`data` 的类型约束）见 [`inventory/README.md`](../inventory/README.md)。

**最容易漏掉的坑**：
- 忘记在厂商级基类（`vendors/huawei.yaml`）设置正确的 `platform` 字段——这个值必须是 Netmiko/ncclient 认识的厂商标识（如 `huawei` 对应 Netmiko 的 `huawei` device_type），错误的值会导致连接层直接报错，而不是配置层的问题，容易误判排查方向
- 最终版本层 Group 必须放在 `os_versions/` 目录下，放在 `vendors/` 或 `product_lines/` 下会被 `build_inventory.py` 的过滤逻辑排除，`hosts.yaml` 引用时会报 `KeyError`（见 [`scripts/build_inventory_README.md`](../scripts/build_inventory_README.md) "常见问题"）
- 跑一次 `python scripts/build_inventory.py`，检查输出的 `groups.yaml` 里确实出现了新厂商的 Group，再进行下一步

## 第 2 步：`hosts.yaml` 补充测试设备

**要做什么**：在 `inventory/hosts.yaml` 中添加至少一台该厂商的测试设备，引用第 1 步新建的 Group。

**参照**：`inventory/hosts.yaml` 中现有 H3C 设备条目的结构（`hostname`、`groups`、`connection_options`、`data`）。

**最容易漏掉的坑**：如果暂时没有真实设备可连接，仍建议先加一条设备定义（哪怕连接会失败），这样后续步骤中 `python verify_inheritance.py`、`python scripts/preview_bootstrap.py <device>` 这类不需要真实连接的验证命令才能跑起来，提前发现 Inventory 层的配置问题。

## 第 3 步：Jinja2 模板

**要做什么**：按"厂商 → 产线 → OS版本"三层，同时创建 `cmd/`（CLI）和 `netconf/`（如果支持 NETCONF）两个协议子目录下的模板文件。

```
templates/
├── vendors/huawei/
│   ├── cmd/
│   │   ├── _macros.j2          # 厂商级宏定义（VRP 命令语法）
│   │   ├── bootstrap.j2
│   │   └── netconf_cmd.j2
│   └── netconf/                 # 如果 VRP 支持 NETCONF
├── product_lines/huawei/<product>/
│   ├── cmd/...
│   └── netconf/...
└── os_versions/huawei/<os_version>/
    ├── cmd/...
    └── netconf/...
```

**参照**：`templates/README.md` 全文，尤其是"目录对称原则"、"继承链路原则"、"宏定义规范"三节；具体的宏写法参照 `templates/vendors/h3c/cmd/_macros.j2`（如 `interface_config`、`ssh_user` 等宏的参数化、单一职责写法）。

**最容易漏掉的坑**：
- 每一层的父模板都必须定义 `{% block custom_config %}`，子模板才能通过覆盖该 block 追加差异内容——**block 名称必须是 `custom_config`**，自创新名称会被静默丢弃，不报错（`templates/README.md` "强约束 1/2"）
- `{% extends %}` 路径必须相对于 `templates/` 根目录，且必须包含 `cmd/` 或 `netconf/` 前缀
- 宏文件只能被同厂商模板 `{% import %}`，不能跨厂商引用——华为的模板绝对不能 `import` H3C 的 `_macros.j2`，即使命令语法看起来相似
- 命令语法差异要在最底层（宏或叶子模板）体现，不要为了"复用 H3C 的模板结构"而在华为的宏里塞 H3C 特有的命令逻辑

## 第 4 步：`PatchExtractor` 与 `PathResolver`

**要做什么**：确认（或新写）该厂商的补丁版本提取规则和模板路径解析器。

**现状**：`scripts/preview_bootstrap.py` 里 `CiscoPatchExtractor` 已经写好了（提取形如 `_V15.2` 的版本号），但**没有对应的 `CiscoPathResolver`**——也就是说 Extractor 写了，Resolver 没写，两者要配套才能工作。Huawei 目前连 `PatchExtractor` 都没有专属实现，会落到 `DefaultPatchExtractor`（提取纯数字结尾）。

**要做的具体工作**：
1. 确认新厂商的 Group 命名规则里，补丁/版本号是怎么编码的（H3C 是 `_r7171` 这种下划线+字母前缀+数字），如果现有的 `CiscoPatchExtractor` 或 `DefaultPatchExtractor` 的正则不匹配，需要新写一个 `<Vendor>PatchExtractor` 子类
2. 新写一个 `<Vendor>PathResolver(PathResolver)`，参照 `H3CBootstrapPathResolver.resolve()` 的候选路径生成逻辑，把其中硬编码的 `"os_versions/h3c/SR88_Comware_V7"` 换成新厂商、新产线对应的路径前缀
3. 在 `get_patch_extractor()` 函数里补充新厂商的关键字判断分支

**参照**：`scripts/preview_bootstrap_README.md` "四层核心抽象"章节第 1、2 点，对 `PatchExtractor` 和 `PathResolver` 的设计原则有完整说明。

**最容易漏掉的坑**：
- `H3CBootstrapPathResolver` 目前是"H3C SR88 专属"硬编码实现，**不能直接复用**给华为/Cisco——必须新写一个类，不是改参数就能通用（`need_to_discuss_prob.md` "新发现①"里记录了这个硬编码问题，如果打算接入第二个厂商，正好是重新评估是否要把 `PathResolver` 改造为通用实现的时机）
- 只写了 `PatchExtractor` 没写配套的 `PathResolver`，会导致补丁提取的结果被算出来了却没人用，模板查找依然走不到分支/热补丁级——这正是当前 Cisco 的真实状态，接入时不要重复这个半成品

## 第 5 步：`atoms/` 原子操作实现

**要做什么**：在 `atoms/huawei/cmd/`（和/或 `atoms/huawei/netconf/`）下，为每个业务场景（接口 IP、Loopback……）实现对应的 Atom 类。

**参照**：[`atoms/README.md`](../atoms/README.md) 全文，尤其是"现有 Atom 一览"表格里标注为"完整实现"的两个参考样例——`atoms/h3c/cmd/interface_ip_address.py`、`atoms/h3c/netconf/interface_ip_address.py`。**不要参照** `atoms/h3c/netconf/interface_loopback.py`（`NetconfLoopbackAtom`），该实现是已知的半成品（未对齐 `Atom` 抽象基类的标准四方法），照抄会把同样的缺陷带到新厂商。

**最容易漏掉的坑**：
- `pre_check()` 必须在 `snapshot` 里记录**变更前的原始状态**，不是只记录"即将下发的目标状态"——这是"快照驱动回退"的核心，抄错了会导致回退逻辑表面能跑但结果是错的（比如本该恢复原IP，却又下发了一遍新IP）
- 删除类的 `rollback()` 必须先过依赖检查安全门（参照 `_rollback_pre_check_impl` 的调用方式），不能省略这一步直接删
- Atom 类里模板路径类属性（如 `DEPLOY_TEMPLATE`）填写的相对路径，必须与第 3 步实际创建的模板文件路径完全对应，这是最常见的"实现了但用不起来"的原因——建议实现完 Atom 后，立即写一个简单的手动调用脚本验证路径能被正确解析和渲染，不要等到接入完整场景脚本才发现路径错误

## 第 6 步：文档同步

**要做什么**：至少更新以下几处文档，把新厂商的状态从"预留未实现"改为"已实现"：

- `atoms/README.md` "现有 Atom 一览" 表格里补充新厂商的条目
- `templates/README.md` 如果新增了跨厂商通用的场景（如 QoS），确认 FAQ 里的"如何新增厂商的模板"步骤描述依然准确
- 本文档（`docs/vendor_onboarding_checklist.md`，如果存在的话）里，把对应厂商从"预留"移动到"已接入"的记录
- 如果这个厂商需要真实设备验证（不是纯粹的模板渲染测试），建议留一份类似 `scenes/summary.md` 的过程记录文档，把调试中遇到的报错、根因、解决方案记录下来——这是 H3C 从"能跑"变成"经过验证、可信赖"的关键产出，不要因为赶进度而跳过这一步

**最容易漏掉的坑**：文档更新往往在功能验证通过后被认为"已经不需要了"而被跳过，但对于一个多人协作或者长期维护的项目，"代码显示已实现但文档还写着预留"的不一致状态，比"干脆没有这个功能"更容易误导后续开发者。

---

## 完整性自查表

接入一个新厂商后，建议按这张表逐项确认，全部打勾才算真正接入完成（不是"能跑通一个 demo"就算完成）：

| 检查项 | 对应步骤 | 确认方法 |
| :--- | :--- | :--- |
| Group 三层定义完整，且能被 `build_inventory.py` 正确合并输出 | 第1步 | `python scripts/build_inventory.py` 输出中包含新厂商 Group，无报错 |
| 至少一台测试设备已在 `hosts.yaml` 中定义 | 第2步 | `python verify_inheritance.py` 能看到该设备引用的 Group 完整配置 |
| 每个场景在 `cmd/`（及 `netconf/`，如适用）下都有厂商级、产线级、OS版本级三层模板 | 第3步 | 对照 `templates/README.md` 目录结构表格逐层检查文件是否存在 |
| 新厂商的 `PatchExtractor` 能正确从 Group 名称提取补丁版本 | 第4步 | 手动调用 `get_patch_extractor("huawei").extract([...], host)`，确认返回值符合预期 |
| 新厂商有专属的 `PathResolver`，且候选路径生成逻辑与模板实际目录结构吻合 | 第4步 | `python scripts/preview_bootstrap.py --scene <场景> <设备>` 能成功渲染，不报 `no_template_found` |
| 每个业务场景都有对应的 Atom 类，四个抽象方法全部实现 | 第5步 | 尝试实例化每个 Atom 子类（不调用 `execute`），确认 Python 不抛 `TypeError: Can't instantiate abstract class` |
| `pre_check`/`rollback` 的快照结构经过至少一次"下发→回退→核对设备实际配置"的完整验证 | 第5步 | 参照 `scenes/test_ip_deploy_full_README.md` 的验证方式，在测试设备上实测一轮 |
| 相关文档已同步更新，不再显示"预留未实现" | 第6步 | 检查 `atoms/README.md`、`templates/README.md`、本文档 |

---

*本文档在没有真实接入第二个厂商之前，是基于对 H3C 现有实现的逆向梳理写成的"预期步骤"，不是已经验证过的操作手册。真正接入华为/Cisco 时，如果发现某个步骤的实际情况与本文档描述不符（比如华为 VRP 的 NETCONF 模型结构和 H3C 私有模型的封装方式差异很大，导致 `atoms/base.py` 的某个假设不成立），应当及时修正本文档，而不是让文档和代码继续脱节。*
