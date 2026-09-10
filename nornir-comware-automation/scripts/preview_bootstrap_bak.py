#!/usr/bin/env python3
"""
模板渲染引擎（支持多场景、多厂商、多版本）
支持嵌套目录结构：os_versions/h3c/SR88_Comware_V7/{branch}/{hotfix}/
用法：
    python scripts/preview.py                          # 渲染所有设备的 bootstrap
    python scripts/preview.py --scene ssh_bootstrap    # SSH 下发 netconf 初始化
    python scripts/preview.py --scene ssh_acl          # SSH 下发 ACL 命令
    python scripts/preview.py --scene netconf_acl      # NETCONF 下发 ACL XML
    python scripts/preview.py H3C-SR88-01              # 渲染指定设备的 bootstrap
"""

import os
import sys
import re
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from abc import ABC, abstractmethod

from jinja2 import Environment, FileSystemLoader, select_autoescape
from nornir import InitNornir
from nornir.core.inventory import Host


# ============================================================
# 补丁版本提取器（支持分支+热补丁嵌套路径）
# ============================================================

class PatchExtractor(ABC):
    @abstractmethod
    def extract(self, group_names: List[str], host: Host) -> Optional[str]:
        """
        从 Group 名称或 Host.data 中提取补丁路径段。
        返回格式：
            - 仅有分支： "R7171"
            - 有分支+热补丁： "R7171/H02"
            - 无匹配： None
        """
        pass

    def _extract_with_pattern(self, group_names: List[str], pattern: str) -> Optional[str]:
        for g in group_names:
            match = re.search(pattern, g, re.IGNORECASE)
            if match:
                return match.group(1).upper()
        return None


class H3CPatchExtractor(PatchExtractor):
    def extract(self, group_names: List[str], host: Host) -> Optional[str]:
        # 1. 如果 host.data 显式指定了 patch，直接使用
        if host.data.get("patch"):
            return host.data.get("patch")

        branch = None
        hotfix = None

        for g in group_names:
            # 提取分支：_rR7171
            branch_match = re.search(r'_r([0-9]+)', g, re.IGNORECASE)
            if branch_match:
                branch = f"R{branch_match.group(1)}"
            # 提取热补丁：_hH02
            hotfix_match = re.search(r'_h([0-9]+)', g, re.IGNORECASE)
            if hotfix_match:
                hotfix = f"H{hotfix_match.group(1)}"

        if branch and hotfix:
            return f"{branch}/{hotfix}"
        elif branch:
            return branch
        return None


class CiscoPatchExtractor(PatchExtractor):
    def extract(self, group_names: List[str], host: Host) -> Optional[str]:
        if host.data.get("patch"):
            return host.data.get("patch")
        return self._extract_with_pattern(group_names, r'_V([\d.]+)')


class DefaultPatchExtractor(PatchExtractor):
    def extract(self, group_names: List[str], host: Host) -> Optional[str]:
        if host.data.get("patch"):
            return host.data.get("patch")
        for g in group_names:
            match = re.search(r'_([\d]+)$', g)
            if match:
                return match.group(1)
        return None


def get_patch_extractor(platform_or_group: str) -> PatchExtractor:
    key = str(platform_or_group).lower()
    if "h3c" in key or "hp_comware" in key:
        return H3CPatchExtractor()
    elif "cisco" in key or "ios" in key:
        return CiscoPatchExtractor()
    else:
        return DefaultPatchExtractor()


# ============================================================
# 路径解析器抽象基类
# ============================================================

class PathResolver(ABC):
    """候选模板路径解析器基类"""

    def __init__(self, extractor: Optional[PatchExtractor] = None):
        self.extractor = extractor or DefaultPatchExtractor()

    @abstractmethod
    def resolve(self, host: Host, template_name: str) -> List[str]:
        """
        返回候选模板路径列表（相对于 templates/ 根目录）
        按优先级从高到低排列
        """
        pass

    def _get_platform(self, host: Host) -> str:
        group_names = [g.name for g in host.groups]
        return host.platform or (group_names[0] if group_names else "")

    def _get_patch(self, host: Host) -> Optional[str]:
        group_names = [g.name for g in host.groups]
        return self.extractor.extract(group_names, host)


# ============================================================
# 具体路径解析器（适配嵌套目录结构）
# ============================================================

class H3CBootstrapPathResolver(PathResolver):
    """
    H3C 通用路径解析器，适配嵌套目录结构：
        os_versions/h3c/SR88_Comware_V7/{branch}/{hotfix}/{template_name}
    搜索顺序：
        1. 补丁级（如 SR88_Comware_V7/R7171/H02/）
        2. 分支级（如 SR88_Comware_V7/R7171/）
        3. OS 版本级（SR88_Comware_V7/）
        4. 产线级（product_lines/h3c/SR88/）
        5. 厂商级（vendors/h3c/）
        6. 全局兜底（_base/）
    """

    def resolve(self, host: Host, template_name: str) -> List[str]:
        candidates: List[str] = []
        patch = self._get_patch(host)  # 可能是 "R7171" 或 "R7171/H02"

        # 1. 补丁/分支级（嵌套结构）
        if patch:
            rel = f"os_versions/h3c/SR88_Comware_V7/{patch}/{template_name}"
            candidates.append(rel)

        # 2. OS 版本级
        rel = f"os_versions/h3c/SR88_Comware_V7/{template_name}"
        candidates.append(rel)

        # 3. 产线级
        rel = f"product_lines/h3c/SR88/{template_name}"
        candidates.append(rel)

        # 4. 厂商级
        rel = f"vendors/h3c/{template_name}"
        candidates.append(rel)

        # 5. 全局兜底
        rel = f"_base/{template_name}"
        candidates.append(rel)

        return candidates


# ============================================================
# 模板渲染引擎（核心类）
# ============================================================

class TemplateRenderer:
    """通用 Jinja2 模板渲染引擎"""

    def __init__(self, templates_root: Optional[Path] = None):
        self.templates_root = templates_root or Path.cwd() / "templates"
        if not self.templates_root.exists():
            raise FileNotFoundError(f"模板目录不存在: {self.templates_root}")

        self.env = Environment(
            loader=FileSystemLoader(str(self.templates_root)),
            autoescape=select_autoescape(['xml', 'j2']),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def build_base_context(self, host: Host) -> Dict[str, Any]:
        """构建基础渲染上下文（所有场景通用）"""
        return {
            "sysname": host.name,
            "mgmt_ip": host.hostname,
            "mgmt_interface": host.data.get("mgmt_interface", "Vlan-interface 100"),
            "mgmt_mask": host.data.get("mgmt_mask", "255.255.255.0"),
            "gateway": host.data.get("gateway"),
            "username": host.username,
            "password": host.password,
            "enable_secret": host.data.get("enable_secret"),
        }

    def render(
        self,
        host: Host,
        template_name: str,
        context_override: Optional[Dict[str, Any]] = None,
        path_resolver: Optional[PathResolver] = None,
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """渲染模板，返回 (config, error)"""
        context = self.build_base_context(host)
        if context_override:
            context.update(context_override)

        if path_resolver is None:
            path_resolver = H3CBootstrapPathResolver(get_patch_extractor("h3c"))

        candidates = path_resolver.resolve(host, template_name)

        # 过滤存在的模板
        existing_candidates = []
        for rel_path in candidates:
            full_path = self.templates_root / rel_path
            if full_path.exists():
                existing_candidates.append(rel_path)

        if not existing_candidates:
            return None, {
                "type": "no_template_found",
                "detail": f"未找到 {template_name}，搜索路径: {candidates}"
            }

        last_error = None
        for rel_path in existing_candidates:
            try:
                template = self.env.get_template(rel_path)
                return template.render(**context), None
            except Exception as e:
                last_error = str(e)
                continue

        return None, {"type": "render_failed", "detail": last_error or "未知错误"}


# ============================================================
# 场景 API（提供各业务场景的渲染入口）
# ============================================================

class SceneAPI:
    def __init__(self, renderer: TemplateRenderer):
        self.renderer = renderer

    # ===== Phase 1: 开局配置 =====
    def bootstrap(self, host: Host) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """开局配置（bootstrap.j2），通过 Console 或 SSH 下发"""
        resolver = H3CBootstrapPathResolver(get_patch_extractor("h3c"))
        return self.renderer.render(host, "bootstrap.j2", path_resolver=resolver)

    # ===== Phase 2: SSH CLI 场景 =====
    def ssh_bootstrap(self, host: Host) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """通过 SSH 下发 NETCONF 初始化命令（netconf_cmd.j2）"""
        resolver = H3CBootstrapPathResolver(get_patch_extractor("h3c"))
        return self.renderer.render(host, "netconf_cmd.j2", path_resolver=resolver)

    def ssh_acl_cmd(self, host: Host) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """通过 SSH 下发 ACL 命令行配置（acl_cmd.j2）"""
        resolver = H3CBootstrapPathResolver(get_patch_extractor("h3c"))
        return self.renderer.render(host, "acl_cmd.j2", path_resolver=resolver)

    def ssh_qos_cmd(self, host: Host) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """通过 SSH 下发 QoS 命令行配置（qos_cmd.j2）"""
        resolver = H3CBootstrapPathResolver(get_patch_extractor("h3c"))
        return self.renderer.render(host, "qos_cmd.j2", path_resolver=resolver)

    # ===== Phase 3: NETCONF XML 场景 =====
    def netconf_acl_xml(self, host: Host) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """通过 NETCONF 下发 ACL XML 配置（acl_xml.j2）"""
        resolver = H3CBootstrapPathResolver(get_patch_extractor("h3c"))
        return self.renderer.render(host, "acl_xml.j2", path_resolver=resolver)

    def netconf_qos_xml(self, host: Host) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """通过 NETCONF 下发 QoS XML 配置（qos_xml.j2）"""
        resolver = H3CBootstrapPathResolver(get_patch_extractor("h3c"))
        return self.renderer.render(host, "qos_xml.j2", path_resolver=resolver)

    # ===== 通用自定义场景 =====
    def custom(
        self,
        host: Host,
        template_name: str,
        path_resolver: Optional[PathResolver] = None,
        context_override: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """自定义场景，灵活指定模板和解析器"""
        return self.renderer.render(host, template_name, context_override, path_resolver)


# ============================================================
# 预览脚本入口
# ============================================================

def render_bootstrap_config(host) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    渲染单台设备的 bootstrap 配置
    返回: (config_string, error_dict) 成功时 error 为 None
    """
    context = build_render_context(host)

    project_root = Path.cwd()
    templates_root = project_root / "templates"

    if not templates_root.exists():
        return None, {
            "type": "templates_root_missing",
            "detail": f"模板目录不存在: {templates_root}"
        }

    group_names = [g.name for g in host.groups]
    platform = host.platform or (group_names[0] if group_names else "")
    extractor = get_patch_extractor(platform)
    patch = extractor.extract(group_names, host)

    # ========== 调试输出：Group 和补丁提取结果 ==========
    print(f"\n🔍 设备: {host.name}")
    print(f"   Group 列表: {group_names}")
    print(f"   提取到的补丁路径段: {patch}")

    candidates: List[str] = []

    # 1. 补丁级（如 SR88_Comware_V7/R7171/H02/）
    if patch:
        rel = f"os_versions/h3c/SR88_Comware_V7/{patch}/netconf_cmd.j2"
        candidates.append(rel)

    # 2. OS 版本级
    rel = "os_versions/h3c/SR88_Comware_V7/netconf_cmd.j2"
    if (templates_root / rel).exists() and rel not in candidates:
        candidates.append(rel)

    # 3. 产线级
    rel = "product_lines/h3c/SR88/netconf_cmd.j2"
    if (templates_root / rel).exists() and rel not in candidates:
        candidates.append(rel)

    # 4. 厂商级
    rel = "vendors/h3c/netconf_cmd.j2"
    if (templates_root / rel).exists() and rel not in candidates:
        candidates.append(rel)

    # 5. 全局兜底
    rel = "_base/netconf_cmd.j2"
    if (templates_root / rel).exists() and rel not in candidates:
        candidates.append(rel)

    # ========== 调试输出：候选路径列表 ==========
    print(f"\n📁 候选模板路径（按优先级）:")
    for idx, rel in enumerate(candidates, 1):
        full_path = templates_root / rel
        exists = "✅ 存在" if full_path.exists() else "❌ 不存在"
        print(f"   {idx}. {rel} -> {exists}")

    if not candidates:
        return None, {
            "type": "no_template_found",
            "detail": "未找到任何 netconf_cmd.j2 模板文件"
        }

    env = Environment(
        loader=FileSystemLoader(str(templates_root)),
        autoescape=select_autoescape(['xml', 'j2']),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    last_error: Optional[str] = None
    for rel_path in candidates:
        full_path = templates_root / rel_path
        if not full_path.exists():
            continue
        try:
            template = env.get_template(rel_path)
            rendered = template.render(**context)
            print(f"\n✅ 成功加载模板: {rel_path}")
            return rendered, None
        except Exception as e:
            last_error = str(e)
            print(f"   ❌ 加载失败: {rel_path} -> {e}")
            continue

    return None, {
        "type": "render_failed",
        "detail": last_error or "未知错误"
    }


def build_render_context(host) -> Dict[str, Any]:
    """
    构建渲染上下文，直接返回字典（不做运行时校验）
    数据类型由 Nornir Inventory 保证，已在 groups.yaml 构建期固化
    """
    return {
        "sysname": host.name,
        "mgmt_ip": host.hostname,
        "mgmt_interface": host.data.get("mgmt_interface", "Vlan-interface 100"),
        "mgmt_mask": host.data.get("mgmt_mask", "255.255.255.0"),
        "gateway": host.data.get("gateway"),
        "username": host.username,
        "password": host.password,
        "enable_secret": host.data.get("enable_secret"),
    }


def main() -> None:
    # 切换到项目根目录
    script_dir = Path(__file__).parent.absolute()
    project_root = script_dir.parent
    os.chdir(str(project_root))
    print(f"📍 工作目录已切换到: {project_root}")

    config_path = project_root / "config.yaml"
    if not config_path.exists():
        print(f"❌ 找不到配置文件: {config_path}")
        sys.exit(1)

    try:
        nr = InitNornir(config_file=str(config_path))
    except Exception as e:
        print(f"❌ Nornir 初始化失败: {e}")
        sys.exit(1)

    # 解析命令行参数
    args = sys.argv[1:]
    scene_name = "bootstrap"  # ✅ 恢复为默认场景：bootstrap.j2
    device_name = None

    if args:
        if args[0].startswith("--scene="):
            scene_name = args[0].split("=")[1]
            args.pop(0)
        elif args[0].startswith("--scene"):
            scene_name = args[1] if len(args) > 1 else "bootstrap"
            args = args[2:]

        if args:
            device_name = args[0]

    # 场景注册表
    scene_methods = {
        "bootstrap": "bootstrap.j2",
        "ssh_bootstrap": "netconf_cmd.j2",
        "ssh_acl": "acl_cmd.j2",
        "ssh_qos": "qos_cmd.j2",
        "netconf_acl": "acl_xml.j2",
        "netconf_qos": "qos_xml.j2",
    }

    if scene_name not in scene_methods:
        print(f"❌ 未知场景: {scene_name}")
        print(f"   可选场景: {list(scene_methods.keys())}")
        sys.exit(1)

    template_name = scene_methods[scene_name]
    print(f"📝 使用场景: {scene_name} -> 模板: {template_name}")

    results: List[Dict[str, Any]] = []

    if device_name:
        host = nr.inventory.hosts.get(device_name)
        if not host:
            print(f"❌ 设备 '{device_name}' 不存在")
            sys.exit(1)
        # 直接调用渲染函数（不是通过 SceneAPI）
        config, error = render_bootstrap_config(host)
        if config is not None:
            print(f"\n=== 设备: {device_name} (场景: {scene_name}) ===")
            print(config)
        else:
            print(f"\n❌ {device_name} 渲染失败: {error}")
        return

    for host_name, host in nr.inventory.hosts.items():
        print(f"\n=== 设备: {host_name} (场景: {scene_name}) ===")
        config, error = render_bootstrap_config(host)
        if config is not None:
            results.append({"name": host_name, "success": True})
            print(config)
        else:
            results.append({"name": host_name, "success": False, "error": error})
            print(f"❌ 渲染失败: {error}")

    success_count = sum(1 for r in results if r["success"])
    failure_count = len(results) - success_count
    print(f"\n📊 渲染汇总 (场景: {scene_name}): 成功 {success_count} 台，失败 {failure_count} 台")
    if failure_count > 0:
        print("失败设备列表:")
        for r in results:
            if not r["success"]:
                print(f"   - {r['name']}: {r.get('error', {}).get('detail', '未知错误')}")


if __name__ == "__main__":
    main()