//! H3C 接口 IP 地址原子操作（NETCONF）
//!
//! 对应 Python 版 `atoms/h3c/netconf/interface_ip_address.py`。
//!
//! 混合路径说明：Comware 的 NETCONF 不支持直接创建 Loopback 接口，
//! 所以接口本身用 CLI 创建、IP 用 NETCONF 配置。本 Atom 只负责 IP 部分，
//! 接口创建由 [`super::loopback::CmdLoopbackAtom`] 负责。

use cwa_inventory::Host;
use cwa_templating::{paths, SceneApi};
use cwa_transport::{ErrorOption, NetconfSession};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::base::{Atom, PreCheckOutcome, RollbackAction, RollbackGuard, RollbackPlan};
use crate::error::AtomError;

/// 调用参数。
#[derive(Debug, Clone)]
pub struct IpAddressParams {
    pub ifindex: i64,
    pub ipv4_address: String,
    pub ipv4_mask: String,
}

/// 变更前状态。**只记录原始状态**，不混入期望值。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IpAddressSnapshot {
    pub ifindex: i64,
    /// 变更前该接口的 IP，`None` 表示原本没有配置。
    pub previous_ip: Option<String>,
    /// 本次下发的 IP，回退时需据此删除。
    pub applied_ip: String,
    pub applied_mask: String,
}

/// 期望状态。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IpAddressDesired {
    pub ifindex: i64,
    pub ipv4_address: String,
    pub ipv4_mask: String,
}

/// NETCONF IP 地址原子操作。
pub struct NetconfIpAddressAtom<'a> {
    scene: &'a SceneApi,
}

impl<'a> NetconfIpAddressAtom<'a> {
    pub fn new(scene: &'a SceneApi) -> Self {
        Self { scene }
    }

    /// 渲染 IP 配置片段。
    fn render(
        &self,
        host: &Host,
        ifindex: i64,
        ip: &str,
        mask: &str,
        operation: &str,
    ) -> Result<String, AtomError> {
        let ctx = cwa_templating::ctx([
            ("ifindex", json!(ifindex)),
            ("ipv4_address", json!(ip)),
            ("ipv4_mask", json!(mask)),
            ("operation", json!(operation)),
        ]);
        self.scene
            .direct_render(host, paths::NC_IP_ADDRESS_ATOM, Some(&ctx))
            .map_err(|e| AtomError::Render(e.to_string()))
    }
}

impl<'a> Atom for NetconfIpAddressAtom<'a> {
    type Snapshot = IpAddressSnapshot;
    type Desired = IpAddressDesired;
    type Channel = NetconfSession;
    type Params = IpAddressParams;

    fn name(&self) -> &'static str {
        "netconf_ip_address"
    }

    async fn pre_check(
        &self,
        _host: &Host,
        channel: &mut Self::Channel,
        params: &Self::Params,
    ) -> Result<PreCheckOutcome<Self::Snapshot, Self::Desired>, AtomError> {
        // 接口存在性：ifindex 必须出现在设备的接口列表里
        let map = channel.ifindex_map().await.map_err(AtomError::Transport)?;
        if !map.iter().any(|(_, idx)| *idx == params.ifindex) {
            return Ok(PreCheckOutcome::Blocked {
                reason: format!("接口 ifindex {} 不存在", params.ifindex),
            });
        }

        let current = channel
            .interface_ip(params.ifindex)
            .await
            .map_err(AtomError::Transport)?;

        // 幂等：已是目标 IP 则跳过
        if current.as_deref() == Some(params.ipv4_address.as_str()) {
            return Ok(PreCheckOutcome::Skipped {
                reason: format!("IP {} 已存在", params.ipv4_address),
            });
        }

        Ok(PreCheckOutcome::Ready {
            snapshot: IpAddressSnapshot {
                ifindex: params.ifindex,
                previous_ip: current,
                applied_ip: params.ipv4_address.clone(),
                applied_mask: params.ipv4_mask.clone(),
            },
            desired: IpAddressDesired {
                ifindex: params.ifindex,
                ipv4_address: params.ipv4_address.clone(),
                ipv4_mask: params.ipv4_mask.clone(),
            },
        })
    }

    async fn deploy(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        desired: &Self::Desired,
    ) -> Result<(), AtomError> {
        let frag = self.render(
            host,
            desired.ifindex,
            &desired.ipv4_address,
            &desired.ipv4_mask,
            "merge",
        )?;
        channel
            .edit_and_commit(&frag, None)
            .await
            .map_err(AtomError::Transport)
    }

    async fn post_check(
        &self,
        _host: &Host,
        channel: &mut Self::Channel,
        desired: &Self::Desired,
    ) -> Result<(), AtomError> {
        let actual = channel
            .interface_ip(desired.ifindex)
            .await
            .map_err(AtomError::Transport)?;

        if actual.as_deref() != Some(desired.ipv4_address.as_str()) {
            return Err(AtomError::PostCheck(format!(
                "IP 不匹配: 期望 {}, 实际 {:?}",
                desired.ipv4_address, actual
            )));
        }
        Ok(())
    }

    async fn rollback_guard(
        &self,
        _host: &Host,
        _channel: &mut Self::Channel,
        _snapshot: &Self::Snapshot,
    ) -> Result<RollbackGuard, AtomError> {
        // Python 版 `get_interface_dependencies()` 是 `return []` 空实现，
        // 即依赖检查从未真正生效。此处保持同样的行为（始终放行），
        // 但把"未实现"这一事实显式记录下来，而不是伪装成已检查。
        tracing::debug!("IP 回退依赖检查尚未实现（Python 版同为空实现），当前始终放行");
        Ok(RollbackGuard::Safe)
    }

    fn plan_rollback(
        &self,
        host: &Host,
        snapshot: &Self::Snapshot,
    ) -> Result<RollbackPlan, AtomError> {
        let plan = match &snapshot.previous_ip {
            // 原本有 IP：恢复原值
            Some(prev) => {
                let frag = self.render(
                    host,
                    snapshot.ifindex,
                    prev,
                    &snapshot.applied_mask,
                    "merge",
                )?;
                RollbackPlan::new().push(
                    format!("恢复 ifindex {} 的原 IP {}", snapshot.ifindex, prev),
                    RollbackAction::NetconfFragment {
                        fragment: frag,
                        continue_on_error: false,
                    },
                )
            }
            // 原本没有 IP：删除本次下发的
            None => {
                let frag = self.render(
                    host,
                    snapshot.ifindex,
                    &snapshot.applied_ip,
                    &snapshot.applied_mask,
                    "delete",
                )?;
                RollbackPlan::new().push(
                    format!(
                        "删除 ifindex {} 上本次下发的 IP {}",
                        snapshot.ifindex, snapshot.applied_ip
                    ),
                    RollbackAction::NetconfFragment {
                        fragment: frag,
                        // 目标可能已被其他途径删除，容忍不存在
                        continue_on_error: true,
                    },
                )
            }
        };
        Ok(plan)
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
                    tracing::info!(step = %step.description, "执行回退步骤");
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
