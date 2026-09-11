//! H3C Loopback 接口原子操作（CLI）
//!
//! 对应 Python 版 `atoms/h3c/cmd/interface_loopback.py`。
//!
//! ## 关于 `NetconfLoopbackAtom`
//!
//! Python 版还有一个 `atoms/h3c/netconf/interface_loopback.py`，试图用纯
//! NETCONF 创建 Loopback，但受 Comware 限制未走通，只实现了两个方法，
//! 是个从未被调用的半成品（`need_to_discuss_prob.md` 延伸②）。
//!
//! 本次重写**明确废弃该路线**，统一以"CLI 建接口 + NETCONF 配 IP"的混合
//! 路径作为正式方案，并把这条路径纳入 Atom 抽象（Python 版是散落在
//! `scenes/test_ip_deploy_full.py` 里手写的）。

use cwa_inventory::Host;
use cwa_templating::{paths, SceneApi};
use cwa_transport::CliSession;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::base::{Atom, PreCheckOutcome, RollbackAction, RollbackGuard, RollbackPlan};
use crate::error::AtomError;

/// 调用参数。
#[derive(Debug, Clone)]
pub struct LoopbackParams {
    /// Loopback 逻辑编号，接口名为 `LoopBack{number}`。
    pub number: u32,
    pub description: Option<String>,
}

/// 变更前状态。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LoopbackSnapshot {
    pub ifname: String,
    /// 变更前接口是否已存在。若已存在，回退时**不应删除**接口本身。
    pub existed_before: bool,
}

/// 期望状态。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoopbackDesired {
    pub ifname: String,
    pub description: Option<String>,
}

/// CLI Loopback 接口原子操作。
pub struct CmdLoopbackAtom<'a> {
    scene: &'a SceneApi,
}

impl<'a> CmdLoopbackAtom<'a> {
    pub fn new(scene: &'a SceneApi) -> Self {
        Self { scene }
    }

    fn render_lines(
        &self,
        host: &Host,
        template: &str,
        ctx: serde_json::Map<String, serde_json::Value>,
    ) -> Result<Vec<String>, AtomError> {
        let text = self
            .scene
            .direct_render(host, template, Some(&ctx))
            .map_err(|e| AtomError::Render(e.to_string()))?;
        Ok(text
            .lines()
            .map(|l| l.trim().to_string())
            .filter(|l| !l.is_empty() && !l.starts_with('#'))
            .collect())
    }
}

impl<'a> Atom for CmdLoopbackAtom<'a> {
    type Snapshot = LoopbackSnapshot;
    type Desired = LoopbackDesired;
    type Channel = CliSession;
    type Params = LoopbackParams;

    fn name(&self) -> &'static str {
        "cmd_loopback"
    }

    async fn pre_check(
        &self,
        _host: &Host,
        channel: &mut Self::Channel,
        params: &Self::Params,
    ) -> Result<PreCheckOutcome<Self::Snapshot, Self::Desired>, AtomError> {
        let ifname = format!("LoopBack{}", params.number);

        // 查询接口是否已存在。
        //
        // Python 版 `get_cli_interface()` 是 `return None` 空实现，导致
        // pre_check 永远认为接口不存在。这里做真实查询。
        let existed = match channel
            .run(
                cwa_transport::mode::ENABLE,
                &format!("display interface {ifname} brief"),
            )
            .await
        {
            Ok(out) => {
                let lower = out.to_lowercase();
                // 接口不存在时 Comware 回显里不会出现接口名
                lower.contains(&ifname.to_lowercase())
            }
            // 查询命令报错通常意味着接口不存在，不视为致命错误
            Err(e) => {
                tracing::debug!(error = %e, %ifname, "接口查询失败，按不存在处理");
                false
            }
        };

        // 幂等：已存在则跳过创建
        if existed {
            return Ok(PreCheckOutcome::Skipped {
                reason: format!("{ifname} 已存在"),
            });
        }

        Ok(PreCheckOutcome::Ready {
            snapshot: LoopbackSnapshot {
                ifname: ifname.clone(),
                existed_before: existed,
            },
            desired: LoopbackDesired {
                ifname,
                description: params.description.clone(),
            },
        })
    }

    async fn deploy(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        desired: &Self::Desired,
    ) -> Result<(), AtomError> {
        let mut ctx = cwa_templating::ctx([("ifname", json!(desired.ifname))]);
        if let Some(d) = &desired.description {
            ctx.insert("description".into(), json!(d));
        }
        let lines = self.render_lines(host, paths::CMD_INTERFACE_CREATE, ctx)?;

        channel
            .send_config(&lines)
            .await
            .map_err(AtomError::Transport)?;
        Ok(())
    }

    async fn post_check(
        &self,
        _host: &Host,
        channel: &mut Self::Channel,
        desired: &Self::Desired,
    ) -> Result<(), AtomError> {
        let out = channel
            .run(
                cwa_transport::mode::ENABLE,
                &format!("display interface {} brief", desired.ifname),
            )
            .await
            .map_err(AtomError::Transport)?;

        if !out.to_lowercase().contains(&desired.ifname.to_lowercase()) {
            return Err(AtomError::PostCheck(format!(
                "{} 未创建成功",
                desired.ifname
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
        // Python 版 `check_cli_dependencies()` 同为空实现。
        tracing::debug!("Loopback 回退依赖检查尚未实现，当前始终放行");
        Ok(RollbackGuard::Safe)
    }

    fn plan_rollback(
        &self,
        host: &Host,
        snapshot: &Self::Snapshot,
    ) -> Result<RollbackPlan, AtomError> {
        // 接口在变更前就存在 → 不是本次创建的 → 不删除
        if snapshot.existed_before {
            return Ok(RollbackPlan::new());
        }

        let ctx = cwa_templating::ctx([("ifname", json!(snapshot.ifname))]);
        let lines = self.render_lines(host, paths::CMD_INTERFACE_ROLLBACK, ctx)?;

        Ok(RollbackPlan::new().push(
            format!("删除本次创建的接口 {}", snapshot.ifname),
            RollbackAction::CliCommands(lines),
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
                RollbackAction::CliCommands(lines) => {
                    tracing::info!(step = %step.description, "执行回退步骤");
                    channel
                        .send_config(lines)
                        .await
                        .map_err(AtomError::Transport)?;
                }
                RollbackAction::NetconfFragment { .. } => {
                    return Err(AtomError::Internal(
                        "CLI 通道无法执行 NETCONF 回退步骤".into(),
                    ))
                }
            }
        }
        Ok(())
    }
}
