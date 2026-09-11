//! 阶段三之一：接口 IP 下发与回退
//!
//! 对应 Python 版 `scenes/test_ip_deploy_full.py`。
//!
//! H3C 混合路径：Loopback 接口用 CLI 创建，IP 地址用 NETCONF 配置。
//! 物理接口回退时**仅删除 IP，不改变 admin 状态**（幂等）。
//!
//! ## 与 Python 版的差异
//!
//! Python 版把下发/回退逻辑手写在 `task_deploy_netconf()` /
//! `task_rollback_netconf()` 里，未走 Atom 抽象。这里改为调用
//! `cwa_atoms` 的 Atom，从而获得幂等跳过、回退计划、依赖检查三项能力。

use cwa_atoms::h3c::{CmdLoopbackAtom, IpAddressParams, LoopbackParams, NetconfIpAddressAtom};
use cwa_atoms::{Atom, AtomStatus, SnapshotStore};
use cwa_inventory::Host;
use cwa_templating::SceneApi;
use cwa_transport::{CliSession, NetconfSession};
use serde::{Deserialize, Serialize};

use crate::plan::DevicePlan;
use crate::runner::{TaskLog, TaskStatus};

/// 本场景的完整快照，用于全量回退。
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct IpSceneSnapshot {
    pub loopback: Option<cwa_atoms::h3c::LoopbackSnapshot>,
    pub loopback_ip: Option<cwa_atoms::h3c::IpAddressSnapshot>,
    pub interfaces: Vec<cwa_atoms::h3c::IpAddressSnapshot>,
}

/// 快照作用域名。
pub const SCOPE: &str = "ip";

/// 预览将要下发的配置。
pub fn preview(host: &Host, scene: &SceneApi, plan: &DevicePlan) -> Vec<String> {
    let mut log = TaskLog::new();
    log.header(&format!("预览: {}", host.name));

    let lb_name = format!("LoopBack{}", plan.loopback.number);
    log.section(&format!(
        "Loopback {} → {}/{}",
        lb_name, plan.loopback.ip, plan.loopback.mask
    ));

    // 接口创建命令（CLI）
    let ctx = cwa_templating::ctx([
        ("ifname", serde_json::json!(lb_name)),
        (
            "description",
            serde_json::json!(format!("{} Loopback", host.name)),
        ),
    ]);
    match scene.direct_render(
        host,
        cwa_templating::paths::CMD_INTERFACE_CREATE,
        Some(&ctx),
    ) {
        Ok(t) => {
            log.line("  [CLI] 接口创建:");
            for l in t.lines() {
                log.line(format!("    {l}"));
            }
        }
        Err(e) => log.line(format!("  接口创建渲染失败: {e}")),
    }

    // IP 配置片段（NETCONF）。ifindex 未知时用占位值展示结构。
    let ctx = cwa_templating::ctx([
        ("ifindex", serde_json::json!("<运行时查询>")),
        ("ipv4_address", serde_json::json!(plan.loopback.ip)),
        ("ipv4_mask", serde_json::json!(plan.loopback.mask)),
        ("operation", serde_json::json!("merge")),
    ]);
    match scene.direct_render(host, cwa_templating::paths::NC_IP_ADDRESS_ATOM, Some(&ctx)) {
        Ok(t) => {
            log.line("  [NETCONF] IP 配置:");
            for l in t.lines() {
                log.line(format!("    {l}"));
            }
        }
        Err(e) => log.line(format!("  IP 渲染失败: {e}")),
    }

    log.section("物理接口");
    for iface in &plan.interfaces {
        log.line(format!("  {} → {}/{}", iface.ifname, iface.ip, iface.mask));
    }

    log.line("\n预览结束（未实际下发）");
    log.into_lines()
}

/// 下发：CLI 建 Loopback + NETCONF 配 IP。
pub async fn deploy(
    host: Host,
    scene: &SceneApi,
    plan: DevicePlan,
    store: &SnapshotStore,
) -> (TaskStatus, Vec<String>) {
    let mut log = TaskLog::new();
    log.header(&format!("IP 下发: {}", host.name));

    let mut snapshot = IpSceneSnapshot::default();

    // ---- 1. CLI 建 Loopback ----
    let cli = match CliSession::connect(&host).await {
        Ok(c) => c,
        Err(e) => {
            log.line(format!("  SSH 连接失败: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };

    let lb_atom = CmdLoopbackAtom::new(scene);
    let lb_params = LoopbackParams {
        number: plan.loopback.number,
        description: Some(format!("{} Loopback", host.name)),
    };

    log.section(&format!("Loopback{} 创建", plan.loopback.number));
    let mut cli_channel = cli;
    match lb_atom
        .execute(&host, &mut cli_channel, &lb_params, true)
        .await
    {
        Ok(AtomStatus::Success) => {
            log.line("  已创建");
            // 记录快照：本次新建，回退时需删除
            snapshot.loopback = Some(cwa_atoms::h3c::LoopbackSnapshot {
                ifname: format!("LoopBack{}", plan.loopback.number),
                existed_before: false,
            });
        }
        Ok(AtomStatus::Skipped { reason }) => {
            log.line(format!("  跳过: {reason}"));
            // 已存在，回退时不应删除接口
            snapshot.loopback = Some(cwa_atoms::h3c::LoopbackSnapshot {
                ifname: format!("LoopBack{}", plan.loopback.number),
                existed_before: true,
            });
        }
        Ok(other) => {
            log.line(format!("  失败: {}", other.label()));
            return (
                TaskStatus::Failed(format!("Loopback 创建 {}", other.label())),
                log.into_lines(),
            );
        }
        Err(e) => {
            log.line(format!("  异常: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    }

    // ---- 2. NETCONF 配 IP ----
    let mut nc = match NetconfSession::connect(&host).await {
        Ok(s) => s,
        Err(e) => {
            log.line(format!("  NETCONF 连接失败: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };

    // 重新查询 ifindex（Loopback 刚创建，需拿到真实索引）
    let ifindex_map = match nc.ifindex_map().await {
        Ok(m) => m,
        Err(e) => {
            log.line(format!("  ifindex 查询失败: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };
    log.line(format!("  已获取 {} 个接口的 ifindex", ifindex_map.len()));

    let lookup = |name: &str| -> Option<i64> {
        ifindex_map
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, i)| *i)
    };

    let ip_atom = NetconfIpAddressAtom::new(scene);
    let mut failed: Option<String> = None;

    // Loopback IP
    let lb_name = format!("LoopBack{}", plan.loopback.number);
    match lookup(&lb_name) {
        Some(idx) => {
            log.section(&format!("{lb_name} IP 配置 (ifindex {idx})"));
            let params = IpAddressParams {
                ifindex: idx,
                ipv4_address: plan.loopback.ip.clone(),
                ipv4_mask: plan.loopback.mask.clone(),
            };
            match run_ip_atom(&ip_atom, &host, &mut nc, &params, &mut log).await {
                Ok(Some(snap)) => snapshot.loopback_ip = Some(snap),
                Ok(None) => {}
                Err(e) => failed = Some(e),
            }
        }
        None => log.line(format!("  未找到 {lb_name} 的 ifindex，跳过 IP 配置")),
    }

    // 物理接口 IP
    if failed.is_none() {
        log.section("物理接口 IP 配置");
        for iface in &plan.interfaces {
            match lookup(&iface.ifname) {
                Some(idx) => {
                    log.line(format!("  {} (ifindex {idx})", iface.ifname));
                    let params = IpAddressParams {
                        ifindex: idx,
                        ipv4_address: iface.ip.clone(),
                        ipv4_mask: iface.mask.clone(),
                    };
                    match run_ip_atom(&ip_atom, &host, &mut nc, &params, &mut log).await {
                        Ok(Some(snap)) => snapshot.interfaces.push(snap),
                        Ok(None) => {}
                        Err(e) => {
                            failed = Some(e);
                            break;
                        }
                    }
                }
                None => log.line(format!("  {} 不在 ifindex 映射中，跳过", iface.ifname)),
            }
        }
    }

    // ---- 3. 保存快照 ----
    match store.save(&host.name, SCOPE, &snapshot) {
        Ok(p) => log.line(format!("\n  快照已保存: {}", p.display())),
        Err(e) => log.line(format!("\n  快照保存失败: {e}")),
    }

    match failed {
        Some(reason) => {
            log.line("\n状态: FAILED");
            (TaskStatus::Failed(reason), log.into_lines())
        }
        None => {
            log.line("\n状态: SUCCESS");
            (TaskStatus::Success, log.into_lines())
        }
    }
}

/// 执行一次 IP Atom，返回快照（跳过时为 `None`）。
async fn run_ip_atom(
    atom: &NetconfIpAddressAtom<'_>,
    host: &Host,
    nc: &mut NetconfSession,
    params: &IpAddressParams,
    log: &mut TaskLog,
) -> Result<Option<cwa_atoms::h3c::IpAddressSnapshot>, String> {
    // 先取 pre_check 结果以拿到快照，再执行
    let outcome = atom
        .pre_check(host, nc, params)
        .await
        .map_err(|e| e.to_string())?;

    match outcome {
        cwa_atoms::PreCheckOutcome::Skipped { reason } => {
            log.line(format!("     跳过: {reason}"));
            Ok(None)
        }
        cwa_atoms::PreCheckOutcome::Blocked { reason } => {
            log.line(format!("     阻止: {reason}"));
            Err(reason)
        }
        cwa_atoms::PreCheckOutcome::Ready { snapshot, desired } => {
            atom.deploy(host, nc, &desired)
                .await
                .map_err(|e| e.to_string())?;
            atom.post_check(host, nc, &desired)
                .await
                .map_err(|e| e.to_string())?;
            log.line("     已配置并校验通过");
            Ok(Some(snapshot))
        }
    }
}

/// 回退：基于快照全量回退。
pub async fn rollback(
    host: Host,
    scene: &SceneApi,
    store: &SnapshotStore,
) -> (TaskStatus, Vec<String>) {
    let mut log = TaskLog::new();
    log.header(&format!("IP 回退: {}", host.name));

    let snapshot: IpSceneSnapshot = match store.load(&host.name, SCOPE) {
        Ok(Some(s)) => s,
        Ok(None) => {
            log.line("  未找到快照，跳过回退");
            return (TaskStatus::Skipped, log.into_lines());
        }
        Err(e) => {
            log.line(format!("  快照读取失败: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };

    let mut nc = match NetconfSession::connect(&host).await {
        Ok(s) => s,
        Err(e) => {
            log.line(format!("  NETCONF 连接失败: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };

    let ip_atom = NetconfIpAddressAtom::new(scene);
    let mut errors = Vec::new();

    // 逆序回退物理接口 IP
    log.section("物理接口 IP 回退（逆序）");
    for snap in snapshot.interfaces.iter().rev() {
        let plan = match ip_atom.plan_rollback(&host, snap) {
            Ok(p) => p,
            Err(e) => {
                errors.push(e.to_string());
                continue;
            }
        };
        log.line(format!("  ifindex {}:", snap.ifindex));
        log.line(plan.describe());
        if let Err(e) = ip_atom.apply_rollback(&host, &mut nc, &plan).await {
            log.line(format!("     失败: {e}"));
            errors.push(e.to_string());
        } else {
            log.line("     已回退（接口 admin 状态保持不变）");
        }
    }

    // 回退 Loopback IP
    if let Some(snap) = &snapshot.loopback_ip {
        log.section("Loopback IP 回退");
        match ip_atom.plan_rollback(&host, snap) {
            Ok(plan) => {
                log.line(plan.describe());
                if let Err(e) = ip_atom.apply_rollback(&host, &mut nc, &plan).await {
                    log.line(format!("     失败: {e}"));
                    errors.push(e.to_string());
                } else {
                    log.line("     已回退");
                }
            }
            Err(e) => errors.push(e.to_string()),
        }
    }

    // 回退 Loopback 接口本身（仅当是本次创建的）
    if let Some(snap) = &snapshot.loopback {
        log.section("Loopback 接口回退");
        let lb_atom = CmdLoopbackAtom::new(scene);
        match lb_atom.plan_rollback(&host, snap) {
            Ok(plan) if plan.is_empty() => {
                log.line("  接口在变更前已存在，不删除");
            }
            Ok(plan) => {
                log.line(plan.describe());
                match CliSession::connect(&host).await {
                    Ok(mut cli) => {
                        if let Err(e) = lb_atom.apply_rollback(&host, &mut cli, &plan).await {
                            log.line(format!("     失败: {e}"));
                            errors.push(e.to_string());
                        } else {
                            log.line("     已删除");
                        }
                    }
                    Err(e) => {
                        log.line(format!("     SSH 连接失败: {e}"));
                        errors.push(e.to_string());
                    }
                }
            }
            Err(e) => errors.push(e.to_string()),
        }
    }

    if errors.is_empty() {
        match store.remove(&host.name, SCOPE) {
            Ok(true) => log.line("\n  快照已删除"),
            Ok(false) => {}
            Err(e) => log.line(format!("\n  快照删除失败: {e}")),
        }
        log.line("\n状态: SUCCESS");
        (TaskStatus::Success, log.into_lines())
    } else {
        log.line("\n状态: FAILED");
        log.line("  快照保留，可修正后重试回退");
        (TaskStatus::Failed(errors.join("; ")), log.into_lines())
    }
}
