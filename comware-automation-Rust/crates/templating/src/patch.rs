//! 补丁版本提取器
//!
//! 对应 Python 版 `scripts/preview_bootstrap.py` 的 `PatchExtractor` 家族。
//!
//! 从 Group 名称或 `host.data["patch"]` 中提取补丁路径段：
//!   - 仅分支：`R7171`
//!   - 分支 + 热补丁：`R7171/H02`
//!   - 无匹配：`None`

use cwa_inventory::Host;
use regex::Regex;

/// 补丁提取策略。
pub trait PatchExtractor: Send + Sync {
    fn extract(&self, host: &Host) -> Option<String>;
}

/// H3C：分支匹配 `_r(\d+)`，热补丁匹配 `_h(\d+)`。
pub struct H3cPatchExtractor {
    branch: Regex,
    hotfix: Regex,
}

impl Default for H3cPatchExtractor {
    fn default() -> Self {
        Self {
            // Rust 的 regex crate 不支持内联 (?i) 之外的 IGNORECASE 标志位传参，
            // 这里用 (?i) 前缀等价于 Python 的 re.IGNORECASE
            branch: Regex::new(r"(?i)_r([0-9]+)").expect("branch regex"),
            hotfix: Regex::new(r"(?i)_h([0-9]+)").expect("hotfix regex"),
        }
    }
}

impl PatchExtractor for H3cPatchExtractor {
    fn extract(&self, host: &Host) -> Option<String> {
        // host.data["patch"] 显式覆盖，优先级最高
        if let Some(p) = host.data_str("patch") {
            if !p.is_empty() {
                return Some(p);
            }
        }

        let mut branch = None;
        let mut hotfix = None;

        for g in &host.groups {
            if let Some(c) = self.branch.captures(g) {
                branch = Some(format!("R{}", c[1].to_uppercase()));
            }
            if let Some(c) = self.hotfix.captures(g) {
                hotfix = Some(format!("H{}", c[1].to_uppercase()));
            }
        }

        match (branch, hotfix) {
            (Some(b), Some(h)) => Some(format!("{b}/{h}")),
            (Some(b), None) => Some(b),
            _ => None,
        }
    }
}

/// Cisco：匹配 `_V([\d.]+)`。
///
/// 说明：Python 版已写好这个提取器但从未被任何 PathResolver 使用
/// （`need_to_discuss_prob.md` 新发现②）。此处保留等价实现，
/// 并在 `resolver.rs` 中提供了可真正使用它的通用解析器。
pub struct CiscoPatchExtractor {
    re: Regex,
}

impl Default for CiscoPatchExtractor {
    fn default() -> Self {
        Self {
            re: Regex::new(r"(?i)_V([\d.]+)").expect("cisco regex"),
        }
    }
}

impl PatchExtractor for CiscoPatchExtractor {
    fn extract(&self, host: &Host) -> Option<String> {
        if let Some(p) = host.data_str("patch") {
            if !p.is_empty() {
                return Some(p);
            }
        }
        host.groups
            .iter()
            .find_map(|g| self.re.captures(g).map(|c| c[1].to_uppercase()))
    }
}

/// 兜底：匹配结尾的 `_(\d+)`。
pub struct DefaultPatchExtractor {
    re: Regex,
}

impl Default for DefaultPatchExtractor {
    fn default() -> Self {
        Self {
            re: Regex::new(r"_([\d]+)$").expect("default regex"),
        }
    }
}

impl PatchExtractor for DefaultPatchExtractor {
    fn extract(&self, host: &Host) -> Option<String> {
        if let Some(p) = host.data_str("patch") {
            if !p.is_empty() {
                return Some(p);
            }
        }
        host.groups
            .iter()
            .find_map(|g| self.re.captures(g).map(|c| c[1].to_string()))
    }
}

/// 按 platform 或 Group 名选择提取器，对应 Python 版 `get_patch_extractor()`。
pub fn get_patch_extractor(platform_or_group: &str) -> Box<dyn PatchExtractor> {
    let key = platform_or_group.to_lowercase();
    if key.contains("h3c") || key.contains("hp_comware") {
        Box::new(H3cPatchExtractor::default())
    } else if key.contains("cisco") || key.contains("ios") {
        Box::new(CiscoPatchExtractor::default())
    } else {
        Box::new(DefaultPatchExtractor::default())
    }
}
