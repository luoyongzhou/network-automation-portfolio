#!/usr/bin/env python3
"""
原子操作工具函数
"""

import os
from pathlib import Path
from ncclient import manager
from lxml import etree
from typing import Dict, Any, List, Optional


def get_netconf_session(host):
    """获取 NETCONF 会话"""
    return manager.connect(
        host=host.hostname,
        port=830,
        username=host.username,
        password=host.password,
        hostkey_verify=False,
        device_params={"name": "hpcomware"},
        timeout=60,
    )


def _safe_parse_xml(xml_str: str):
    """安全解析 XML 字符串"""
    if xml_str.startswith('\ufeff'):
        xml_str = xml_str[1:]
    return etree.fromstring(xml_str.encode('utf-8'))


def get_ifindex_map(host, session=None) -> Dict[str, int]:
    """
    通过 NETCONF 获取设备的接口名 → ifindex 映射
    使用命名空间前缀，避免 XPath 解析问题
    """
    filter_xml = """
    <top xmlns="http://www.h3c.com/netconf/data:1.0">
        <Ifmgr>
            <Interfaces>
                <Interface>
                    <IfIndex/>
                    <Name/>
                </Interface>
            </Interfaces>
        </Ifmgr>
    </top>
    """
    if session is None:
        with get_netconf_session(host) as sess:
            reply = sess.get(filter=('subtree', filter_xml))
    else:
        reply = session.get(filter=('subtree', filter_xml))

    # 保存 XML 以便调试
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    xml_file = logs_dir / f"ifindex_{host.name}.xml"
    with open(xml_file, "w", encoding="utf-8") as f:
        f.write(reply.xml)
    print(f"📁 ifindex XML 已保存: {xml_file}")

    root = _safe_parse_xml(reply.xml)

    # 注册命名空间
    ns = {'h3c': 'http://www.h3c.com/netconf/data:1.0'}

    # 使用命名空间前缀进行 XPath
    ifindex_map = {}
    for iface in root.xpath('//h3c:Ifmgr/h3c:Interfaces/h3c:Interface', namespaces=ns):
        ifindex_elem = iface.find('h3c:IfIndex', namespaces=ns)
        name_elem = iface.find('h3c:Name', namespaces=ns)
        if ifindex_elem is not None and name_elem is not None:
            ifindex_map[name_elem.text] = int(ifindex_elem.text)

    print(f"🔍 解析到 {len(ifindex_map)} 个接口")
    return ifindex_map


def get_netconf_interfaces(host, session=None) -> Dict[str, Dict]:
    """通过 NETCONF 获取所有接口信息（简化版）"""
    if session is None:
        with get_netconf_session(host) as sess:
            reply = sess.get()
    else:
        reply = session.get()

    root = _safe_parse_xml(reply.xml)
    ns = {'h3c': 'http://www.h3c.com/netconf/data:1.0'}
    interfaces = {}
    for iface in root.xpath('//h3c:Ifmgr/h3c:Interfaces/h3c:Interface', namespaces=ns):
        ifindex_elem = iface.find('h3c:IfIndex', namespaces=ns)
        iftype_elem = iface.find('h3c:Type', namespaces=ns)
        desc_elem = iface.find('h3c:Description', namespaces=ns)
        if ifindex_elem is not None and iftype_elem is not None:
            interfaces[ifindex_elem.text] = {
                "type": iftype_elem.text,
                "description": desc_elem.text if desc_elem is not None else "",
            }
    return interfaces


def get_netconf_ip(host, ifindex, session=None) -> Optional[str]:
    """通过 NETCONF 获取接口 IP"""
    if session is None:
        with get_netconf_session(host) as sess:
            reply = sess.get()
    else:
        reply = session.get()

    root = _safe_parse_xml(reply.xml)
    ns = {"h3c": "http://www.h3c.com/netconf/data:1.0"}
    xpath = f"//h3c:IPV4ADDRESS/h3c:Ipv4Addresses/h3c:Ipv4Address[h3c:IfIndex='{ifindex}']/h3c:Ipv4Address"
    ip_node = root.xpath(xpath, namespaces=ns)
    if ip_node:
        return ip_node[0].text
    return None


def get_interface_dependencies(host, ifindex, session=None) -> List[str]:
    return []


def get_cli_interface(host, ifname) -> Optional[Dict]:
    return None


def get_cli_interface_ip(host, ifname) -> Optional[str]:
    return None


def check_cli_dependencies(host, ifname) -> List[str]:
    return []