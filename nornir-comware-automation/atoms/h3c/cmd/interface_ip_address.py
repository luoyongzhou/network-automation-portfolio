#!/usr/bin/env python3
"""
H3C SR88 CMD IP 地址操作封装
"""

from atoms.base import CmdAtom
from atoms.utils import get_cli_interface, get_cli_interface_ip, check_cli_dependencies


class CmdIPAddressAtom(CmdAtom):
    """
    CMD IP 地址操作封装
    正向模板：product_lines/h3c/SR88/cmd/ip_address_cmd.j2
    回退模板：product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2
    """

    DEPLOY_TEMPLATE = "product_lines/h3c/SR88/cmd/ip_address_cmd.j2"
    ROLLBACK_TEMPLATE = "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2"

    # ===== 对外接口 =====

    def pre_check(self, host, ifname, ipv4_address, ipv4_mask, **kwargs):
        return self._pre_check_impl(host, ifname, ipv4_address, ipv4_mask, **kwargs)

    def deploy(self, host, snapshot, net_connect, **kwargs):
        return self._deploy_impl(host, snapshot, net_connect, **kwargs)

    def post_check(self, host, snapshot, desired, **kwargs):
        return self._post_check_impl(host, snapshot, desired, **kwargs)

    def rollback(self, host, snapshot, net_connect, **kwargs):
        return self._rollback_impl(host, snapshot, net_connect, **kwargs)

    # ===== 内部实现 =====

    def _pre_check_impl(self, host, ifname, ipv4_address, ipv4_mask, **kwargs):
        current = get_cli_interface(host, ifname)
        if not current:
            return {
                "status": "blocked",
                "snapshot": None,
                "reason": f"接口 {ifname} 不存在"
            }

        current_ip = get_cli_interface_ip(host, ifname)
        if current_ip == ipv4_address:
            return {
                "status": "skipped",
                "snapshot": None,
                "reason": f"IP {ipv4_address} 已存在"
            }

        return {
            "status": "ready",
            "snapshot": {
                "ifname": ifname,
                "ipv4_address": ipv4_address,
                "ipv4_mask": ipv4_mask,
                "current_ip": current_ip,
            },
            "reason": None,
        }

    def _deploy_impl(self, host, snapshot, net_connect, **kwargs):
        return self._render_and_send(
            host, self.DEPLOY_TEMPLATE,
            {
                "ifname": snapshot["ifname"],
                "ipv4_address": snapshot["ipv4_address"],
                "ipv4_mask": snapshot["ipv4_mask"],
            },
            net_connect
        )

    def _post_check_impl(self, host, snapshot, desired, **kwargs):
        ip_current = get_cli_interface_ip(host, snapshot["ifname"])
        if ip_current != snapshot["ipv4_address"]:
            return {
                "status": "failed",
                "actual": {"ip": ip_current},
                "detail": f"IP 不匹配: 期望 {snapshot['ipv4_address']}, 实际 {ip_current}"
            }
        return {"status": "success", "actual": {"ip": ip_current}, "detail": None}

    def _rollback_impl(self, host, snapshot, net_connect, **kwargs):
        if snapshot.get("current_ip"):
            # 恢复到原 IP
            return self._render_and_send(
                host, self.DEPLOY_TEMPLATE,
                {
                    "ifname": snapshot["ifname"],
                    "ipv4_address": snapshot["current_ip"],
                    "ipv4_mask": snapshot["ipv4_mask"],
                },
                net_connect
            )
        else:
            # 删除 IP
            return self._render_and_send(
                host, self.ROLLBACK_TEMPLATE,
                {"ifname": snapshot["ifname"], "ipv4_address": snapshot["ipv4_address"]},
                net_connect
            )