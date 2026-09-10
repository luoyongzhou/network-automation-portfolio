#!/usr/bin/env python3
"""
完整 IP 下发与回退测试（H3C 适配版）- 并发聚合版
- 下发命令：test-cmd / test-netconf（只下发，不回退）
- 回退命令：rollback-cmd / rollback-netconf（基于快照文件执行全量回退）
- H3C 适配：接口用 CLI 创建/删除，IP 用 NETCONF 配置/删除
- 支持 Loopback 和物理接口
- ifindex 完全通过 NETCONF 获取（无 CLI 兜底）
- 预览模式（不下发）
- 多设备并发执行，日志聚合输出
- 回退时物理接口仅删除 IP，不改变 admin 状态（幂等）
- CMD 与 NETCONF 回退行为一致
"""

import sys
import time
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
from nornir.core.task import Task, Result

from scripts.build_inventory import merge_group_files
from scripts.preview_bootstrap import TemplateRenderer, SceneAPI, PathResolver
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


def cli_create_loopback(host, logical_number, description=None):
    """CLI 创建 Loopback 接口"""
    ifname = f"LoopBack{logical_number}"
    lines = [f"interface {ifname}", "undo shutdown"]
    if description:
        lines.append(f"description {description}")
    lines.append("quit")
    send_cmd(host, lines)
    time.sleep(1)


def cli_delete_loopback(host, logical_number):
    """CLI 删除 Loopback 接口"""
    ifname = f"LoopBack{logical_number}"
    send_cmd(host, [f"undo interface {ifname}"])


def cli_create_physical(host, ifname, description=None):
    """CLI 准备物理接口（undo shutdown）"""
    lines = [f"interface {ifname}", "undo shutdown"]
    if description:
        lines.append(f"description {description}")
    lines.append("quit")
    send_cmd(host, lines)


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


def load_snapshot(device_name, mode):
    """加载快照文件，若不存在返回 None"""
    filename = Path("logs") / f"snapshots_{device_name}_{mode}.json"
    if not filename.exists():
        return None
    with open(filename) as f:
        return json.load(f)


# ============================================================
# 路径解析器与渲染
# ============================================================

class DirectPathResolver(PathResolver):
    def resolve(self, host: Host, template_name: str) -> List[str]:
        return [template_name]


def render_direct(renderer, host, template_name, context):
    return renderer.render(host, template_name, context_override=context, path_resolver=DirectPathResolver())


# ============================================================
# 预览功能（串行）
# ============================================================

def preview_device(host, renderer, config, ifindex_map=None):
    """预览单台设备配置（不下发）"""
    logs = []
    logs.append(f"\n{'='*70}")
    logs.append(f"  预览: {host.name}")
    logs.append(f"{'='*70}")

    # CMD 预览
    logs.append("\n[CMD 配置预览]")
    logs.append("-" * 50)

    lb = config["loopback"]
    logical_number = lb["number"]
    ifname = f"LoopBack{logical_number}"
    logs.append(f"\n# {ifname}: {lb['ip']}/{lb['mask']}")

    cmd_text, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/cmd/interface_create_cmd.j2",
                                  {"ifname": ifname, "description": f"{host.name} Loopback"})
    if err:
        logs.append(f"渲染失败: {err}")
    else:
        logs.append(cmd_text)

    cmd_text, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/cmd/ip_address_cmd.j2",
                                  {"ifname": ifname, "ipv4_address": lb['ip'], "ipv4_mask": lb['mask']})
    if err:
        logs.append(f"渲染失败: {err}")
    else:
        logs.append(cmd_text)

    for iface in config["interfaces"]:
        ifname = iface["ifname"]
        logs.append(f"\n# {ifname}: {iface['ip']}/{iface['mask']}")
        cmd_text, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/cmd/ip_address_cmd.j2",
                                      {"ifname": ifname, "ipv4_address": iface['ip'], "ipv4_mask": iface['mask']})
        if err:
            logs.append(f"渲染失败: {err}")
        else:
            logs.append(cmd_text)

    # NETCONF 预览
    logs.append("\n[NETCONF 配置预览]")
    logs.append("-" * 50)

    if ifindex_map is None:
        try:
            with get_netconf_session(host) as session:
                ifindex_map = get_ifindex_map(host, session)
            logs.append("✅ 已获取 ifindex 映射")
        except Exception as e:
            logs.append(f"⚠️ 无法获取 ifindex 映射: {e}")
            ifindex_map = {}

    # Loopback
    logs.append(f"\n# {ifname}: {lb['ip']}/{lb['mask']}")
    xml_frag, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/netconf/_fragments/interface_atom.j2",
                                  {"ifindex": logical_number, "iftype": "Loopback",
                                   "description": f"{host.name} Loopback", "operation": "merge"})
    if err:
        logs.append(f"渲染失败: {err}")
    else:
        logs.append(xml_frag)

    xml_frag, err = render_direct(renderer, host,
                                  "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                  {"ifindex": logical_number, "ipv4_address": lb['ip'], "ipv4_mask": lb['mask'],
                                   "operation": "merge"})
    if err:
        logs.append(f"渲染失败: {err}")
    else:
        logs.append(xml_frag)

    # 物理接口
    for iface in config["interfaces"]:
        ifname = iface["ifname"]
        actual_ifindex = ifindex_map.get(ifname)
        if actual_ifindex is None:
            logs.append(f"\n# {ifname}: 未找到 ifindex，使用占位符 '?'")
            actual_ifindex = "?"
        else:
            logs.append(f"\n# {ifname} (ifindex {actual_ifindex}): {iface['ip']}/{iface['mask']}")
        xml_frag, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                      {"ifindex": actual_ifindex, "ipv4_address": iface['ip'],
                                       "ipv4_mask": iface['mask'], "operation": "merge"})
        if err:
            logs.append(f"渲染失败: {err}")
        else:
            logs.append(xml_frag)

    logs.append("\n预览结束（未实际下发）")
    return logs


# ============================================================
# 任务函数（用于 Nornir 并发）
# ============================================================

def task_deploy_cmd(task: Task) -> Result:
    """CMD 下发任务（聚合日志）"""
    host = task.host
    renderer = task.host.data['renderer']
    config = task.host.data['config']
    logs = []

    logs.append(f"\n{'='*70}")
    logs.append(f"  CMD 下发: {host.name}")
    logs.append(f"{'='*70}")

    snapshots = {"loopback": None, "interfaces": []}
    success = True

    try:
        # 1. Loopback
        lb = config["loopback"]
        logical_number = lb["number"]
        ip = lb["ip"]
        mask = lb["mask"]
        ifname = f"LoopBack{logical_number}"
        logs.append(f"\n  📌 Loopback: {ifname} {ip}/{mask}")

        # 创建接口
        cmd_text, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/cmd/interface_create_cmd.j2",
                                      {"ifname": ifname, "description": f"{host.name} Loopback"})
        if err:
            logs.append(f"     ❌ 渲染失败: {err}")
            success = False
        else:
            lines = cmd_text.splitlines()
            send_cmd(host, lines)
            logs.append(f"     ✅ 接口创建成功")

        # 配置 IP
        cmd_text, err = render_direct(renderer, host,
                                      "product_lines/h3c/SR88/cmd/ip_address_cmd.j2",
                                      {"ifname": ifname, "ipv4_address": ip, "ipv4_mask": mask})
        if err:
            logs.append(f"     ❌ 渲染失败: {err}")
            success = False
        else:
            lines = cmd_text.splitlines()
            send_cmd(host, lines)
            logs.append(f"     ✅ IP 配置成功")
            snapshots["loopback"] = {"ifname": ifname, "ipv4_address": ip, "ipv4_mask": mask}

        # 2. 物理接口
        logs.append(f"\n  📌 物理接口:")
        for iface in config["interfaces"]:
            ifname = iface["ifname"]
            ip = iface["ip"]
            mask = iface["mask"]
            logs.append(f"\n     {ifname}: {ip}/{mask}")

            # 确保接口 up
            send_cmd(host, [f"interface {ifname}", "undo shutdown", "quit"])

            cmd_text, err = render_direct(renderer, host,
                                          "product_lines/h3c/SR88/cmd/ip_address_cmd.j2",
                                          {"ifname": ifname, "ipv4_address": ip, "ipv4_mask": mask})
            if err:
                logs.append(f"        ❌ 渲染失败: {err}")
                success = False
            else:
                lines = cmd_text.splitlines()
                send_cmd(host, lines)
                logs.append(f"        ✅ IP 配置成功")
                snapshots["interfaces"].append({"ifname": ifname, "ipv4_address": ip, "ipv4_mask": mask})

        save_snapshot(host.name, "cmd", snapshots)
        logs.append(f"\n📁 快照已保存: logs/snapshots_{host.name}_cmd.json")

    except Exception as e:
        logs.append(f"  ❌ 任务执行异常: {e}")
        success = False

    status = "success" if success else "failed"
    logs.append(f"\n状态: {status.upper()}")
    return Result(host=host, result={"status": status, "logs": logs})


def task_rollback_cmd(task: Task) -> Result:
    """CMD 回退任务（基于快照）- 物理接口仅删除 IP，不改变 admin 状态"""
    host = task.host
    renderer = task.host.data['renderer']
    logs = []
    logs.append(f"\n{'='*70}")
    logs.append(f"  CMD 回退: {host.name}")
    logs.append(f"{'='*70}")

    snapshots = load_snapshot(host.name, "cmd")
    if snapshots is None:
        logs.append("  ⏭️ 未找到 CMD 快照，跳过回退")
        return Result(host=host, result={"status": "skipped", "logs": logs})

    if not snapshots.get("loopback") and not snapshots.get("interfaces"):
        logs.append("  ⏭️ 快照为空，无需回退")
        return Result(host=host, result={"status": "skipped", "logs": logs})

    success = True
    try:
        # 1. 回退物理接口（逆序）
        for snapshot in reversed(snapshots.get("interfaces", [])):
            ifname = snapshot["ifname"]
            logs.append(f"\n  🔄 回退 {ifname} 的 IP")
            cmd_text, err = render_direct(renderer, host,
                                          "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2",
                                          {"ifname": ifname})
            if err:
                logs.append(f"     ❌ 渲染失败: {err}")
                success = False
            else:
                lines = cmd_text.splitlines()
                send_cmd(host, lines)
                logs.append(f"     ✅ IP 删除成功")
                logs.append(f"     ℹ️ 接口 {ifname} 的 IP 已删除，接口状态保持不变")

        # 2. 回退 Loopback
        lb_snap = snapshots.get("loopback")
        if lb_snap:
            ifname = lb_snap["ifname"]
            logs.append(f"\n  🔄 回退 {ifname}")
            # 删除 IP
            cmd_text, err = render_direct(renderer, host,
                                          "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2",
                                          {"ifname": ifname})
            if err:
                logs.append(f"     ❌ 渲染失败: {err}")
                success = False
            else:
                lines = cmd_text.splitlines()
                send_cmd(host, lines)
                logs.append(f"     ✅ IP 删除成功")
            # 删除接口（因为它是脚本创建的）
            cmd_text, err = render_direct(renderer, host,
                                          "product_lines/h3c/SR88/cmd/interface_create_rollback_cmd.j2",
                                          {"ifname": ifname})
            if err:
                logs.append(f"     ❌ 渲染失败: {err}")
                success = False
            else:
                lines = cmd_text.splitlines()
                send_cmd(host, lines)
                logs.append(f"     ✅ 接口删除成功")

    except Exception as e:
        logs.append(f"  ❌ 回退异常: {e}")
        success = False

    status = "success" if success else "failed"
    logs.append(f"\n状态: {status.upper()}")
    return Result(host=host, result={"status": status, "logs": logs})


def task_deploy_netconf(task: Task) -> Result:
    """NETCONF 下发任务（聚合日志）"""
    host = task.host
    renderer = task.host.data['renderer']
    config = task.host.data['config']
    ifindex_map = task.host.data['ifindex_map']
    logs = []

    logs.append(f"\n{'='*70}")
    logs.append(f"  NETCONF 下发: {host.name}")
    logs.append(f"{'='*70}")

    snapshots = {"loopback": None, "interfaces": []}
    success = True

    try:
        with get_netconf_session(host) as session:
            # ---- Loopback ----
            lb = config["loopback"]
            logical_number = lb["number"]
            ip = lb["ip"]
            mask = lb["mask"]
            loopback_name = f"LoopBack{logical_number}"
            logs.append(f"\n  📌 Loopback: {loopback_name} {ip}/{mask}")

            real_ifindex = ifindex_map.get(loopback_name)
            if real_ifindex is not None:
                logs.append(f"     ℹ️ {loopback_name} 已存在（ifindex {real_ifindex}）")
            else:
                # CLI 创建
                cli_create_loopback(host, logical_number, f"{host.name} Loopback")
                logs.append(f"     ✅ CLI 创建 {loopback_name} 成功")
                time.sleep(2)

                # 通过 NETCONF 重新获取 ifindex 映射
                try:
                    new_map = get_ifindex_map(host, session)
                    ifindex_map.clear()
                    ifindex_map.update(new_map)
                    logs.append(f"     ✅ 已通过 NETCONF 更新 ifindex 映射，共 {len(ifindex_map)} 个接口")
                    real_ifindex = ifindex_map.get(loopback_name)
                    if real_ifindex is None:
                        logs.append(f"     ❌ 更新后仍未找到 {loopback_name} 的 ifindex")
                        success = False
                    else:
                        logs.append(f"     ✅ 从 NETCONF 获取到 {loopback_name} ifindex = {real_ifindex}")
                except Exception as e:
                    logs.append(f"     ❌ NETCONF 更新映射失败: {e}")
                    success = False
                    real_ifindex = None

            # 配置 IP
            if real_ifindex is not None:
                current_ip = get_netconf_ip(host, real_ifindex, session)
                if current_ip == ip:
                    logs.append(f"     ⏭️ IP {ip} 已存在，跳过配置")
                else:
                    xml_frag, err = render_direct(renderer, host,
                                                  "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                                  {"ifindex": real_ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                                   "operation": "merge"})
                    if err:
                        logs.append(f"     ❌ IP 渲染失败: {err}")
                        success = False
                    else:
                        config_xml = f"<config>{xml_frag}</config>"
                        logs.append("\n=== 发送的 IP 配置 XML ===")
                        logs.append(config_xml)
                        logs.append("==========================\n")
                        try:
                            session.edit_config(config=config_xml, target="candidate")
                            session.commit()
                            logs.append(f"     ✅ IP 配置成功")
                            snapshots["loopback"] = {"ifindex": real_ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                                     "logical_number": logical_number}
                        except Exception as e:
                            logs.append(f"     ❌ 发送失败: {e}")
                            success = False
            else:
                logs.append(f"     ⚠️ 跳过 IP 配置（未获取到真实 ifindex）")

            # ---- 物理接口 ----
            logs.append(f"\n  📌 物理接口:")
            for iface in config["interfaces"]:
                ifname = iface["ifname"]
                ip = iface["ip"]
                mask = iface["mask"]
                actual_ifindex = ifindex_map.get(ifname)
                if actual_ifindex is None:
                    logs.append(f"\n     ⚠️ 接口 {ifname} 不在 ifindex 映射中，跳过")
                    continue

                logs.append(f"\n     {ifname} (ifindex {actual_ifindex}): {ip}/{mask}")
                cli_create_physical(host, ifname)

                current_ip = get_netconf_ip(host, actual_ifindex, session)
                if current_ip == ip:
                    logs.append(f"        ⏭️ IP {ip} 已存在，跳过配置")
                    continue

                xml_frag, err = render_direct(renderer, host,
                                              "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                              {"ifindex": actual_ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                               "operation": "merge"})
                if err:
                    logs.append(f"        ❌ 渲染失败: {err}")
                    success = False
                else:
                    config_xml = f"<config>{xml_frag}</config>"
                    logs.append("\n=== 发送的物理接口 IP 配置 XML ===")
                    logs.append(config_xml)
                    logs.append("=====================================\n")
                    try:
                        session.edit_config(config=config_xml, target="candidate")
                        session.commit()
                        logs.append(f"        ✅ IP 配置成功")
                        snapshots["interfaces"].append(
                            {"ifindex": actual_ifindex, "ipv4_address": ip, "ipv4_mask": mask})
                    except Exception as e:
                        logs.append(f"        ❌ 发送失败: {e}")
                        success = False

    except Exception as e:
        logs.append(f"  ❌ NETCONF 会话异常: {e}")
        status = "failed"
        logs.append(f"\n状态: {status.upper()}")
        return Result(host=host, result={"status": status, "logs": logs})

    save_snapshot(host.name, "netconf", snapshots)
    logs.append(f"\n📁 快照已保存: logs/snapshots_{host.name}_netconf.json")
    status = "success" if success else "failed"
    logs.append(f"\n状态: {status.upper()}")
    return Result(host=host, result={"status": status, "logs": logs})


def task_rollback_netconf(task: Task) -> Result:
    """NETCONF 回退任务（基于快照）- 物理接口仅删除 IP，不改变 admin 状态"""
    host = task.host
    renderer = task.host.data['renderer']
    ifindex_map = task.host.data['ifindex_map']
    logs = []

    logs.append(f"\n{'='*70}")
    logs.append(f"  NETCONF 回退: {host.name}")
    logs.append(f"{'='*70}")

    snapshots = load_snapshot(host.name, "netconf")
    if snapshots is None:
        logs.append("  ⏭️ 未找到 NETCONF 快照，跳过回退")
        return Result(host=host, result={"status": "skipped", "logs": logs})

    if not snapshots.get("loopback") and not snapshots.get("interfaces"):
        logs.append("  ⏭️ 快照为空，无需回退")
        return Result(host=host, result={"status": "skipped", "logs": logs})

    success = True
    try:
        with get_netconf_session(host) as session:
            # ---- 回退物理接口（逆序） ----
            for snapshot in reversed(snapshots.get("interfaces", [])):
                ifindex = snapshot["ifindex"]
                ip = snapshot["ipv4_address"]
                mask = snapshot["ipv4_mask"]
                logs.append(f"\n  🔄 回退 ifindex {ifindex} 的 IP ({ip})")

                # NETCONF 删除 IP
                xml_frag, err = render_direct(renderer, host,
                                              "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                              {"ifindex": ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                               "operation": "delete"})
                if err:
                    logs.append(f"     ❌ 渲染失败: {err}")
                    success = False
                else:
                    config_xml = f"<config>{xml_frag}</config>"
                    try:
                        session.edit_config(config=config_xml, target="candidate")
                        session.commit()
                        logs.append(f"     ✅ IP 删除成功（NETCONF）")
                        # 记录接口名（仅用于提示）
                        ifname = None
                        for name, idx in ifindex_map.items():
                            if idx == ifindex:
                                ifname = name
                                break
                        if ifname:
                            logs.append(f"     ℹ️ 接口 {ifname} 的 IP 已删除，接口状态保持不变")
                        else:
                            logs.append(f"     ℹ️ 接口 ifindex {ifindex} 的 IP 已删除，接口状态保持不变")
                    except Exception as e:
                        logs.append(f"     ❌ 删除失败: {e}")
                        success = False
                        continue

            # ---- 回退 Loopback ----
            lb_snap = snapshots.get("loopback")
            if lb_snap:
                real_ifindex = lb_snap["ifindex"]
                logical_number = lb_snap.get("logical_number", real_ifindex)
                ip = lb_snap["ipv4_address"]
                mask = lb_snap["ipv4_mask"]
                logs.append(f"\n  🔄 回退 Loopback (逻辑编号 {logical_number}) 的 IP ({ip})")

                # NETCONF 删除 IP
                xml_frag, err = render_direct(renderer, host,
                                              "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2",
                                              {"ifindex": real_ifindex, "ipv4_address": ip, "ipv4_mask": mask,
                                               "operation": "delete"})
                if err:
                    logs.append(f"     ❌ 删除 IP 失败: {err}")
                    success = False
                else:
                    config_xml = f"<config>{xml_frag}</config>"
                    try:
                        session.edit_config(config=config_xml, target="candidate")
                        session.commit()
                        logs.append(f"     ✅ IP 删除成功（NETCONF）")
                    except Exception as e:
                        logs.append(f"     ❌ 删除 IP 失败: {e}")
                        success = False

                # CLI 删除 Loopback 接口（因为它是脚本创建的）
                cli_delete_loopback(host, logical_number)
                logs.append(f"     ✅ Loopback{logical_number} 已删除（CLI）")

    except Exception as e:
        logs.append(f"  ❌ 回退异常: {e}")
        success = False

    status = "success" if success else "failed"
    logs.append(f"\n状态: {status.upper()}")
    return Result(host=host, result={"status": status, "logs": logs})


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
    nr = InitNornir(
        config_file="config.yaml",
        runner={"plugin": "threaded", "options": {"num_workers": 4}}
    )
    project_root = Path(__file__).parent.parent.absolute()
    renderer = TemplateRenderer(project_root / "templates")

    print("已加载的主机:", list(nr.inventory.hosts.keys()))

    if device_names:
        target_devices = [d for d in device_names if d in DEVICE_CONFIG]
    else:
        target_devices = list(DEVICE_CONFIG.keys())

    if not target_devices:
        print("❌ 没有有效的目标设备")
        sys.exit(1)

    # 手动过滤主机
    filtered_hosts = {name: host for name, host in nr.inventory.hosts.items() if name in target_devices}
    nr.inventory.hosts = filtered_hosts
    if not nr.inventory.hosts:
        print("❌ 未找到匹配的主机")
        sys.exit(1)

    # 获取 ifindex 映射
    ifindex_maps = {}
    print("\n📡 正在获取设备 ifindex 映射...")
    for host in nr.inventory.hosts.values():
        try:
            with get_netconf_session(host) as session:
                ifindex_map = get_ifindex_map(host, session)
                ifindex_maps[host.name] = ifindex_map
                print(f"   {host.name}: ✅ {len(ifindex_map)} 个接口")
        except Exception as e:
            print(f"   {host.name}: ❌ 获取失败: {e}")
            ifindex_maps[host.name] = {}

    for host in nr.inventory.hosts.values():
        host.data["config"] = DEVICE_CONFIG[host.name]
        host.data["ifindex_map"] = ifindex_maps.get(host.name, {})
        host.data["renderer"] = renderer

    if command == "preview":
        print("=" * 70)
        print("  IP 配置预览模式（不下发）")
        print("=" * 70)
        for host in nr.inventory.hosts.values():
            config = host.data["config"]
            ifindex_map = host.data["ifindex_map"]
            logs = preview_device(host, renderer, config, ifindex_map)
            for line in logs:
                print(line)
        return

    print("=" * 70)
    print(f"  {command.upper()} 测试模式（并发执行）")
    print("=" * 70)

    if command == "test-cmd":
        result = nr.run(task=task_deploy_cmd)
    elif command == "test-netconf":
        result = nr.run(task=task_deploy_netconf)
    elif command == "rollback-cmd":
        result = nr.run(task=task_rollback_cmd)
    elif command == "rollback-netconf":
        result = nr.run(task=task_rollback_netconf)
    else:
        print(f"❌ 未知命令: {command}")
        sys.exit(1)

    all_status = []
    for device_name in sorted(result.keys()):
        multi_result = result[device_name]
        task_result = multi_result[0]
        res = task_result.result
        if isinstance(res, dict):
            logs = res.get("logs", [])
            status = res.get("status", "unknown")
        else:
            logs = [f"返回结果异常: {res}"] if res else ["无返回结果"]
            status = "failed"
        all_status.append((device_name, status))
        for line in logs:
            print(line)

    print_separator("测试结果汇总")
    for name, status in all_status:
        icon = "✅" if status == "success" else ("⏭️" if status == "skipped" else "❌")
        print(f"  {icon} {name}: {status.upper()}")
    passed = sum(1 for _, s in all_status if s == "success")
    total = len(all_status)
    print(f"\n  总计: {passed}/{total} 通过")
    print("=" * 70)


if __name__ == "__main__":
    main()