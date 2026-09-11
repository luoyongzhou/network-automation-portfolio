use std::path::PathBuf;
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| ".".into()));
    let (g, rep) = cwa_inventory::build_groups(&root.join("inventory/groups"))?;
    eprintln!(
        "warnings={} errors={}",
        rep.warnings.len(),
        rep.errors.len()
    );
    for w in &rep.warnings {
        eprintln!("  WARN {w}");
    }
    print!("{}", cwa_inventory::dump_groups_yaml(&g)?);
    let inv = cwa_inventory::Inventory::load(&root)?;
    eprintln!("--- hosts ---");
    for (n, h) in &inv.hosts {
        eprintln!(
            "{n} hostname={} user={} platform={:?} conns={:?} ssh_port={} nc_port={} nc_timeout={}",
            h.hostname,
            h.username,
            h.platform,
            h.connection_options.keys().collect::<Vec<_>>(),
            h.ssh_port(),
            h.netconf_port(),
            h.netconf_timeout()
        );
    }
    Ok(())
}
