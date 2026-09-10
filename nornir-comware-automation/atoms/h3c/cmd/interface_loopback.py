#!/usr/bin/env python3
"""
H3C SR88 CMD Loopback 操作封装
"""

from atoms.base import CmdAtom
from atoms.utils import get_cli_interface, get_cli_interface_ip, check_cli_dependencies


class CmdLoopbackAtom(CmdAtom):
    """
    CMD Loopback 操作封装
    正向模板：product_lines/h3c/SR88/cmd/interface_create_cmd.j2 + ip_address_cmd.j2
    回退模板：product_lines/h3c/SR88/cmd/interface_create_rollback_cmd.j2 + ip_address_rollback_cmd.j2
    """

    CREATE_TEMPLATE = "product_lines/h3c/SR88/cmd/interface_create_cmd.j2"
    IP_TEMPLATE = "product_lines/h3c/SR88/cmd/ip_address_cmd.j2"
    ROLLBACK_INTERFACE_TEMPLATE = "product_lines/h3c/SR88/cmd/interface_create_rollback_cmd.j2"
    ROLLBACK_IP_TEMPLATE = "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2"

    # ===== 对外接口 =====

    def pre_check(self, host, ifindex, description=None, ipv4_address=None, ipv4_mask=None, **kwargs):
        ifname = f"LoopBack{ifindex}"
        return self._pre_check_impl(host, ifname, description, ipv4_address, ipv4_mask, **kwargs)

    def deploy(self, host, snapshot, net_connect, **kwargs):
        return self._deploy_impl(host, snapshot, net_connect, **kwargs)

    def post_check(self, host, snapshot, desired, **kwargs):
        return self._post_check_impl(host, snapshot, desired, **kwargs)

    def rollback(self, host, snapshot, net_connect, **kwargs):
        rollback_check = self._rollback_pre_check_impl(host, snapshot, **kwargs)
        if rollback_check["status"] == "blocked":
            return {"status": "blocked", "reason": rollback_check.get("reason")}
        return self._rollback_impl(host, snapshot, net_connect, **kwargs)

    # ===== 内部实现 =====

    def _pre_check_impl(self, host, ifname, description=None, ipv4_address=None, ipv4_mask=None, **kwargs):
        current = get_cli_interface(host, ifname)
        if current:
            return {
                "status": "blocked",
                "snapshot": None,
                "reason": f"{ifname} 已存在"
            }
        return {
            "status": "ready",
            "snapshot": {
                "ifname": ifname,
                "description": description,
                "ipv4_address": ipv4_address,
                "ipv4_mask": ipv4_mask,
            },
            "reason": None,
        }

    def _deploy_impl(self, host, snapshot, net_connect, **kwargs):
        # 1. 创建接口
        result = self._render_and_send(
            host, self.CREATE_TEMPLATE,
            {"ifname": snapshot["ifname"], "description": snapshot["description"]},
            net_connect
        )
        if result["status"] == "failed":
            return result

        # 2. 配置 IP
        if snapshot["ipv4_address"] and snapshot["ipv4_mask"]:
            result = self._render_and_send(
                host, self.IP_TEMPLATE,
                {
                    "ifname": snapshot["ifname"],
                    "ipv4_address": snapshot["ipv4_address"],
                    "ipv4_mask": snapshot["ipv4_mask"],
                },
                net_connect
            )
        return result

    def _post_check_impl(self, host, snapshot, desired, **kwargs):
        current = get_cli_interface(host, snapshot["ifname"])
        if not current:
            return {
                "status": "failed",
                "actual": None,
                "detail": f"{snapshot['ifname']} 未创建"
            }

        if snapshot["ipv4_address"]:
            ip_current = get_cli_interface_ip(host, snapshot["ifname"])
            if ip_current != snapshot["ipv4_address"]:
                return {
                    "status": "failed",
                    "actual": {"ip": ip_current},
                    "detail": f"IP 不匹配: 期望 {snapshot['ipv4_address']}, 实际 {ip_current}"
                }

        return {"status": "success", "actual": current, "detail": None}

    def _rollback_pre_check_impl(self, host, snapshot, **kwargs):
        deps = check_cli_dependencies(host, snapshot["ifname"])
        if deps:
            return {
                "status": "blocked",
                "dependencies": deps,
                "reason": f"{snapshot['ifname']} 被依赖: {deps}"
            }
        return {"status": "safe", "dependencies": [], "reason": None}

    def _rollback_impl(self, host, snapshot, net_connect, **kwargs):
        # 1. 删除 IP
        if snapshot["ipv4_address"]:
            self._render_and_send(
                host, self.ROLLBACK_IP_TEMPLATE,
                {"ifname": snapshot["ifname"], "ipv4_address": snapshot["ipv4_address"]},
                net_connect
            )
        # 2. 删除接口
        return self._render_and_send(
            host, self.ROLLBACK_INTERFACE_TEMPLATE,
            {"ifname": snapshot["ifname"]},
            net_connect
        )