//! NETCONF 通道
//!
//! 对应 Python 版 `atoms/utils.py`（会话建立、ifindex / IP 查询）
//! 与 `scenes/*.py` 中的 `edit_config` + `commit` 调用。
//!
//! ## 与 Python 版的关键语义差异
//!
//! Python 版 ncclient 的 `edit_config(config=...)` 通过
//! `validated_element(config, ("config", qualify("config")))` **要求根元素必须是
//! `config`**，所以 `scenes/*.py` 手动拼 `<config>{frag}</config>` 才是正确用法，
//! 而 `atoms/base.py::NetconfAtom._edit_config()` 传裸片段的那条分支一旦被调用
//! 就会抛 `XMLError`（当前因该 Atom 从未被实例化而未暴露）。
//!
//! `rustnetconf` 的约定相反：`edit_config` 内部执行 `vendor_profile.wrap_config()`，
//! **调用方必须传裸片段**。本模块统一按裸片段处理，并在 `edit_config_fragment()`
//! 里主动剥除误传的 `<config>` 外壳，从架构上消除这个歧义。

use std::time::Duration;

use cwa_inventory::Host;
use rustnetconf::transport::ssh::HostKeyVerification;
use rustnetconf::types::{Datastore, ErrorOption};
use rustnetconf::Client;

use crate::error::TransportError;

/// H3C 私有 NETCONF 命名空间。
pub const H3C_DATA_NS: &str = "http://www.h3c.com/netconf/data:1.0";

/// NETCONF 会话封装。
pub struct NetconfSession {
    client: Client,
    host_name: String,
}

impl NetconfSession {
    /// 建立会话。
    ///
    /// 端口与超时取自 `host.connection_options["ncclient"]`，缺省 830 / 60s，
    /// 与 Python 版 `get_netconf_session()` 一致。
    ///
    /// # 安全说明
    ///
    /// 主机密钥校验设为 `AcceptAll`，对应 Python 版的 `hostkey_verify=False`。
    /// 这在实验/开局场景下是必要的（设备密钥尚未分发），但**不适合生产环境**——
    /// 无法防御中间人攻击。生产部署应改用 `HostKeyVerification::Fingerprint`
    /// 或 known_hosts 策略。
    pub async fn connect(host: &Host) -> Result<Self, TransportError> {
        let addr = format!("{}:{}", host.hostname, host.netconf_port());
        tracing::debug!(host = %host.name, %addr, "建立 NETCONF 会话");

        let client = Client::connect(&addr)
            .username(&host.username)
            .password(&host.password)
            .host_key_verification(HostKeyVerification::AcceptAll)
            .rpc_timeout(Duration::from_secs(host.netconf_timeout()))
            // H3C 无专属 vendor profile，回落 GenericVendor。
            // 项目所有 XML 均在模板里写全了 xmlns，不依赖 profile 注入命名空间。
            .gather_facts(false)
            .connect()
            .await
            .map_err(|e| TransportError::NetconfConnect(host.name.clone(), e.to_string()))?;

        Ok(Self {
            client,
            host_name: host.name.clone(),
        })
    }

    /// 下发配置片段到 candidate。
    ///
    /// `fragment` 应为**不带 `<config>` 外壳**的片段。若误传了外壳，会被自动剥除。
    pub async fn edit_config_fragment(
        &mut self,
        fragment: &str,
        error_option: Option<ErrorOption>,
    ) -> Result<(), TransportError> {
        let payload = strip_config_wrapper(fragment);

        let mut builder = self
            .client
            .edit_config(Datastore::Candidate)
            .config(payload);
        if let Some(opt) = error_option {
            builder = builder.error_option(opt);
        }
        builder
            .send()
            .await
            .map_err(|e| TransportError::EditConfig(self.host_name.clone(), e.to_string()))
    }

    /// 提交 candidate。
    pub async fn commit(&mut self) -> Result<(), TransportError> {
        self.client
            .commit()
            .await
            .map_err(|e| TransportError::Commit(self.host_name.clone(), e.to_string()))
    }

    /// 丢弃 candidate 中未提交的改动。
    ///
    /// Python 版没有调用过 `discard_changes` —— 一旦 `edit_config` 成功但
    /// `commit` 失败，candidate 会残留脏数据，影响同一会话后续操作。
    /// 这里补上，供场景层在失败路径调用。
    pub async fn discard_changes(&mut self) -> Result<(), TransportError> {
        self.client
            .discard_changes()
            .await
            .map_err(|e| TransportError::Netconf(self.host_name.clone(), e.to_string()))
    }

    /// candidate 是否有未提交改动。
    pub fn candidate_dirty(&self) -> bool {
        self.client.candidate_dirty()
    }

    /// 下发 + 提交，失败时自动 discard，避免脏 candidate 残留。
    pub async fn edit_and_commit(
        &mut self,
        fragment: &str,
        error_option: Option<ErrorOption>,
    ) -> Result<(), TransportError> {
        if let Err(e) = self.edit_config_fragment(fragment, error_option).await {
            let _ = self.discard_changes().await;
            return Err(e);
        }
        if let Err(e) = self.commit().await {
            let _ = self.discard_changes().await;
            return Err(e);
        }
        Ok(())
    }

    /// 带 subtree filter 的 `<get>`。
    pub async fn get(&mut self, filter: Option<&str>) -> Result<String, TransportError> {
        self.client
            .get(filter)
            .await
            .map_err(|e| TransportError::Netconf(self.host_name.clone(), e.to_string()))
    }

    /// 查询接口名 → ifindex 映射。
    ///
    /// 对应 Python 版 `get_ifindex_map()`，但**不再把响应 XML 写盘**——
    /// Python 版每次查询都覆盖写 `logs/ifindex_<device>.xml`，属调试产物混入
    /// 业务目录（`need_to_discuss_prob.md` 新发现③）。需要留痕时由调用方决定。
    pub async fn ifindex_map(&mut self) -> Result<Vec<(String, i64)>, TransportError> {
        let filter = r#"<top xmlns="http://www.h3c.com/netconf/data:1.0">
    <Ifmgr>
        <Interfaces>
            <Interface>
                <IfIndex/>
                <Name/>
            </Interface>
        </Interfaces>
    </Ifmgr>
</top>"#;

        let reply = self.get(Some(filter)).await?;
        parse_ifindex_map(&reply).map_err(|e| TransportError::Parse(self.host_name.clone(), e))
    }

    /// 查询指定 ifindex 的 IPv4 地址。对应 Python 版 `get_netconf_ip()`。
    pub async fn interface_ip(&mut self, ifindex: i64) -> Result<Option<String>, TransportError> {
        let filter = format!(
            r#"<top xmlns="http://www.h3c.com/netconf/data:1.0">
    <IPV4ADDRESS>
        <Ipv4Addresses>
            <Ipv4Address>
                <IfIndex>{ifindex}</IfIndex>
                <Ipv4Address/>
                <Ipv4Mask/>
            </Ipv4Address>
        </Ipv4Addresses>
    </IPV4ADDRESS>
</top>"#
        );

        let reply = self.get(Some(&filter)).await?;
        parse_interface_ip(&reply, ifindex)
            .map_err(|e| TransportError::Parse(self.host_name.clone(), e))
    }

    /// 关闭会话。
    pub async fn close(mut self) -> Result<(), TransportError> {
        self.client
            .close_session()
            .await
            .map_err(|e| TransportError::Netconf(self.host_name.clone(), e.to_string()))
    }
}

/// 剥除误传的 `<config>` 外壳。
///
/// `rustnetconf` 会自行包裹，重复包裹会导致 `<config><config>...` 嵌套。
fn strip_config_wrapper(fragment: &str) -> &str {
    let t = fragment.trim();
    let Some(rest) = t.strip_prefix("<config>") else {
        return t;
    };
    match rest.strip_suffix("</config>") {
        Some(inner) => {
            tracing::warn!("edit_config 收到带 <config> 外壳的片段，已自动剥除");
            inner.trim()
        }
        None => t,
    }
}

/// 剥除 UTF-8 BOM，对应 Python 版 `_safe_parse_xml()` 的处理。
fn strip_bom(s: &str) -> &str {
    s.strip_prefix('\u{feff}').unwrap_or(s)
}

/// 解析 ifindex 映射。
pub fn parse_ifindex_map(xml: &str) -> Result<Vec<(String, i64)>, String> {
    let clean = strip_bom(xml);
    let doc = roxmltree::Document::parse(clean).map_err(|e| format!("XML 解析失败: {e}"))?;

    let mut out = Vec::new();
    for node in doc.descendants().filter(|n| {
        n.is_element()
            && n.tag_name().name() == "Interface"
            && n.tag_name().namespace() == Some(H3C_DATA_NS)
    }) {
        let idx = child_text(node, "IfIndex").and_then(|t| t.trim().parse::<i64>().ok());
        let name = child_text(node, "Name").map(|t| t.trim().to_string());
        if let (Some(idx), Some(name)) = (idx, name) {
            out.push((name, idx));
        }
    }
    Ok(out)
}

/// 解析指定 ifindex 的 IPv4 地址。
pub fn parse_interface_ip(xml: &str, ifindex: i64) -> Result<Option<String>, String> {
    let clean = strip_bom(xml);
    let doc = roxmltree::Document::parse(clean).map_err(|e| format!("XML 解析失败: {e}"))?;

    for node in doc.descendants().filter(|n| {
        n.is_element()
            && n.tag_name().name() == "Ipv4Address"
            && n.tag_name().namespace() == Some(H3C_DATA_NS)
            // 排除同名叶子节点，只取容器（容器必有 IfIndex 子元素）
            && n.children().any(|c| c.is_element() && c.tag_name().name() == "IfIndex")
    }) {
        let this_idx = child_text(node, "IfIndex").and_then(|t| t.trim().parse::<i64>().ok());
        if this_idx != Some(ifindex) {
            continue;
        }
        if let Some(ip) = child_text(node, "Ipv4Address") {
            let ip = ip.trim();
            if !ip.is_empty() {
                return Ok(Some(ip.to_string()));
            }
        }
    }
    Ok(None)
}

fn child_text<'a>(node: roxmltree::Node<'a, 'a>, name: &str) -> Option<&'a str> {
    node.children()
        .find(|c| c.is_element() && c.tag_name().name() == name)
        .and_then(|c| c.text())
}
