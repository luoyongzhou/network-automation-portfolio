#!/usr/bin/env python3
"""
合并 inventory/groups/ 下所有 YAML 文件，展开继承关系，
最终只保留 os_versions/ 目录下定义的 Group（版本层），
其他层级（vendors, product_lines）仅作为中间继承源，不输出。

预校验 Hook 已预留，用于未来逐步增加对 Group 文件结构的强约束。
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import yaml


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    深度合并两个字典，override 覆盖 base

    Args:
        base: 基础字典（父级配置）
        override: 覆盖字典（子级配置）

    Returns:
        合并后的新字典
    """
    result: Dict[str, Any] = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def expand_inheritance(groups: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    递归展开所有 Group 的 parents 链，返回展开后的字典

    Args:
        groups: 原始 Group 定义字典

    Returns:
        展开后的 Group 字典（每个 Group 已包含所有继承属性）

    Raises:
        RecursionError: 检测到循环继承
        KeyError: 引用了不存在的父组
    """
    processing: Set[str] = set()
    expanded: Dict[str, Dict[str, Any]] = {}

    def resolve_group(name: str) -> Optional[Dict[str, Any]]:
        if name in expanded:
            return expanded[name]
        if name in processing:
            raise RecursionError(f"循环继承检测: {name}")
        if name not in groups:
            raise KeyError(f"Group '{name}' 未定义")

        processing.add(name)
        group_def: Dict[str, Any] = deepcopy(groups[name])
        parents: List[str] = group_def.get("parents", [])

        if not parents:
            result: Dict[str, Any] = group_def
        else:
            merged: Dict[str, Any] = {}
            for parent_name in parents:
                parent_data: Optional[Dict[str, Any]] = resolve_group(parent_name)
                if parent_data is not None:
                    merged = deep_merge(merged, parent_data)
            merged = deep_merge(merged, group_def)
            result = merged
            result.pop("parents", None)

        processing.remove(name)
        expanded[name] = result
        return result

    # 展开所有 Group
    for name in list(groups.keys()):
        if name not in expanded:
            resolve_group(name)

    return expanded


def pre_validate_groups(
    raw_groups: Dict[str, Dict[str, Any]], group_source: Dict[str, str]
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    预校验 Group 定义文件的结构是否符合规范。

    当前版本为空实现（仅作 Hook 预留），未来可逐步增加：
      - 目录层级校验
      - 类型一致性校验
      - 命名规范校验
      - 跨厂商继承隔离校验
      - 循环继承检测（已在 expand_inheritance 中实现，此处可补充更细粒度校验）

    Args:
        raw_groups: 原始 Group 定义字典
        group_source: Group 名称到来源目录的映射

    Returns:
        (errors, warnings): 致命错误列表和警告列表
    """
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    # ===== 未来扩展示例（注释） =====
    # 1. 目录层级校验：确保 os_versions 下的 Group 不直接引用同目录 Group
    # for name, cfg in raw_groups.items():
    #     if group_source.get(name) == "os_versions":
    #         for p in cfg.get("parents", []):
    #             if group_source.get(p) == "os_versions":
    #                 warnings.append({
    #                     "group": name,
    #                     "message": f"os_versions Group 直接继承同目录 Group '{p}'，建议检查继承链"
    #                 })

    # 2. 类型一致性校验
    # for name, cfg in raw_groups.items():
    #     for p in cfg.get("parents", []):
    #         if p in raw_groups:
    #             parent_cfg = raw_groups[p]
    #             for key in set(cfg.keys()) & set(parent_cfg.keys()):
    #                 if type(cfg[key]) != type(parent_cfg[key]):
    #                     errors.append({
    #                         "group": name,
    #                         "parent": p,
    #                         "key": key,
    #                         "message": f"类型不一致: 父级 {type(parent_cfg[key]).__name__}，子级 {type(cfg[key]).__name__}"
    #                     })

    return errors, warnings


def merge_group_files() -> None:
    """主函数：执行 Group 文件合并流程"""
    script_dir: Path = Path(__file__).parent.absolute()
    project_root: Path = script_dir.parent
    groups_root: Path = project_root / "inventory" / "groups"
    output_file: Path = project_root / "inventory" / "groups.yaml"

    if not groups_root.exists():
        print(f"❌ 目录不存在: {groups_root}")
        sys.exit(1)

    raw_groups: Dict[str, Dict[str, Any]] = {}
    group_source: Dict[str, str] = {}

    yaml_files: List[Path] = sorted(
        groups_root.rglob("*.yaml"),
        key=lambda p: (len(p.relative_to(groups_root).parents), p.name),
    )

    if not yaml_files:
        print(f"⚠️  未找到任何 .yaml 文件: {groups_root}")
        return

    print(f"📁 扫描到 {len(yaml_files)} 个 YAML 文件")

    for file_path in yaml_files:
        if file_path.resolve() == output_file.resolve():
            continue
        if file_path.name.startswith("_"):
            print(f"⏭️  跳过模板: {file_path.relative_to(project_root)}")
            continue

        rel_path: Path = file_path.relative_to(groups_root)
        source_dir: str = rel_path.parts[0] if rel_path.parts else ""

        print(f"📄 加载: {rel_path}")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data: Optional[Dict[str, Any]] = yaml.safe_load(f)
                if not data:
                    continue
                for group_name, group_config in data.items():
                    if group_name in raw_groups:
                        print(f"   ⚠️  重复 Group '{group_name}'，将被覆盖")
                    raw_groups[group_name] = group_config
                    group_source[group_name] = source_dir
        except Exception as e:
            print(f"   ❌ 加载失败: {e}")
            sys.exit(1)

    if not raw_groups:
        print("❌ 没有解析到任何 Group 定义")
        sys.exit(1)

    # ===== 预校验 Hook =====
    print("\n🔍 执行 Group 结构预校验...")
    errors, warnings = pre_validate_groups(raw_groups, group_source)
    for warn in warnings:
        print(f"⚠️  [警告] {warn.get('message', '')}")
    if errors:
        print("❌ 预校验失败，终止合并：")
        for err in errors:
            print(f"   {err.get('message', '')}")
        sys.exit(1)
    else:
        print("✅ 预校验通过（当前版本未启用实质性校验）")

    print("\n🔗 正在展开继承关系...")
    try:
        expanded: Dict[str, Dict[str, Any]] = expand_inheritance(raw_groups)
    except Exception as e:
        print(f"❌ 展开继承失败: {e}")
        sys.exit(1)

    # 过滤：只保留来自 os_versions 目录的 Group
    filtered: Dict[str, Dict[str, Any]] = {
        name: cfg
        for name, cfg in expanded.items()
        if group_source.get(name) == "os_versions"
    }

    if not filtered:
        print("❌ 过滤后没有任何 Group 保留（请检查 os_versions 目录是否存在有效文件）")
        sys.exit(1)

    # 输出最终结果
    with open(output_file, "w", encoding="utf-8") as f:
        yaml.dump(
            filtered,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    print(f"\n✅ 成功合并并展开，最终保留 {len(filtered)} 个版本层 Group")
    print(f"📁 输出: {output_file}")
    print("📋 保留的 Group: " + ", ".join(filtered.keys()))


if __name__ == "__main__":
    merge_group_files()