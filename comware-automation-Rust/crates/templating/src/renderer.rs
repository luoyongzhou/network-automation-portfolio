//! 模板渲染引擎
//!
//! 对应 Python 版 `scripts/preview_bootstrap.py` 的 `TemplateRenderer` / `SceneAPI`。
//!
//! ## 与 Python 版的行为对齐点
//!
//! 1. `trim_blocks` / `lstrip_blocks` 均开启
//! 2. 注册 `minijinja-contrib` 的 `pycompat`，使模板中的 `name.startswith(...)`
//!    可用（`templates/vendors/h3c/cmd/_macros.j2` 依赖此能力）
//! 3. **autoescape 默认关闭**。Python 版是 `select_autoescape(['xml','j2'])`，
//!    即对所有 `.j2` 做 HTML 转义。对本项目而言这实际是个隐患：NETCONF 的 XML
//!    模板走同一个 Environment，一旦配置值里出现 `&` 就会被转义成 `&amp;` 并
//!    下发到设备。CLI 命令同理。目前因配置值里没有 `<>&` 而未暴露。
//!    此处默认关闭，并提供 `with_autoescape()` 用于需要逐字节对齐 Python 输出
//!    时（黄金文件比对）临时开启。

use std::path::{Path, PathBuf};

use cwa_inventory::Host;
use minijinja::{Environment, Value as JinjaValue};
use serde_json::{Map as JsonMap, Value as JsonValue};

use crate::error::TemplateError;
use crate::resolver::PathResolver;

/// 模板渲染引擎。
pub struct TemplateRenderer {
    templates_root: PathBuf,
    env: Environment<'static>,
}

impl TemplateRenderer {
    /// 以 `templates_root` 为根创建引擎。
    pub fn new(templates_root: impl Into<PathBuf>) -> Result<Self, TemplateError> {
        Self::build(templates_root.into(), false)
    }

    /// 开启 autoescape，用于与 Python 版做逐字节比对。
    pub fn with_autoescape(templates_root: impl Into<PathBuf>) -> Result<Self, TemplateError> {
        Self::build(templates_root.into(), true)
    }

    fn build(templates_root: PathBuf, autoescape: bool) -> Result<Self, TemplateError> {
        if !templates_root.is_dir() {
            return Err(TemplateError::MissingRoot(templates_root));
        }

        let mut env = Environment::new();
        env.set_trim_blocks(true);
        env.set_lstrip_blocks(true);
        env.set_loader(minijinja::path_loader(&templates_root));

        // 使模板中的 Python 字符串方法可用（startswith / endswith / split / ...）
        env.set_unknown_method_callback(minijinja_contrib::pycompat::unknown_method_callback);

        if autoescape {
            env.set_auto_escape_callback(|name| {
                if name.ends_with(".xml") || name.ends_with(".j2") {
                    minijinja::AutoEscape::Html
                } else {
                    minijinja::AutoEscape::None
                }
            });
        } else {
            env.set_auto_escape_callback(|_| minijinja::AutoEscape::None);
        }

        Ok(Self {
            templates_root,
            env,
        })
    }

    /// 构造基础渲染上下文，对应 Python 版 `build_base_context()`。
    pub fn build_base_context(&self, host: &Host) -> JsonMap<String, JsonValue> {
        let mut ctx = JsonMap::new();
        ctx.insert("sysname".into(), JsonValue::from(host.name.clone()));
        ctx.insert("mgmt_ip".into(), JsonValue::from(host.hostname.clone()));
        ctx.insert(
            "mgmt_interface".into(),
            JsonValue::from(
                host.data_str("mgmt_interface")
                    .unwrap_or_else(|| "Vlan-interface 100".to_string()),
            ),
        );
        ctx.insert(
            "mgmt_mask".into(),
            JsonValue::from(
                host.data_str("mgmt_mask")
                    .unwrap_or_else(|| "255.255.255.0".to_string()),
            ),
        );
        ctx.insert(
            "gateway".into(),
            host.data_str("gateway")
                .map(JsonValue::from)
                .unwrap_or(JsonValue::Null),
        );
        ctx.insert("username".into(), JsonValue::from(host.username.clone()));
        ctx.insert("password".into(), JsonValue::from(host.password.clone()));
        ctx.insert(
            "enable_secret".into(),
            host.data_str("enable_secret")
                .map(JsonValue::from)
                .unwrap_or(JsonValue::Null),
        );
        ctx
    }

    /// 渲染模板。
    ///
    /// 先按 resolver 给出的候选路径逐个探测文件是否存在，再对存在的候选依次
    /// 尝试渲染，返回首个成功结果 —— 与 Python 版的两阶段逻辑一致。
    pub fn render(
        &self,
        host: &Host,
        template_name: &str,
        resolver: &dyn PathResolver,
        context_override: Option<&JsonMap<String, JsonValue>>,
    ) -> Result<String, TemplateError> {
        let mut ctx = self.build_base_context(host);
        if let Some(over) = context_override {
            for (k, v) in over {
                ctx.insert(k.clone(), v.clone());
            }
        }

        let candidates = resolver.resolve(host, template_name);

        let existing: Vec<String> = candidates
            .iter()
            .filter(|rel| self.templates_root.join(rel).is_file())
            .cloned()
            .collect();

        if existing.is_empty() {
            return Err(TemplateError::NoTemplateFound {
                template: template_name.to_string(),
                searched: candidates,
            });
        }

        let value = JinjaValue::from_serialize(JsonValue::Object(ctx));
        let mut last_err = None;

        for rel in &existing {
            match self.env.get_template(rel) {
                Ok(tpl) => match tpl.render(&value) {
                    Ok(out) => {
                        tracing::debug!(host = %host.name, template = %rel, "渲染成功");
                        return Ok(out);
                    }
                    Err(e) => last_err = Some(format!("{rel}: {e:#}")),
                },
                Err(e) => last_err = Some(format!("{rel}: {e:#}")),
            }
        }

        Err(TemplateError::RenderFailed(
            last_err.unwrap_or_else(|| "未知错误".to_string()),
        ))
    }

    pub fn templates_root(&self) -> &Path {
        &self.templates_root
    }
}

/// 便捷上下文构造宏式辅助：把键值对收集成 JSON 映射。
pub fn ctx<const N: usize>(pairs: [(&str, JsonValue); N]) -> JsonMap<String, JsonValue> {
    pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}
