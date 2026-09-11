//! 分层继承的设备清单
//!
//! 对应 Python 版的 `scripts/build_inventory.py` + Nornir 的 Inventory 语义。

pub mod error;
pub mod groups;
pub mod host;

pub use error::InventoryError;
pub use groups::{
    build_groups, deep_merge, dump_groups_yaml, expand_inheritance, pre_validate_groups,
    ValidationReport,
};
pub use host::{ConnectionOptions, Host, Inventory};
