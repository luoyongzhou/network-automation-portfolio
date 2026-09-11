//! 并发执行器
//!
//! 对应 Python 版 Nornir 的 threaded runner（`config.yaml` 里 `num_workers: 10`）。
//!
//! Python 版对 runner 的使用极浅——只有 `nr.run(task=...)` 和 worker 数配置，
//! 因此这里用 `tokio` + `Semaphore` 即可等价覆盖，且是真并发，不受 GIL 约束。

use std::sync::Arc;

use cwa_inventory::{Host, Inventory};
use tokio::sync::Semaphore;

/// 单台设备的执行结果。
#[derive(Debug, Clone)]
pub struct HostResult {
    pub host_name: String,
    pub status: TaskStatus,
    /// 聚合日志。与 Python 版一致：并发执行期间各自缓冲，结束后统一输出，
    /// 避免多设备日志交错。
    pub logs: Vec<String>,
}

/// 任务状态。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskStatus {
    Success,
    Skipped,
    Failed(String),
}

impl TaskStatus {
    pub fn icon(&self) -> &'static str {
        match self {
            TaskStatus::Success => "[ OK ]",
            TaskStatus::Skipped => "[SKIP]",
            TaskStatus::Failed(_) => "[FAIL]",
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            TaskStatus::Success => "SUCCESS",
            TaskStatus::Skipped => "SKIPPED",
            TaskStatus::Failed(_) => "FAILED",
        }
    }
}

/// 单设备日志收集器。
#[derive(Debug, Default)]
pub struct TaskLog {
    lines: Vec<String>,
}

impl TaskLog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn line(&mut self, s: impl Into<String>) {
        self.lines.push(s.into());
    }

    pub fn header(&mut self, title: &str) {
        let bar = "=".repeat(70);
        self.lines.push(String::new());
        self.lines.push(bar.clone());
        self.lines.push(format!("  {title}"));
        self.lines.push(bar);
    }

    pub fn section(&mut self, title: &str) {
        self.lines.push(String::new());
        self.lines.push(format!("  -- {title} --"));
    }

    pub fn into_lines(self) -> Vec<String> {
        self.lines
    }
}

/// 并发执行任务。
///
/// `task` 接收一台 Host，返回 `(状态, 日志)`。
pub async fn run_concurrent<F, Fut>(
    inventory: &Inventory,
    max_workers: usize,
    task: F,
) -> Vec<HostResult>
where
    F: Fn(Host) -> Fut + Send + Sync + 'static + Clone,
    Fut: std::future::Future<Output = (TaskStatus, Vec<String>)> + Send,
{
    let sem = Arc::new(Semaphore::new(max_workers.max(1)));
    let mut handles = Vec::new();

    for host in inventory.hosts.values().cloned() {
        let sem = sem.clone();
        let task = task.clone();
        let name = host.name.clone();

        handles.push(tokio::spawn(async move {
            // 并发上限由信号量控制，等价于 Nornir 的 num_workers
            let _permit = sem.acquire().await.expect("semaphore closed");
            let (status, logs) = task(host).await;
            HostResult {
                host_name: name,
                status,
                logs,
            }
        }));
    }

    let mut results = Vec::new();
    for h in handles {
        match h.await {
            Ok(r) => results.push(r),
            Err(e) => results.push(HostResult {
                host_name: "<unknown>".into(),
                status: TaskStatus::Failed(format!("任务 panic: {e}")),
                logs: vec![],
            }),
        }
    }

    // 按设备名排序，保证输出顺序稳定可比对
    results.sort_by(|a, b| a.host_name.cmp(&b.host_name));
    results
}

/// 打印聚合结果并返回是否全部成功。
pub fn report(results: &[HostResult]) -> bool {
    for r in results {
        for line in &r.logs {
            println!("{line}");
        }
    }

    println!("\n{}", "=".repeat(70));
    println!("  执行结果汇总");
    println!("{}", "=".repeat(70));

    let mut passed = 0;
    for r in results {
        println!(
            "  {} {}: {}",
            r.status.icon(),
            r.host_name,
            r.status.label()
        );
        if let TaskStatus::Failed(reason) = &r.status {
            println!("         原因: {reason}");
        }
        if matches!(r.status, TaskStatus::Success | TaskStatus::Skipped) {
            passed += 1;
        }
    }

    println!("\n  总计: {passed}/{} 通过", results.len());
    println!("{}", "=".repeat(70));

    passed == results.len()
}
