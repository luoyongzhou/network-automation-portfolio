//! 阶段三之二：OSPF 下发与回退
//!
//! 对应 Python 版 `scenes/test_ospf_deploy_full.py`。
//!
//! 下发用 `rollback-on-error` 保证单次 RPC 原子性；
//! 回退逐层删除接口 → 区域 → 进程，只发索引列。
//!
//! ## 与 Python 版的差异
//!
//! Python 版此场景完全绕过 Atom 抽象，且 `from scenes.test_ip_deploy_full
//! import DEVICE_CONFIG` 反向依赖 IP 场景脚本。这里改为：
//!   - 走 `NetconfOspfAtom`，获得幂等判断与回退前依赖检查
//!   - 规划数据由 `plan.rs` 统一提供，场景之间无相互依赖

use cwa_atoms::h3c::{
    NetconfOspfAtom, OspfArea, OspfConfig, OspfInterface, OspfParams, OspfSnapshot,
};
use cwa_atoms::{Atom, PreCheckOutcome, SnapshotStore};
use cwa_inventory::Host;
use cwa_templating::SceneApi;
use cwa_transport::NetconfSession;

use crate::plan::DevicePlan;
use crate::runner::{TaskLog, TaskStatus};

pub const SCOPE: &str = "ospf";

/// 根据 ifindex 映射构造 OSPF 配置。
///
/// 与 Python 版 `build_ospf_config()` 一致：router-id 取 Loopback IP，
/// Loopback 接口不设 network_type（用默认 broadcast），
/// 物理接口设 network_type = 3（P2P）。
pub fn build_ospf_config(plan: &DevicePlan, ifindex_map: &[(String, i64)]) -> OspfConfig {
    let lookup = |name: &str| -> Option<i64> {
        ifindex_map
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, i)| *i)
    };

    let mut interfaces = Vec::new();

    let lb_name = format!("LoopBack{}", plan.loopback.number);
    if let Some(idx) = lookup(&lb_name) {
        interfaces.push(OspfInterface {
            ifindex: idx,
            network_type: None,
            cost: None,
            priority: None,
        });
    }

    for iface in &plan.interfaces {
        if let Some(idx) = lookup(&iface.ifname) {
            interfaces.push(OspfInterface {
                ifindex: idx,
                network_type: Some(3),
                cost: None,
                priority: None,
            });
        }
    }

    OspfConfig {
        instance_name: "1".to_string(),
        router_id: plan.loopback.ip.clone(),
        areas: vec![OspfArea {
            area_id: "0.0.0.0".to_string(),
            area_type: 0,
            interfaces,
        }],
    }
}

/// 预览 OSPF 配置 XML。
///
/// ifindex 需要连设备才能拿到，预览时用规划表里的接口名占位，
/// 展示 XML 结构而非真实索引。
pub fn preview(host: &Host, scene: &SceneApi, plan: &DevicePlan) -> Vec<String> {
    let mut log = TaskLog::new();
    log.header(&format!("预览: {} OSPF 配置", host.name));

    // 用序号占位，仅用于展示结构
    let placeholder: Vec<(String, i64)> =
        std::iter::once((format!("LoopBack{}", plan.loopback.number), 9000))
            .chain(
                plan.interfaces
                    .iter()
                    .enumerate()
                    .map(|(i, f)| (f.ifname.clone(), 9001 + i as i64)),
            )
            .collect();

    log.line("  注意: 以下 ifindex 为占位值，实际下发时从设备查询");
    let cfg = build_ospf_config(plan, &placeholder);
    log.line(format!("  router-id: {}", cfg.router_id));
    log.line(format!("  进程名: {}", cfg.instance_name));

    let atom = NetconfOspfAtom::new(scene);
    match atom.render_config(host, &cfg) {
        Ok(xml) => {
            log.line("");
            for l in xml.lines() {
                log.line(format!("  {l}"));
            }
        }
        Err(e) => log.line(format!("  渲染失败: {e}")),
    }

    // 展示回退计划——Python 版无此能力
    let snap = OspfSnapshot {
        instance_existed_before: false,
        applied: cfg,
    };
    log.section("回退计划（若需回退将执行）");
    match atom.plan_rollback(host, &snap) {
        Ok(plan) => log.line(plan.describe()),
        Err(e) => log.line(format!("  推导失败: {e}")),
    }

    log.line("\n预览结束（未实际下发）");
    log.into_lines()
}

/// 下发 OSPF 配置。
pub async fn deploy(
    host: Host,
    scene: &SceneApi,
    plan: DevicePlan,
    store: &SnapshotStore,
) -> (TaskStatus, Vec<String>) {
    let mut log = TaskLog::new();
    log.header(&format!("OSPF 下发: {}", host.name));

    let mut nc = match NetconfSession::connect(&host).await {
        Ok(s) => s,
        Err(e) => {
            log.line(format!("  NETCONF 连接失败: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };

    let ifindex_map = match nc.ifindex_map().await {
        Ok(m) => m,
        Err(e) => {
            log.line(format!("  ifindex 查询失败: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };
    log.line(format!("  已获取 {} 个接口的 ifindex", ifindex_map.len()));

    let cfg = build_ospf_config(&plan, &ifindex_map);
    let total_ifaces: usize = cfg.areas.iter().map(|a| a.interfaces.len()).sum();
    log.line(format!(
        "  router-id {} / {} 个接口纳入 area 0.0.0.0",
        cfg.router_id, total_ifaces
    ));

    let atom = NetconfOspfAtom::new(scene);
    let params = OspfParams { config: cfg };

    // 手动展开生命周期以便拿到快照并落盘
    let outcome = match atom.pre_check(&host, &mut nc, &params).await {
        Ok(o) => o,
        Err(e) => {
            log.line(format!("  预检查异常: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };

    let (snapshot, desired) = match outcome {
        PreCheckOutcome::Blocked { reason } => {
            log.line(format!("  预检查阻止: {reason}"));
            return (TaskStatus::Failed(reason), log.into_lines());
        }
        PreCheckOutcome::Skipped { reason } => {
            log.line(format!("  跳过: {reason}"));
            return (TaskStatus::Skipped, log.into_lines());
        }
        PreCheckOutcome::Ready { snapshot, desired } => (snapshot, desired),
    };

    if snapshot.instance_existed_before {
        log.line(format!(
            "  注意: OSPF 进程 {} 在本次变更前已存在，回退时将拒绝删除",
            snapshot.applied.instance_name
        ));
    }

    // 下发前先落盘快照，避免下发成功但进程被杀导致无快照可回退
    match store.save(&host.name, SCOPE, &snapshot) {
        Ok(p) => log.line(format!("  快照已保存: {}", p.display())),
        Err(e) => log.line(format!("  快照保存失败: {e}")),
    }

    log.line("  下发中（merge + rollback-on-error）...");
    if let Err(e) = atom.deploy(&host, &mut nc, &desired).await {
        log.line(format!("  下发失败（设备已自动回滚本次 RPC）: {e}"));
        return (TaskStatus::Failed(e.to_string()), log.into_lines());
    }
    log.line("  原子性提交成功");

    if let Err(e) = atom.post_check(&host, &mut nc, &desired).await {
        log.line(format!("  后检查失败: {e}"));
        return (TaskStatus::Failed(e.to_string()), log.into_lines());
    }
    log.line("  后检查通过");

    log.line("\n状态: SUCCESS");
    (TaskStatus::Success, log.into_lines())
}

/// 回退 OSPF 配置。
pub async fn rollback(
    host: Host,
    scene: &SceneApi,
    store: &SnapshotStore,
) -> (TaskStatus, Vec<String>) {
    let mut log = TaskLog::new();
    log.header(&format!("OSPF 回退: {}", host.name));

    let snapshot: OspfSnapshot = match store.load(&host.name, SCOPE) {
        Ok(Some(s)) => s,
        Ok(None) => {
            log.line("  未找到 OSPF 快照，跳过回退");
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

    let atom = NetconfOspfAtom::new(scene);

    // 回退前依赖检查——Python 版没有这一步
    match atom.rollback_guard(&host, &mut nc, &snapshot).await {
        Ok(cwa_atoms::RollbackGuard::Blocked { dependencies }) => {
            log.line("  回退被阻止:");
            for d in &dependencies {
                log.line(format!("    - {d}"));
            }
            log.line("\n状态: FAILED");
            return (
                TaskStatus::Failed(format!("回退被阻止: {}", dependencies.join("; "))),
                log.into_lines(),
            );
        }
        Ok(cwa_atoms::RollbackGuard::Safe) => {}
        Err(e) => {
            log.line(format!("  依赖检查异常: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    }

    let plan = match atom.plan_rollback(&host, &snapshot) {
        Ok(p) => p,
        Err(e) => {
            log.line(format!("  回退计划推导失败: {e}"));
            return (TaskStatus::Failed(e.to_string()), log.into_lines());
        }
    };

    log.section("回退计划");
    log.line(plan.describe());

    if let Err(e) = atom.apply_rollback(&host, &mut nc, &plan).await {
        log.line(format!("\n  回退失败: {e}"));
        log.line("  快照保留，可修正后重试");
        return (TaskStatus::Failed(e.to_string()), log.into_lines());
    }

    log.line("\n  已清理接口、区域、进程");
    match store.remove(&host.name, SCOPE) {
        Ok(true) => log.line("  快照已删除"),
        Ok(false) => {}
        Err(e) => log.line(format!("  快照删除失败: {e}")),
    }

    log.line("\n状态: SUCCESS");
    (TaskStatus::Success, log.into_lines())
}
