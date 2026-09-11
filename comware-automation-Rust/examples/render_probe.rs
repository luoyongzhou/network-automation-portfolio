use std::path::PathBuf;
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(".");
    let inv = cwa_inventory::Inventory::load(&root)?;
    let r = cwa_templating::TemplateRenderer::new(root.join("templates"))?;
    let api = cwa_templating::SceneApi::new(r);
    let host = inv.hosts.get("H3C-SR88-01").unwrap();

    // 验证分层坐标推导
    let coords = cwa_templating::LayerCoords::derive(host);
    eprintln!("coords = {coords:?}");
    let ex = cwa_templating::get_patch_extractor("h3c");
    eprintln!("patch  = {:?}", ex.extract(host));
    let res = cwa_templating::h3c_resolver();
    for c in cwa_templating::PathResolver::resolve(&res, host, "cmd/bootstrap.j2") {
        let hit = root.join("templates").join(&c).is_file();
        eprintln!("  {} {}", if hit { "HIT " } else { "miss" }, c);
    }

    println!("========== bootstrap ==========");
    println!("{}", api.bootstrap(host)?);
    println!("========== netconf_cmd ==========");
    println!("{}", api.ssh_bootstrap(host)?);
    Ok(())
}
