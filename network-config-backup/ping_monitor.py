#!/usr/bin/env python3.9
"""
批量 Ping 监控工具（基于 icmplib）- 不可达设备钉钉告警

用法: python3 ping_monitor.py
建议通过 crontab 每 5 分钟执行一次，检测清单中的设备是否可达，
不可达时汇总推送到钉钉群机器人。

设备清单与钉钉 Webhook 均从环境变量 / .env 读取，不在代码中硬编码。
"""
import os
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from icmplib import ping
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ================== 配置区域 ==================
# 设备清单：JSON 格式的 {"IP": "备注"} 字典，通过环境变量注入
# 示例: PING_TARGETS='{"10.0.0.1": "示例网关", "10.0.0.2": "示例服务器"}'
TARGETS = json.loads(os.environ.get("PING_TARGETS", "{}"))

# 钉钉自定义机器人 Webhook（含 access_token，务必只通过环境变量配置）
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")

# Ping 参数
USE_THREADING = True
MAX_WORKERS = int(os.environ.get("PING_MAX_WORKERS", "10"))   # 并发线程数
PING_COUNT = int(os.environ.get("PING_COUNT", "2"))           # 发送包数量
PING_TIMEOUT = float(os.environ.get("PING_TIMEOUT", "1.0"))   # 单次超时（秒）
# =============================================


def send_dingtalk_alert(failed_hosts):
    """发送钉钉告警消息"""
    if not failed_hosts:
        return
    if not DINGTALK_WEBHOOK:
        print("⚠️ 未配置 DINGTALK_WEBHOOK，跳过告警发送")
        return

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    message_lines = [
        "# ⚠️ 网络设备告警",
        f"**告警时间**: {current_time}",
        f"**不可达设备数量**: {len(failed_hosts)}",
        "",
        "## 📋 详细信息："
    ]

    for host, remark in failed_hosts:
        message_lines.append(f"- ❌ **{host}** ({remark})")

    message_lines.append("")
    message_lines.append("> 请检查设备是否存活！")

    markdown_text = "\n".join(message_lines)

    data = {
        "msgtype": "markdown",
        "markdown": {
            "title": "网络设备告警",
            "text": markdown_text
        }
    }

    try:
        response = requests.post(
            DINGTALK_WEBHOOK,
            headers={"Content-Type": "application/json"},
            data=json.dumps(data),
            timeout=5
        )

        if response.status_code == 200:
            result = response.json()
            if result.get("errcode") == 0:
                print(f"\n✅ 告警消息已发送到钉钉 (共 {len(failed_hosts)} 个故障设备)")
            else:
                print(f"\n❌ 钉钉消息发送失败: {result.get('errmsg')}")
        else:
            print(f"\n❌ 钉钉消息发送失败: HTTP {response.status_code}")

    except Exception as e:
        print(f"\n❌ 发送钉钉消息时出错: {e}")


def ping_host_with_icmplib(host):
    """使用 icmplib 检测单个主机，返回是否可达和平均延迟"""
    try:
        result = ping(host, count=PING_COUNT, timeout=PING_TIMEOUT, privileged=False)
        return result.is_alive, result.avg_rtt
    except Exception as e:
        print(f"Ping {host} 时发生错误: {e}")
        return False, 0


def main():
    if not TARGETS:
        print("⚠️ 未配置 PING_TARGETS，无设备可检测，退出")
        return

    print(f"准备检测 {len(TARGETS)} 个地址 (使用 icmplib)...\n")

    failed_hosts = []  # 记录不可达的设备

    if USE_THREADING and len(TARGETS) > 1:
        print(f"启动多线程检测 (最大 {MAX_WORKERS} 线程)...\n")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_host = {
                executor.submit(ping_host_with_icmplib, host): (host, remark)
                for host, remark in TARGETS.items()
            }

            for future in as_completed(future_to_host):
                host, remark = future_to_host[future]
                try:
                    is_alive, avg_rtt = future.result()
                    if is_alive:
                        print(f"{host:20s} ({remark:20s}) ✅ 可达 (平均延迟: {avg_rtt:.2f} ms)")
                    else:
                        print(f"{host:20s} ({remark:20s}) ❌ 不可达")
                        failed_hosts.append((host, remark))
                except Exception as e:
                    print(f"{host:20s} ({remark:20s}) ❌ 检测失败 ({e})")
                    failed_hosts.append((host, remark))
    else:
        print("顺序检测...\n")
        for host, remark in TARGETS.items():
            is_alive, avg_rtt = ping_host_with_icmplib(host)
            if is_alive:
                print(f"{host:20s} ({remark:20s}) ✅ 可达 (平均延迟: {avg_rtt:.2f} ms)")
            else:
                print(f"{host:20s} ({remark:20s}) ❌ 不可达")
                failed_hosts.append((host, remark))

    print("\n检测完成。")

    if failed_hosts:
        send_dingtalk_alert(failed_hosts)
    else:
        print("\n✅ 所有设备均可达，无需告警。")


if __name__ == "__main__":
    main()
