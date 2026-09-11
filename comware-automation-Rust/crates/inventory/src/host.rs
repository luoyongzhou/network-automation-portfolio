//! Host / Group / defaults 三级数据模型
//!
//! 对应 Nornir 的 Inventory 语义。项目实际用到的解析点有限，这里只实现
//! 这些点，但保持与 Nornir 相同的优先级：**host > group（按声明顺序）> defaults**。

use std::collections::BTreeMap;
use std::path::Path;

use serde_yaml_ng::{Mapping, Value};

use crate::error::InventoryError;
use crate::groups;

/// 一个命名连接的选项，对应 Nornir 的 `connection_options`。
#[derive(Debug, Clone, Default)]
pub struct ConnectionOptions {
    pub hostname: Option<String>,
    pub port: Option<u16>,
    pub username: Option<String>,
    pub password: Option<String>,
    pub platform: Option<String>,
    /// `extras` 原样保留，例如 `device_type`、`timeout`、`global_delay_factor`。
    pub extras: Mapping,
}

impl ConnectionOptions {
    fn from_mapping(m: &Mapping) -> Self {
        Self {
            hostname: get_str(m, "hostname"),
            port: m
                .get(Value::from("port"))
                .and_then(|v| v.as_u64())
                .map(|p| p as u16),
            username: get_str(m, "username"),
            password: get_str(m, "password"),
            platform: get_str(m, "platform"),
            extras: m
                .get(Value::from("extras"))
                .and_then(|v| v.as_mapping())
                .cloned()
                .unwrap_or_default(),
        }
    }

    /// 读取 `extras` 中的整数项（如 `timeout`）。
    pub fn extra_u64(&self, key: &str) -> Option<u64> {
        self.extras.get(Value::from(key)).and_then(|v| v.as_u64())
    }

    /// 读取 `extras` 中的字符串项（如 `device_type`）。
    pub fn extra_str(&self, key: &str) -> Option<String> {
        self.extras
            .get(Value::from(key))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
    }
}

/// 一台设备。字段已按 host > group > defaults 解析完毕。
#[derive(Debug, Clone)]
pub struct Host {
    pub name: String,
    pub hostname: String,
    pub username: String,
    pub password: String,
    pub platform: Option<String>,
    pub port: Option<u16>,
    /// 该 Host 所属 Group 名称，按 `hosts.yaml` 中声明顺序。
    pub groups: Vec<String>,
    /// 合并后的 `data` 段。
    pub data: Mapping,
    /// 合并后的命名连接选项。
    pub connection_options: BTreeMap<String, ConnectionOptions>,
}

impl Host {
    /// 读取 `data` 中的字符串项。
    pub fn data_str(&self, key: &str) -> Option<String> {
        self.data
            .get(Value::from(key))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
    }

    /// 取指定命名连接，例如 `netmiko_oob` / `ncclient`。
    pub fn connection(&self, name: &str) -> Option<&ConnectionOptions> {
        self.connection_options.get(name)
    }

    /// NETCONF 端口：优先 `ncclient` 连接选项，否则默认 830。
    pub fn netconf_port(&self) -> u16 {
        self.connection("ncclient")
            .and_then(|c| c.port)
            .unwrap_or(830)
    }

    /// NETCONF 超时秒数：优先 `ncclient.extras.timeout`，否则 60。
    pub fn netconf_timeout(&self) -> u64 {
        self.connection("ncclient")
            .and_then(|c| c.extra_u64("timeout"))
            .unwrap_or(60)
    }

    /// SSH 端口：优先 `netmiko` 连接选项，否则 `host.port`，否则 22。
    pub fn ssh_port(&self) -> u16 {
        self.connection("netmiko")
            .and_then(|c| c.port)
            .or(self.port)
            .unwrap_or(22)
    }
}

/// 完整的 Inventory。
#[derive(Debug, Clone)]
pub struct Inventory {
    pub hosts: BTreeMap<String, Host>,
}

impl Inventory {
    /// 从项目根目录加载。目录结构与 Python 版一致：
    /// `inventory/hosts.yaml`、`inventory/defaults.yaml`、`inventory/groups/`。
    ///
    /// 与 Python 版的差异：Group 继承在内存中展开，不依赖磁盘上的
    /// `inventory/groups.yaml` 中间产物。
    pub fn load(project_root: &Path) -> Result<Self, InventoryError> {
        let inv_dir = project_root.join("inventory");

        let (expanded_groups, report) = groups::build_groups(&inv_dir.join("groups"))?;
        for w in &report.warnings {
            tracing::warn!("{}", w);
        }

        let defaults = read_mapping(&inv_dir.join("defaults.yaml"))?.unwrap_or_default();
        let hosts_raw = read_mapping(&inv_dir.join("hosts.yaml"))?.unwrap_or_default();

        let mut hosts = BTreeMap::new();

        for (name_val, host_val) in &hosts_raw {
            let Some(name) = name_val.as_str() else {
                continue;
            };
            let Some(host_map) = host_val.as_mapping() else {
                return Err(InventoryError::NotAMapping(name.to_string()));
            };

            let group_names: Vec<String> = host_map
                .get(Value::from("groups"))
                .and_then(|v| v.as_sequence())
                .map(|seq| {
                    seq.iter()
                        .filter_map(|v| v.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .unwrap_or_default();

            // 按 Nornir 优先级自低到高叠加：defaults → groups（声明顺序）→ host
            let mut merged = defaults.clone();
            for g in &group_names {
                if let Some(gcfg) = expanded_groups.get(g) {
                    merged = groups::deep_merge(&merged, gcfg);
                } else {
                    tracing::warn!(host = name, group = g, "引用了未定义的 Group");
                }
            }
            merged = groups::deep_merge(&merged, host_map);

            let hostname = get_str(&merged, "hostname").ok_or_else(|| {
                InventoryError::MissingHostField(name.to_string(), "hostname".into())
            })?;

            let data = merged
                .get(Value::from("data"))
                .and_then(|v| v.as_mapping())
                .cloned()
                .unwrap_or_default();

            let mut connection_options = BTreeMap::new();
            if let Some(co) = merged
                .get(Value::from("connection_options"))
                .and_then(|v| v.as_mapping())
            {
                for (cname, cval) in co {
                    if let (Some(cname), Some(cmap)) = (cname.as_str(), cval.as_mapping()) {
                        connection_options
                            .insert(cname.to_string(), ConnectionOptions::from_mapping(cmap));
                    }
                }
            }

            hosts.insert(
                name.to_string(),
                Host {
                    name: name.to_string(),
                    hostname,
                    username: get_str(&merged, "username").unwrap_or_default(),
                    password: get_str(&merged, "password").unwrap_or_default(),
                    platform: get_str(&merged, "platform"),
                    port: merged
                        .get(Value::from("port"))
                        .and_then(|v| v.as_u64())
                        .map(|p| p as u16),
                    groups: group_names,
                    data,
                    connection_options,
                },
            );
        }

        Ok(Self { hosts })
    }

    /// 按名称过滤，对应 Python 版 `nr.filter(F(name=...))` 与手动替换 hosts 字典。
    pub fn filter_names(&self, names: &[String]) -> Result<Self, InventoryError> {
        let mut out = BTreeMap::new();
        for n in names {
            let host = self
                .hosts
                .get(n)
                .ok_or_else(|| InventoryError::UnknownHost(n.clone()))?;
            out.insert(n.clone(), host.clone());
        }
        Ok(Self { hosts: out })
    }

    pub fn names(&self) -> Vec<String> {
        self.hosts.keys().cloned().collect()
    }
}

fn read_mapping(path: &Path) -> Result<Option<Mapping>, InventoryError> {
    if !path.exists() {
        return Ok(None);
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| InventoryError::Io(path.to_path_buf(), e.to_string()))?;
    let parsed: Option<Mapping> = serde_yaml_ng::from_str(&text)
        .map_err(|e| InventoryError::Yaml(path.to_path_buf(), e.to_string()))?;
    Ok(parsed)
}

fn get_str(m: &Mapping, key: &str) -> Option<String> {
    m.get(Value::from(key))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}
