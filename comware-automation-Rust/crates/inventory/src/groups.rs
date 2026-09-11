//! Group 继承合并引擎
//!
//! 对应 Python 版 `scripts/build_inventory.py`。
//!
//! 流程与 Python 版严格一致：
//!   1. 递归扫描 `inventory/groups/**/*.yaml`，跳过 `_` 前缀文件
//!   2. 按路径深度 + 文件名排序加载（保证重复 Group 的覆盖顺序可复现）
//!   3. 递归展开 `parents` 链，`deep_merge` 深合并，检测循环继承
//!   4. 过滤：只保留来源目录为 `os_versions` 的 Group

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};

use serde_yaml_ng::{Mapping, Value};

use crate::error::InventoryError;

/// 递归深合并两个 YAML 映射，`override_map` 覆盖 `base`。
///
/// 与 Python 版 `deep_merge()` 行为一致：仅当双方同一 key 都是映射时才递归合并，
/// 否则由 override 整体替换（列表不做元素级合并）。
pub fn deep_merge(base: &Mapping, override_map: &Mapping) -> Mapping {
    let mut result = base.clone();

    for (key, value) in override_map {
        let merged = match (result.get(key), value) {
            (Some(Value::Mapping(base_inner)), Value::Mapping(override_inner)) => {
                Value::Mapping(deep_merge(base_inner, override_inner))
            }
            _ => value.clone(),
        };
        result.insert(key.clone(), merged);
    }

    result
}

/// 一份原始 Group 定义及其来源目录（`vendors` / `product_lines` / `os_versions`）。
#[derive(Debug, Clone)]
pub struct RawGroup {
    pub config: Mapping,
    /// 相对 `inventory/groups/` 的第一层目录名，用于最终过滤。
    pub source_dir: String,
}

/// 扫描并加载 `groups_root` 下所有 Group 定义。
pub fn load_raw_groups(groups_root: &Path) -> Result<BTreeMap<String, RawGroup>, InventoryError> {
    if !groups_root.is_dir() {
        return Err(InventoryError::MissingPath(groups_root.to_path_buf()));
    }

    let mut files = Vec::new();
    collect_yaml_files(groups_root, &mut files)?;

    // 与 Python 版排序键一致：(路径深度, 文件名)
    files.sort_by_key(|path| {
        let depth = path
            .strip_prefix(groups_root)
            .map(|rel| rel.components().count())
            .unwrap_or(0);
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        (depth, name)
    });

    let mut raw: BTreeMap<String, RawGroup> = BTreeMap::new();

    for path in files {
        // 跳过下划线前缀的模板文件
        let file_name = path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        if file_name.starts_with('_') {
            tracing::debug!(file = %path.display(), "跳过模板文件");
            continue;
        }

        let rel = path.strip_prefix(groups_root).unwrap_or(&path);
        let source_dir = rel
            .components()
            .next()
            .map(|c| c.as_os_str().to_string_lossy().to_string())
            .unwrap_or_default();

        let text = std::fs::read_to_string(&path)
            .map_err(|e| InventoryError::Io(path.clone(), e.to_string()))?;
        let parsed: Option<Mapping> = serde_yaml_ng::from_str(&text)
            .map_err(|e| InventoryError::Yaml(path.clone(), e.to_string()))?;

        let Some(mapping) = parsed else { continue };

        for (name_val, config_val) in mapping {
            let Some(name) = name_val.as_str() else {
                continue;
            };
            let Value::Mapping(config) = config_val else {
                return Err(InventoryError::NotAMapping(name.to_string()));
            };

            if raw.contains_key(name) {
                tracing::warn!(group = name, "重复 Group 定义，将被覆盖");
            }
            raw.insert(
                name.to_string(),
                RawGroup {
                    config,
                    source_dir: source_dir.clone(),
                },
            );
        }
    }

    if raw.is_empty() {
        return Err(InventoryError::NoGroups);
    }

    Ok(raw)
}

fn collect_yaml_files(dir: &Path, out: &mut Vec<PathBuf>) -> Result<(), InventoryError> {
    let entries =
        std::fs::read_dir(dir).map_err(|e| InventoryError::Io(dir.to_path_buf(), e.to_string()))?;

    for entry in entries {
        let entry = entry.map_err(|e| InventoryError::Io(dir.to_path_buf(), e.to_string()))?;
        let path = entry.path();
        if path.is_dir() {
            collect_yaml_files(&path, out)?;
        } else if path.extension().and_then(|e| e.to_str()) == Some("yaml") {
            out.push(path);
        }
    }
    Ok(())
}

/// 递归展开所有 Group 的 `parents` 继承链。
///
/// 与 Python 版一致：多父继承按声明顺序依次合并，最后叠加自身定义，
/// 并从结果中移除 `parents` 键。
pub fn expand_inheritance(
    raw: &BTreeMap<String, RawGroup>,
) -> Result<BTreeMap<String, Mapping>, InventoryError> {
    let mut expanded: BTreeMap<String, Mapping> = BTreeMap::new();
    let mut processing: HashSet<String> = HashSet::new();

    let names: Vec<String> = raw.keys().cloned().collect();
    for name in names {
        if !expanded.contains_key(&name) {
            resolve_group(&name, raw, &mut expanded, &mut processing)?;
        }
    }

    Ok(expanded)
}

fn resolve_group(
    name: &str,
    raw: &BTreeMap<String, RawGroup>,
    expanded: &mut BTreeMap<String, Mapping>,
    processing: &mut HashSet<String>,
) -> Result<Mapping, InventoryError> {
    if let Some(done) = expanded.get(name) {
        return Ok(done.clone());
    }
    if processing.contains(name) {
        return Err(InventoryError::CircularInheritance(name.to_string()));
    }
    let Some(entry) = raw.get(name) else {
        return Err(InventoryError::UndefinedGroup(name.to_string()));
    };

    processing.insert(name.to_string());

    let group_def = entry.config.clone();
    let parents: Vec<String> = group_def
        .get(Value::from("parents"))
        .and_then(|v| v.as_sequence())
        .map(|seq| {
            seq.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();

    let result = if parents.is_empty() {
        group_def
    } else {
        let mut merged = Mapping::new();
        for parent in &parents {
            let parent_data = resolve_group(parent, raw, expanded, processing)?;
            merged = deep_merge(&merged, &parent_data);
        }
        let mut merged = deep_merge(&merged, &group_def);
        merged.remove(Value::from("parents"));
        merged
    };

    processing.remove(name);
    expanded.insert(name.to_string(), result.clone());
    Ok(result)
}

/// 结构预校验结果。
#[derive(Debug, Default)]
pub struct ValidationReport {
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}

impl ValidationReport {
    pub fn has_errors(&self) -> bool {
        !self.errors.is_empty()
    }
}

/// Group 结构预校验。
///
/// Python 版 `pre_validate_groups()` 是空实现（仅保留 Hook）。这里补上
/// 那份代码注释中列为"未来扩展"的两项校验：
///   1. 跨厂商继承隔离
///   2. 同一 key 在父子间的类型一致性
pub fn pre_validate_groups(raw: &BTreeMap<String, RawGroup>) -> ValidationReport {
    let mut report = ValidationReport::default();

    for (name, entry) in raw {
        let parents: Vec<String> = entry
            .config
            .get(Value::from("parents"))
            .and_then(|v| v.as_sequence())
            .map(|seq| {
                seq.iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default();

        for parent in &parents {
            let Some(parent_entry) = raw.get(parent) else {
                report
                    .errors
                    .push(format!("Group '{name}' 引用了不存在的父组 '{parent}'"));
                continue;
            };

            // 跨厂商继承隔离：厂商段取 Group 名的第一个下划线分段
            let vendor_of = |g: &str| g.split('_').next().unwrap_or("").to_string();
            let (child_vendor, parent_vendor) = (vendor_of(name), vendor_of(parent));
            if !child_vendor.is_empty()
                && !parent_vendor.is_empty()
                && child_vendor != parent_vendor
            {
                report.warnings.push(format!(
                    "Group '{name}'（厂商段 {child_vendor}）继承了 '{parent}'（厂商段 {parent_vendor}），疑似跨厂商继承"
                ));
            }

            // 类型一致性：父子同名 key 的 YAML 类型应一致
            for (key, child_val) in &entry.config {
                if key.as_str() == Some("parents") {
                    continue;
                }
                if let Some(parent_val) = parent_entry.config.get(key) {
                    let (ct, pt) = (type_name(child_val), type_name(parent_val));
                    if ct != pt {
                        report.errors.push(format!(
                            "Group '{name}' 的 '{}' 类型为 {ct}，但父组 '{parent}' 中为 {pt}",
                            key.as_str().unwrap_or("?")
                        ));
                    }
                }
            }
        }
    }

    report
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Sequence(_) => "sequence",
        Value::Mapping(_) => "mapping",
        Value::Tagged(_) => "tagged",
    }
}

/// 完整的构建流程：加载 → 预校验 → 展开继承 → 过滤 `os_versions` 层。
///
/// 返回展开后的版本层 Group。对应 Python 版 `merge_group_files()`，
/// 但不写盘 —— 上层直接持有内存结构，避免 Python 版"每次运行都重写
/// groups.yaml"带来的中间产物依赖。
pub fn build_groups(
    groups_root: &Path,
) -> Result<(BTreeMap<String, Mapping>, ValidationReport), InventoryError> {
    let raw = load_raw_groups(groups_root)?;

    let report = pre_validate_groups(&raw);
    if report.has_errors() {
        return Err(InventoryError::ValidationFailed(report.errors.clone()));
    }

    let expanded = expand_inheritance(&raw)?;

    let filtered: BTreeMap<String, Mapping> = expanded
        .into_iter()
        .filter(|(name, _)| {
            raw.get(name)
                .map(|e| e.source_dir == "os_versions")
                .unwrap_or(false)
        })
        .collect();

    if filtered.is_empty() {
        return Err(InventoryError::NoVersionGroups);
    }

    Ok((filtered, report))
}

/// 将展开结果序列化为 YAML，用于与 Python 版 `inventory/groups.yaml` 做比对。
pub fn dump_groups_yaml(groups: &BTreeMap<String, Mapping>) -> Result<String, InventoryError> {
    let mut root = Mapping::new();
    for (name, cfg) in groups {
        root.insert(Value::from(name.clone()), Value::Mapping(cfg.clone()));
    }
    serde_yaml_ng::to_string(&Value::Mapping(root))
        .map_err(|e| InventoryError::Serialize(e.to_string()))
}

/// 便捷方法：把展开后的 Group 转成普通 HashMap，供上层查询。
pub fn groups_as_map(groups: &BTreeMap<String, Mapping>) -> HashMap<String, Mapping> {
    groups.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
}
