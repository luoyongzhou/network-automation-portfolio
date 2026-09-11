//! 错误类型

#[derive(Debug, thiserror::Error)]
pub enum TransportError {
    // —— NETCONF ——
    #[error("[{0}] NETCONF 连接失败: {1}")]
    NetconfConnect(String, String),

    #[error("[{0}] NETCONF 操作失败: {1}")]
    Netconf(String, String),

    #[error("[{0}] edit-config 失败: {1}")]
    EditConfig(String, String),

    #[error("[{0}] commit 失败: {1}")]
    Commit(String, String),

    #[error("[{0}] 响应解析失败: {1}")]
    Parse(String, String),

    // —— CLI ——
    #[error("[{0}] SSH 连接失败: {1}")]
    CliConnect(String, String),

    #[error("[{0}] CLI 操作失败: {1}")]
    Cli(String, String),

    /// 设备回显中检出错误。这是 Python 版缺失的判定能力。
    #[error("[{host}] 命令 `{command}` 执行失败: {detail}")]
    CommandFailed {
        host: String,
        command: String,
        detail: String,
    },

    // —— Console ——
    #[error("[{0}] Console 连接失败: {1}")]
    ConsoleConnect(String, String),

    #[error("[{0}] Console 操作失败: {1}")]
    Console(String, String),

    #[error("[{0}] Console 等待提示符超时，尾部回显: {1}")]
    ConsoleTimeout(String, String),

    #[error("设备 {0} 缺少连接配置: {1}")]
    MissingConnectionOption(String, String),
}
