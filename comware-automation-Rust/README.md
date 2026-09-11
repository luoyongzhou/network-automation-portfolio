# comware-automation-Rust

[`nornir-comware-automation`](../nornir-comware-automation)（Python + Nornir + Netmiko + ncclient）的 **Rust 重构可行性调研与迁移企划**。

> **当前状态：仅有文档，无代码。** 本目录是决策阶段的产出，不是一个可运行的项目。
> 调研与企划均在**无测试环境**条件下完成，未执行任何真机测试。

## 文档

| 文档 | 内容 |
| :--- | :--- |
| [`RESEARCH.md`](./RESEARCH.md) | 成熟度调研报告。逐层核实 Rust 生态能否替代 Nornir / Jinja2 / Netmiko / ncclient，含 crates.io 元数据与候选 crate 源码核查 |
| [`MIGRATION_PLAN.md`](./MIGRATION_PLAN.md) | 迁移重构企划书。目标架构、crate 选型、分期计划、工作量估算、风险登记、以及**是否应该现在迁移**的决策建议 |

## 调研结论速览

| 层次 | 是否需要自己实现 | 方案 |
| :--- | :--- | :--- |
| 并发调度（替 Nornir runner） | 否 | `tokio` + `Semaphore`，几十行即可覆盖现有用法 |
| 模板渲染（替 Jinja2） | 否 | `minijinja` + `minijinja-contrib`（`pycompat` 覆盖 `startswith`） |
| NETCONF（替 ncclient） | 否 | `rustnetconf`，`error_option` 是一等参数，`rollback-on-error` / `continue-on-error` 直接可用 |
| CLI over SSH（替 Netmiko） | 否 | `rneter`，内置 `h3c_comware` 模板，含 `%` / `^` 错误检测 |
| **Console 转 Telnet 开局** | **是** | 仅有 `telnet` crate 提供协议层，提示符状态机与 ZTP 中断时序需自写 |

## 决策建议

**结论是"能做，但建议先不做"。**

`nornir-comware-automation` 的 [`need_to_discuss_prob.md`](../nornir-comware-automation/need_to_discuss_prob.md) 里未解决的开放问题——cmd 下发错误检测、快照到回退步骤的映射、多设备两阶段提交、`atoms/` 抽象只落地一半——**没有一条是靠换语言解决的**。先迁移会导致在未经真机验证的传输层上调试设计问题，不确定性叠乘。

推荐先在 Python 侧收敛设计，待稳定后再按需评估迁移。若因部署形态（单二进制、跳板机无 Python 环境）必须迁移，企划书第一期（inventory + templating + preview）是纯机械翻译，可用黄金文件比对完全离线验证，风险最低。

## 调研过程中的一个副产物

阅读 ncclient 源码时确认：`edit_config(config=...)` 通过 `validated_element(config, ("config", qualify("config")))` 强制要求根元素为 `config`。

这意味着现有 Python 项目里 `atoms/base.py::NetconfAtom._edit_config()` 传裸片段的那条分支**一旦被调用就会抛 `XMLError`**（当前因该 Atom 从未被实例化而未暴露），而 `scenes/*.py` 手动拼 `<config>` 外壳才是正确用法。

这正好回答了 `need_to_discuss_prob.md` 延伸⑥提出的"两种包法哪个对"，且与语言选择无关——是当前代码里的一个待修缺陷。
