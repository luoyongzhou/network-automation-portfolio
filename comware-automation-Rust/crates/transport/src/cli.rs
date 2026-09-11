//! CLI over SSH 通道
//!
//! 对应 Python 版 `scenes/test_ip_deploy_full.py::send_cmd()` 与
//! `atoms/base.py::CmdAtom._render_and_send()`。
//!
//! ## 补齐 Python 版缺失的能力
//!
//! Python 版 `_render_and_send()` 对 Netmiko `send_command()` 的返回值
//! **不做任何失败检测**——只要连接层没抛异常就认为成功：
//!
//! ```python
//! net_connect.send_command(config_text, expect_string=expect_string)
//! return {"status": "success", "detail": None}
//! ```
//!
//! 设备返回 `% Unrecognized command` 这类错误时依然报成功
//! （`need_to_discuss_prob.md` 延伸③）。
//!
//! 本模块借助 `rneter` 的 `h3c_comware` 模板做错误检测，该模板的
//! `error_regex` 已包含 `.+%.+`、`.+\^.+`、`Permission denied\.`、
//! `Failed to apply .+` 等 H3C 常见错误形态，并额外在
//! `check_output_for_errors()` 里补了一层显式校验，使"下发是否真的成功"
//! 成为可判定的结果。

use cwa_inventory::Host;
use regex::Regex;
use rneter::session::{CmdJob, Command, ConnectionRequest, ExecutionContext, MANAGER};
use rneter::templates;

use crate::error::TransportError;

/// H3C 错误关键字。
///
/// 与 `rneter` 内置 `h3c_comware` 模板的 `error_regex` 保持同一语义，
/// 在本地再做一次显式校验，避免依赖库内部行为的变化。
fn error_patterns() -> &'static Vec<Regex> {
    use std::sync::OnceLock;
    static PATTERNS: OnceLock<Vec<Regex>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        [
            // H3C 命令错误统一以 % 开头，或用 ^ 标记出错位置
            r"^\s*%\s*\S",
            r"\^\s*$",
            r"(?i)unrecognized command",
            r"(?i)wrong parameter",
            r"(?i)incomplete command",
            r"(?i)too many parameters",
            r"(?i)permission denied",
            r"(?i)doesn't exist",
            r"(?i)does not exist",
            r"(?i)failed to apply",
            r"(?i)invalid input",
        ]
        .iter()
        .filter_map(|p| Regex::new(p).ok())
        .collect()
    })
}

/// 扫描设备回显，判断是否包含错误。
///
/// 返回首个命中的错误行，`None` 表示未检出错误。
pub fn check_output_for_errors(output: &str) -> Option<String> {
    let pats = error_patterns();
    for line in output.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        for p in pats {
            if p.is_match(trimmed) {
                return Some(trimmed.to_string());
            }
        }
    }
    None
}

/// 设备模式名，对应 `rneter` h3c 模板中定义的状态机节点。
pub mod mode {
    /// 用户视图 `<HOSTNAME>`
    pub const ENABLE: &str = "Enable";
    /// 系统视图 `[HOSTNAME]`
    pub const CONFIG: &str = "Config";
}

/// CLI 会话封装。
pub struct CliSession {
    sender: tokio::sync::mpsc::Sender<CmdJob>,
    host_name: String,
}

impl CliSession {
    /// 建立 SSH 会话，使用 `rneter` 内置的 `h3c_comware` 设备模板。
    pub async fn connect(host: &Host) -> Result<Self, TransportError> {
        let handler = templates::h3c()
            .map_err(|e| TransportError::CliConnect(host.name.clone(), e.to_string()))?;

        let request = ConnectionRequest::new(
            host.username.clone(),
            host.hostname.clone(),
            host.ssh_port(),
            host.password.clone(),
            None,
            handler,
        );

        let sender = MANAGER
            .get_with_context(request, ExecutionContext::default())
            .await
            .map_err(|e| TransportError::CliConnect(host.name.clone(), e.to_string()))?;

        Ok(Self {
            sender,
            host_name: host.name.clone(),
        })
    }

    /// 在指定模式下执行单条命令，并对回显做错误检测。
    pub async fn run(&self, mode: &str, command: &str) -> Result<String, TransportError> {
        let (tx, rx) = tokio::sync::oneshot::channel();
        let job = CmdJob {
            data: Command {
                mode: mode.to_string(),
                command: command.to_string(),
                timeout: Some(60),
                ..Command::default()
            },
            sys: None,
            responder: tx,
        };

        self.sender
            .send(job)
            .await
            .map_err(|e| TransportError::Cli(self.host_name.clone(), e.to_string()))?;

        let output = rx
            .await
            .map_err(|e| TransportError::Cli(self.host_name.clone(), e.to_string()))?
            .map_err(|e| TransportError::Cli(self.host_name.clone(), e.to_string()))?;

        // 双重校验：库层 success 标志 + 本地错误关键字扫描
        if !output.success {
            return Err(TransportError::CommandFailed {
                host: self.host_name.clone(),
                command: command.to_string(),
                detail: output.content.clone(),
            });
        }
        if let Some(err_line) = check_output_for_errors(&output.content) {
            return Err(TransportError::CommandFailed {
                host: self.host_name.clone(),
                command: command.to_string(),
                detail: err_line,
            });
        }

        Ok(output.content)
    }

    /// 在系统视图下按顺序下发多条配置命令。
    ///
    /// 任一条命令检出错误即中止并返回错误，**不继续下发后续命令**——
    /// 这与 Python 版"无论回显如何都继续"的行为不同，是有意的收紧。
    pub async fn send_config(&self, lines: &[String]) -> Result<Vec<String>, TransportError> {
        let mut outputs = Vec::new();
        for line in lines {
            let cmd = line.trim();
            if cmd.is_empty() || cmd.starts_with('#') {
                continue;
            }
            let out = self.run(mode::CONFIG, cmd).await?;
            outputs.push(out);
        }
        Ok(outputs)
    }

    /// 下发一段配置文本（按行拆分）。
    pub async fn send_config_text(&self, text: &str) -> Result<Vec<String>, TransportError> {
        let lines: Vec<String> = text.lines().map(|l| l.to_string()).collect();
        self.send_config(&lines).await
    }
}

/// 模板中的独立连接块。
///
/// 对应 Python 版 `bootstrap_step2.py::parse_preserving_order()` 识别的
/// `#INDEPENDENT:START` / `#INDEPENDENT:END` 标记。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExecUnit {
    /// 普通命令块，可在同一连接内顺序下发。
    Normal { commands: Vec<String> },
    /// 独立块：需独立连接、含交互输入、采用"发送即忘"策略。
    Independent { lines: Vec<String> },
}

/// 严格保序地解析配置文本为执行单元序列。
///
/// 行为与 Python 版 `parse_preserving_order()` 一致：
///   - 空行跳过
///   - `#INDEPENDENT:START` / `END` 切分独立块
///   - 其他 `#` 开头的行视为注释跳过
pub fn parse_preserving_order(config_text: &str) -> Vec<ExecUnit> {
    let mut units = Vec::new();
    let mut normal_buffer: Vec<String> = Vec::new();
    let mut current_block: Vec<String> = Vec::new();
    let mut in_block = false;

    for raw in config_text.trim().lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }

        if line.starts_with("#INDEPENDENT:START") {
            if !normal_buffer.is_empty() {
                units.push(ExecUnit::Normal {
                    commands: std::mem::take(&mut normal_buffer),
                });
            }
            in_block = true;
            current_block.clear();
            continue;
        }

        if line.starts_with("#INDEPENDENT:END") {
            if !current_block.is_empty() {
                units.push(ExecUnit::Independent {
                    lines: std::mem::take(&mut current_block),
                });
            }
            in_block = false;
            continue;
        }

        if line.starts_with('#') {
            continue;
        }

        if in_block {
            current_block.push(line.to_string());
        } else {
            normal_buffer.push(line.to_string());
        }
    }

    if !normal_buffer.is_empty() {
        units.push(ExecUnit::Normal {
            commands: normal_buffer,
        });
    }

    units
}
