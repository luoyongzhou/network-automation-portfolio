//! 原子化配置变更单元
//!
//! 四阶段生命周期：`pre_check → deploy → post_check → rollback`。
//!
//! 相对 Python 版 `atoms/` 的改进见 [`base`] 模块文档。

pub mod base;
pub mod error;
pub mod h3c;
pub mod snapshot;

pub use base::{
    Atom, AtomStatus, PreCheckOutcome, RollbackAction, RollbackGuard, RollbackPlan, RollbackStep,
};
pub use error::AtomError;
pub use snapshot::{DebugStore, SnapshotStore};
