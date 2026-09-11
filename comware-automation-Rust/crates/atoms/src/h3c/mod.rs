//! H3C 厂商实现

pub mod ip_address;
pub mod loopback;
pub mod ospf;

pub use ip_address::{IpAddressDesired, IpAddressParams, IpAddressSnapshot, NetconfIpAddressAtom};
pub use loopback::{CmdLoopbackAtom, LoopbackDesired, LoopbackParams, LoopbackSnapshot};
pub use ospf::{
    NetconfOspfAtom, OspfArea, OspfConfig, OspfDesired, OspfInterface, OspfParams, OspfSnapshot,
};
