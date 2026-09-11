//! 错误类型

use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum TemplateError {
    #[error("模板根目录不存在: {0}")]
    MissingRoot(PathBuf),

    #[error("未找到模板 {template}，搜索路径: {}", searched.join(", "))]
    NoTemplateFound {
        template: String,
        searched: Vec<String>,
    },

    #[error("模板渲染失败: {0}")]
    RenderFailed(String),
}
