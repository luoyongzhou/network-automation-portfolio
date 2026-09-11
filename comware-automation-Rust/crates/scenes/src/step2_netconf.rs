//! 阶段二：SSH 下发使能 NETCONF
//!
//! 对应 Python 版 `scenes/bootstrap_step2.py`。
//!
//! 此阶段设备已有 SSH 业务 IP，通过 SSH 下发命令开启 NETCONF 服务（端口 830）。
//! 模板中用 `#INDEPENDENT:START/END` 标记的块需独立连接、含交互输入
//! （`public-key local create rsa` 需要回答 `y` 和密钥长度 `512`）。

use cwa_inventory::{Host, Inventory};
use cwa_templating::SceneApi;
use cwa_transport::{parse_preserving_order, CliSession, ExecUnit};

use crate::runner::{TaskLog, TaskStatus};

/// 预览 NETCONF 使能配置及其执行单元切分。
pub fn preview(inventory: &Inventory, scene: &SceneApi) -> anyhow::Result<()> {
    for (name, host) in &inventory.hosts {
        println!("\n{}", "=".repeat(70));
        println!("  设备: {name}");
        println!("  Group: {:?}", host.groups);
        println!("{}", "=".repeat(70));

        let text = match scene.ssh_bootstrap(host) {
            Ok(t) => t,
            Err(e) => {
                println!("渲染失败: {e}");
                continue;
            }
        };
        println!("{text}");
        println!("{}", "-".repeat(70));

        let units = parse_preserving_order(&text);
        let normal = units
            .iter()
            .filter(|u| matches!(u, ExecUnit::Normal { .. }))
            .count();
        let indep = units.len() - normal;
        println!(
            "\n执行单元: {} 个（普通命令块 {normal} 个，独立块 {indep} 个）",
            units.len()
        );
        for (i, u) in units.iter().enumerate() {
            match u {
                ExecUnit::Normal { commands } => {
                    println!("  {}. 普通命令块: {} 条", i + 1, commands.len())
                }
                ExecUnit::Independent { lines } => {
                    println!("  {}. 独立块: {} 行", i + 1, lines.len())
                }
            }
        }
        println!("{}", "=".repeat(70));
    }
    Ok(())
}

/// 通过 SSH 下发 NETCONF 使能配置，严格保持模板原始顺序。
pub async fn deploy(host: Host, config_text: String) -> (TaskStatus, Vec<String>) {
    let mut log = TaskLog::new();
    log.header(&format!("SSH 使能 NETCONF: {}", host.name));

    let units = parse_preserving_order(&config_text);
    if units.is_empty() {
        log.line("  无执行单元，跳过");
        return (TaskStatus::Skipped, log.into_lines());
    }

    for (idx, unit) in units.iter().enumerate() {
        match unit {
            ExecUnit::Normal { commands } => {
                log.line(format!(
                    "  普通命令块 #{}: {} 条命令",
                    idx + 1,
                    commands.len()
                ));

                let session = match CliSession::connect(&host).await {
                    Ok(s) => s,
                    Err(e) => {
                        log.line(format!("  连接失败: {e}"));
                        return (TaskStatus::Failed(e.to_string()), log.into_lines());
                    }
                };

                if let Err(e) = session.send_config(commands).await {
                    log.line(format!("  普通命令块 #{} 下发失败: {e}", idx + 1));
                    return (TaskStatus::Failed(e.to_string()), log.into_lines());
                }
                log.line("     已完成");
            }

            ExecUnit::Independent { lines } => {
                log.line(format!("  独立块 #{}: {} 行", idx + 1, lines.len()));

                // 独立块采用"发送即忘"：交互输入（y / 512）不产生标准提示符，
                // 逐行发送后不等待特定提示符，与 Python 版策略一致。
                let session = match CliSession::connect(&host).await {
                    Ok(s) => s,
                    Err(e) => {
                        log.line(format!("  连接失败: {e}"));
                        return (TaskStatus::Failed(e.to_string()), log.into_lines());
                    }
                };

                for line in lines {
                    if line.starts_with("system-view") {
                        continue;
                    }
                    // 忽略单行错误：交互应答行本身不构成合法命令
                    match session.run(cwa_transport::mode::CONFIG, line).await {
                        Ok(_) => {}
                        Err(e) => {
                            tracing::debug!(line = %line, error = %e, "独立块行执行返回异常（预期内）");
                        }
                    }
                    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                }
                log.line("     已完成（发送即忘）");
            }
        }
    }

    log.line("\n状态: SUCCESS");
    (TaskStatus::Success, log.into_lines())
}
