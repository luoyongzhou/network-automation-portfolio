#!/usr/bin/env python3
"""批量获取设备运行配置并按规范存储

用法: python3 collect_configs.py
输出: <BASE_DIR>/YYYY-MM-DD/<region>/设备名_IP_型号_版本_HHMMSS.txt

依赖: nornir + nornir-netmiko，设备清单通过 config/hosts.yaml 定义（见 hosts.yaml.example）。
建议通过 crontab 每天定时调用，不建议对外暴露为 API。
"""
import sys, os, re
from datetime import datetime
from pathlib import Path

from nornir import InitNornir
from nornir_netmiko.tasks import netmiko_send_command

# 配置文件与输出目录均可通过环境变量覆盖，便于容器化部署
BASE_DIR = os.environ.get("CONFIG_BACKUP_DIR", "/home/network_device_configs")
CONFIG_FILE = os.environ.get("NORNIR_CONFIG_FILE", "/app/config/config.yaml")


def get_config_and_version(task):
    """根据设备平台选择对应命令，获取运行配置和版本信息"""
    platform = task.host.platform

    if "cisco" in platform:
        config_cmd = "show running-config"
        version_cmd = "show version"
    elif "huawei" in platform or "vrp" in platform:
        config_cmd = "display current-configuration"
        version_cmd = "display version"
    elif "h3c" in platform or "comware" in platform:
        config_cmd = "display current-configuration"
        version_cmd = "display version"
    else:
        config_cmd = "show running-config"
        version_cmd = "show version"

    config_result = task.run(task=netmiko_send_command, command_string=config_cmd, read_timeout=120)
    version_result = task.run(task=netmiko_send_command, command_string=version_cmd, read_timeout=30)

    return config_result, version_result


def extract_version(version_output, platform):
    """从 version 输出中提取软件版本号，兼容 Cisco / Huawei / H3C 三种常见格式"""
    text = str(version_output)
    if "comware" in platform or "h3c" in platform:
        # H3C: "Version 7.1.070, Release 6615P25"
        m = re.search(r'Version\s+([\d.]+),\s*Release\s+(\S+)', text)
        if m:
            return f"{m.group(1)}_Release_{m.group(2)}"
        m = re.search(r'Software Version\s+(\S+)', text)
    elif "vrp" in platform or "huawei" in platform:
        # 华为: "V200R019C10SPC500"
        m = re.search(r'(V\d+R\d+C\d+\S*)', text)
        if m:
            return m.group(1).rstrip(')')
        m = re.search(r'VRP.*Software.*Version\s+(\S+)', text)
    elif "cisco" in platform:
        m = re.search(r'Version\s+(\S+)', text)
    else:
        m = re.search(r'[Vv]ersion\s+(\S+)', text)
    return m.group(1).rstrip(')').rstrip(',') if m else "unknown"


def safe_filename(s):
    """清理文件名中的非法字符"""
    return re.sub(r'[<>:"/\\|?*\s]', '_', s)[:80]


def resolve_region(location: str) -> str:
    """根据设备的机房/站点标签归类到备份子目录，标签体系按需自行调整"""
    if not location:
        return "other"
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', location.lower()) or "other"


def main():
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")

    nr = InitNornir(config_file=CONFIG_FILE)
    total = len(nr.inventory.hosts)
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开始采集 {total} 台设备配置")

    result = nr.run(task=get_config_and_version)

    success = 0
    fail = 0

    for host_name, r in result.items():
        host = nr.inventory.hosts[host_name]
        hostname = host.hostname
        platform = host.platform
        hardware = host.data.get('hardware', 'unknown')
        location = host.data.get('location', '')
        region = resolve_region(location)

        if r.failed:
            print(f"  ❌ {host_name} ({hostname}) - {r.exception}")
            fail += 1
            continue

        # 提取版本
        config_text = str(r[1].result)
        version_text = str(r[2].result)
        sw_version = extract_version(version_text, platform)

        # 构建文件名: 设备名_IP_型号_版本_HHMMSS.txt
        fname = safe_filename(f"{host_name}_{hostname}_{hardware}_{sw_version}_{time_str}") + ".txt"

        # 构建目录: BASE_DIR/日期/地区/
        out_dir = Path(BASE_DIR) / date_str / region
        out_dir.mkdir(parents=True, exist_ok=True)

        # 写入配置（头部加元信息）
        out_path = out_dir / fname
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"! 设备: {host_name}\n")
            f.write(f"! IP: {hostname}\n")
            f.write(f"! 型号: {hardware}\n")
            f.write(f"! 版本: {sw_version}\n")
            f.write(f"! 平台: {platform}\n")
            f.write(f"! 地区: {region}\n")
            f.write(f"! 采集时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"!\n")
            f.write(config_text)

        print(f"  ✅ {host_name:20s} → {out_dir.name}/{fname}")
        success += 1

    print(f"\n完成: 成功 {success}, 失败 {fail}, 总计 {total}")
    print(f"输出目录: {BASE_DIR}/{date_str}/")


if __name__ == "__main__":
    main()
