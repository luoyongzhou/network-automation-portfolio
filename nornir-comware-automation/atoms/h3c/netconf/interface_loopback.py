from atoms.base import NetconfAtom
from atoms.utils import get_ifindex_map, get_netconf_ip


class NetconfLoopbackAtom(NetconfAtom):
    INTERFACE_TEMPLATE = "product_lines/h3c/SR88/netconf/_fragments/interface_atom.j2"
    IP_TEMPLATE = "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2"

    def deploy_pre_check(self, host, ifindex, description=None, ipv4_address=None, ipv4_mask=None, **kwargs):
        # 1. 检查 Loopback 是否已存在
        ifindex_map = get_ifindex_map(host)
        if f"LoopBack{ifindex}" in ifindex_map:
            return {"status": "blocked", "snapshot": None, "reason": f"LoopBack{ifindex} already exists"}
        return {"status": "ready", "snapshot": {"ifindex": ifindex, "description": description, "ipv4_address": ipv4_address, "ipv4_mask": ipv4_mask}}

    def deploy(self, host, snapshot, session, **kwargs):
        # 1. 创建 Loopback
        result = self._edit_config(host, self.INTERFACE_TEMPLATE, {"ifindex": snapshot["ifindex"], "description": snapshot["description"], "operation": "create"}, session)
        if result["status"] == "failed":
            return result

        # 2. 获取真实 ifindex
        ifindex_map = get_ifindex_map(host, session)
        real_ifindex = ifindex_map.get(f"LoopBack{snapshot['ifindex']}")
        if real_ifindex is None:
            return {"status": "failed", "detail": f"Failed to get real ifindex for LoopBack{snapshot['ifindex']}"}

        # 3. 配置 IP（使用真实 ifindex）
        if snapshot["ipv4_address"] and snapshot["ipv4_mask"]:
            result = self._edit_config(host, self.IP_TEMPLATE, {"ifindex": real_ifindex, "ipv4_address": snapshot["ipv4_address"], "ipv4_mask": snapshot["ipv4_mask"], "operation": "create"}, session)
        return result