#!/usr/bin/env python3
"""
原子操作基类
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class Atom(ABC):
    """原子操作基类"""

    @abstractmethod
    def pre_check(self, host, **kwargs) -> Dict[str, Any]:
        pass

    @abstractmethod
    def deploy(self, host, snapshot, **kwargs) -> Dict[str, Any]:
        pass

    @abstractmethod
    def post_check(self, host, snapshot, desired, **kwargs) -> Dict[str, Any]:
        pass

    @abstractmethod
    def rollback(self, host, snapshot, **kwargs) -> Dict[str, Any]:
        pass

    def execute(self, host, auto_rollback: bool = False, **kwargs) -> Dict[str, Any]:
        pre_result = self.pre_check(host, **kwargs)
        if pre_result["status"] == "blocked":
            return {"status": "failed", "reason": pre_result.get("reason", "预检查未通过")}
        if pre_result["status"] == "skipped":
            return {"status": "skipped", "reason": pre_result.get("reason", "无需变更")}

        snapshot = pre_result.get("snapshot")
        if snapshot is None:
            return {"status": "failed", "reason": "预检查未返回快照"}

        deploy_result = self.deploy(host, snapshot, **kwargs)
        if deploy_result["status"] == "failed":
            if auto_rollback:
                rollback_result = self.rollback(host, snapshot, **kwargs)
                return {
                    "status": "rolled_back",
                    "reason": deploy_result.get("detail", "部署失败"),
                    "rollback_detail": rollback_result.get("detail", ""),
                }
            return {"status": "failed", "reason": deploy_result.get("detail", "部署失败")}

        post_result = self.post_check(host, snapshot, kwargs, **kwargs)
        if post_result["status"] == "failed":
            if auto_rollback:
                rollback_result = self.rollback(host, snapshot, **kwargs)
                return {
                    "status": "rolled_back",
                    "reason": post_result.get("detail", "后检查失败"),
                    "rollback_detail": rollback_result.get("detail", ""),
                }
            return {"status": "failed", "reason": post_result.get("detail", "后检查失败")}

        return {"status": "success", "snapshot": snapshot}


class CmdAtom(Atom):
    def __init__(self, scene_api):
        self.scene_api = scene_api

    def _render_and_send(self, host, template_name, context, net_connect, expect_string=r"\[.*?\]"):
        config_text, error = self.scene_api.custom(host, template_name, context_override=context)
        if error:
            return {"status": "failed", "detail": error}
        net_connect.send_command(config_text, expect_string=expect_string)
        return {"status": "success", "detail": None}


class NetconfAtom(Atom):
    def __init__(self, scene_api):
        self.scene_api = scene_api

    def _edit_config(self, host, template_name, context, session):
        """
        渲染 XML 并下发到 candidate
        注意：edit_config 的 config 参数不能包含外层 <config> 标签
        """
        xml_frag, error = self.scene_api.custom(host, template_name, context_override=context)
        if error:
            return {"status": "failed", "detail": error}
        # 直接传递 xml_frag，不要包裹 <config>
        session.edit_config(xml_frag, target="candidate")
        return {"status": "success", "detail": None}