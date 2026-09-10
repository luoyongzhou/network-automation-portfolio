#!/usr/bin/env python3
"""
OSPF 配置下发与回退测试（H3C 适配版）
- 下发采用 merge + rollback-on-error 保证原子性
- 回退逐项删除接口、区域、进程，确保完全清理（包括接口残留的 OSPF 参数）
- 删除操作只指定索引列，符合 H3C NETCONF 规范
- 快照保存完整配置，支持精确回退
"""

import sys
import json
from pathlib import Path
from typing import List, Dict, Any

# 添加项目根目录到 sys.path
_project_root = Path(__file__).parent.parent.absolute()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from nornir import InitNornir
from nornir.core.inventory import Host
from nornir.core.task import Task, Result

from scripts.build_inventory import merge_group_files
from scripts.preview_bootstrap import TemplateRenderer, PathResolver
from atoms.utils import get_netconf_session, get_ifindex_map

# 复用 IP 测试中的设备配置
from scenes.test_ip_deploy_full import DEVICE_CONFIG


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


def save_snapshot(device_name, snapshots):
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    filename = log_dir / f"snapshots_{device_name}_ospf.json"
    with open(filename, "w") as f:
        json.dump(snapshots, f, indent=2)


def load_snapshot(device_name):
    filename = Path("logs") / f"snapshots_{device_name}_ospf.json"
    if not filename.exists():
        return None
    with open(filename) as f:
        return json.load(f)


class DirectPathResolver(PathResolver):
    def resolve(self, host: Host, template_name: str) -> List[str]:
        return [template_name]


def render_direct(renderer, host, template_name, context):
    return renderer.render(host, template_name, context_override=context, path_resolver=DirectPathResolver())


def build_ospf_config(device_name: str, ifindex_map: dict) -> Dict[str, Any]:
    """生成 OSPF 配置字典（包含接口、区域、进程）"""
    dev_cfg = DEVICE_CONFIG[device_name]
    loopback_ip = dev_cfg["loopback"]["ip"]
    loopback_number = dev_cfg["loopback"]["number"]
    loopback_name = f"LoopBack{loopback_number}"

    loopback_ifindex = ifindex_map.get(loopback_name)

    ospf_areas = [{
        "area_id": "0.0.0.0",
        "area_type": 0,
        "interfaces": []
    }]

    if loopback_ifindex is not None:
        ospf_areas[0]["interfaces"].append({
            "ifindex": loopback_ifindex,
        })

    for iface in dev_cfg["interfaces"]:
        ifname = iface["ifname"]
        actual_ifindex = ifindex_map.get(ifname)
        if actual_ifindex is not None:
            ospf_areas[0]["interfaces"].append({
                "ifindex": actual_ifindex,
                "network_type": 3,
            })

    return {
        "instance_name": "1",
        "router_id": loopback_ip,
        "areas": ospf_areas
    }


def preview_device(host, renderer, ospf_config):
    logs = []
    logs.append(f"\n{'='*70}")
    logs.append(f"  预览: {host.name} OSPF 配置")
    logs.append(f"{'='*70}")

    xml_text, err = render_direct(
        renderer,
        host,
        "product_lines/h3c/SR88/netconf/ospf_xml.j2",
        {"ospf": ospf_config}
    )
    if err:
        logs.append(f"❌ 渲染失败: {err}")
    else:
        logs.append(xml_text)

    logs.append("\n预览结束（未实际下发）")
    return logs


def task_deploy_ospf(task: Task) -> Result:
    """OSPF 下发（merge + rollback-on-error）"""
    host = task.host
    renderer = task.host.data['renderer']
    ospf_config = task.host.data['ospf_config']
    logs = []

    logs.append(f"\n{'='*70}")
    logs.append(f"  OSPF 下发: {host.name}")
    logs.append(f"{'='*70}")

    success = True
    try:
        xml_text, err = render_direct(
            renderer,
            host,
            "product_lines/h3c/SR88/netconf/ospf_xml.j2",
            {"ospf": ospf_config}
        )
        if err:
            logs.append(f"❌ 渲染失败: {err}")
            success = False
        else:
            config_xml = f"<config>{xml_text}</config>"
            logs.append("=== 发送的 OSPF 配置 XML (merge + rollback-on-error) ===")
            logs.append(config_xml)
            logs.append("==================================================\n")

            with get_netconf_session(host) as session:
                session.edit_config(
                    config=config_xml,
                    target="candidate",
                    error_option="rollback-on-error"
                )
                session.commit()
                logs.append("✅ OSPF 配置原子性提交成功")
                # 保存完整配置快照，用于精确回退
                save_snapshot(host.name, {"ospf_config": ospf_config})
                logs.append(f"📁 快照已保存: logs/snapshots_{host.name}_ospf.json")
    except Exception as e:
        logs.append(f"❌ NETCONF 下发失败（已自动回滚）: {e}")
        success = False

    status = "success" if success else "failed"
    logs.append(f"\n状态: {status.upper()}")
    return Result(host=host, result={"status": status, "logs": logs})


def task_rollback_ospf(task: Task) -> Result:
    """OSPF 回退：逐项删除接口、区域、进程（continue-on-error 容忍不存在）"""
    host = task.host
    logs = []

    logs.append(f"\n{'='*70}")
    logs.append(f"  OSPF 回退: {host.name}")
    logs.append(f"{'='*70}")

    snapshot = load_snapshot(host.name)
    if snapshot is None:
        logs.append("  ⏭️ 未找到 OSPF 快照，跳过回退")
        return Result(host=host, result={"status": "skipped", "logs": logs})

    ospf_config = snapshot.get("ospf_config")
    if not ospf_config:
        logs.append("  ⏭️ 快照中无配置信息，跳过回退")
        return Result(host=host, result={"status": "skipped", "logs": logs})

    success = True
    try:
        # 构建删除 XML，只指定索引列
        delete_xml_parts = []
        delete_xml_parts.append('<config>')
        delete_xml_parts.append('  <OSPF xmlns="http://www.h3c.com/netconf/config:1.0">')

        # 1. 删除接口配置：仅 IfIndex（索引列）
        delete_xml_parts.append('    <Interfaces>')
        for area in ospf_config.get("areas", []):
            for iface in area.get("interfaces", []):
                ifindex = iface["ifindex"]
                delete_xml_parts.append(
                    f'      <Interface xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">'
                )
                delete_xml_parts.append(f'        <IfIndex>{ifindex}</IfIndex>')
                delete_xml_parts.append('      </Interface>')
        delete_xml_parts.append('    </Interfaces>')

        # 2. 删除区域：Name 和 AreaId（均为索引列）
        delete_xml_parts.append('    <Areas>')
        for area in ospf_config.get("areas", []):
            area_id = area["area_id"]
            delete_xml_parts.append(
                f'      <Area xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">'
            )
            delete_xml_parts.append(f'        <Name>{ospf_config["instance_name"]}</Name>')
            delete_xml_parts.append(f'        <AreaId>{area_id}</AreaId>')
            delete_xml_parts.append('      </Area>')
        delete_xml_parts.append('    </Areas>')

        # 3. 删除进程：Name（索引列）
        delete_xml_parts.append('    <Instances>')
        delete_xml_parts.append(
            f'      <Instance xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">'
        )
        delete_xml_parts.append(f'        <Name>{ospf_config["instance_name"]}</Name>')
        delete_xml_parts.append('      </Instance>')
        delete_xml_parts.append('    </Instances>')

        delete_xml_parts.append('  </OSPF>')
        delete_xml_parts.append('</config>')
        delete_xml = '\n'.join(delete_xml_parts)

        logs.append("=== 发送的删除 OSPF 配置 XML (continue-on-error) ===")
        logs.append(delete_xml)
        logs.append("===================================================\n")

        with get_netconf_session(host) as session:
            session.edit_config(
                config=delete_xml,
                target="candidate",
                error_option="continue-on-error"
            )
            session.commit()
            logs.append("✅ OSPF 回退完成（接口、区域、进程已清理）")

            # 删除快照文件
            snapshot_file = Path("logs") / f"snapshots_{host.name}_ospf.json"
            if snapshot_file.exists():
                snapshot_file.unlink()
                logs.append(f"🗑️ 快照文件已删除: {snapshot_file}")
    except Exception as e:
        logs.append(f"❌ 回退失败: {e}")
        success = False

    status = "success" if success else "failed"
    logs.append(f"\n状态: {status.upper()}")
    return Result(host=host, result={"status": status, "logs": logs})


def main():
    args = sys.argv[1:]
    if not args:
        print("用法:")
        print("  python scenes/test_ospf_deploy_full.py preview [device]")
        print("  python scenes/test_ospf_deploy_full.py test-ospf [device]")
        print("  python scenes/test_ospf_deploy_full.py rollback-ospf [device]")
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
        host.data["ospf_config"] = build_ospf_config(host.name, ifindex_maps.get(host.name, {}))
        host.data["renderer"] = renderer

    if command == "preview":
        print("=" * 70)
        print("  OSPF 配置预览模式（不下发）")
        print("=" * 70)
        for host in nr.inventory.hosts.values():
            logs = preview_device(host, renderer, host.data["ospf_config"])
            for line in logs:
                print(line)
        return

    print("=" * 70)
    print(f"  {command.upper()} 测试模式（并发执行）")
    print("=" * 70)

    if command == "test-ospf":
        result = nr.run(task=task_deploy_ospf)
    elif command == "rollback-ospf":
        result = nr.run(task=task_rollback_ospf)
    else:
        print(f"❌ 未知命令: {command}")
        sys.exit(1)

    all_status = []
    for device_name in sorted(result.keys()):
        task_result = result[device_name][0]
        res = task_result.result
        if isinstance(res, dict):
            logs = res.get("logs", [])
            status = res.get("status", "unknown")
        else:
            logs = [f"返回结果异常: {res}"]
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