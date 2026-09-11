//! 场景 API
//!
//! 对应 Python 版 `scripts/preview_bootstrap.py` 的 `SceneAPI`。
//! 为各业务场景提供命名入口，隐藏模板路径与解析器选择。

use cwa_inventory::Host;
use serde_json::{Map as JsonMap, Value as JsonValue};

use crate::error::TemplateError;
use crate::renderer::TemplateRenderer;
use crate::resolver::{h3c_resolver, DirectPathResolver, LayeredPathResolver, PathResolver};

pub struct SceneApi {
    renderer: TemplateRenderer,
    layered: LayeredPathResolver,
    direct: DirectPathResolver,
}

impl SceneApi {
    pub fn new(renderer: TemplateRenderer) -> Self {
        Self {
            renderer,
            layered: h3c_resolver(),
            direct: DirectPathResolver,
        }
    }

    pub fn renderer(&self) -> &TemplateRenderer {
        &self.renderer
    }

    // ===== 阶段一：Console 开局 =====

    /// 开局配置（`cmd/bootstrap.j2`，走分层查找）。
    pub fn bootstrap(&self, host: &Host) -> Result<String, TemplateError> {
        self.layered_render(host, "cmd/bootstrap.j2", None)
    }

    // ===== 阶段二：SSH 使能 NETCONF =====

    /// NETCONF 初始化命令（`cmd/netconf_cmd.j2`，走分层查找）。
    pub fn ssh_bootstrap(&self, host: &Host) -> Result<String, TemplateError> {
        self.layered_render(host, "cmd/netconf_cmd.j2", None)
    }

    // ===== 通用入口 =====

    /// 按分层继承链查找并渲染。
    pub fn layered_render(
        &self,
        host: &Host,
        template_name: &str,
        context: Option<&JsonMap<String, JsonValue>>,
    ) -> Result<String, TemplateError> {
        self.renderer
            .render(host, template_name, &self.layered, context)
    }

    /// 按给定路径直接渲染，不做分层查找。
    pub fn direct_render(
        &self,
        host: &Host,
        template_path: &str,
        context: Option<&JsonMap<String, JsonValue>>,
    ) -> Result<String, TemplateError> {
        self.renderer
            .render(host, template_path, &self.direct, context)
    }

    /// 自定义解析器渲染。
    pub fn render_with(
        &self,
        host: &Host,
        template_name: &str,
        resolver: &dyn PathResolver,
        context: Option<&JsonMap<String, JsonValue>>,
    ) -> Result<String, TemplateError> {
        self.renderer.render(host, template_name, resolver, context)
    }
}

/// 模板路径常量，集中管理，避免像 Python 版那样散落在各 Atom 类属性里。
pub mod paths {
    // —— CLI ——
    pub const CMD_INTERFACE_CREATE: &str = "product_lines/h3c/SR88/cmd/interface_create_cmd.j2";
    pub const CMD_INTERFACE_ROLLBACK: &str =
        "product_lines/h3c/SR88/cmd/interface_create_rollback_cmd.j2";
    pub const CMD_IP_ADDRESS: &str = "product_lines/h3c/SR88/cmd/ip_address_cmd.j2";
    pub const CMD_IP_ROLLBACK: &str = "product_lines/h3c/SR88/cmd/ip_address_rollback_cmd.j2";

    // —— NETCONF ——
    pub const NC_IP_ADDRESS_ATOM: &str =
        "product_lines/h3c/SR88/netconf/_fragments/ip_address_atom.j2";
    pub const NC_INTERFACE_ATOM: &str =
        "product_lines/h3c/SR88/netconf/_fragments/interface_atom.j2";
    pub const NC_OSPF_XML: &str = "product_lines/h3c/SR88/netconf/ospf_xml.j2";
}
