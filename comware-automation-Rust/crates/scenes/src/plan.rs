//! 设备 IP 规划表
//!
//! 对应 Python 版 `scenes/test_ip_deploy_full.py` 顶部的 `DEVICE_CONFIG` 字典。
//!
//! ## 与 Python 版的差异
//!
//! Python 版把这份规划**硬编码在场景脚本里**，且 `test_ospf_deploy_full.py`
//! 通过 `from scenes.test_ip_deploy_full import DEVICE_CONFIG` 反向依赖它，
//! 形成了场景脚本之间的耦合。
//!
//! 这里改为从 YAML 外部化加载（`plan.yaml`），场景之间只依赖数据不依赖彼此。
//! 若文件不存在则回落到与 Python 版一致的内置默认值，保证行为等价。

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};

/// Loopback 规划。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoopbackPlan {
    pub number: u32,
    pub ip: String,
    pub mask: String,
}

/// 物理接口规划。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InterfacePlan {
    pub ifname: String,
    pub ip: String,
    pub mask: String,
}

/// 单台设备的规划。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DevicePlan {
    pub loopback: LoopbackPlan,
    pub interfaces: Vec<InterfacePlan>,
}

/// 全部设备规划。
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DevicePlans {
    #[serde(flatten)]
    pub devices: BTreeMap<String, DevicePlan>,
}

impl DevicePlans {
    /// 从 `<project_root>/plan.yaml` 加载，不存在则用内置默认值。
    pub fn load(project_root: &Path) -> Self {
        let path = project_root.join("plan.yaml");
        if path.exists() {
            match std::fs::read_to_string(&path) {
                Ok(text) => match serde_yaml_ng::from_str::<Self>(&text) {
                    Ok(p) => {
                        tracing::debug!(path = %path.display(), "已加载外部规划表");
                        return p;
                    }
                    Err(e) => tracing::warn!(error = %e, "规划表解析失败，回落内置默认值"),
                },
                Err(e) => tracing::warn!(error = %e, "规划表读取失败，回落内置默认值"),
            }
        }
        Self::builtin()
    }

    pub fn get(&self, device: &str) -> Option<&DevicePlan> {
        self.devices.get(device)
    }

    pub fn names(&self) -> Vec<String> {
        self.devices.keys().cloned().collect()
    }

    /// 内置默认规划，数值与 Python 版 `DEVICE_CONFIG` 完全一致。
    pub fn builtin() -> Self {
        let mut devices = BTreeMap::new();

        devices.insert(
            "H3C-SR88-01".to_string(),
            DevicePlan {
                loopback: LoopbackPlan {
                    number: 1,
                    ip: "11.11.11.11".into(),
                    mask: "255.255.255.255".into(),
                },
                interfaces: vec![
                    InterfacePlan {
                        ifname: "GigabitEthernet0/0/1".into(),
                        ip: "10.0.0.0".into(),
                        mask: "255.255.255.254".into(),
                    },
                    InterfacePlan {
                        ifname: "GigabitEthernet0/0/2".into(),
                        ip: "10.0.0.2".into(),
                        mask: "255.255.255.254".into(),
                    },
                ],
            },
        );

        devices.insert(
            "H3C-SR88-02".to_string(),
            DevicePlan {
                loopback: LoopbackPlan {
                    number: 1,
                    ip: "22.22.22.22".into(),
                    mask: "255.255.255.255".into(),
                },
                interfaces: vec![
                    InterfacePlan {
                        ifname: "GigabitEthernet0/0/1".into(),
                        ip: "10.0.0.1".into(),
                        mask: "255.255.255.254".into(),
                    },
                    InterfacePlan {
                        ifname: "GigabitEthernet0/0/2".into(),
                        ip: "10.0.0.4".into(),
                        mask: "255.255.255.254".into(),
                    },
                ],
            },
        );

        devices.insert(
            "H3C-SR88-03".to_string(),
            DevicePlan {
                loopback: LoopbackPlan {
                    number: 1,
                    ip: "33.33.33.33".into(),
                    mask: "255.255.255.255".into(),
                },
                interfaces: vec![
                    InterfacePlan {
                        ifname: "GigabitEthernet0/0/1".into(),
                        ip: "10.0.0.3".into(),
                        mask: "255.255.255.254".into(),
                    },
                    InterfacePlan {
                        ifname: "GigabitEthernet0/0/2".into(),
                        ip: "10.0.0.5".into(),
                        mask: "255.255.255.254".into(),
                    },
                ],
            },
        );

        Self { devices }
    }
}
