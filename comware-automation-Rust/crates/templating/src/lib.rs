//! 模板渲染层：补丁提取 + 分层路径解析 + minijinja 渲染 + 场景入口

pub mod error;
pub mod patch;
pub mod renderer;
pub mod resolver;
pub mod scene_api;

pub use error::TemplateError;
pub use patch::{
    get_patch_extractor, CiscoPatchExtractor, DefaultPatchExtractor, H3cPatchExtractor,
    PatchExtractor,
};
pub use renderer::{ctx, TemplateRenderer};
pub use resolver::{
    h3c_resolver, DirectPathResolver, LayerCoords, LayeredPathResolver, PathResolver,
};
pub use scene_api::{paths, SceneApi};
