//! 错误类型

use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum InventoryError {
    #[error("路径不存在或不是目录: {0}")]
    MissingPath(PathBuf),

    #[error("读取文件失败 {0}: {1}")]
    Io(PathBuf, String),

    #[error("解析 YAML 失败 {0}: {1}")]
    Yaml(PathBuf, String),

    #[error("序列化失败: {0}")]
    Serialize(String),

    #[error("Group '{0}' 的定义不是一个映射")]
    NotAMapping(String),

    #[error("未解析到任何 Group 定义")]
    NoGroups,

    #[error("过滤后没有任何版本层（os_versions）Group 保留")]
    NoVersionGroups,

    #[error("检测到循环继承: {0}")]
    CircularInheritance(String),

    #[error("Group '{0}' 未定义")]
    UndefinedGroup(String),

    #[error("Group 结构预校验失败: {}", .0.join("; "))]
    ValidationFailed(Vec<String>),

    #[error("设备 '{0}' 不存在")]
    UnknownHost(String),

    #[error("设备 '{0}' 缺少必需字段: {1}")]
    MissingHostField(String, String),
}
