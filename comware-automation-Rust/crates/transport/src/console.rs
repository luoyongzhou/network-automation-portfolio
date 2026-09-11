//! Console 转 Telnet 通道
//!
//! 对应 Python 版 `scenes/bootstrap_step1.py::task_console_deploy()`。
//!
//! 这是整个迁移里唯一没有对等库的部分（`RESEARCH.md` 2.5）：`telnet` crate
//! 只提供协议层，提示符状态机、ZTP 中断时序、交互应答均需自行实现。
//!
//! ## 与 Python 版的行为对应
//!
//! | Python (Netmiko)                       | 本模块 |
//! | :--- | :--- |
//! | `device_type='hp_comware_telnet'`      | 直接走 telnet 协议 |
//! | `write_channel("\r")` 唤醒              | `wake()` |
//! | `write_channel("\x03")` ×2 中断 ZTP     | `interrupt_ztp()` |
//! | 检测 `Press ENTER` 并响应               | `wait_for_prompt()` 内处理 |
//! | `send_command(cmd, expect_string=...)` | `send_expect()` |
//! | `global_delay_factor: 3`               | `delay_factor` 字段 |

use std::net::TcpStream;
use std::time::{Duration, Instant};

use cwa_inventory::Host;
use regex::Regex;
use telnet::{Event, Telnet};

use crate::error::TransportError;

/// 用户视图提示符 `<HOSTNAME>`
const RE_ENABLE: &str = r"<[^>\r\n]+>\s*$";
/// 系统视图提示符 `[HOSTNAME]`
const RE_CONFIG: &str = r"\[[^\]\r\n]+\]\s*$";
/// 需要按回车继续
const RE_PRESS_ENTER: &str = r"(?i)press\s+enter";

/// Console 会话。
pub struct ConsoleSession {
    conn: Telnet,
    host_name: String,
    /// 对应 Netmiko 的 `global_delay_factor`，用于放大等待时间。
    delay_factor: u32,
    read_timeout: Duration,
    re_enable: Regex,
    re_config: Regex,
    re_press_enter: Regex,
}

impl ConsoleSession {
    /// 通过 `netmiko_oob` 连接选项建立 Console Telnet 会话。
    ///
    /// 缺少该连接选项时报错，对应 Python 版对 `netmiko_oob` 的检查。
    pub fn connect(host: &Host) -> Result<Self, TransportError> {
        let oob = host.connection("netmiko_oob").ok_or_else(|| {
            TransportError::MissingConnectionOption(host.name.clone(), "netmiko_oob".into())
        })?;

        let hostname = oob.hostname.clone().ok_or_else(|| {
            TransportError::MissingConnectionOption(
                host.name.clone(),
                "netmiko_oob.hostname".into(),
            )
        })?;
        let port = oob.port.unwrap_or(23);

        let delay_factor = oob.extra_u64("global_delay_factor").unwrap_or(1).max(1) as u32;
        let timeout_secs = oob.extra_u64("timeout").unwrap_or(120);

        tracing::debug!(
            host = %host.name, %hostname, port, delay_factor,
            "建立 Console Telnet 会话"
        );

        let stream = TcpStream::connect((hostname.as_str(), port))
            .map_err(|e| TransportError::ConsoleConnect(host.name.clone(), e.to_string()))?;
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .map_err(|e| TransportError::ConsoleConnect(host.name.clone(), e.to_string()))?;

        let conn = Telnet::from_stream(Box::new(stream), 4096);

        Ok(Self {
            conn,
            host_name: host.name.clone(),
            delay_factor,
            read_timeout: Duration::from_secs(timeout_secs),
            re_enable: Regex::new(RE_ENABLE).expect("enable regex"),
            re_config: Regex::new(RE_CONFIG).expect("config regex"),
            re_press_enter: Regex::new(RE_PRESS_ENTER).expect("press enter regex"),
        })
    }

    fn scaled(&self, base_ms: u64) -> Duration {
        Duration::from_millis(base_ms * self.delay_factor as u64)
    }

    /// 写入原始字节，对应 Netmiko `write_channel()`。
    pub fn write_raw(&mut self, data: &str) -> Result<(), TransportError> {
        self.conn
            .write(data.as_bytes())
            .map_err(|e| TransportError::Console(self.host_name.clone(), e.to_string()))?;
        Ok(())
    }

    /// 读取当前可用数据，直到超时或匹配到任一提示符。
    ///
    /// 返回累积的回显文本。
    fn read_until(&mut self, matcher: &dyn Fn(&str) -> bool) -> Result<String, TransportError> {
        let deadline = Instant::now() + self.read_timeout;
        let mut buf = String::new();

        while Instant::now() < deadline {
            match self.conn.read_timeout(Duration::from_millis(500)) {
                Ok(Event::Data(bytes)) => {
                    buf.push_str(&String::from_utf8_lossy(&bytes));

                    // 设备要求按回车继续时自动响应
                    if self.re_press_enter.is_match(&buf) {
                        tracing::debug!(host = %self.host_name, "检测到 Press ENTER，自动响应");
                        self.write_raw("\r")?;
                        buf.clear();
                        std::thread::sleep(self.scaled(500));
                        continue;
                    }

                    if matcher(&buf) {
                        return Ok(buf);
                    }
                }
                Ok(Event::TimedOut) => {
                    if matcher(&buf) {
                        return Ok(buf);
                    }
                }
                Ok(_) => {}
                Err(e) => {
                    return Err(TransportError::Console(
                        self.host_name.clone(),
                        e.to_string(),
                    ))
                }
            }
        }

        Err(TransportError::ConsoleTimeout(
            self.host_name.clone(),
            buf.chars().rev().take(200).collect::<String>(),
        ))
    }

    /// 唤醒会话：发一个回车。
    pub fn wake(&mut self) -> Result<(), TransportError> {
        self.write_raw("\r")?;
        std::thread::sleep(self.scaled(500));
        Ok(())
    }

    /// 中断 ZTP：连发两次 Ctrl-C，再回车。
    ///
    /// 与 Python 版 `bootstrap_step1.py` 的时序一致。
    pub fn interrupt_ztp(&mut self) -> Result<(), TransportError> {
        for _ in 0..2 {
            self.write_raw("\x03")?;
            std::thread::sleep(self.scaled(500));
        }
        self.write_raw("\r")?;
        std::thread::sleep(self.scaled(1000));

        // 读一次，吸收 ZTP 中断后的横幅与可能的 Press ENTER
        let re_enable = self.re_enable.clone();
        let re_config = self.re_config.clone();
        let _ = self.read_until(&move |s: &str| re_enable.is_match(s) || re_config.is_match(s));
        Ok(())
    }

    /// 发送命令并等待任一视图提示符。
    pub fn send_expect(&mut self, command: &str) -> Result<String, TransportError> {
        self.write_raw(command)?;
        self.write_raw("\r")?;

        let re_enable = self.re_enable.clone();
        let re_config = self.re_config.clone();
        let out =
            self.read_until(&move |s: &str| re_enable.is_match(s) || re_config.is_match(s))?;

        // 与 CLI 通道共用同一套错误检测
        if let Some(err) = crate::cli::check_output_for_errors(&out) {
            return Err(TransportError::CommandFailed {
                host: self.host_name.clone(),
                command: command.to_string(),
                detail: err,
            });
        }
        Ok(out)
    }

    /// 进入系统视图。
    pub fn enter_system_view(&mut self) -> Result<(), TransportError> {
        self.send_expect("system-view")?;
        Ok(())
    }

    /// 按顺序下发配置行（跳过空行与注释）。
    pub fn send_config_lines(&mut self, lines: &[String]) -> Result<(), TransportError> {
        for line in lines {
            let cmd = line.trim();
            if cmd.is_empty() || cmd.starts_with('#') {
                continue;
            }
            self.send_expect(cmd)?;
        }
        Ok(())
    }

    /// 退回用户视图并断开。
    pub fn finish(&mut self) -> Result<(), TransportError> {
        let _ = self.send_expect("end");
        let _ = self.write_raw("quit\r");
        Ok(())
    }
}
