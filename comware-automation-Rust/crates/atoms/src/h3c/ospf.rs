//! H3C OSPF 原子操作（NETCONF）
//!
//! **这是 Python 版没有的东西。**
//!
//! Python 版的 OSPF 逻辑是直接手写在 `scenes/test_ospf_deploy_full.py` 的
//! `task_deploy_ospf()` / `task_rollback_ospf()` 函数体内，没有走 Atom 抽象
//! （`need_to_discuss_prob.md` 延伸①），因此缺少：
//!   - `pre_check` 幂等判断（不会告诉你"其实什么都不用做"）
//!   - 回退前依赖检查
//!   - 与 IP 场景一致的快照语义（OSPF 存的是"期望配置全量"而非"变更前状态"）
//!
//! 本模块把 OSPF 纳入同一套 Atom 抽象，并统一快照语义为"变更前状态"。
//!
//! 回退的 XML 构造遵循 `scenes/summary.md` 第七章的结论：
//! **删除操作只指定索引列**，且按 接口 → 区域 → 进程 的顺序逐层删除。

use cwa_inventory::Host;
use cwa_templating::{paths, SceneApi};
use cwa_transport::{ErrorOption, NetconfSession};
use serde::{Deserialize, Serialize};

use crate::base::{Atom, PreCheckOutcome, RollbackAction, RollbackGuard, RollbackPlan};
use crate::error::AtomError;

/// OSPF 接口配置。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OspfInterface {
    pub ifindex: i64,
    /// 网络类型。3 = P2P；缺省为设备默认（broadcast）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub network_type: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cost: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub priority: Option<u32>,
}

/// OSPF 区域配置。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OspfArea {
    pub area_id: String,
    pub area_type: u32,
    pub interfaces: Vec<OspfInterface>,
}

/// OSPF 完整配置。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OspfConfig {
    pub instance_name: String,
    pub router_id: String,
    pub areas: Vec<OspfArea>,
}

/// 调用参数。
#[derive(Debug, Clone)]
pub struct OspfParams {
    pub config: OspfConfig,
}

/// 变更前状态。
///
/// 与 Python 版不同：这里记录的是**变更前设备上是否已有该 OSPF 进程**，
/// 以及本次实际下发了什么（用于推导回退步骤），而不是把期望配置当快照。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OspfSnapshot {
    /// 变更前该 OSPF 进程是否已存在。
    pub instance_existed_before: bool,
    /// 本次下发的配置，回退时据此推导删除步骤。
    pub applied: OspfConfig,
}

/// 期望状态。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OspfDesired {
    pub config: OspfConfig,
}

/// NETCONF OSPF 原子操作。
pub struct NetconfOspfAtom<'a> {
    scene: &'a SceneApi,
}

impl<'a> NetconfOspfAtom<'a> {
    pub fn new(scene: &'a SceneApi) -> Self {
        Self { scene }
    }

    /// 渲染 OSPF 配置片段。
    pub fn render_config(&self, host: &Host, cfg: &OspfConfig) -> Result<String, AtomError> {
        let ospf = serde_json::to_value(cfg)
            .map_err(|e| AtomError::Render(format!("OSPF 配置序列化失败: {e}")))?;
        let ctx = cwa_templating::ctx([("ospf", ospf)]);
        self.scene
            .direct_render(host, paths::NC_OSPF_XML, Some(&ctx))
            .map_err(|e| AtomError::Render(e.to_string()))
    }

    /// 构造回退删除 XML。
    ///
    /// 严格遵循 `summary.md` 第七章 v4 方案：只发索引列，逐层删除。
    /// 顺序必须是 接口 → 区域 → 进程，反之会因对象仍被引用而失败。
    fn build_delete_xml(cfg: &OspfConfig) -> String {
        const NC_ATTR: &str =
            r#"xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete""#;

        let mut s = String::new();
        s.push_str("<OSPF xmlns=\"http://www.h3c.com/netconf/config:1.0\">\n");

        // 1. 删除接口配置：索引列仅 IfIndex
        s.push_str("  <Interfaces>\n");
        for area in &cfg.areas {
            for iface in &area.interfaces {
                s.push_str(&format!("    <Interface {NC_ATTR}>\n"));
                s.push_str(&format!("      <IfIndex>{}</IfIndex>\n", iface.ifindex));
                s.push_str("    </Interface>\n");
            }
        }
        s.push_str("  </Interfaces>\n");

        // 2. 删除区域：索引列为 Name + AreaId
        s.push_str("  <Areas>\n");
        for area in &cfg.areas {
            s.push_str(&format!("    <Area {NC_ATTR}>\n"));
            s.push_str(&format!("      <Name>{}</Name>\n", cfg.instance_name));
            s.push_str(&format!("      <AreaId>{}</AreaId>\n", area.area_id));
            s.push_str("    </Area>\n");
        }
        s.push_str("  </Areas>\n");

        // 3. 删除进程：索引列为 Name
        s.push_str("  <Instances>\n");
        s.push_str(&format!("    <Instance {NC_ATTR}>\n"));
        s.push_str(&format!("      <Name>{}</Name>\n", cfg.instance_name));
        s.push_str("    </Instance>\n");
        s.push_str("  </Instances>\n");

        s.push_str("</OSPF>");
        s
    }

    /// 查询设备上现有的 OSPF 进程名。
    async fn query_instances(channel: &mut NetconfSession) -> Result<Vec<String>, AtomError> {
        let filter = r#"<top xmlns="http://www.h3c.com/netconf/data:1.0">
    <OSPF>
        <Instances>
            <Instance>
                <Name/>
            </Instance>
        </Instances>
    </OSPF>
</top>"#;

        let reply = channel
            .get(Some(filter))
            .await
            .map_err(AtomError::Transport)?;

        let doc = roxmltree_parse(&reply)?;
        let mut names = Vec::new();
        for node in doc.descendants().filter(|n| {
            n.is_element()
                && n.tag_name().name() == "Name"
                && n.parent()
                    .map(|p| p.tag_name().name() == "Instance")
                    .unwrap_or(false)
        }) {
            if let Some(t) = node.text() {
                names.push(t.trim().to_string());
            }
        }
        Ok(names)
    }
}

fn roxmltree_parse(xml: &str) -> Result<roxmltree::Document<'_>, AtomError> {
    let clean = xml.strip_prefix('\u{feff}').unwrap_or(xml);
    roxmltree::Document::parse(clean)
        .map_err(|e| AtomError::Internal(format!("OSPF 响应解析失败: {e}")))
}

impl<'a> Atom for NetconfOspfAtom<'a> {
    type Snapshot = OspfSnapshot;
    type Desired = OspfDesired;
    type Channel = NetconfSession;
    type Params = OspfParams;

    fn name(&self) -> &'static str {
        "netconf_ospf"
    }

    async fn pre_check(
        &self,
        _host: &Host,
        channel: &mut Self::Channel,
        params: &Self::Params,
    ) -> Result<PreCheckOutcome<Self::Snapshot, Self::Desired>, AtomError> {
        // 至少要有一个区域、且区域内至少有一个接口，否则下发无意义
        let total_ifaces: usize = params.config.areas.iter().map(|a| a.interfaces.len()).sum();
        if total_ifaces == 0 {
            return Ok(PreCheckOutcome::Blocked {
                reason: "OSPF 配置中没有任何接口，拒绝下发".into(),
            });
        }

        // 幂等判断：Python 版完全没有这一步
        let existing = Self::query_instances(channel).await.unwrap_or_default();
        let instance_existed = existing.contains(&params.config.instance_name);

        Ok(PreCheckOutcome::Ready {
            snapshot: OspfSnapshot {
                instance_existed_before: instance_existed,
                applied: params.config.clone(),
            },
            desired: OspfDesired {
                config: params.config.clone(),
            },
        })
    }

    async fn deploy(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        desired: &Self::Desired,
    ) -> Result<(), AtomError> {
        let frag = self.render_config(host, &desired.config)?;
        // rollback-on-error 保证单次 RPC 的原子性
        channel
            .edit_and_commit(&frag, Some(ErrorOption::RollbackOnError))
            .await
            .map_err(AtomError::Transport)
    }

    async fn post_check(
        &self,
        _host: &Host,
        channel: &mut Self::Channel,
        desired: &Self::Desired,
    ) -> Result<(), AtomError> {
        let existing = Self::query_instances(channel).await?;
        if !existing.contains(&desired.config.instance_name) {
            return Err(AtomError::PostCheck(format!(
                "OSPF 进程 {} 下发后未在设备上查询到",
                desired.config.instance_name
            )));
        }
        Ok(())
    }

    async fn rollback_guard(
        &self,
        _host: &Host,
        _channel: &mut Self::Channel,
        snapshot: &Self::Snapshot,
    ) -> Result<RollbackGuard, AtomError> {
        // 若该 OSPF 进程在本次变更前就已存在，说明不是本次创建的，
        // 贸然删除会影响既有业务 —— 阻止回退。
        //
        // Python 版没有这道检查，回退时会无条件删除整个进程。
        if snapshot.instance_existed_before {
            return Ok(RollbackGuard::Blocked {
                dependencies: vec![format!(
                    "OSPF 进程 {} 在本次变更前已存在，非本次创建，拒绝删除",
                    snapshot.applied.instance_name
                )],
            });
        }
        Ok(RollbackGuard::Safe)
    }

    fn plan_rollback(
        &self,
        _host: &Host,
        snapshot: &Self::Snapshot,
    ) -> Result<RollbackPlan, AtomError> {
        let xml = Self::build_delete_xml(&snapshot.applied);
        let iface_count: usize = snapshot
            .applied
            .areas
            .iter()
            .map(|a| a.interfaces.len())
            .sum();

        Ok(RollbackPlan::new().push(
            format!(
                "逐层删除 OSPF 进程 {}（{} 个接口、{} 个区域、1 个进程），仅发索引列",
                snapshot.applied.instance_name,
                iface_count,
                snapshot.applied.areas.len()
            ),
            RollbackAction::NetconfFragment {
                fragment: xml,
                // 容忍部分对象已不存在
                continue_on_error: true,
            },
        ))
    }

    async fn apply_rollback(
        &self,
        _host: &Host,
        channel: &mut Self::Channel,
        plan: &RollbackPlan,
    ) -> Result<(), AtomError> {
        for step in &plan.steps {
            match &step.action {
                RollbackAction::NetconfFragment {
                    fragment,
                    continue_on_error,
                } => {
                    let opt = if *continue_on_error {
                        Some(ErrorOption::ContinueOnError)
                    } else {
                        None
                    };
                    tracing::info!(step = %step.description, "执行 OSPF 回退");
                    channel
                        .edit_and_commit(fragment, opt)
                        .await
                        .map_err(AtomError::Transport)?;
                }
                RollbackAction::CliCommands(_) => {
                    return Err(AtomError::Internal(
                        "NETCONF 通道无法执行 CLI 回退步骤".into(),
                    ))
                }
            }
        }
        Ok(())
    }
}
