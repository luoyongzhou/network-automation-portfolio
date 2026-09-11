//! `cwa` —— Comware 自动化命令行入口
//!
//! 替代 Python 版散落的多个脚本入口（`bootstrap_step1.py` / `bootstrap_step2.py` /
//! `test_ip_deploy_full.py` / `test_ospf_deploy_full.py` / `preview_bootstrap.py`），
//! 统一为单一二进制 + 子命令。

use std::path::PathBuf;

use clap::{Parser, Subcommand};
use cwa_atoms::SnapshotStore;
use cwa_inventory::Inventory;
use cwa_scenes::{
    plan::DevicePlans, report, step1_console, step2_netconf, step3_ip, step3_ospf, TaskStatus,
};
use cwa_templating::{SceneApi, TemplateRenderer};

#[derive(Parser)]
#[command(
    name = "cwa",
    about = "H3C Comware 网络设备自动化（Rust 实现）",
    long_about = None,
    version
)]
struct Cli {
    /// 项目根目录（包含 inventory/ 与 templates/）
    #[arg(long, default_value = ".", global = true)]
    root: PathBuf,

    /// 指定设备，可重复；缺省为 inventory 中全部设备
    #[arg(long, short = 'd', global = true)]
    device: Vec<String>,

    /// 并发数
    #[arg(long, default_value_t = 10, global = true)]
    workers: usize,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// 展开并打印 Group 继承结果（对应 build_inventory.py）
    Groups,

    /// 渲染任意场景模板但不下发（对应 preview_bootstrap.py）
    Preview {
        /// 场景名
        #[arg(value_enum)]
        scene: PreviewScene,
    },

    /// 阶段一：Console 零配置开局
    Console {
        /// 仅预览，不下发
        #[arg(long)]
        dry_run: bool,
    },

    /// 阶段二：SSH 下发使能 NETCONF
    EnableNetconf {
        #[arg(long)]
        dry_run: bool,
    },

    /// 阶段三之一：接口 IP 下发
    DeployIp {
        #[arg(long)]
        dry_run: bool,
    },

    /// 阶段三之一：接口 IP 回退
    RollbackIp,

    /// 阶段三之二：OSPF 下发
    DeployOspf {
        #[arg(long)]
        dry_run: bool,
    },

    /// 阶段三之二：OSPF 回退
    RollbackOspf,
}

#[derive(Clone, Copy, clap::ValueEnum)]
enum PreviewScene {
    /// 开局配置 cmd/bootstrap.j2
    Bootstrap,
    /// NETCONF 使能 cmd/netconf_cmd.j2
    NetconfCmd,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .with_target(false)
        .init();

    let cli = Cli::parse();
    let root = cli.root.canonicalize().unwrap_or(cli.root.clone());

    // ---- 加载 inventory ----
    let inventory = Inventory::load(&root)?;
    let inventory = if cli.device.is_empty() {
        inventory
    } else {
        inventory.filter_names(&cli.device)?
    };

    if inventory.hosts.is_empty() {
        anyhow::bail!("没有匹配的设备");
    }

    // ---- 模板引擎 ----
    let templates_root = root.join("templates");
    let renderer = TemplateRenderer::new(&templates_root)?;
    let scene = SceneApi::new(renderer);

    let plans = DevicePlans::load(&root);
    let store = SnapshotStore::new(&root);

    match cli.command {
        Commands::Groups => {
            let (groups, rep) = cwa_inventory::build_groups(&root.join("inventory/groups"))?;
            for w in &rep.warnings {
                eprintln!("警告: {w}");
            }
            print!("{}", cwa_inventory::dump_groups_yaml(&groups)?);
        }

        Commands::Preview { scene: which } => {
            for (name, host) in &inventory.hosts {
                println!("\n=== 设备: {name} ===");
                let out = match which {
                    PreviewScene::Bootstrap => scene.bootstrap(host),
                    PreviewScene::NetconfCmd => scene.ssh_bootstrap(host),
                };
                match out {
                    Ok(text) => println!("{text}"),
                    Err(e) => println!("渲染失败: {e}"),
                }
            }
        }

        Commands::Console { dry_run } => {
            if dry_run {
                step1_console::preview(&inventory, &scene)?;
            } else {
                // 渲染在主线程完成，避免把 SceneApi 跨线程传递
                let mut rendered = Vec::new();
                for host in inventory.hosts.values() {
                    let text = scene.bootstrap(host)?;
                    rendered.push((host.clone(), text));
                }

                let results = run_serial(rendered, step1_console::deploy).await;
                if !report(&results) {
                    std::process::exit(1);
                }
            }
        }

        Commands::EnableNetconf { dry_run } => {
            if dry_run {
                step2_netconf::preview(&inventory, &scene)?;
            } else {
                let mut rendered = Vec::new();
                for host in inventory.hosts.values() {
                    let text = scene.ssh_bootstrap(host)?;
                    rendered.push((host.clone(), text));
                }

                let results = run_concurrent_pairs(rendered, cli.workers, |host, text| {
                    step2_netconf::deploy(host, text)
                })
                .await;
                if !report(&results) {
                    std::process::exit(1);
                }
            }
        }

        Commands::DeployIp { dry_run } => {
            let mut results = Vec::new();
            for host in inventory.hosts.values() {
                let Some(plan) = plans.get(&host.name) else {
                    eprintln!("设备 {} 无 IP 规划，跳过", host.name);
                    continue;
                };
                if dry_run {
                    for line in step3_ip::preview(host, &scene, plan) {
                        println!("{line}");
                    }
                } else {
                    let (status, logs) =
                        step3_ip::deploy(host.clone(), &scene, plan.clone(), &store).await;
                    results.push(cwa_scenes::HostResult {
                        host_name: host.name.clone(),
                        status,
                        logs,
                    });
                }
            }
            if !dry_run && !report(&results) {
                std::process::exit(1);
            }
        }

        Commands::RollbackIp => {
            let mut results = Vec::new();
            for host in inventory.hosts.values() {
                let (status, logs) = step3_ip::rollback(host.clone(), &scene, &store).await;
                results.push(cwa_scenes::HostResult {
                    host_name: host.name.clone(),
                    status,
                    logs,
                });
            }
            if !report(&results) {
                std::process::exit(1);
            }
        }

        Commands::DeployOspf { dry_run } => {
            let mut results = Vec::new();
            for host in inventory.hosts.values() {
                let Some(plan) = plans.get(&host.name) else {
                    eprintln!("设备 {} 无 IP 规划，跳过", host.name);
                    continue;
                };
                if dry_run {
                    for line in step3_ospf::preview(host, &scene, plan) {
                        println!("{line}");
                    }
                } else {
                    let (status, logs) =
                        step3_ospf::deploy(host.clone(), &scene, plan.clone(), &store).await;
                    results.push(cwa_scenes::HostResult {
                        host_name: host.name.clone(),
                        status,
                        logs,
                    });
                }
            }
            if !dry_run && !report(&results) {
                std::process::exit(1);
            }
        }

        Commands::RollbackOspf => {
            let mut results = Vec::new();
            for host in inventory.hosts.values() {
                let (status, logs) = step3_ospf::rollback(host.clone(), &scene, &store).await;
                results.push(cwa_scenes::HostResult {
                    host_name: host.name.clone(),
                    status,
                    logs,
                });
            }
            if !report(&results) {
                std::process::exit(1);
            }
        }
    }

    Ok(())
}

/// 并发执行"已渲染文本 + Host"配对任务。
async fn run_concurrent_pairs<F, Fut>(
    pairs: Vec<(cwa_inventory::Host, String)>,
    workers: usize,
    task: F,
) -> Vec<cwa_scenes::HostResult>
where
    F: Fn(cwa_inventory::Host, String) -> Fut + Clone + Send + 'static,
    Fut: std::future::Future<Output = (TaskStatus, Vec<String>)> + Send,
{
    use std::sync::Arc;
    use tokio::sync::Semaphore;

    let sem = Arc::new(Semaphore::new(workers.max(1)));
    let mut handles = Vec::new();

    for (host, text) in pairs {
        let sem = sem.clone();
        let task = task.clone();
        let name = host.name.clone();
        handles.push(tokio::spawn(async move {
            let _p = sem.acquire().await.expect("semaphore");
            let (status, logs) = task(host, text).await;
            cwa_scenes::HostResult {
                host_name: name,
                status,
                logs,
            }
        }));
    }

    let mut out = Vec::new();
    for h in handles {
        if let Ok(r) = h.await {
            out.push(r);
        }
    }
    out.sort_by(|a, b| a.host_name.cmp(&b.host_name));
    out
}

/// 串行执行。
///
/// Console 开局刻意串行：多台设备共用一台串口服务器时并发连接容易互相干扰，
/// 且开局阶段本身是低频操作，串行更可控。
async fn run_serial<F, Fut>(
    pairs: Vec<(cwa_inventory::Host, String)>,
    task: F,
) -> Vec<cwa_scenes::HostResult>
where
    F: Fn(cwa_inventory::Host, String) -> Fut,
    Fut: std::future::Future<Output = (TaskStatus, Vec<String>)>,
{
    let mut out = Vec::new();
    for (host, text) in pairs {
        let name = host.name.clone();
        let (status, logs) = task(host, text).await;
        out.push(cwa_scenes::HostResult {
            host_name: name,
            status,
            logs,
        });
    }
    out
}
