//! 错误类型

#[derive(Debug, thiserror::Error)]
pub enum AtomError {
    #[error(transparent)]
    Transport(#[from] cwa_transport::TransportError),

    #[error("模板渲染失败: {0}")]
    Render(String),

    #[error("后检查失败: {0}")]
    PostCheck(String),

    #[error("快照操作失败: {0}")]
    Snapshot(String),

    #[error("内部错误: {0}")]
    Internal(String),
}
