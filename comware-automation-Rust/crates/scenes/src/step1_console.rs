//! 阶段一：Console 零配置开局
//!
//! 对应 Python 版 `scenes/bootstrap_step1.py`。
//!
//! 通过 Console 映射的 Telnet 端口连接设备，中断 ZTP，下发开局配置。
//! 此阶段设备处于零配置状态，SSH 尚不可达。

use cwa_inventory::{Host, Inventory};
use cwa_templating::SceneApi;
use cwa_transport::ConsoleSession;

use crate::runner::{TaskLog, TaskStatus};

/// 预览开局配置（不连接设备）。
pub fn preview(inventory: &Inventory, scene: &SceneApi) -> anyhow::Result<()> {
    for (name, host) in &inventory.hosts {
        println!("\n{}", "=".repeat(70));
        println!("  设备: {name}");
        println!("  Group: {:?}", host.groups);
        println!("{}", "=".repeat(70));
        match scene.bootstrap(host) {
            Ok(text) => println!("{text}"),
            Err(e) => println!("渲染失败: {e}"),
        }
        println!("{}", "=".repeat(70));
    }
    Ok(())
}

/// 通过 Console 下发开局配置。
///
/// 注意：`ConsoleSession` 是同步阻塞实现（`telnet` crate 无 async 版本），
/// 因此放进 `spawn_blocking` 避免阻塞 tokio 运行时。
pub async fn deploy(host: Host, config_text: String) -> (TaskStatus, Vec<String>) {
    let mut log = TaskLog::new();
    log.header(&format!("Console 开局: {}", host.name));

    let commands: Vec<String> = config_text
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .collect();

    if commands.is_empty() {
        log.line("  无配置命令，跳过");
        return (TaskStatus::Skipped, log.into_lines());
    }

    log.line(format!("  待下发 {} 条命令", commands.len()));

    let host_name = host.name.clone();
    let result = tokio::task::spawn_blocking(move || -> Result<Vec<String>, String> {
        let mut steps = Vec::new();

        let mut console = ConsoleSession::connect(&host).map_err(|e| e.to_string())?;
        steps.push("  已连接 Console".to_string());

        console.wake().map_err(|e| e.to_string())?;
        console.interrupt_ztp().map_err(|e| e.to_string())?;
        steps.push("  已中断 ZTP".to_string());

        console.enter_system_view().map_err(|e| e.to_string())?;
        steps.push("  已进入系统视图".to_string());

        console
            .send_config_lines(&commands)
            .map_err(|e| e.to_string())?;
        steps.push(format!("  已下发 {} 条命令", commands.len()));

        console.finish().map_err(|e| e.to_string())?;
        steps.push("  已退出并断开".to_string());

        Ok(steps)
    })
    .await;

    match result {
        Ok(Ok(steps)) => {
            for s in steps {
                log.line(s);
            }
            log.line("\n状态: SUCCESS");
            (TaskStatus::Success, log.into_lines())
        }
        Ok(Err(e)) => {
            log.line(format!("  失败: {e}"));
            log.line("\n状态: FAILED");
            (TaskStatus::Failed(e), log.into_lines())
        }
        Err(e) => {
            let msg = format!("任务调度失败: {e}");
            log.line(format!("  {msg}"));
            (TaskStatus::Failed(msg), log.into_lines())
        }
    }
    .tap_host(&host_name)
}

/// 小工具 trait：便于在返回值上补充上下文而不打断链式写法。
trait TapHost {
    fn tap_host(self, host: &str) -> Self;
}

impl TapHost for (TaskStatus, Vec<String>) {
    fn tap_host(self, host: &str) -> Self {
        if let TaskStatus::Failed(ref reason) = self.0 {
            tracing::warn!(host, %reason, "Console 开局失败");
        }
        self
    }
}
