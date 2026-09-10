#!/usr/bin/env python3
"""
H3C SR88 NETCONF IP 地址原子操作
在执行配置下发前，会检查接口是否存在以及 IP 是否已存在，若已存在则自动跳过（幂等），
否则记录当前状态作为快照；配置下发时通过标准 NETCONF 会话创建 IP；下发完成后会进行校验，确保配置生效。回退逻辑尤为关键——快照中保存了变更前的原始 IP，
回退时会判断：如果之前存在 IP，则恢复原 IP，如果之前不存在，则执行删除操作。此外，还内置了依赖检查，当检测到其他服务（如路由协议）依赖该 IP 时，会阻止回退以避免业务影响，
实现了配置变更的精准可控与安全守护。
"""

from atoms.base import NetconfAtom
from atoms.utils import get_netconf_interfaces, get_netconf_ip, get_interface_dependencies


class NetconfIPAddressAtom(NetconfAtom):
    """
    NETCONF IP 地址原子操作
    模板：product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2
    """
    
    TEMPLATE = "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2"
    
    # ===== 对外接口 =====
    
    def pre_check(self, host, ifindex, ipv4_address, ipv4_mask, **kwargs):
        return self._pre_check_impl(host, ifindex, ipv4_address, ipv4_mask, **kwargs)
    
    def deploy(self, host, snapshot, session, **kwargs):
        return self._deploy_impl(host, snapshot, session, **kwargs)
    
    def post_check(self, host, snapshot, desired, **kwargs):
        return self._post_check_impl(host, snapshot, desired, **kwargs)
    
    def rollback(self, host, snapshot, session, **kwargs):
        rollback_check = self._rollback_pre_check_impl(host, snapshot, session, **kwargs)
        if rollback_check["status"] == "blocked":
            return {"status": "blocked", "reason": rollback_check.get("reason")}
        return self._rollback_impl(host, snapshot, session, **kwargs)
    
    # ===== 内部实现 =====
    
    def _pre_check_impl(self, host, ifindex, ipv4_address, ipv4_mask, **kwargs):
        current = get_netconf_interfaces(host)
        if str(ifindex) not in current:
            return {
                "status": "blocked",
                "snapshot": None,
                "reason": f"接口 ifindex {ifindex} 不存在"
            }
        
        current_ip = get_netconf_ip(host, ifindex)
        if current_ip == ipv4_address:
            return {
                "status": "skipped",
                "snapshot": None,
                "reason": f"IP {ipv4_address} 已存在"
            }
        
        return {
            "status": "ready",
            "snapshot": {
                "ifindex": ifindex,
                "ipv4_address": ipv4_address,
                "ipv4_mask": ipv4_mask,
                "current_ip": current_ip,
            },
            "reason": None,
        }
    
    def _deploy_impl(self, host, snapshot, session, **kwargs):
        return self._edit_config(
            host, self.TEMPLATE,
            {
                "ifindex": snapshot["ifindex"],
                "ipv4_address": snapshot["ipv4_address"],
                "ipv4_mask": snapshot["ipv4_mask"],
                "operation": "create",
            },
            session
        )
    
    def _post_check_impl(self, host, snapshot, desired, **kwargs):
        ip_current = get_netconf_ip(host, snapshot["ifindex"])
        if ip_current != snapshot["ipv4_address"]:
            return {
                "status": "failed",
                "actual": {"ip": ip_current},
                "detail": f"IP 不匹配: 期望 {snapshot['ipv4_address']}, 实际 {ip_current}"
            }
        return {"status": "success", "actual": {"ip": ip_current}, "detail": None}
    
    def _rollback_pre_check_impl(self, host, snapshot, session, **kwargs):
        deps = get_interface_dependencies(host, snapshot["ifindex"], session)
        if deps:
            return {
                "status": "blocked",
                "dependencies": deps,
                "reason": f"IP 被依赖，无法回退: {deps}"
            }
        return {"status": "safe", "dependencies": [], "reason": None}
    
    def _rollback_impl(self, host, snapshot, session, **kwargs):
        # 恢复到变更前的 IP（可能为 None）
        if snapshot.get("current_ip"):
            return self._edit_config(
                host, self.TEMPLATE,
                {
                    "ifindex": snapshot["ifindex"],
                    "ipv4_address": snapshot["current_ip"],
                    "ipv4_mask": snapshot["ipv4_mask"],
                    "operation": "create",
                },
                session
            )
        else:
            # 原来没有 IP，删除
            return self._edit_config(
                host, self.TEMPLATE,
                {
                    "ifindex": snapshot["ifindex"],
                    "ipv4_address": snapshot["ipv4_address"],
                    "ipv4_mask": snapshot["ipv4_mask"],
                    "operation": "delete",
                },
                session
            )