#!/usr/bin/env python3
"""
网络自动化主入口脚本

功能：
    1、自动构建 groups.yaml（调用 scripts/build_inventory.py）
    2、加载 Nornir Inventory
    3、通过 Jinja2 渲染 bootstrap.j2 模板
    4、通过 Console 映射 Telnet 连接设备，中断 ZTP，逐条下发配置
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到 sys.path（因为脚本在 scenes/ 子目录中）
_project_root = Path(__file__).parent.parent.absolute()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from netmiko import ConnectHandler
import time

from nornir import InitNornir
from nornir.core.filter import F
from nornir.core.task import Task, Result

# 导入构建脚本（直接调用函数，无需 subprocess）
from scripts.build_inventory import merge_group_files

# 导入渲染引擎（复用 preview_bootstrap 的核心类）
from scripts.preview_bootstrap import TemplateRenderer, SceneAPI


# ============================================================
# 前置步骤：自动构建 groups.yaml
# ============================================================

def ensure_groups_up_to_date() -> None:
    """
    在 Nornir 初始化之前，自动构建 groups.yaml
    如果构建失败，直接退出（防止使用过期数据）
    """
    print("📦 正在构建 groups.yaml...")
    try:
        merge_group_files()
        print("✅ groups.yaml 构建完成")
    except Exception as e:
        print(f"❌ groups.yaml 构建失败: {e}")
        sys.exit(1)


# ============================================================
# 任务函数：Console 下发
# ============================================================

def task_console_deploy(task: Task, scene_api: SceneAPI) -> Result:
    """通过 Console 映射 Telnet 下发配置（优化版：使用 send_command 减少等待）"""
    host = task.host

    # ---- 1. 提取 OOB 连接参数 ----
    oob_conn = host.connection_options.get("netmiko_oob")
    if not oob_conn:
        return Result(host=host, failed=True, exception=Exception(f"设备 {host.name} 缺少 netmiko_oob 连接配置"))
    oob_hostname = oob_conn.hostname
    oob_port = oob_conn.port

    # ---- 2. 连接设备 ----
    try:
        net_connect = ConnectHandler(
            device_type='hp_comware_telnet',
            host=oob_hostname,
            port=oob_port,
            timeout=60,
            username='',
            password='',
        )
    except Exception:
        try:
            net_connect = ConnectHandler(
                device_type='generic_telnet',
                host=oob_hostname,
                port=oob_port,
                timeout=60,
            )
        except Exception as e2:
            return Result(host=host, failed=True, exception=Exception(f"连接失败: {e2}"))

    # ---- 3. 唤醒 + ZTP 中断（保留必要的 sleep） ----
    net_connect.write_channel("\r")
    time.sleep(0.5)

    for _ in range(2):
        net_connect.write_channel("\x03")
        time.sleep(0.5)
    net_connect.write_channel("\r")
    time.sleep(1)

    try:
        output = net_connect.read_until_prompt()
        if "Press ENTER" in output:
            net_connect.write_channel("\r")
            time.sleep(0.5)
            output = net_connect.read_until_prompt()
    except Exception:
        pass

    # ---- 4. 渲染模板 ----
    config_text, error = scene_api.bootstrap(host)
    if error:
        net_connect.disconnect()
        return Result(host=host, failed=True, exception=Exception(error.get("detail", "渲染失败")))
    config_commands = [
        cmd.strip() for cmd in config_text.splitlines()
        if cmd.strip() and not cmd.strip().startswith("#")
    ]
    if not config_commands:
        net_connect.disconnect()
        return Result(host=host, result="SKIPPED: 无配置命令")

    # ---- 5. 使用 send_command 下发（无需额外 sleep） ----
    net_connect.send_command("system-view", expect_string=r"\[.*?\]")

    for cmd in config_commands:
        net_connect.send_command(cmd, expect_string=r"\[.*?\]")

    net_connect.send_command("end", expect_string=r"<.*?>")
    net_connect.write_channel("quit\r")

    # ---- 6. 断开连接 ----
    net_connect.disconnect()

    print(f"✅ {host.name} Console 配置下发完成")
    return Result(host=host, result="OK")


# ============================================================
# 任务函数：预览（不下发）
# ============================================================

def task_preview(task: Task, scene_api: SceneAPI) -> Result:
    """仅预览配置，不下发到设备"""
    host = task.host

    # 预览同样使用 bootstrap 场景
    config_text, error = scene_api.bootstrap(host)
    if error:
        return Result(host=host, failed=True, exception=Exception(error.get("detail", "渲染失败")))

    print(f"\n{'='*60}")
    print(f"设备: {host.name}")
    print(f"Group: {[g.name for g in host.groups]}")
    print(f"{'='*60}")
    print(config_text)
    print(f"{'='*60}\n")

    return Result(host=host, result="PREVIEW")


# ============================================================
# 主入口
# ============================================================

def main():
    # 1. 切换到项目根目录
    script_dir = Path(__file__).parent.absolute()
    project_root = script_dir.parent
    os.chdir(str(project_root))
    print(f"📍 工作目录: {project_root}")

    # 2. 解析命令行参数
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    command = args[0]
    device_name = args[1] if len(args) > 1 else None

    # 3. 前置步骤：构建 groups.yaml（所有命令都需要）
    ensure_groups_up_to_date()

    # 4. 初始化 Nornir
    nr = InitNornir(config_file="config.yaml")

    # 5. 如果指定了设备名，只选择该设备
    if device_name:
        host = nr.inventory.hosts.get(device_name)
        if not host:
            print(f"❌ 设备 '{device_name}' 不存在")
            sys.exit(1)
        # 通过 filter 过滤
        nr = nr.filter(F(name=device_name))

    # 6. 初始化渲染引擎（所有任务共享）
    renderer = TemplateRenderer(project_root / "templates")
    scene = SceneAPI(renderer)

    # 7. 路由到对应的任务
    command_map = {
        "console": task_console_deploy,
        "preview": task_preview,
        # 后续可扩展: "ssh": task_ssh_deploy, "netconf": task_netconf_deploy
    }

    if command not in command_map:
        print(f"❌ 未知命令: {command}")
        print(f"   可用命令: {list(command_map.keys())}")
        sys.exit(1)

    task_func = command_map[command]

    # 8. 执行任务
    print(f"\n🚀 执行任务: {command}")
    # 在 nr.run 之前添加，用于回显关键信息
    for host_name, host in nr.inventory.hosts.items():
        print(f"\n🔍 设备: {host_name}")
        print(f"   hostname: {host.hostname}")
        print(f"   connection_options 键: {list(host.connection_options.keys())}")
        if "netmiko_oob" in host.connection_options:
            print(f"   netmiko_oob 配置: {host.connection_options['netmiko_oob'].dict()}")
        else:
            print(f"   ❌ netmiko_oob 不存在！")
    result = nr.run(task=task_func, scene_api=scene)

    # 9. 输出结果
    print("\n📊 执行结果:")
    for host_name, host_result in result.items():
        if host_result[0].failed:
            print(f"   ❌ {host_name}: 失败 - {host_result[0].exception}")
        else:
            print(f"   ✅ {host_name}: {host_result[0].result}")


if __name__ == "__main__":
    main()