//! 场景编排层

pub mod plan;
pub mod runner;
pub mod step1_console;
pub mod step2_netconf;
pub mod step3_ip;
pub mod step3_ospf;

pub use plan::{DevicePlan, DevicePlans, InterfacePlan, LoopbackPlan};
pub use runner::{report, run_concurrent, HostResult, TaskLog, TaskStatus};
