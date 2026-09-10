#!/usr/bin/env python3
"""
阶段二：SSH 下发 NETCONF 初始化配置（支持独立连接块，严格保序）

功能：
    1. 自动构建 groups.yaml（调用 build_inventory.py）
    2. 通过 SSH 下发 netconf_cmd.j2 模板渲染的配置
    3. 支持 #INDEPENDENT:START / #INDEPENDENT:END 标记独立连接块
    4. 独立块采用“发送即忘”策略，不等待特定提示符，超时兜底
    5. 支持预览模式（不下发）

用法：
    python scenes/bootstrap_step2.py preview [device]   # 预览 NETCONF 配置
    python scenes/bootstrap_step2.py deploy [device]    # 通过 SSH 下发 NETCONF 配置
"""

import sys
import os
import time
from pathlib import Path

# 添加项目根目录到 sys.path（因为脚本在 scenes/ 子目录中）
_project_root = Path(__file__).parent.parent.absolute()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from nornir import InitNornir
from nornir.core.filter import F
from nornir.core.task import Task, Result
from nornir_netmiko.tasks import netmiko_send_config
from netmiko import ConnectHandler

from scripts.build_inventory import merge_group_files
from scripts.preview_bootstrap import TemplateRenderer, SceneAPI


# ============================================================
# 前置步骤：自动构建 groups.yaml
# ============================================================

def ensure_groups_up_to_date() -> None:
    """在 Nornir 初始化之前，自动构建 groups.yaml"""
    print("📦 正在构建 groups.yaml...")
    try:
        merge_group_files()
        print("✅ groups.yaml 构建完成")
    except Exception as e:
        print(f"❌ groups.yaml 构建失败: {e}")
        sys.exit(1)


# ============================================================
# 解析器：严格保序，识别独立连接块
# ============================================================

def parse_preserving_order(config_text: str) -> list:
    """
    解析配置文本，返回按原始顺序排列的执行单元列表

    每个单元是：
        {"type": "normal", "commands": ["cmd1", "cmd2"]}   # 普通命令块
        或
        {"type": "block", "lines": ["line1", "line2"]}     # 独立块（含交互输入）
    """
    lines = config_text.strip().splitlines()

    units = []
    normal_buffer = []
    in_block = False
    current_block = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 独立块开始
        if stripped.startswith("#INDEPENDENT:START"):
            if normal_buffer:
                units.append({"type": "normal", "commands": normal_buffer})
                normal_buffer = []
            in_block = True
            current_block = []
            continue

        # 独立块结束
        if stripped.startswith("#INDEPENDENT:END"):
            if current_block:
                units.append({"type": "block", "lines": current_block})
            in_block = False
            continue

        # 普通注释（跳过）
        if stripped.startswith("#"):
            continue

        # 有效行
        if in_block:
            current_block.append(stripped)
        else:
            normal_buffer.append(stripped)

    if normal_buffer:
        units.append({"type": "normal", "commands": normal_buffer})

    return units


# ============================================================
# 核心任务：SSH 下发 NETCONF 配置（严格保序）
# ============================================================

def task_ssh_netconf_deploy(task: Task, scene_api: SceneAPI) -> Result:
    """
    通过 SSH 下发 NETCONF 初始化配置（netconf_cmd.j2）
    严格按模板原始顺序执行，独立块使用独立 SSH 连接
    """
    host = task.host

    # ---- 1. 渲染模板 ----
    config_text, error = scene_api.ssh_bootstrap(host)
    if error:
        return Result(
            host=host,
            failed=True,
            exception=Exception(error.get("detail", "渲染失败"))
        )

    # ---- 2. 按原始顺序解析执行单元 ----
    units = parse_preserving_order(config_text)

    # ---- 3. 按顺序逐个执行 ----
    for idx, unit in enumerate(units):
        if unit["type"] == "normal":
            commands = unit["commands"]
            if commands:
                print(f"   📝 普通命令块 #{idx + 1}: {len(commands)} 条命令")
                try:
                    task.run(
                        task=netmiko_send_config,
                        config_commands=commands,
                    )
                except Exception as e:
                    return Result(
                        host=host,
                        failed=True,
                        exception=Exception(f"普通命令块 #{idx + 1} 下发失败: {e}")
                    )

        elif unit["type"] == "block":
            lines = unit["lines"]
            print(f"   🔗 独立块 #{idx + 1}: {len(lines)} 行")

            try:
                with ConnectHandler(
                        device_type='hp_comware',
                        host=host.hostname,
                        port=host.port or 22,
                        username=host.username,
                        password=host.password,
                        timeout=60,
                ) as net_connect:
                    # 进入系统视图
                    if lines and not lines[0].startswith("system-view"):
                        net_connect.send_command("system-view", expect_string=r"\[.*?\]")

                    # 逐行发送，不等待特定提示符
                    for line in lines:
                        if line.startswith("system-view"):
                            continue
                        net_connect.send_command(line, expect_string=r".*", delay_factor=1)
                        time.sleep(0.2)

                    # 不再额外发送 end，连接断开会自动退出系统视图

                # 退出 with 块后，连接自动关闭

            except Exception as e:
                return Result(
                    host=host,
                    failed=True,
                    exception=Exception(f"独立块 #{idx + 1} 执行失败: {e}")
                )

    print(f"✅ {host.name} SSH NETCONF 配置下发完成")
    return Result(host=host, result="OK")


# ============================================================
# 预览任务（只收集数据，不打印）
# ============================================================

def task_preview(task: Task, scene_api: SceneAPI) -> Result:
    """预览 NETCONF 配置，不下发（只收集数据）"""
    host = task.host

    config_text, error = scene_api.ssh_bootstrap(host)
    if error:
        return Result(
            host=host,
            failed=True,
            exception=Exception(error.get("detail", "渲染失败"))
        )

    units = parse_preserving_order(config_text)

    return Result(
        host=host,
        result={
            "device": host.name,
            "groups": [g.name for g in host.groups],
            "config_text": config_text,
            "units": units,
        }
    )


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

    # 3. 前置步骤：构建 groups.yaml
    ensure_groups_up_to_date()

    # 4. 初始化 Nornir
    nr = InitNornir(config_file="config.yaml")

    # 5. 如果指定了设备名，只选择该设备
    if device_name:
        host = nr.inventory.hosts.get(device_name)
        if not host:
            print(f"❌ 设备 '{device_name}' 不存在")
            sys.exit(1)
        nr = nr.filter(F(name=device_name))

    # 6. 初始化渲染引擎（所有任务共享）
    renderer = TemplateRenderer(project_root / "templates")
    scene = SceneAPI(renderer)

    # 7. 命令路由
    if command == "preview":
        print(f"\n🔍 预览模式（不下发）")
        result = nr.run(task=task_preview, scene_api=scene)

        # ---- 统一打印所有设备的预览结果 ----
        print("\n" + "=" * 80)
        for host_name, host_result in result.items():
            if host_result[0].failed:
                print(f"❌ {host_name}: 预览失败 - {host_result[0].exception}")
                continue

            data = host_result[0].result
            print(f"\n📌 设备: {data['device']}")
            print(f"   Group: {data['groups']}")
            print("-" * 60)
            print(data["config_text"])
            print("-" * 60)

            # 显示执行单元摘要
            units = data["units"]
            normal_count = sum(1 for u in units if u["type"] == "normal")
            block_count = sum(1 for u in units if u["type"] == "block")
            print(f"\n📋 执行单元: {len(units)} 个（普通命令块 {normal_count} 个，独立块 {block_count} 个）")
            for idx, unit in enumerate(units, 1):
                if unit["type"] == "normal":
                    print(f"   {idx}. 📝 普通命令块: {len(unit['commands'])} 条")
                else:
                    print(f"   {idx}. 🔗 独立块: {len(unit['lines'])} 行")
            print("=" * 80)

        print(f"\n📊 预览完成: {len(result)} 台设备")
        return

    elif command == "deploy":
        print(f"\n🚀 下发模式")
        result = nr.run(task=task_ssh_netconf_deploy, scene_api=scene)

        print("\n📊 执行结果:")
        for host_name, host_result in result.items():
            if host_result[0].failed:
                print(f"   ❌ {host_name}: 失败 - {host_result[0].exception}")
            else:
                print(f"   ✅ {host_name}: {host_result[0].result}")
    else:
        print(f"❌ 未知命令: {command}")
        print("   可用命令: preview, deploy")
        sys.exit(1)


if __name__ == "__main__":
    main()