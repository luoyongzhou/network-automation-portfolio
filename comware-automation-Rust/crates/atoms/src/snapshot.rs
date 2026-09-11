//! 快照持久化
//!
//! 对应 Python 版 `scenes/*.py` 里各自实现的 `save_snapshot()` / `load_snapshot()`。
//!
//! ## 与 Python 版的差异
//!
//! Python 版把调试产物（`ifindex_<device>.xml`，每次查询覆盖写）与业务快照
//! （`snapshots_<device>_<mode>.json`，**回退的唯一依据**）混放在同一个
//! `logs/` 目录，做日志清理时容易连带误删快照
//! （`need_to_discuss_prob.md` 新发现③）。
//!
//! 这里按生命周期分离：
//!   - `state/snapshots/` —— 业务状态，需纳入备份策略
//!   - `logs/debug/`     —— 调试产物，可随时清理
//!
//! 并且写入采用"临时文件 + 原子重命名"，避免进程中断导致快照文件截断
//! （Python 版直接 `open(...,"w")` 写入，中断即得到半个 JSON）。

use std::path::{Path, PathBuf};

use serde::{de::DeserializeOwned, Serialize};

use crate::error::AtomError;

/// 快照存储。
pub struct SnapshotStore {
    root: PathBuf,
}

impl SnapshotStore {
    /// 以项目根目录初始化，快照落在 `<root>/state/snapshots/`。
    pub fn new(project_root: impl Into<PathBuf>) -> Self {
        Self {
            root: project_root.into().join("state").join("snapshots"),
        }
    }

    fn path_for(&self, device: &str, scope: &str) -> PathBuf {
        self.root.join(format!("{device}_{scope}.json"))
    }

    /// 保存快照。写临时文件后原子重命名，保证不会留下半截文件。
    pub fn save<T: Serialize>(
        &self,
        device: &str,
        scope: &str,
        snapshot: &T,
    ) -> Result<PathBuf, AtomError> {
        std::fs::create_dir_all(&self.root)
            .map_err(|e| AtomError::Snapshot(format!("创建目录失败 {:?}: {e}", self.root)))?;

        let final_path = self.path_for(device, scope);
        let tmp_path = final_path.with_extension("json.tmp");

        let json = serde_json::to_string_pretty(snapshot)
            .map_err(|e| AtomError::Snapshot(format!("序列化失败: {e}")))?;

        std::fs::write(&tmp_path, json)
            .map_err(|e| AtomError::Snapshot(format!("写入失败 {tmp_path:?}: {e}")))?;
        std::fs::rename(&tmp_path, &final_path)
            .map_err(|e| AtomError::Snapshot(format!("重命名失败: {e}")))?;

        tracing::debug!(device, scope, path = %final_path.display(), "快照已保存");
        Ok(final_path)
    }

    /// 读取快照，不存在时返回 `None`。
    pub fn load<T: DeserializeOwned>(
        &self,
        device: &str,
        scope: &str,
    ) -> Result<Option<T>, AtomError> {
        let path = self.path_for(device, scope);
        if !path.exists() {
            return Ok(None);
        }
        let text = std::fs::read_to_string(&path)
            .map_err(|e| AtomError::Snapshot(format!("读取失败 {path:?}: {e}")))?;
        let parsed = serde_json::from_str(&text)
            .map_err(|e| AtomError::Snapshot(format!("解析失败 {path:?}: {e}")))?;
        Ok(Some(parsed))
    }

    /// 删除快照。回退成功后调用。
    pub fn remove(&self, device: &str, scope: &str) -> Result<bool, AtomError> {
        let path = self.path_for(device, scope);
        if !path.exists() {
            return Ok(false);
        }
        std::fs::remove_file(&path)
            .map_err(|e| AtomError::Snapshot(format!("删除失败 {path:?}: {e}")))?;
        tracing::debug!(device, scope, "快照已删除");
        Ok(true)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }
}

/// 调试产物存储，与业务快照物理隔离。
pub struct DebugStore {
    root: PathBuf,
}

impl DebugStore {
    pub fn new(project_root: impl Into<PathBuf>) -> Self {
        Self {
            root: project_root.into().join("logs").join("debug"),
        }
    }

    /// 写入调试文本（如 NETCONF 原始响应）。失败仅告警，不影响主流程。
    pub fn write(&self, name: &str, content: &str) {
        if let Err(e) = std::fs::create_dir_all(&self.root) {
            tracing::warn!(error = %e, "创建调试目录失败");
            return;
        }
        let path = self.root.join(name);
        if let Err(e) = std::fs::write(&path, content) {
            tracing::warn!(error = %e, path = %path.display(), "写入调试文件失败");
        }
    }
}
