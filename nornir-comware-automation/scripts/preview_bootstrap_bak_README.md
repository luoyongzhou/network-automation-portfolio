# preview_bootstrap_bak.py

## 概述

`preview_bootstrap_bak.py` 是 `preview_bootstrap.py` 的**历史备份版本**，代表了模板渲染引擎在"引入 `cmd/`/`netconf/` 协议分层"这次重构之前的状态。本文件**不被任何脚本导入或调用**，纯粹作为演进过程的存档保留在仓库中。

## 为什么保留这个文件

删除旧代码是危险的：模板渲染引擎是全项目最核心的公共依赖（`atoms/`、`scenes/` 都间接依赖它），如果重构后发现新版本在某个边界场景下行为有差异，能够直接 diff 出"旧版本当时是怎么处理的"，比凭记忆回想或者去翻 git 历史（如果没有做真正的版本控制提交）更快。这份 `_bak` 文件本质上是一次手工的、粒度为"整个文件"的快照，而不是精细的版本控制。

## 与当前版本的核心差异

详见 [`preview_bootstrap_README.md`](./preview_bootstrap_README.md) 中"与 `preview_bootstrap_bak.py` 的关系"章节的对比表格。最关键的一点：**本文件里的模板路径不带 `cmd/`/`netconf/` 协议前缀**（如 `"bootstrap.j2"` 而不是 `"cmd/bootstrap.j2"`），如果直接拿本文件去跑，会因为找不到模板文件而报 `no_template_found`——这不是 bug，只是文件对应的模板目录结构（重构前）已经不再存在，`templates/` 目录现在是重构后的新结构。

## 本文件独有、当前版本已移除的实现：`render_bootstrap_config()`

当前版本（`preview_bootstrap.py`）里所有场景统一通过 `SceneAPI` 方法 + `TemplateRenderer.render()` 处理。本文件里额外存在一个独立的 `render_bootstrap_config(host)` 函数，**只针对 `netconf_cmd.j2` 场景**手写了一遍候选路径生成、模板查找、渲染的全过程，和 `TemplateRenderer.render()` 的逻辑高度重复，区别在于它带有大量调试用的 `print` 语句：

```python
print(f"\n🔍 设备: {host.name}")
print(f"   Group 列表: {group_names}")
print(f"   提取到的补丁路径段: {patch}")
...
print(f"\n📁 候选模板路径（按优先级）:")
for idx, rel in enumerate(candidates, 1):
    exists = "✅ 存在" if full_path.exists() else "❌ 不存在"
    print(f"   {idx}. {rel} -> {exists}")
```

这份重复实现看起来是在调试"补丁路径提取是否正确"、"候选路径命中了哪一层"这类问题时，为了快速看到中间过程而写的临时排障代码，之后没有被清理、也没有被合并回通用的 `TemplateRenderer` 里，而是随着场景扩展到 ACL/QoS 等场景后，被完全统一走 `SceneAPI` 的新实现所取代。这段历史提示：如果未来在 `TemplateRenderer` 里调试候选路径匹配问题，可以参考这里的调试打印风格,临时加回类似的 print，而不需要重新设计一套调试输出格式。

## 使用建议

**不要在新代码中 import 本文件**。如果需要参考旧版本行为，直接阅读或 diff 即可；如果需要恢复某个旧行为（如更细粒度的调试打印），应该把对应逻辑迁移进 `preview_bootstrap.py` 的对应类中，而不是让两份文件同时被使用——那样会导致"模板路径解析规则在项目里存在两套"的维护风险。
