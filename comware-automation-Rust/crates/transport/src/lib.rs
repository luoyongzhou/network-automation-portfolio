//! 传输层：CLI(SSH) / NETCONF / Console(Telnet) 三通道
//!
//! 所有对 `rustnetconf` / `rneter` / `telnet` 的调用都收敛在本 crate 内部
//! （`MIGRATION_PLAN.md` 6.2 的供应链隔离要求）。上层 `atoms` / `scenes`
//! 只依赖本 crate 暴露的类型，不直接引用第三方 crate 的类型。

pub mod cli;
pub mod console;
pub mod error;
pub mod netconf;

pub use cli::{check_output_for_errors, mode, parse_preserving_order, CliSession, ExecUnit};
pub use console::ConsoleSession;
pub use error::TransportError;
pub use netconf::{NetconfSession, H3C_DATA_NS};

/// 重新导出 NETCONF 错误处理选项，避免上层直接依赖 `rustnetconf`。
pub use rustnetconf::types::ErrorOption;
