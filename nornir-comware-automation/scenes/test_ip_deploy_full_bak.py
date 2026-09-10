#!/usr/bin/env python3
"""
完整 IP 下发与回退测试（H3C 适配版）
- 下发命令：test-cmd / test-netconf（只下发，不回退）
- 回退命令：rollback-cmd / rollback-netconf（基于快照文件执行全量回退）
- H3C 适配：接口用 CLI 创建/删除，IP 用 NETCONF 配置/删除
- 支持 Loopback 和物理接口
- ifindex 完全通过 NETCONF 获取（无 CLI 兜底）
- 预览模式（不下发）
"""

import sys
import os
import time
import re
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

# 添加项目根目录到 sys.path
_project_root = Path(__file__).parent.parent.absolute()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from netmiko import ConnectHandler
from nornir import InitNornir
from nornir.core.inventory import Host

from scripts.build_inventory import merge_group_files
from scripts.preview_bootstrap import TemplateRenderer, SceneAPI, PathResolver
from atoms.h3c.cmd.interface_loopback import CmdLoopbackAtom
from atoms.h3c.cmd.interface_ip_address import CmdIPAddressAtom
from atoms.h3c.netconf.interface_loopback import NetconfLoopbackAtom
from atoms.h3c.netconf.interface_ip_address import NetconfIPAddressAtom
from atoms.utils import get_netconf_session, get_ifindex_map, get_netconf_ip

# ============================================================
# IP 规划
# ============================================================

DEVICE_CONFIG = {
    "H3C-SR88-01": {
        "loopback": {"number": 1, "ip": "11.11.11.11", "mask": "255.255.255.255"},
        "interfaces": [
            {"ifname": "GigabitEthernet0/0/1", "ip": "10.0.0.0", "mask": "255.255.255.254"},
            {"ifname": "GigabitEthernet0/0/2", "ip": "10.0.0.2", "mask": "255.255.255.254"},
        ]
    },
    "H3C-SR88-02": {
        "loopback": {"number": 1, "ip": "22.22.22.22", "mask": "255.255.255.255"},
        "interfaces": [
            {"ifname": "GigabitEthernet0/0/1", "ip": "10.0.0.1", "mask": "255.255.255.254"},
            {"ifname": "GigabitEthernet0/0/2", "ip": "10.0.0.4", "mask": "255.255.255.254"},
        ]
    },
    "H3C-SR88-03": {
        "loopback": {"number": 1, "ip": "33.33.33.33", "mask": "255.255.255.255"},
        "interfaces": [
            {"ifname": "GigabitEthernet0/0/1", "ip": "10.0.0.3", "mask": "255.255.255.254"},
            {"ifname": "GigabitEthernet0/0/2", "ip": "10.0.0.5", "mask": "255.255.255.254"},
        ]
    },
}


# ============================================================
# 工具函数
# ============================================================

def ensure_groups_up_to_date():
    print("📦 正在构建 groups.yaml...")
    try:
        merge_group_files()
        print("✅ groups.yaml 构建完成")
    except Exception as e:
        print(f"❌ groups.yaml 构建失败: {e}")
        sys.exit(1)


def print_separator(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def get_cli_connection(host):
    return ConnectHandler(
        device_type='hp_comware',
        host=host.hostname,
        port=22,
        username=host.username,
        password=host.password,
        timeout=60,
    )


def send_cmd(host, config_lines):
    if not config_lines:
        return
    net_connect = get_cli_connection(host)
    try:
        net_connect.send_command("system-view", expect_string=r"\[.*?\]")
        for line in config_lines:
            if line.strip():
                net_connect.send_command(line, expect_string=r"\[.*?\]")
        net_connect.send_command("quit", expect_string=r"<.*?>")
    finally:
        net_connect.disconnect()


def get_cli_interface(host, ifname):
    """通过 CLI 精确判断接口是否存在（仅用于打印）"""
    net_connect = get_cli_connection(host)
    try:
        output = net_connect.send_command(
            f"display interface {ifname}",
            expect_string=r".*?",
            delay_factor=2
        )
        if "does not exist" in output.lower() or "is not present" in output.lower():
            return None
        if ifname.lower() in output.lower():
            return output
        return None
    except Exception:
        return None
    finally:
        net_connect.disconnect()


def cli_create_loopback(host, logical_number, description=None):
    """CLI 创建 Loopback 接口"""
    ifname = f"LoopBack{logical_number}"
    print(f"     🔧 CLI 创建接口 {ifname}")
    lines = [f"interface {ifname}", "undo shutdown"]
    if description:
        lines.append(f"description {description}")
    lines.append("quit")
    send_cmd(host, lines)
    time.sleep(1)


def cli_delete_loopback(host, logical_number):
    """CLI 删除 Loopback 接口"""
    ifname = f"LoopBack{logical_number}"
    print(f"     🔧 CLI 删除接口 {ifname}")
    send_cmd(host, [f"undo interface {ifname}"])


def cli_create_physical(host, ifname, description=None):
    """CLI 创建物理接口（实际是确保接口 up）"""
    print(f"     🔧 CLI 准备接口 {ifname}")
    lines = [f"interface {ifname}", "undo shutdown"]
    if description:
        lines.append(f"description {description}")
    lines.append("quit")
    send_cmd(host, lines)


def cli_delete_physical(host, ifname):
    """CLI 删除物理接口（用默认配置重置）"""
    print(f"     🔧 CLI 重置接口 {ifname}")
    send_cmd(host, [
        f"interface {ifname}",
        "shutdown",
        "quit"
    ])


# ============================================================
# 快照读写工具
# ============================================================

def save_snapshot(device_name, mode, snapshots):
    """保存快照到 logs 目录"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    filename = log_dir / f"snapshots_{device_name}_{mode}.json"
    with open(filename, "w") as f:
        json.dump(snapshots, f, indent=2)
    print(f"📁 快照已保存: {filename}")


def load_snapshot(device_name, mode):
    """加载快照文件，若不存在返回 None"""
    filename = Path("logs") / f"snapshots_{device_name}_{mode}.json"
    if not filename.exists():
        return None
    with open(filename) as f:
        return json.load(f)


# ============================================================
# 直接路径解析器
# ============================================================

class DirectPathResolver(PathResolver):
    def resolve(self, host: Host, template_name: str) -> List[str]:
        return [template_name]


def render_direct(renderer, host, template_name, context):
    return renderer.render(host, template_name, context_override=context, path_resolver=DirectPathResolver())


# ============================================================
# 预览功能
# ============================================================

def preview_device(host, renderer, config, ifindex_map=None):
    print_separator(f"预览: {host.name}")

    # ---- CMD 预览 ----
    print("\n[CMD 配置预览]")
    print("-" * 50)

    lb = config["loopback"]
    logical_number = lb["number"]
    ifname = f"LoopBack{logical_number}"
    print(f"\n# {ifname}: {lb['ip']}/{lb['mask']}")

    cmd_text, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/cmd/interface_create_cmd.j2",
                                  {"ifname": ifname, "description": f"{host.name} Loopback"})
    if err:
        print(f"渲染失败: {err}")
    else:
        print(cmd_text)

    cmd_text, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/cmd/ip_address_cmd.j2",
                                  {"ifname": ifname, "ipv4_address": lb['ip'], "ipv4_mask": lb['mask']})
    if err:
        print(f"渲染失败: {err}")
    else:
        print(cmd_text)

    for iface in config["interfaces"]:
        ifname = iface["ifname"]
        print(f"\n# {ifname}: {iface['ip']}/{iface['mask']}")
        cmd_text, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/cmd/ip_address_cmd.j2",
                                      {"ifname": ifname, "ipv4_address": iface['ip'], "ipv4_mask": iface['mask']})
        if err:
            print(f"渲染失败: {err}")
        else:
            print(cmd_text)

    # ---- NETCONF 预览 ----
    print("\n[NETCONF 配置预览]")
    print("-" * 50)

    if ifindex_map is None:
        try:
            with get_netconf_session(host) as session:
                ifindex_map = get_ifindex_map(host, session)
            print("✅ 已获取 ifindex 映射")
        except Exception as e:
            print(f"⚠️ 无法获取 ifindex 映射: {e}")
            ifindex_map = {}

    # Loopback（使用逻辑编号）
    print(f"\n# {ifname}: {lb['ip']}/{lb['mask']}")

    xml_frag, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/netconf/_fragments/interface_atom.j2",
                                  {"ifindex": logical_number, "iftype": "Loopback",
                                   "description": f"{host.name} Loopback", "operation": "merge"})
    if err:
        print(f"渲染失败: {err}")
    else:
        print(xml_frag)

    xml_frag, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                  {"ifindex": logical_number, "ipv4_address": lb['ip'], "ipv4_mask": lb['mask'],
                                   "operation": "merge"})
    if err:
        print(f"渲染失败: {err}")
    else:
        print(xml_frag)

    # 物理接口
    for iface in config["interfaces"]:
        ifname = iface["ifname"]
        actual_ifindex = ifindex_map.get(ifname)
        if actual_ifindex is None:
            print(f"\n# {ifname}: 未找到 ifindex，使用占位符 '?'")
            actual_ifindex = "?"
        else:
            print(f"\n# {ifname} (ifindex {actual_ifindex}): {iface['ip']}/{iface['mask']}")
        xml_frag, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                      {"ifindex": actual_ifindex, "ipv4_address": iface['ip'],
                                       "ipv4_mask": iface['mask'], "operation": "merge"})
        if err:
            print(f"渲染失败: {err}")
        else:
            print(xml_frag)

    print("\n预览结束（未实际下发）")


# ============================================================
# CMD 下发测试（不下发回退）
# ============================================================

def test_cmd_deploy(host, renderer, config):
    print_separator(f"CMD 下发: {host.name}")
    snapshots = {"loopback": None, "interfaces": []}
    success = True
    details = []

    # ---- 1. Loopback ----
    lb = config["loopback"]
    logical_number = lb["number"]
    ip = lb["ip"]
    mask = lb["mask"]
    ifname = f"LoopBack{logical_number}"
    print(f"\n  📌 Loopback: {ifname} {ip}/{mask}")

    # 创建接口
    cmd_text, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/cmd/interface_create_cmd.j2",
                                  {"ifname": ifname, "description": f"{host.name} Loopback"})
    if err:
        print(f"     ❌ 渲染失败: {err}")
        success = False
    else:
        lines = cmd_text.splitlines()
        send_cmd(host, lines)
        print(f"     ✅ 接口创建成功")

    # 配置 IP
    cmd_text, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/cmd/ip_address_cmd.j2",
                                  {"ifname": ifname, "ipv4_address": ip, "ipv4_mask": mask})
    if err:
        print(f"     ❌ 渲染失败: {err}")
        success = False
    else:
        lines = cmd_text.splitlines()
        send_cmd(host, lines)
        print(f"     ✅ IP 配置成功")
        snapshots["loopback"] = {"ifname": ifname, "ipv4_address": ip, "ipv4_mask": mask}

    # ---- 2. 物理接口 ----
    print(f"\n  📌 物理接口:")
    for iface in config["interfaces"]:
        ifname = iface["ifname"]
        ip = iface["ip"]
        mask = iface["mask"]
        print(f"\n     {ifname}: {ip}/{mask}")

        # 确保接口 up
        send_cmd(host, [f"interface {ifname}", "undo shutdown", "quit"])

        cmd_text, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/cmd/ip_address_cmd.j2",
                                      {"ifname": ifname, "ipv4_address": ip, "ipv4_mask": mask})
        if err:
            print(f"        ❌ 渲染失败: {err}")
            success = False
        else:
            lines = cmd_text.splitlines()
            send_cmd(host, lines)
            print(f"        ✅ IP 配置成功")
            snapshots["interfaces"].append({"ifname": ifname, "ipv4_address": ip, "ipv4_mask": mask})

    # 保存快照
    save_snapshot(host.name, "cmd", snapshots)
    return {"status": "success" if success else "failed", "snapshots": snapshots, "detail": "; ".join(details)}


# ============================================================
# CMD 回退测试（基于快照）
# ============================================================

def test_cmd_rollback(host, renderer, snapshots):
    print_separator(f"CMD 回退: {host.name}")
    if not snapshots or (not snapshots.get("loopback") and not any(snapshots.get("interfaces", []))):
        print("  ⏭️ 没有可回退的配置")
        return {"status": "skipped"}

    success = True
    details = []

    # ---- 1. 回退物理接口（逆序） ----
    for snapshot in reversed(snapshots.get("interfaces", [])):
        ifname = snapshot["ifname"]
        print(f"\n  🔄 回退 {ifname} 的 IP")

        cmd_text, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2",
                                      {"ifname": ifname})
        if err:
            print(f"     ❌ 渲染失败: {err}")
            success = False
        else:
            lines = cmd_text.splitlines()
            send_cmd(host, lines)
            print(f"     ✅ IP 删除成功")

    # ---- 2. 回退 Loopback ----
    lb_snap = snapshots.get("loopback")
    if lb_snap:
        ifname = lb_snap["ifname"]
        print(f"\n  🔄 回退 {ifname}")
        # 删除 IP
        cmd_text, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2",
                                      {"ifname": ifname})
        if err:
            print(f"     ❌ 渲染失败: {err}")
            success = False
        else:
            lines = cmd_text.splitlines()
            send_cmd(host, lines)
            print(f"     ✅ IP 删除成功")

        # 删除接口
        cmd_text, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/cmd/interface_create_rollback_cmd.j2",
                                      {"ifname": ifname})
        if err:
            print(f"     ❌ 渲染失败: {err}")
            success = False
        else:
            lines = cmd_text.splitlines()
            send_cmd(host, lines)
            print(f"     ✅ 接口删除成功")

    return {"status": "success" if success else "failed", "detail": "; ".join(details)}


# ============================================================
# NETCONF 下发测试（混合模式：CLI 建接口，NETCONF 配 IP）
# ============================================================

def test_netconf_deploy(host, renderer, config, ifindex_map):
    print_separator(f"NETCONF 下发: {host.name}")
    snapshots = {"loopback": None, "interfaces": []}
    success = True
    details = []

    try:
        with get_netconf_session(host) as session:
            # ---- 1. Loopback ----
            lb = config["loopback"]
            logical_number = lb["number"]
            ip = lb["ip"]
            mask = lb["mask"]
            loopback_name = f"LoopBack{logical_number}"
            print(f"\n  📌 Loopback: {loopback_name} {ip}/{mask}")

            # 先尝试从当前 ifindex_map 中获取（可能已存在）
            real_ifindex = ifindex_map.get(loopback_name)

            if real_ifindex is not None:
                print(f"     ℹ️ {loopback_name} 已存在（ifindex {real_ifindex}）")
            else:
                # 不存在，CLI 创建
                cli_create_loopback(host, logical_number, f"{host.name} Loopback")
                print(f"     ✅ CLI 创建 {loopback_name} 成功")
                time.sleep(2)  # 等待设备同步

                # 【唯一方式】通过 NETCONF 重新获取全量 ifindex 映射
                try:
                    new_map = get_ifindex_map(host, session)
                    ifindex_map.clear()
                    ifindex_map.update(new_map)
                    print(f"     ✅ 已通过 NETCONF 更新 ifindex 映射，共 {len(ifindex_map)} 个接口")
                    real_ifindex = ifindex_map.get(loopback_name)
                    if real_ifindex is None:
                        # NETCONF 返回的映射中没有该接口，视为失败
                        print(f"     ❌ 更新后仍未找到 {loopback_name} 的 ifindex")
                        success = False
                        real_ifindex = None
                    else:
                        print(f"     ✅ 从 NETCONF 获取到 {loopback_name} ifindex = {real_ifindex}")
                except Exception as e:
                    print(f"     ❌ NETCONF 更新映射失败: {e}")
                    success = False
                    real_ifindex = None

            # 如果成功获取到 real_ifindex，则配置 IP
            if real_ifindex is not None:
                current_ip = get_netconf_ip(host, real_ifindex, session)
                if current_ip == ip:
                    print(f"     ⏭️ IP {ip} 已存在，跳过配置")
                else:
                    xml_frag, err = render_direct(renderer, host,
                                                  "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                                  {"ifindex": real_ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                                   "operation": "merge"})
                    if err:
                        print(f"     ❌ IP 渲染失败: {err}")
                        success = False
                    else:
                        config_xml = f"<config>{xml_frag}</config>"
                        print("\n=== 发送的 IP 配置 XML ===")
                        print(config_xml)
                        print("==========================\n")
                        try:
                            session.edit_config(config=config_xml, target="candidate")
                            session.commit()
                            print(f"     ✅ IP 配置成功")
                            snapshots["loopback"] = {"ifindex": real_ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                                     "logical_number": logical_number}
                        except Exception as e:
                            print(f"     ❌ 发送失败: {e}")
                            success = False
            else:
                print(f"     ⚠️ 跳过 IP 配置（未获取到真实 ifindex）")

            # ---- 2. 物理接口 ----
            print(f"\n  📌 物理接口:")
            for iface in config["interfaces"]:
                ifname = iface["ifname"]
                ip = iface["ip"]
                mask = iface["mask"]
                actual_ifindex = ifindex_map.get(ifname)
                if actual_ifindex is None:
                    print(f"\n     ⚠️ 接口 {ifname} 不在 ifindex 映射中，跳过")
                    continue

                print(f"\n     {ifname} (ifindex {actual_ifindex}): {ip}/{mask}")

                # 确保接口 up（CLI）
                cli_create_physical(host, ifname)

                # 检查 IP 是否已存在
                current_ip = get_netconf_ip(host, actual_ifindex, session)
                if current_ip == ip:
                    print(f"        ⏭️ IP {ip} 已存在，跳过配置")
                    continue

                xml_frag, err = render_direct(renderer, host,
                                              "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                              {"ifindex": actual_ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                               "operation": "merge"})
                if err:
                    print(f"        ❌ 渲染失败: {err}")
                    success = False
                else:
                    config_xml = f"<config>{xml_frag}</config>"
                    print("\n=== 发送的物理接口 IP 配置 XML ===")
                    print(config_xml)
                    print("=====================================\n")
                    try:
                        session.edit_config(config=config_xml, target="candidate")
                        session.commit()
                        print(f"        ✅ IP 配置成功")
                        snapshots["interfaces"].append(
                            {"ifindex": actual_ifindex, "ipv4_address": ip, "ipv4_mask": mask})
                    except Exception as e:
                        print(f"        ❌ 发送失败: {e}")
                        success = False

    except Exception as e:
        print(f"  ❌ NETCONF 会话异常: {e}")
        return {"status": "failed", "snapshots": snapshots, "detail": str(e)}

    # 保存快照
    save_snapshot(host.name, "netconf", snapshots)
    return {"status": "success" if success else "failed", "snapshots": snapshots, "detail": "; ".join(details)}


# ============================================================
# NETCONF 回退测试（基于快照）
# ============================================================

def test_netconf_rollback(host, renderer, snapshots, ifindex_map):
    print_separator(f"NETCONF 回退: {host.name}")
    if not snapshots or (not snapshots.get("loopback") and not any(snapshots.get("interfaces", []))):
        print("  ⏭️ 没有可回退的配置")
        return {"status": "skipped"}

    success = True
    try:
        with get_netconf_session(host) as session:
            # ---- 1. 回退物理接口（逆序：先删 IP，再重置接口） ----
            for snapshot in reversed(snapshots.get("interfaces", [])):
                ifindex = snapshot["ifindex"]
                ip = snapshot["ipv4_address"]
                mask = snapshot["ipv4_mask"]
                print(f"\n  🔄 回退 ifindex {ifindex} 的 IP ({ip})")

                # NETCONF 删除 IP
                xml_frag, err = render_direct(renderer, host,
                                              "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                              {"ifindex": ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                               "operation": "delete"})
                if err:
                    print(f"     ❌ 渲染失败: {err}")
                    success = False
                else:
                    config_xml = f"<config>{xml_frag}</config>"
                    try:
                        session.edit_config(config=config_xml, target="candidate")
                        session.commit()
                        print(f"     ✅ IP 删除成功（NETCONF）")
                    except Exception as e:
                        print(f"     ❌ 删除失败: {e}")
                        success = False
                        continue

                # CLI 重置接口（H3C 不支持 NETCONF 删除接口）
                # 尝试从 ifindex_map 反向查找接口名
                ifname = None
                for name, idx in ifindex_map.items():
                    if idx == ifindex:
                        ifname = name
                        break
                if ifname:
                    cli_delete_physical(host, ifname)
                    print(f"     ✅ 接口 {ifname} 已重置（CLI）")
                else:
                    print(f"     ⚠️ 无法确定接口名，跳过 CLI 重置")

            # ---- 2. 回退 Loopback ----
            lb_snap = snapshots.get("loopback")
            if lb_snap:
                real_ifindex = lb_snap["ifindex"]
                logical_number = lb_snap.get("logical_number", real_ifindex)  # 向后兼容旧快照
                ip = lb_snap["ipv4_address"]
                mask = lb_snap["ipv4_mask"]
                print(f"\n  🔄 回退 Loopback (逻辑编号 {logical_number}) 的 IP ({ip})")

                # NETCONF 删除 IP
                xml_frag, err = render_direct(renderer, host,
                                              "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                              {"ifindex": real_ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                               "operation": "delete"})
                if err:
                    print(f"     ❌ 删除 IP 失败: {err}")
                    success = False
                else:
                    config_xml = f"<config>{xml_frag}</config>"
                    try:
                        session.edit_config(config=config_xml, target="candidate")
                        session.commit()
                        print(f"     ✅ IP 删除成功（NETCONF）")
                    except Exception as e:
                        print(f"     ❌ 删除 IP 失败: {e}")
                        success = False

                # CLI 删除 Loopback 接口
                cli_delete_loopback(host, logical_number)
                print(f"     ✅ Loopback{logical_number} 已删除（CLI）")

    except Exception as e:
        print(f"  ❌ 回退异常: {e}")
        return {"status": "failed", "detail": str(e)}

    return {"status": "success" if success else "failed"}


# ============================================================
# 主入口
# ============================================================

def main():
    args = sys.argv[1:]
    if not args:
        print("用法:")
        print("  python scenes/test_ip_deploy_full.py preview [device]            # 预览配置（不下发）")
        print("  python scenes/test_ip_deploy_full.py test-cmd [device]           # CMD 下发（不回退）")
        print("  python scenes/test_ip_deploy_full.py test-netconf [device]       # NETCONF 下发（不回退）")
        print("  python scenes/test_ip_deploy_full.py rollback-cmd [device]       # CMD 全量回退（基于快照）")
        print("  python scenes/test_ip_deploy_full.py rollback-netconf [device]   # NETCONF 全量回退（基于快照）")
        sys.exit(1)

    command = args[0].lower()
    device_names = args[1:] if len(args) > 1 else None

    ensure_groups_up_to_date()
    nr = InitNornir(config_file="config.yaml")
    project_root = Path(__file__).parent.parent.absolute()
    renderer = TemplateRenderer(project_root / "templates")

    if device_names:
        target_devices = [d for d in device_names if d in DEVICE_CONFIG]
    else:
        target_devices = list(DEVICE_CONFIG.keys())

    if not target_devices:
        print("❌ 没有有效的目标设备")
        sys.exit(1)

    # 获取 ifindex 映射（NETCONF 测试需要）
    ifindex_maps = {}
    print("\n📡 正在获取设备 ifindex 映射...")
    for device_name in target_devices:
        host = nr.inventory.hosts.get(device_name)
        if not host:
            continue
        try:
            with get_netconf_session(host) as session:
                ifindex_map = get_ifindex_map(host, session)
                ifindex_maps[device_name] = ifindex_map
                print(f"   {device_name}: ✅ {len(ifindex_map)} 个接口")
        except Exception as e:
            print(f"   {device_name}: ❌ 获取失败: {e}")
            ifindex_maps[device_name] = {}

    all_results = []

    # 预览
    if command == "preview":
        print("=" * 70)
        print("  IP 配置预览模式（不下发）")
        print("=" * 70)
        for device_name in target_devices:
            host = nr.inventory.hosts.get(device_name)
            if not host:
                continue
            config = DEVICE_CONFIG[device_name]
            ifindex_map = ifindex_maps.get(device_name)
            preview_device(host, renderer, config, ifindex_map)
        return

    # 执行测试
    print("=" * 70)
    print(f"  {command.upper()} 测试模式")
    print("=" * 70)

    for device_name in target_devices:
        host = nr.inventory.hosts.get(device_name)
        if not host:
            continue
        config = DEVICE_CONFIG[device_name]
        ifindex_map = ifindex_maps.get(device_name, {})

        print(f"\n{'#' * 70}")
        print(f"# 设备: {device_name} ({host.hostname})")
        print(f"# Loopback: {config['loopback']['ip']}")
        print(f"# 物理接口: {len(config['interfaces'])} 个")
        print(f"{'#' * 70}")

        if command == "test-cmd":
            result = test_cmd_deploy(host, renderer, config)
            all_results.append((f"{device_name} CMD 下发", result["status"]))

        elif command == "test-netconf":
            result = test_netconf_deploy(host, renderer, config, ifindex_map)
            all_results.append((f"{device_name} NETCONF 下发", result["status"]))

        elif command == "rollback-cmd":
            snapshots = load_snapshot(device_name, "cmd")
            if snapshots is None:
                print(f"  ⏭️ 未找到 CMD 快照，跳过回退")
                all_results.append((f"{device_name} CMD 回退", "skipped"))
            else:
                result = test_cmd_rollback(host, renderer, snapshots)
                all_results.append((f"{device_name} CMD 回退", result["status"]))

        elif command == "rollback-netconf":
            snapshots = load_snapshot(device_name, "netconf")
            if snapshots is None:
                print(f"  ⏭️ 未找到 NETCONF 快照，跳过回退")
                all_results.append((f"{device_name} NETCONF 回退", "skipped"))
            else:
                result = test_netconf_rollback(host, renderer, snapshots, ifindex_map)
                all_results.append((f"{device_name} NETCONF 回退", result["status"]))

        else:
            print(f"❌ 未知命令: {command}")
            sys.exit(1)

    # 汇总
    print_separator("测试结果汇总")
    for name, status in all_results:
        icon = "✅" if status == "success" else ("⏭️" if status == "skipped" else "❌")
        print(f"  {icon} {name}: {status.upper()}")
    passed = sum(1 for _, s in all_results if s == "success")
    total = len(all_results)
    print(f"\n  总计: {passed}/{total} 通过")
    print("=" * 70)


if __name__ == "__main__":
    main()