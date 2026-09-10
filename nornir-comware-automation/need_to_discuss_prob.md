①参考group分级，事务拆分原子化，很多模板语言都具备继承和基本的逻辑、block插入能力。
②架构需要关注编码细节，确保核心逻辑足够鲁棒，而后补全幂等性验证，回退逻辑等基于原子化必备的能力。
③cmd是continue-on-error模式，在某些不支持candidate-config和commit的配置管理的设备，回退逻辑需要很健壮。
④netconf可以配置rollback-on-error，回退基于快照模式。
⑤netconf下发可以借用netconf-browser做下发测试，跑通后写原子化的模板和回退逻辑。
⑥ncclient不是哑管道，类似edit-config会自己包标签，注意避免标签重合。

---

## 补充梳理（结合当前代码实现现状）

以下内容是在给各模块补写 README 的过程中，逐一阅读现有代码后发现的具体缺陷和待讨论项，按"原有条目的延伸"和"新发现的问题"两部分组织。目的是把原来六条比较抽象的原则，落到具体的文件、具体的函数上，方便下一步动手改的时候有明确抓手。

### 延伸①②：原子化目前只有一半落地，两种风格并存

`atoms/` 目录定义的四阶段模板方法（`pre_check → deploy → post_check → rollback`）只在 H3C 的接口 IP 和 Loopback 两个场景（`atoms/h3c/cmd/*.py`、`atoms/h3c/netconf/interface_ip_address.py`）完整落地。OSPF 场景（`scenes/test_ospf_deploy_full.py`）没有走这套抽象，是直接在场景脚本的 `task_deploy_ospf`/`task_rollback_ospf` 函数体内手写下发和回退逻辑。

这不只是"风格不统一"的洁癖问题，实际差异体现在能力上：
- OSPF 场景没有 `pre_check` 阶段，也就没有幂等跳过的判断（`merge` 操作本身幂等，但脚本不会在下发前告诉你"其实什么都不用做"）
- OSPF 的回退没有走 `atoms/base.py` 里"回退前依赖检查"的安全门（`_rollback_pre_check_impl`），是不是要在删除 OSPF 接口/区域/进程前检查是否被其他配置依赖，目前完全没做
- 快照结构不一致：IP 场景的快照记录的是"变更前的原始状态"（`current_ip` 可能是 `None`），OSPF 场景的快照直接存了"期望配置的完整数据"（`ospf_config` 整个字典），两种模式如果以后要合并到同一套 Atom 框架，需要先统一快照的语义

**待讨论**：是否要把 OSPF 重构为 `NetconfOspfAtom`？如果重构，快照该按 IP 场景的"变更前状态"模式，还是保留"期望配置"模式？两者哪个更适合 OSPF 这种"一次性配置多层级对象"的场景，需要先想清楚再动手，不要为了统一而统一。

### 延伸②：`NetconfLoopbackAtom` 是半成品，暴露了抽象基类的一个设计漏洞

`atoms/h3c/netconf/interface_loopback.py` 里的 `NetconfLoopbackAtom` 只实现了 `deploy_pre_check()` 和 `deploy()`，没有实现 `Atom` 抽象基类要求的标准四方法（`pre_check`/`post_check`/`rollback`）。因为 Python 的 `ABC` 只在**实例化时**检查抽象方法是否全部实现，如果这个类从来没有被真正 `执行()` 调用过（目前项目里确实没有任何地方实例化它），这个不完整实现不会在运行时报错，会一直静默存在，直到某天有人真的想用它才发现调不通。

**待讨论**：能否在 `build_inventory.py` 式的"预校验 Hook"精神下，加一个简单的启动期自检脚本，扫描 `atoms/` 下所有 `Atom` 子类，尝试实例化（不实际执行 `execute`）来提前暴露这类"定义了类但没实现全部抽象方法"的问题？还是说保持现状，靠 Code Review 兜底就够了（考虑到当前项目规模，重型工具可能得不偿失）。

### 延伸②：`execute()` 里 `post_check` 参数传递的设计随意点

`atoms/base.py` 的 `execute()` 中：
```python
post_result = self.post_check(host, snapshot, kwargs, **kwargs)
```
`post_check` 的形参名叫 `desired`（期望状态），但实际传入的是 `kwargs`（调用时的原始参数字典），二者语义不一致。目前各 Atom 子类的 `_post_check_impl` 都没有真正用到这个 `desired` 参数（校验逻辑改用 `snapshot` 里存的期望值），所以问题没有暴露，但这是一个隐藏的技术债——如果以后有场景真的需要在 `post_check` 里用到一个语义清晰的"期望状态"对象，需要先明确 `desired` 到底应该是什么、由谁构造、以什么形式传递，而不是继续复用 `kwargs`。

### 延伸③：cmd 回退的"健壮性"具体要健壮在哪

原有条目③提到"cmd 是 continue-on-error 模式，回退逻辑需要很健壮"，但当前 `atoms/h3c/cmd/*.py` 的实现中，`CmdAtom._render_and_send()` 对 Netmiko `send_command()` 的返回值**没有做任何失败检测**——只要连接没有抛异常，就认为下发成功：
```python
net_connect.send_command(config_text, expect_string=expect_string)
return {"status": "success", "detail": None}
```
如果设备实际返回了错误提示（如 H3C 常见的 `% Unrecognized command` 或 `% Wrong parameter`），但连接层面没有异常，这个函数依然会返回 `success`。也就是说，"回退逻辑需要健壮"的前提——"能准确判断每一步下发是否真的成功"——目前还没做到。这比回退逻辑本身更基础，需要先解决。

**待讨论**：是否要在 `_render_and_send()` 里增加对返回文本的错误关键字匹配（如扫描 `%`、`Error`、`Invalid` 等 H3C 常见错误前缀）？如果要做，这份"错误关键字列表"应该按厂商维护在哪里（`atoms/h3c/` 下新增一个常量文件，还是放进 `atoms/utils.py` 做成厂商可扩展的检测函数）？

### 延伸④：`rollback-on-error` 的原子性边界需要写进架构约束，不能只停留在文档里

`test_ospf_deploy_full.py` 用 `rollback-on-error` 保证了单次 `edit_config` 调用的原子性，`scenes/summary.md` 第八章已经分析得很清楚：这个保证不跨多次 RPC、不跨多台设备。当前项目对这个边界只是"写文档说明"，代码层面没有任何机制去补偿——如果三台设备中一台失败，另外两台已经生效的配置目前需要**人工**登录检查、决定要不要手动回退。

**待讨论**：这是否需要做成应用层的两阶段提交（先全部设备 `validate` 通过，再统一 `commit`）？H3C NETCONF 是否支持 `validate` 操作（在 `edit_config` 到 candidate 之后、`commit` 之前先校验，多设备都 `validate` 通过后再统一 `commit`，理论上能缩小"部分成功"的时间窗口，但仍不能完全消除，因为 `commit` 本身也可能在某台设备上失败）？这属于分布式事务的经典难题，投入产出比需要评估——当前 3 台设备的验证规模下，人工兜底可能已经够用，但如果未来设备数量上升到几十台，人工检查的成本会线性增长。

### 延伸⑤：netconf-browser 驱动的开发流程，目前只停留在个人经验，没有固化为可复用产物

原有条目⑤提到"用 netconf-browser 做下发测试，跑通后写模板"，这是一个很实际的开发方法论，但目前项目里没有留下任何 netconf-browser 探索阶段的产物（比如测试用的原始 XML 请求/响应样例）。`scenes/summary.md` 里记录的报错信息（`The data model is not supported`、`Configuration already exists` 等）本质上就是这个探索过程的副产品，但目前是以"事后总结"的形式存在，不是以"可重放的测试样例"形式存在。

**待讨论**：是否值得把每次探索验证过的 XML 请求/响应对，按场景归档成类似 `atoms/h3c/netconf/_verified_samples/ospf_create.xml` 这样的文件？好处是新人接手一个新场景时，可以先照抄一份已验证的 XML 手动用 netconf-browser 跑通，再回头写 Jinja2 模板，把"模型探索"和"模板工程化"两个阶段的产物都留痕，而不是只留下最终代码。

### 延伸⑥：`<config>` 标签重复包裹的问题在代码里有两种不同的处理方式，不统一

原有条目⑥提到 ncclient 会自动包 `<config>` 标签的坑。翻查代码发现，项目里对这个坑的规避方式并不统一：

- `atoms/base.py` 的 `NetconfAtom._edit_config()`：直接传 `xml_frag`（片段），**不**手动包 `<config>`，依赖 ncclient 自动包裹——这是符合库设计的正确用法
- `scenes/test_ip_deploy_full.py` 的 `task_deploy_netconf()`：手动拼接 `config_xml = f"<config>{xml_frag}</config>"` 后传给 `session.edit_config(config=config_xml, ...)`
- `scenes/test_ospf_deploy_full.py` 同样手动拼接 `<config>` 外壳

场景脚本里的手动包裹方式之所以没有报错，大概是因为 ncclient 对"已经带 `<config>` 外壳的输入"做了兼容处理（或者这几个场景验证时刚好没触发标签重合的报错）。但这意味着**同一个坑，项目里现在有两种不同的规避方式，其中至少一种可能只是"恰好没出问题"，不是"确认正确"**。

**待讨论**：需要用 ncclient 的源码或官方文档确认：`edit_config(config=...)` 参数到底期望"带 `<config>` 外壳的完整文档"还是"不带外壳的片段"，两种传法是否总是等价，还是在某些 ncclient 版本/某些设备的响应场景下会有差异。确认后应该把 `atoms/base.py` 和 `scenes/*.py` 里的用法统一成同一种，避免"两份代码用不同方式绕开同一个坑，靠运气都没触发"的隐患。

### 新发现①：SR88 型号硬编码分散在多处，产线扩展成本比目录结构看起来的要高

`templates/README.md` 和 `atoms/README.md` 都强调了"分层对称"的设计，但实际的路径字符串是硬编码在多个地方的：`scripts/preview_bootstrap.py` 的 `H3CBootstrapPathResolver.resolve()` 里硬编码了 `"os_versions/h3c/SR88_Comware_V7"` 这个具体产线路径，`atoms/h3c/cmd/interface_ip_address.py` 等 Atom 类的模板路径类属性里也硬编码了 `"product_lines/h3c/SR88/..."`。

也就是说，即使 `inventory/groups/product_lines/h3c/` 下新增了一个 `ASR.yaml` 产线定义，`H3CBootstrapPathResolver` 也**不会**自动支持这个新产线——因为路径解析器本身就是按 SR88 写死的，需要新写一个 `H3CASRPathResolver` 或者把 SR88 改成从 Group 名称动态提取的产线段。当前"分层"更多体现在**模板文件和 Group 定义**的组织上，路径解析这一层代码目前还是单产线特化的。

**待讨论**：`PathResolver` 是否需要从"H3C SR88 专属"重构为"从 Group 名称动态提取产线名"的通用实现？如果要做，产线名的提取规则（类似 `PatchExtractor` 对补丁版本的提取）应该按什么模式匹配 Group 名称？这会牵动 `atoms/` 里所有硬编码模板路径的 Atom 类，是一次有一定影响面的重构，建议在真正需要接入第二个产线时才做，避免过早抽象。

### 新发现②：`huawei/`、`cisco/` 的"预留空目录"目前是纯粹的目录占位，没有任何契约约束

`atoms/huawei/`、`atoms/cisco/` 和 `templates/vendors/`（目前只有 `h3c/`）都是"预留但未实现"的状态。这类预留在没有实际的第二个厂商接入之前，很难验证"预留的结构是否真的够用"——比如 `PatchExtractor` 已经为 Cisco 写好了正则规则（`CiscoPatchExtractor`），但从来没有一个 `CiscoPathResolver` 真正用过它，也没有真实的 Cisco 模板/Atom 去验证这套补丁提取规则是否符合 Cisco 实际的 Group 命名习惯。

**待讨论**：这类"预留"是否需要一份专门的"新增厂商接入 checklist"文档？（`templates/README.md` 里已经有"如何新增一个厂商的模板"的 FAQ，但只覆盖模板层，没有覆盖 `atoms/`、`PathResolver`、`PatchExtractor` 三者需要同步新增的完整清单。）在真正接入华为/Cisco 之前，这份 checklist 更多是"防止遗漏步骤"的检查表，而不是急需的开发任务。

**已产出**：[`docs/vendor_onboarding_checklist.md`](./docs/vendor_onboarding_checklist.md) 梳理了六个必须同步改动的维度（Inventory Group → hosts.yaml → 模板 → PatchExtractor/PathResolver → atoms → 文档）及对应的完整性自查表。目前仍是基于 H3C 现有实现逆向整理的"预期步骤"，尚未经过真实的第二个厂商接入验证，真正接入时如发现与文档描述不符，应及时修正文档本身。

### 新发现③：`logs/` 目录混杂了"调试产物"和"业务快照"，两者的生命周期和敏感度不同

当前 `logs/` 目录下同时存放：
- `ifindex_<device>.xml`：纯调试产物，`get_ifindex_map()` 每次查询都会覆盖写入，没有实际业务价值，随时可删
- `snapshots_<device>_<mode>.json`：**回退操作的唯一依据**，如果这个文件在需要回退的时候意外被清理（比如误执行了清理脚本、或者磁盘空间不足被日志轮转工具误删），对应设备的变更将无法通过脚本自动回退，只能靠人工登录设备手工核对配置

两者混在同一个目录、用同样的命名前缀风格，容易在做日志清理运维时被一并处理，增大误删快照的风险。

**待讨论**：`logs/` 是否要拆分成 `logs/debug/`（调试用，可随时清理）和 `state/snapshots/`（业务状态，需要纳入备份策略）两个目录？如果要拆，涉及修改 `atoms/utils.py::get_ifindex_map()` 和 `scenes/*.py` 里多处 `Path("logs")` 的写入路径，需要同步调整，改动面不小，但从数据安全角度看这个分离是有价值的。

---

*本节内容基于对当前代码的实际阅读整理，目的是把讨论项和具体文件、具体函数对应起来，不代表已经有定论——多数条目标注了"待讨论"，意味着需要进一步权衡投入产出比、验证真实需求后才适合动手实现，避免脱离真实场景的过度设计。*
