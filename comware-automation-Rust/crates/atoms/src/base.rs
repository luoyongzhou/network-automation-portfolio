//! 原子操作抽象
//!
//! 对应 Python 版 `atoms/base.py` 的 `Atom` / `CmdAtom` / `NetconfAtom`。
//!
//! ## 相对 Python 版修正的三点
//!
//! ### 1. `desired` 参数语义明确化
//!
//! Python 版 `execute()` 里写的是：
//!
//! ```python
//! post_result = self.post_check(host, snapshot, kwargs, **kwargs)
//! ```
//!
//! 形参名叫 `desired`（期望状态），实际传入的却是 `kwargs`（原始调用参数字典），
//! 语义不一致（`need_to_discuss_prob.md` 延伸②）。这里把"期望状态"提升为
//! trait 的关联类型 `Desired`，由每个 Atom 显式定义，`pre_check` 产出、
//! `post_check` 消费，不再复用参数字典。
//!
//! ### 2. 快照语义统一
//!
//! Python 版两条路线的快照语义不一致：IP 场景存"变更前原始状态"，
//! OSPF 场景存"期望配置全量"（延伸①）。这里统一为：
//! **`Snapshot` 只记录变更前状态，`Desired` 记录期望状态**，两者分开。
//!
//! ### 3. 回退步骤显式化
//!
//! Python 版只有快照，没有"快照 → 回退步骤"的映射（延伸③深层）。
//! 这里引入 [`RollbackPlan`]，由 `plan_rollback()` 从快照推导出有序步骤，
//! 使回退过程可预览、可审计。

use std::fmt::Debug;

use cwa_inventory::Host;

use crate::error::AtomError;

/// 预检查结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PreCheckOutcome<S, D> {
    /// 可以执行，携带变更前快照与期望状态。
    Ready { snapshot: S, desired: D },
    /// 已达目标状态，无需变更（幂等跳过）。
    Skipped { reason: String },
    /// 前置条件不满足，禁止执行。
    Blocked { reason: String },
}

/// 回退安全性检查结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RollbackGuard {
    /// 可安全回退。
    Safe,
    /// 存在依赖，禁止回退。
    Blocked { dependencies: Vec<String> },
}

/// 单个回退步骤。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RollbackStep {
    /// 人类可读的步骤描述，用于预览与日志。
    pub description: String,
    /// 该步骤的具体动作。
    pub action: RollbackAction,
}

/// 回退动作。把"该发什么"与"怎么发"分离，便于预览时只打印不执行。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RollbackAction {
    /// 下发 CLI 命令行。
    CliCommands(Vec<String>),
    /// 下发 NETCONF 配置片段（不含 `<config>` 外壳）。
    NetconfFragment {
        fragment: String,
        /// 是否使用 continue-on-error（容忍目标对象已不存在）。
        continue_on_error: bool,
    },
}

/// 回退计划：从快照推导出的有序步骤。
///
/// 这是 Python 版完全缺失的一层——它只有快照，回退时的步骤是硬编码在
/// `_rollback_impl()` 里的，无法预览也无法审计。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RollbackPlan {
    pub steps: Vec<RollbackStep>,
}

impl RollbackPlan {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(mut self, description: impl Into<String>, action: RollbackAction) -> Self {
        self.steps.push(RollbackStep {
            description: description.into(),
            action,
        });
        self
    }

    pub fn is_empty(&self) -> bool {
        self.steps.is_empty()
    }

    /// 渲染为可读文本，用于 preview 模式。
    pub fn describe(&self) -> String {
        if self.steps.is_empty() {
            return "（无回退步骤）".to_string();
        }
        self.steps
            .iter()
            .enumerate()
            .map(|(i, s)| format!("  {}. {}", i + 1, s.description))
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// 单次原子操作的最终状态。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AtomStatus {
    Success,
    Skipped {
        reason: String,
    },
    Failed {
        reason: String,
    },
    /// 下发或校验失败后已回退。
    RolledBack {
        reason: String,
        rollback_detail: String,
    },
    /// 回退被依赖检查阻止。
    RollbackBlocked {
        dependencies: Vec<String>,
    },
}

impl AtomStatus {
    pub fn is_success(&self) -> bool {
        matches!(self, AtomStatus::Success)
    }

    pub fn label(&self) -> &'static str {
        match self {
            AtomStatus::Success => "SUCCESS",
            AtomStatus::Skipped { .. } => "SKIPPED",
            AtomStatus::Failed { .. } => "FAILED",
            AtomStatus::RolledBack { .. } => "ROLLED_BACK",
            AtomStatus::RollbackBlocked { .. } => "ROLLBACK_BLOCKED",
        }
    }
}

/// 原子操作。
///
/// 四阶段生命周期与 Python 版一致，但 `Snapshot` / `Desired` 分离，
/// 且新增 `plan_rollback()` 让回退步骤可推导、可预览。
///
/// 与 Python 的 `ABC` 不同：Rust trait 在**编译期**强制所有方法实现，
/// 因此不可能出现 Python 版 `NetconfLoopbackAtom` 那种"定义了类但只实现
/// 一半方法、且因从未实例化而静默存在"的半成品（延伸②）。
#[allow(async_fn_in_trait)]
pub trait Atom {
    /// 变更前的原始状态快照。
    type Snapshot: Debug + Clone + Send;
    /// 期望达成的状态。
    type Desired: Debug + Clone + Send;
    /// 执行下发所需的通道（CLI 会话或 NETCONF 会话）。
    type Channel;
    /// 调用参数。
    type Params: Debug + Clone + Send;

    /// 原子操作名称，用于日志。
    fn name(&self) -> &'static str;

    /// 幂等检查 + 快照采集。
    async fn pre_check(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        params: &Self::Params,
    ) -> Result<PreCheckOutcome<Self::Snapshot, Self::Desired>, AtomError>;

    /// 下发变更。
    async fn deploy(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        desired: &Self::Desired,
    ) -> Result<(), AtomError>;

    /// 校验变更是否生效。
    async fn post_check(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        desired: &Self::Desired,
    ) -> Result<(), AtomError>;

    /// 回退前依赖检查。
    async fn rollback_guard(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        snapshot: &Self::Snapshot,
    ) -> Result<RollbackGuard, AtomError>;

    /// 从快照推导回退步骤。
    ///
    /// 纯函数，不接触设备，因此可以在 preview 模式下调用以展示
    /// "如果回退，将会执行什么"。
    fn plan_rollback(
        &self,
        host: &Host,
        snapshot: &Self::Snapshot,
    ) -> Result<RollbackPlan, AtomError>;

    /// 执行回退计划。
    async fn apply_rollback(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        plan: &RollbackPlan,
    ) -> Result<(), AtomError>;

    /// 完整生命周期编排。
    ///
    /// 与 Python 版 `execute()` 的流程一致，但回退走 `plan_rollback()` +
    /// `apply_rollback()` 两步，且 `post_check` 收到的是语义明确的 `desired`。
    async fn execute(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        params: &Self::Params,
        auto_rollback: bool,
    ) -> Result<AtomStatus, AtomError> {
        let outcome = self.pre_check(host, channel, params).await?;

        let (snapshot, desired) = match outcome {
            PreCheckOutcome::Blocked { reason } => {
                tracing::info!(atom = self.name(), host = %host.name, %reason, "预检查阻止");
                return Ok(AtomStatus::Failed { reason });
            }
            PreCheckOutcome::Skipped { reason } => {
                tracing::info!(atom = self.name(), host = %host.name, %reason, "幂等跳过");
                return Ok(AtomStatus::Skipped { reason });
            }
            PreCheckOutcome::Ready { snapshot, desired } => (snapshot, desired),
        };

        if let Err(e) = self.deploy(host, channel, &desired).await {
            return self
                .handle_failure(host, channel, &snapshot, e.to_string(), auto_rollback)
                .await;
        }

        if let Err(e) = self.post_check(host, channel, &desired).await {
            return self
                .handle_failure(host, channel, &snapshot, e.to_string(), auto_rollback)
                .await;
        }

        Ok(AtomStatus::Success)
    }

    /// 失败后的回退处理。
    async fn handle_failure(
        &self,
        host: &Host,
        channel: &mut Self::Channel,
        snapshot: &Self::Snapshot,
        reason: String,
        auto_rollback: bool,
    ) -> Result<AtomStatus, AtomError> {
        if !auto_rollback {
            return Ok(AtomStatus::Failed { reason });
        }

        match self.rollback_guard(host, channel, snapshot).await? {
            RollbackGuard::Blocked { dependencies } => {
                tracing::warn!(
                    atom = self.name(), host = %host.name,
                    ?dependencies, "回退被依赖检查阻止"
                );
                Ok(AtomStatus::RollbackBlocked { dependencies })
            }
            RollbackGuard::Safe => {
                let plan = self.plan_rollback(host, snapshot)?;
                match self.apply_rollback(host, channel, &plan).await {
                    Ok(()) => Ok(AtomStatus::RolledBack {
                        reason,
                        rollback_detail: format!("已执行 {} 个回退步骤", plan.steps.len()),
                    }),
                    Err(e) => Ok(AtomStatus::RolledBack {
                        reason,
                        rollback_detail: format!("回退失败: {e}"),
                    }),
                }
            }
        }
    }
}
