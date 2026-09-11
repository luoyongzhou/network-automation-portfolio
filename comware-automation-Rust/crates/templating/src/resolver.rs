//! 分层模板路径解析
//!
//! 对应 Python 版 `scripts/preview_bootstrap.py` 的 `PathResolver` 家族。
//!
//! 候选路径优先级（高 → 低），与 Python 版一致：
//!   1. 补丁级   `os_versions/{vendor}/{os_version}/{patch}/{template}`
//!   2. OS 版本级 `os_versions/{vendor}/{os_version}/{template}`
//!   3. 产线级   `product_lines/{vendor}/{product_line}/{template}`
//!   4. 厂商级   `vendors/{vendor}/{template}`
//!   5. 全局兜底 `_base/{template}`
//!
//! ## 与 Python 版的差异
//!
//! Python 版 `H3CBootstrapPathResolver.resolve()` 把 `"os_versions/h3c/SR88_Comware_V7"`
//! 和 `"product_lines/h3c/SR88"` 硬编码在方法体内，导致新增产线时解析器不会自动支持
//! （`need_to_discuss_prob.md` 新发现①）。
//!
//! 这里改为从 Group 名称**动态推导**厂商段、产线段、OS 版本段，并允许通过
//! `host.data` 显式覆盖，从而不再与单一产线绑定。

use cwa_inventory::Host;

use crate::patch::PatchExtractor;

/// 从 Group 名推导出的分层坐标。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LayerCoords {
    /// 厂商目录段，如 `h3c`
    pub vendor: String,
    /// 产线目录段，如 `SR88`
    pub product_line: String,
    /// OS 版本目录段，如 `SR88_Comware_V7`
    pub os_version: String,
}

impl LayerCoords {
    /// 从 Host 推导分层坐标。
    ///
    /// 优先读取 `host.data` 中的显式覆盖（`template_vendor` /
    /// `template_product_line` / `template_os_version`），未提供时从第一个
    /// Group 名按约定推导。
    ///
    /// 推导规则（以 `h3c_sr88_comware_v7_r7171` 为例）：
    ///   - 先剥除末尾的分支段 `_r7171` 与热补丁段 `_h02`
    ///   - 剩余 `h3c_sr88_comware_v7`，首段为厂商 → `h3c`
    ///   - 其余段做目录名规范化 → `SR88_Comware_V7`，其首段为产线 → `SR88`
    ///
    /// 规范化规则：形如 `字母+数字` 的段整体大写（`sr88` → `SR88`、`v7` → `V7`），
    /// 其余段首字母大写（`comware` → `Comware`）。
    pub fn derive(host: &Host) -> Option<Self> {
        let explicit_vendor = host.data_str("template_vendor");
        let explicit_pl = host.data_str("template_product_line");
        let explicit_os = host.data_str("template_os_version");

        if let (Some(v), Some(p), Some(o)) = (&explicit_vendor, &explicit_pl, &explicit_os) {
            return Some(Self {
                vendor: v.clone(),
                product_line: p.clone(),
                os_version: o.clone(),
            });
        }

        let group = host.groups.first()?;
        let stripped = strip_version_suffixes(group);
        let mut segs = stripped.split('_').filter(|s| !s.is_empty());

        let vendor = explicit_vendor.or_else(|| segs.next().map(|s| s.to_lowercase()))?;
        let rest: Vec<String> = segs.map(normalize_segment).collect();
        if rest.is_empty() {
            return None;
        }

        let product_line = explicit_pl.unwrap_or_else(|| rest[0].clone());
        let os_version = explicit_os.unwrap_or_else(|| rest.join("_"));

        Some(Self {
            vendor,
            product_line,
            os_version,
        })
    }
}

/// 剥除 Group 名末尾的分支 / 热补丁段。
fn strip_version_suffixes(group: &str) -> String {
    let mut segs: Vec<&str> = group.split('_').collect();
    while let Some(last) = segs.last() {
        let l = last.to_lowercase();
        let is_version_seg = (l.starts_with('r') || l.starts_with('h'))
            && l.len() > 1
            && l[1..].chars().all(|c| c.is_ascii_digit());
        if is_version_seg {
            segs.pop();
        } else {
            break;
        }
    }
    segs.join("_")
}

/// 目录名规范化：`sr88` → `SR88`，`comware` → `Comware`，`v7` → `V7`。
fn normalize_segment(seg: &str) -> String {
    let has_digit = seg.chars().any(|c| c.is_ascii_digit());
    let has_alpha = seg.chars().any(|c| c.is_ascii_alphabetic());
    if has_digit && has_alpha {
        return seg.to_uppercase();
    }
    let mut chars = seg.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + &chars.as_str().to_lowercase(),
        None => String::new(),
    }
}

/// 模板路径解析策略。
pub trait PathResolver: Send + Sync {
    /// 返回候选模板路径（相对 `templates/` 根），按优先级从高到低。
    fn resolve(&self, host: &Host, template_name: &str) -> Vec<String>;
}

/// 分层解析器：产线无关，坐标由 Group 名动态推导。
pub struct LayeredPathResolver {
    extractor: Box<dyn PatchExtractor>,
}

impl LayeredPathResolver {
    pub fn new(extractor: Box<dyn PatchExtractor>) -> Self {
        Self { extractor }
    }
}

impl PathResolver for LayeredPathResolver {
    fn resolve(&self, host: &Host, template_name: &str) -> Vec<String> {
        let mut candidates = Vec::new();

        let Some(coords) = LayerCoords::derive(host) else {
            tracing::warn!(
                host = %host.name,
                "无法从 Group 名推导分层坐标，仅使用 _base 兜底"
            );
            candidates.push(format!("_base/{template_name}"));
            return candidates;
        };

        let LayerCoords {
            vendor,
            product_line,
            os_version,
        } = &coords;

        // 1. 补丁级（可能是 R7171 或 R7171/H02）
        if let Some(patch) = self.extractor.extract(host) {
            candidates.push(format!(
                "os_versions/{vendor}/{os_version}/{patch}/{template_name}"
            ));

            // 若补丁段形如 "R7171/H02"，还需补上仅分支级 "R7171" 这一层，
            // 否则热补丁不存在对应模板时会直接跳到 OS 版本级，丢掉分支级模板。
            if let Some((branch, _hotfix)) = patch.split_once('/') {
                candidates.push(format!(
                    "os_versions/{vendor}/{os_version}/{branch}/{template_name}"
                ));
            }
        }

        // 2. OS 版本级
        candidates.push(format!("os_versions/{vendor}/{os_version}/{template_name}"));
        // 3. 产线级
        candidates.push(format!(
            "product_lines/{vendor}/{product_line}/{template_name}"
        ));
        // 4. 厂商级
        candidates.push(format!("vendors/{vendor}/{template_name}"));
        // 5. 全局兜底
        candidates.push(format!("_base/{template_name}"));

        candidates
    }
}

/// 直通解析器：模板名即路径，不做分层查找。
///
/// 对应 Python 版 `scenes/test_*.py` 里各自定义的 `DirectPathResolver`。
pub struct DirectPathResolver;

impl PathResolver for DirectPathResolver {
    fn resolve(&self, _host: &Host, template_name: &str) -> Vec<String> {
        vec![template_name.to_string()]
    }
}

/// 构造 H3C 场景的默认分层解析器。
pub fn h3c_resolver() -> LayeredPathResolver {
    LayeredPathResolver::new(crate::patch::get_patch_extractor("h3c"))
}
