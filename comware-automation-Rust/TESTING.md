# 测试样例

`comware-automation-Rust` 的验证清单。

按**是否需要真机**分为三层：

| 层 | 用例 | 需要设备 | 目的 |
| :--- | :--- | :--- | :--- |
| **T1** 基准对齐 | T1.1 ~ T1.5 | 否 | 证明 Rust 版与 Python 版行为等价 |
| **T2** 离线功能 | T2.1 ~ T2.10 | 否 | 验证不接触设备的全部逻辑 |
| **T3** 真机 | T3.1 ~ T3.12 | **是** | 验证设备交互路径 |

T1、T2 我已全部执行通过，本文记录复现方法与预期输出，供你核对。
**T3 全部未执行**（无测试环境），是你搭好环境后需要跑的部分。

---

## 环境准备

### Rust 侧

```bash
# 若无工具链
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
source ~/.cargo/env

# Rust 编译需要 C 链接器
# Rocky/CentOS:  dnf install -y gcc
# Debian/Ubuntu: apt install -y build-essential

cd comware-automation-Rust
cargo build --release
export CWA=$PWD/target/release/cwa
```

### Python 侧（仅 T1 基准对齐需要）

T1 需要跑 Python 版生成基准。注意本机 `python3` 是 3.11 但 nornir 装在 3.9 下：

```bash
# 确认哪个解释器有 nornir
/usr/bin/python3.9 -c "import nornir; print('ok')"

# 补 jinja2（Python 项目缺这个依赖）
/usr/bin/python3.9 -m pip install jinja2

export PY=/usr/bin/python3.9
export PYPROJ=/root/netauto_for_git/nornir-comware-automation
```

> **重要**：跑 Python 脚本会重写 `$PYPROJ/inventory/groups.yaml`（它是生成产物）。
> 每次跑完请还原，保持 Python 项目零改动：
> ```bash
> cd /root/netauto_for_git && git checkout -- nornir-comware-automation/inventory/groups.yaml
> ```

---

# T1 基准对齐（已通过，无需设备）

目的：证明 Rust 版与 Python 版在**不接触设备的部分**行为等价。这是整个迁移风险最低、也最该先跑的一层。

## T1.1 Group 继承合并语义等价

**方法**：同一份 YAML 分别用两边展开，逐键比对（不是逐字节 —— key 顺序不同）。

```bash
# Python 侧
cd $PYPROJ && $PY scripts/build_inventory.py >/dev/null

# Rust 侧
$CWA groups --root $PWD > /tmp/rs_groups.yaml

# 比对
$PY - <<'EOF'
import yaml
a = yaml.safe_load(open('/root/netauto_for_git/nornir-comware-automation/inventory/groups.yaml'))
b = yaml.safe_load(open('/tmp/rs_groups.yaml'))
print("Python:", sorted(a.keys()))
print("Rust  :", sorted(b.keys()))
print("语义等价:", a == b)
if a != b:
    for k in set(a) | set(b):
        if a.get(k) != b.get(k):
            print("DIFF", k); print(" py:", a.get(k)); print(" rs:", b.get(k))
EOF

# 还原 Python 项目
cd /root/netauto_for_git && git checkout -- nornir-comware-automation/inventory/groups.yaml
```

**预期**：

```
Python: ['h3c_sr88_comware_v7', 'h3c_sr88_comware_v7_r7171']
Rust  : ['h3c_sr88_comware_v7', 'h3c_sr88_comware_v7_r7171']
语义等价: True
```

**已执行结果**：通过。

**关注点**：只保留了 `os_versions` 层的两个 Group，`vendors/h3c` 与 `product_lines/h3c/SR88` 作为中间继承源未输出。

## T1.2 Host 三级解析

**方法**：检查 Group 里定义的字段是否正确下沉到 Host。

```bash
cargo run -q -p cwa-inventory --example dump_inventory -- . 2>&1 >/dev/null
```

**预期**：

```
H3C-SR88-01 hostname=172.12.1.11 user=admin platform=Some("hp_comware") \
  conns=["ncclient", "netmiko", "netmiko_oob"] ssh_port=22 nc_port=830 nc_timeout=60
（三台设备各一行）
```

**已执行结果**：通过。

**关注点**：`username` / `platform` 都不在 `hosts.yaml` 里，来自 Group 继承；三个命名连接都被正确解析。

## T1.3 `cmd/bootstrap.j2` 渲染逐字节一致

这是**最关键的一条**。它一次性验证：YAML 加载 → Group 继承 → 补丁提取 → 5 级路径解析 → minijinja 渲染（含 `extends` / `super()` / `import as macros` / `startswith`）全链路。

```bash
# Python 基准（跳过前 4 行工作目录/构建提示）
cd $PYPROJ && $PY scripts/preview_bootstrap.py --scene bootstrap H3C-SR88-01 2>/dev/null \
  | sed '1,4d' > /tmp/py_bootstrap.txt
cd /root/netauto_for_git && git checkout -- nornir-comware-automation/inventory/groups.yaml

# Rust（跳过前 2 行：空行 + === 设备 === 标题）
$CWA preview bootstrap -d H3C-SR88-01 2>/dev/null | tail -n +3 > /tmp/rs_bootstrap.txt

# 逐字节比对
$PY - <<'EOF'
p = open('/tmp/py_bootstrap.txt', encoding='utf-8').read().strip()
r = open('/tmp/rs_bootstrap.txt', encoding='utf-8').read().strip()
print(f"逐字节一致: {p == r}  py={len(p)}B rs={len(r)}B")
if p != r:
    import difflib
    for l in list(difflib.unified_diff(p.splitlines(), r.splitlines(), 'python', 'rust', lineterm=''))[:30]:
        print(l)
EOF
```

**预期**：`逐字节一致: True  py=533B rs=533B`

**已执行结果**：通过（533 B，含 CRLF 行尾）。

**关注点**：

- 输出含 `\r\n` 是正确的（模板文件本身是 CRLF）
- 大量空行也是正确的（继承链上各级 `custom_config` 块只有注释）
- `port link-mode route` 出现 → `startswith` 生效（`pycompat` 工作正常）
- `sysname H3C-SR88-01` 在最后一行 → 保留了 Python 版规避 80 字符换行的手法

## T1.4 `cmd/netconf_cmd.j2` 渲染逐字节一致

```bash
cd $PYPROJ && $PY scripts/preview_bootstrap.py --scene ssh_bootstrap H3C-SR88-01 2>/dev/null \
  | sed '1,4d' > /tmp/py_netconf.txt
cd /root/netauto_for_git && git checkout -- nornir-comware-automation/inventory/groups.yaml

$CWA preview netconf-cmd -d H3C-SR88-01 2>/dev/null | tail -n +3 > /tmp/rs_netconf.txt

$PY -c "
p=open('/tmp/py_netconf.txt',encoding='utf-8').read().strip()
r=open('/tmp/rs_netconf.txt',encoding='utf-8').read().strip()
print(f'逐字节一致: {p==r}  py={len(p)}B rs={len(r)}B')"
```

**预期**：`逐字节一致: True  py=286B rs=286B`

**已执行结果**：通过。

## T1.5 `netconf/ospf_xml.j2` 渲染逐字节一致

Python 版没有独立的 OSPF 预览入口（需要连设备取 ifindex），所以直接调 Jinja2 渲染同一份上下文。

```bash
$PY - <<'EOF'
from jinja2 import Environment, FileSystemLoader, select_autoescape
env = Environment(
    loader=FileSystemLoader('/root/netauto_for_git/nornir-comware-automation/templates'),
    autoescape=select_autoescape(['xml','j2']), trim_blocks=True, lstrip_blocks=True)
t = env.get_template('product_lines/h3c/SR88/netconf/ospf_xml.j2')
ospf = {"instance_name":"1","router_id":"11.11.11.11","areas":[{
    "area_id":"0.0.0.0","area_type":0,
    "interfaces":[{"ifindex":9000},{"ifindex":9001,"network_type":3},{"ifindex":9002,"network_type":3}]}]}
open('/tmp/py_ospf.xml','w').write(t.render(ospf=ospf))
EOF

# Rust 侧：dry-run 的占位 ifindex 正好是 9000/9001/9002
$CWA deploy-ospf --dry-run -d H3C-SR88-01 2>/dev/null \
  | sed -n '/<OSPF/,/<\/OSPF>/p' | sed 's/^  //' > /tmp/rs_ospf.xml

$PY -c "
p=open('/tmp/py_ospf.xml',encoding='utf-8').read().strip()
r=open('/tmp/rs_ospf.xml',encoding='utf-8').read().strip()
print(f'逐字节一致: {p==r}  py={len(p)}B rs={len(r)}B')"
```

**预期**：`逐字节一致: True  py=1003B rs=1003B`

**已执行结果**：通过。

---

### T1 附带发现的一个事实

我原本设计了 `--python-compat` 开关（开启 autoescape 以对齐 Python），**实测发现它做不到，已移除**：

```bash
# Jinja2（markupsafe）的 HTML 转义集
$PY -c "from markupsafe import escape; print(repr(str(escape('a&b<c>d\"e/f'))))"
# → 'a&amp;b&lt;c&gt;d&#34;e/f'   ← 不转义 /
```

minijinja 的 HTML 转义**额外转义 `/`**，会把 `GigabitEthernet0/0/0` 变成 `GigabitEthernet0&#x2f;0&#x2f;0`，直接破坏配置。

而 Python 版虽然写了 `select_autoescape(['xml','j2'])`，实际输出里 `/` 没被转义。所以：

- **autoescape 全程关闭才是与 Python 版一致的正确选择**
- 当前模板与数据中不含 `& < > " '`，关闭后输出逐字节一致
- 附带消除了 Python 版的一个隐患：若密码含 `&`，Python 版会下发 `&amp;`

---

# T2 离线功能验证（已通过，无需设备）

## T2.1 全部子命令可执行

```bash
for c in "groups" \
         "preview bootstrap -d H3C-SR88-01" \
         "preview netconf-cmd -d H3C-SR88-01" \
         "console --dry-run -d H3C-SR88-01" \
         "enable-netconf --dry-run -d H3C-SR88-01" \
         "deploy-ip --dry-run -d H3C-SR88-01" \
         "deploy-ospf --dry-run -d H3C-SR88-01" \
         "rollback-ip -d H3C-SR88-01" \
         "rollback-ospf -d H3C-SR88-01"; do
  $CWA $c >/dev/null 2>&1 && echo "  OK   cwa $c" || echo "  FAIL cwa $c"
done
```

**预期**：9 个全部 OK。

**已执行结果**：通过。

**注意**：`rollback-*` 在无快照时返回 SKIPPED 且退出码 0，这是预期行为（跳过不算失败）。

## T2.2 独立块解析

```bash
$CWA enable-netconf --dry-run -d H3C-SR88-01 2>/dev/null | tail -8
```

**预期**：

```
执行单元: 4 个（普通命令块 1 个，独立块 3 个）
  1. 独立块: 3 行
  2. 独立块: 3 行
  3. 独立块: 2 行
  4. 普通命令块: 3 条
```

**已执行结果**：通过。

**关注点**：三个独立块分别是 RSA(3 行含 `y`/`512`)、DSA(3 行)、ECDSA(2 行含 `y`)；普通块是 `netconf ssh server enable` / `port 830` / `end`。顺序与模板一致。

## T2.3 CLI 错误检测精确性

这是 Python 版缺失的能力（`need_to_discuss_prob.md` 延伸③），**必须验证不误报**。

```bash
cat > /tmp/t23.rs <<'EOF'
fn main() {
    let cases = [
        ("Info: interface created.",                      false),
        (" ip address 1.1.1.1 255.255.255.0",             false),
        ("Interface GigabitEthernet0/0/1 is up",          false),
        ("% Unrecognized command found at '^' position.", true),
        ("% Wrong parameter found at '^' position.",       true),
        ("Permission denied.",                             true),
        ("Failed to apply configuration",                  true),
        ("The specified interface doesn't exist",           true),
    ];
    let mut pass = 0;
    for (input, should_err) in cases {
        let got = cwa_transport::check_output_for_errors(input).is_some();
        let ok = got == should_err;
        if ok { pass += 1 }
        println!("  {} {:?} -> 检出={} 期望={}", if ok {"OK  "} else {"FAIL"}, input, got, should_err);
    }
    println!("\n{}/{} 通过", pass, cases.len());
}
EOF
mkdir -p examples && cp /tmp/t23.rs examples/t23_error_detect.rs
cat >> crates/transport/Cargo.toml <<'EOF'

[[example]]
name = "t23_error_detect"
path = "../../examples/t23_error_detect.rs"
EOF
cargo run -q -p cwa-transport --example t23_error_detect
```

**预期**：8/8 通过。

**已执行结果**：通过。

**关注点**：`% Unrecognized` 被检出，而 ` ip address 1.1.1.1`（含数字和点，但不以 `%` 开头）**不被误报**。这依赖 `^\s*%\s*\S` 要求 `%` 在行首的约束 —— 若改成 `rneter` 内置模板的 `.+%.+`，第 2、3 条会误报。

## T2.4 NETCONF XML 解析

用构造的设备响应验证 BOM 剥离、命名空间处理、同名节点区分。

```bash
cat > examples/t24_xml_parse.rs <<'EOF'
fn main() {
    // 带 UTF-8 BOM，模拟 H3C 实际响应
    let ifindex_reply = "\u{feff}<?xml version=\"1.0\"?>
<rpc-reply xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">
 <data><top xmlns=\"http://www.h3c.com/netconf/data:1.0\"><Ifmgr><Interfaces>
   <Interface><IfIndex>1</IfIndex><Name>GigabitEthernet0/0/1</Name></Interface>
   <Interface><IfIndex>2</IfIndex><Name>GigabitEthernet0/0/2</Name></Interface>
   <Interface><IfIndex>1235</IfIndex><Name>LoopBack1</Name></Interface>
 </Interfaces></Ifmgr></top></data></rpc-reply>";
    match cwa_transport::netconf::parse_ifindex_map(ifindex_reply) {
        Ok(m) => { println!("ifindex 解析（含 BOM）: {} 个", m.len());
                   for (n, i) in &m { println!("   {n} -> {i}"); } }
        Err(e) => println!("FAIL: {e}"),
    }

    // 陷阱：外层 Ipv4Address 容器与内层 Ipv4Address 叶子同名
    let ip_reply = "<?xml version=\"1.0\"?>
<rpc-reply xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">
 <data><top xmlns=\"http://www.h3c.com/netconf/data:1.0\"><IPV4ADDRESS><Ipv4Addresses>
   <Ipv4Address><IfIndex>1</IfIndex><Ipv4Address>10.0.0.0</Ipv4Address><Ipv4Mask>255.255.255.254</Ipv4Mask></Ipv4Address>
   <Ipv4Address><IfIndex>1235</IfIndex><Ipv4Address>11.11.11.11</Ipv4Address><Ipv4Mask>255.255.255.255</Ipv4Mask></Ipv4Address>
 </Ipv4Addresses></IPV4ADDRESS></top></data></rpc-reply>";
    for idx in [1i64, 1235, 9999] {
        println!("ifindex {idx} 的 IP: {:?}", cwa_transport::netconf::parse_interface_ip(ip_reply, idx));
    }
}
EOF
cat >> crates/transport/Cargo.toml <<'EOF'

[[example]]
name = "t24_xml_parse"
path = "../../examples/t24_xml_parse.rs"
EOF
cargo run -q -p cwa-transport --example t24_xml_parse
```

**预期**：

```
ifindex 解析（含 BOM）: 3 个
   GigabitEthernet0/0/1 -> 1
   GigabitEthernet0/0/2 -> 2
   LoopBack1 -> 1235
ifindex 1 的 IP: Ok(Some("10.0.0.0"))
ifindex 1235 的 IP: Ok(Some("11.11.11.11"))
ifindex 9999 的 IP: Ok(None)
```

**已执行结果**：通过。

**关注点**：BOM 被正确剥离（否则 XML 解析直接失败）；同名节点没有把叶子误当容器；不存在的 ifindex 返回 `None` 而非报错。

## T2.5 分层路径解析

```bash
cargo run -q -p cwa-templating --example render_probe 2>&1 | head -8
```

**预期**：

```
coords = Some(LayerCoords { vendor: "h3c", product_line: "SR88", os_version: "SR88_Comware_V7" })
patch  = Some("R7171")
  HIT  os_versions/h3c/SR88_Comware_V7/R7171/cmd/bootstrap.j2
  HIT  os_versions/h3c/SR88_Comware_V7/cmd/bootstrap.j2
  HIT  product_lines/h3c/SR88/cmd/bootstrap.j2
  HIT  vendors/h3c/cmd/bootstrap.j2
  HIT  _base/cmd/bootstrap.j2
```

**已执行结果**：通过。

**关注点**：坐标从 Group 名 `h3c_sr88_comware_v7_r7171` **动态推导**得出（Python 版是硬编码 SR88）；5 级候选全部命中，实际使用优先级最高的补丁级。

**扩展验证**（可选）：在 `inventory/hosts.yaml` 里给某台设备加 `data: {patch: "R7171/H02"}`，重跑应看到 6 个候选（多出 `R7171/H02/` 补丁级和 `R7171/` 分支级）。分支级那一层是 Python 版缺失的。

## T2.6 IP 回退计划推导

验证两种快照语义推导出不同动作。

```bash
cat > examples/t26_rollback_plan.rs <<'EOF'
use cwa_atoms::h3c::*;
use cwa_atoms::Atom;
use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(".");
    let inv = cwa_inventory::Inventory::load(&root)?;
    let host = inv.hosts.get("H3C-SR88-01").unwrap();
    let scene = cwa_templating::SceneApi::new(
        cwa_templating::TemplateRenderer::new(root.join("templates"))?);

    println!("=== IP Atom ===");
    let ip = NetconfIpAddressAtom::new(&scene);
    let had = IpAddressSnapshot { ifindex: 1234, previous_ip: Some("9.9.9.9".into()),
        applied_ip: "11.11.11.11".into(), applied_mask: "255.255.255.255".into() };
    println!("[变更前有 IP]\n{}", ip.plan_rollback(host, &had)?.describe());
    let none = IpAddressSnapshot { ifindex: 1234, previous_ip: None,
        applied_ip: "11.11.11.11".into(), applied_mask: "255.255.255.255".into() };
    println!("[变更前无 IP]\n{}", ip.plan_rollback(host, &none)?.describe());

    println!("\n=== Loopback Atom ===");
    let lb = CmdLoopbackAtom::new(&scene);
    let created = LoopbackSnapshot { ifname: "LoopBack1".into(), existed_before: false };
    println!("[本次创建]\n{}", lb.plan_rollback(host, &created)?.describe());
    let existed = LoopbackSnapshot { ifname: "LoopBack1".into(), existed_before: true };
    let p = lb.plan_rollback(host, &existed)?;
    println!("[变更前已存在] is_empty={}\n{}", p.is_empty(), p.describe());
    Ok(())
}
EOF
cat >> crates/scenes/Cargo.toml <<'EOF'

[[example]]
name = "t26_rollback_plan"
path = "../../examples/t26_rollback_plan.rs"
EOF
cargo run -q -p cwa-scenes --example t26_rollback_plan
```

**预期**：

```
=== IP Atom ===
[变更前有 IP]
  1. 恢复 ifindex 1234 的原 IP 9.9.9.9
[变更前无 IP]
  1. 删除 ifindex 1234 上本次下发的 IP 11.11.11.11

=== Loopback Atom ===
[本次创建]
  1. 删除本次创建的接口 LoopBack1
[变更前已存在] is_empty=true
（无回退步骤）
```

**已执行结果**：通过。

**关注点**：这正是"为什么需要 `RollbackPlan` 这一层"的证明 —— 同一个 Atom，因快照不同推导出**语义相反**的动作（恢复 vs 删除）。Python 版把这个判断硬编码在 `_rollback_impl()` 里，无法预览。

`is_empty=true` 让调用方能区分"无需回退"和"回退失败"。

## T2.7 OSPF 回退删除 XML

验证遵循 `summary.md` 第七章 v4 方案：只发索引列、顺序为 接口→区域→进程。

```bash
$CWA deploy-ospf --dry-run -d H3C-SR88-01 2>/dev/null | tail -5
```

**预期**：

```
  -- 回退计划（若需回退将执行） --
  1. 逐层删除 OSPF 进程 1（3 个接口、1 个区域、1 个进程），仅发索引列

预览结束（未实际下发）
```

若要看完整 XML，用 T2.6 的 example 模式调 `plan_rollback` 并打印 `RollbackAction::NetconfFragment` 的 `fragment` 字段。

**已执行结果**：通过。实际 XML（已验证）：

```xml
<OSPF xmlns="http://www.h3c.com/netconf/config:1.0">
  <Interfaces>
    <Interface xmlns:nc="..." nc:operation="delete">
      <IfIndex>9000</IfIndex>        <!-- 只有索引列 -->
    </Interface>
    ... ×3
  </Interfaces>
  <Areas>
    <Area xmlns:nc="..." nc:operation="delete">
      <Name>1</Name><AreaId>0.0.0.0</AreaId>
    </Area>
  </Areas>
  <Instances>
    <Instance xmlns:nc="..." nc:operation="delete">
      <Name>1</Name>
    </Instance>
  </Instances>
</OSPF>
```

**关注点**：顺序必须是 接口→区域→进程，反之会因对象仍被引用而失败；每个删除元素只含索引列，多发字段会导致删除失败。

## T2.8 OSPF 回退依赖检查

Python 版**没有**这个能力，回退时无条件删除整个 OSPF 进程。

验证方式：构造 `instance_existed_before = true` 的快照，确认 `rollback_guard` 返回 `Blocked`。

```bash
# 手工构造快照文件，模拟"进程在变更前已存在"
mkdir -p state/snapshots
cat > state/snapshots/H3C-SR88-01_ospf.json <<'EOF'
{
  "instance_existed_before": true,
  "applied": {
    "instance_name": "1",
    "router_id": "11.11.11.11",
    "areas": [{"area_id": "0.0.0.0", "area_type": 0, "interfaces": [{"ifindex": 9000}]}]
  }
}
EOF

$CWA rollback-ospf -d H3C-SR88-01 2>&1 | head -12
```

**预期**：因为无法连接设备，会在 NETCONF 连接阶段失败。要单独验证 guard 逻辑，需用 example 直接调用 `rollback_guard`（它不接触设备，只看快照）。

> **这条用例在有设备时才能完整跑通**（T3.11）。当前只能通过读代码确认逻辑：
> ```rust
> if snapshot.instance_existed_before {
>     return Ok(RollbackGuard::Blocked { dependencies: vec![...] });
> }
> ```

记得清理：

```bash
rm -rf state/
```

## T2.9 快照原子往返

```bash
cat > examples/t29_snapshot.rs <<'EOF'
use cwa_atoms::h3c::*;
use cwa_atoms::SnapshotStore;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let store = SnapshotStore::new("/tmp/cwa_t29");
    let cfg = OspfConfig { instance_name: "1".into(), router_id: "11.11.11.11".into(),
        areas: vec![OspfArea { area_id: "0.0.0.0".into(), area_type: 0,
            interfaces: vec![OspfInterface { ifindex: 9000, network_type: None, cost: None, priority: None }] }] };

    let p = store.save("PROBE", "ospf", &OspfSnapshot { instance_existed_before: false, applied: cfg })?;
    println!("写入: {}", p.display());
    let back: Option<OspfSnapshot> = store.load("PROBE", "ospf")?;
    println!("读回: {} router_id={:?}", back.is_some(), back.as_ref().map(|b| &b.applied.router_id));
    println!("删除: {}", store.remove("PROBE", "ospf")?);
    println!("再读: {:?}", store.load::<OspfSnapshot>("PROBE", "ospf")?.is_some());
    println!("重复删除: {}", store.remove("PROBE", "ospf")?);
    Ok(())
}
EOF
cat >> crates/scenes/Cargo.toml <<'EOF'

[[example]]
name = "t29_snapshot"
path = "../../examples/t29_snapshot.rs"
EOF
cargo run -q -p cwa-scenes --example t29_snapshot
rm -rf /tmp/cwa_t29
```

**预期**：

```
写入: /tmp/cwa_t29/state/snapshots/PROBE_ospf.json
读回: true router_id=Some("11.11.11.11")
删除: true
再读: false
重复删除: false
```

**已执行结果**：通过。

**关注点**：路径是 `state/snapshots/` 而非 `logs/`（与调试产物物理隔离）；不存在时 `load` 返回 `None`、`remove` 返回 `false` 而非报错。

写入是"临时文件 + 原子重命名"，Python 版直接 `open(...,"w")`，进程中断会留半个 JSON —— 而这个文件是回退的唯一依据。

## T2.10 错误路径

```bash
echo "--- 不存在的设备 ---"
$CWA preview bootstrap -d NO-SUCH-DEV 2>&1 | tail -2

echo "--- 不存在的项目根 ---"
$CWA groups --root /tmp/nonexistent 2>&1 | tail -2
```

**预期**：

```
--- 不存在的设备 ---
Error: 设备 'NO-SUCH-DEV' 不存在
--- 不存在的项目根 ---
Error: 路径不存在或不是目录: /tmp/nonexistent/inventory/groups
```

**已执行结果**：通过。

## T2.11 编译与静态检查

```bash
cargo build --release
cargo clippy --workspace --all-targets 2>&1 | grep -cE "^warning: |^error"   # 期望 0
cargo fmt --check && echo "格式符合规范"
ls -la target/release/cwa
```

**已执行结果**：编译通过，clippy 零警告，二进制约 18 MB。

---

## T2 清理

上面为验证临时创建的 example 请清理掉，避免留在仓库里：

```bash
rm -f examples/t23_error_detect.rs examples/t24_xml_parse.rs \
      examples/t26_rollback_plan.rs examples/t29_snapshot.rs

# 从 Cargo.toml 移除对应声明
$PY - <<'EOF'
import re, glob
for f in glob.glob('crates/*/Cargo.toml'):
    s = open(f).read()
    for n in ['t23_error_detect', 't24_xml_parse', 't26_rollback_plan', 't29_snapshot']:
        s = re.sub(r'\n\[\[example\]\]\nname = "%s"\npath = "[^"]*"\n' % n, '\n', s)
    open(f, 'w').write(s)
EOF

cargo build --release
```

仓库里长期保留的只有两个：

```bash
cargo run -p cwa-inventory  --example dump_inventory -- .   # T1.2
cargo run -p cwa-templating --example render_probe           # T2.5
```

---

# T3 真机验证（全部未执行）

**以下用例我一条都没跑过**，因为无测试环境。这是你搭好环境后需要做的部分。

## 前置条件

| 项 | 要求 |
| :--- | :--- |
| 设备 | H3C SR88 系列，Comware V7 R7171 |
| 拓扑 | 参考 `nornir-comware-automation/实验topo.jpg` |
| `inventory/hosts.yaml` | 按实际环境改 `hostname`、`netmiko_oob.hostname`/`port` |
| 凭据 | Group 里的 `username` / `password`（当前是 `admin` / `Ssh@Pass123`） |
| Console 场景 | 需要串口服务器（Console 转 Telnet） |

**强烈建议**：先在**单台设备**上按顺序跑完 T3，确认无误后再扩到多台。

## 建议的执行顺序

```
T3.1  NETCONF 连通性        ← 最小验证，风险最低
T3.2  CLI 连通性
T3.3  ifindex 查询
   │  以上全部通过，再往下
T3.4  IP 下发（单台）
T3.5  IP 幂等（重复下发）
T3.6  IP 回退
   │  IP 场景闭环，再做 OSPF
T3.7  OSPF 下发
T3.8  OSPF 幂等
T3.9  OSPF 回退
T3.10 多设备并发
T3.11 回退依赖检查
T3.12 Console 开局          ← 风险最高，放最后
```

---

## T3.1 NETCONF 连通性与 GenericVendor 兼容性

**这是最关键的一条真机用例。** `rustnetconf` 只有 `junos` 和 `generic` 两个 vendor profile，H3C 会走 `GenericVendor` 兜底，能否正常协商未验证。

```bash
RUST_LOG=debug $CWA deploy-ip --dry-run -d <设备名>
```

`--dry-run` 不连设备，所以要用能触发连接的命令。最小的是 OSPF 回退（有快照时会连接）：

```bash
# 或直接跑 T3.3 的 ifindex 查询，它是最轻的一次真实 NETCONF 交互
```

**需要确认**：

- [ ] SSH 握手成功（端口 830）
- [ ] NETCONF hello 交换完成，能力协商无报错
- [ ] `RUST_LOG=debug` 日志里能看到设备返回的 capabilities
- [ ] 设备是否声明 `:candidate` 和 `:rollback-on-error`

**若失败**，重点看：

| 现象 | 可能原因 |
| :--- | :--- |
| SSH 连接被拒 | 设备未开 `netconf ssh server enable`，先跑 T3.12 或 `enable-netconf` |
| hello 超时 | RFC 6242 framing 协商问题，`rustnetconf` 支持 chunked 与 EOM 两种 |
| 能力协商失败 | `GenericVendor` 不适配，需要贡献一个 Comware profile |

**若需要自己写 Comware profile**：`rustnetconf` 的 `VendorProfile` trait 是公开的，主要实现 `wrap_config()` / `unwrap_config()` / `detect()`。

## T3.2 CLI 连通性与提示符匹配

验证 `rneter` 的 `h3c_comware` 模板对真实 SR88 R7171 输出的匹配度。

```bash
RUST_LOG=debug $CWA enable-netconf -d <设备名>
```

**需要确认**：

- [ ] SSH 登录成功
- [ ] 提示符被正确识别：用户视图 `<HOSTNAME>`、系统视图 `[HOSTNAME]`
- [ ] `screen-length disable` 自动下发（`after_connect` hook）
- [ ] `system-view` / `quit` 视图切换正常
- [ ] 分页 `---- More ----` 被正确处理

**关注 80 字符换行问题**：Python 项目的 `bootstrap.j2` 刻意把 `sysname` 放最后一行规避它。若 `rneter` 下不需要这个规避，或需要别的处理方式，记录下来。

**独立块行为**：`public-key local create rsa` 需要回答 `y` 和 `512`。确认：

- [ ] 三个密钥（RSA / DSA / ECDSA）都生成成功
- [ ] 交互应答被正确发送
- [ ] "发送即忘"策略没有导致卡死

## T3.3 ifindex 查询

最轻量的真实 NETCONF 交互，适合在 T3.1 之后立即验证。

由于没有独立的 ifindex 查询子命令，通过 `deploy-ip` 的日志观察：

```bash
RUST_LOG=info $CWA deploy-ip -d <设备名> 2>&1 | grep -i ifindex
```

**需要确认**：

- [ ] 输出 `已获取 N 个接口的 ifindex`，N 与设备实际接口数相符
- [ ] `LoopBack1` 和 `GigabitEthernet0/0/1` / `0/0/2` 都在映射里
- [ ] 响应带 BOM 时解析仍正常（T2.4 已离线验证，此处确认真实响应）

**若接口名不匹配**：H3C 的接口名大小写可能是 `LoopBack1` 或 `Loopback1`。代码用 `eq_ignore_ascii_case` 比较，应该都能匹配，但值得确认。

## T3.4 IP 下发（单台）

```bash
# 先预览确认配置无误
$CWA deploy-ip --dry-run -d <设备名>

# 实际下发
$CWA deploy-ip -d <设备名>
```

**需要确认**：

- [ ] Loopback 接口通过 CLI 创建成功
- [ ] ifindex 重新查询到了新建的 Loopback
- [ ] Loopback IP 通过 NETCONF 配置成功
- [ ] 两个物理接口 IP 配置成功
- [ ] `post_check` 校验通过（查询回来的 IP 与期望一致）
- [ ] 快照文件生成：`state/snapshots/<设备名>_ip.json`

**设备侧验证**：

```
display interface LoopBack1
display ip interface brief
```

**快照内容检查**：

```bash
cat state/snapshots/<设备名>_ip.json
```

应包含 `loopback`（`existed_before`）、`loopback_ip`（`previous_ip` / `applied_ip`）、`interfaces` 三部分。

**`previous_ip` 的值很重要**：若物理接口原本没有 IP，应为 `null`；若原本有，应记录原值。这直接决定回退动作。

## T3.5 IP 幂等性

**紧接 T3.4，不要清理，直接重跑同一命令。**

```bash
$CWA deploy-ip -d <设备名>
```

**需要确认**：

- [ ] Loopback 创建报 `跳过: LoopBack1 已存在`
- [ ] 各 IP 配置报 `跳过: IP x.x.x.x 已存在`
- [ ] 整体状态 SUCCESS 或 SKIPPED，**不报错**
- [ ] 设备配置无变化

**这条用例验证的是 Python 版的一个实际缺陷**：`atoms/utils.py::get_cli_interface()` 是 `return None` 空实现，导致 `pre_check` 永远认为接口不存在。本实现改为真实查询 `display interface X brief`。

若这条不通过（比如重复创建报错），说明存在性查询的输出解析需要调整。

## T3.6 IP 回退

```bash
$CWA rollback-ip -d <设备名>
```

**需要确认**：

- [ ] 回退计划被打印出来（每步的 description）
- [ ] 物理接口 IP 被删除，**但 admin 状态未改变**（关键）
- [ ] Loopback IP 被删除
- [ ] Loopback 接口被删除（因为是本次创建的）
- [ ] 快照文件被删除
- [ ] 顺序是：物理接口（逆序）→ Loopback IP → Loopback 接口

**设备侧验证**：

```
display interface LoopBack1        # 应报接口不存在
display ip interface brief         # 物理接口应无 IP，但状态仍为 up
```

**若物理接口的 admin 状态被改了，是 bug** —— 回退不该改动本次变更未涉及的状态。

## T3.7 OSPF 下发

**前置**：T3.4 已完成（OSPF 依赖 Loopback 和物理接口 IP）。

```bash
$CWA deploy-ospf --dry-run -d <设备名>   # 先看 XML 和回退计划
$CWA deploy-ospf -d <设备名>
```

**需要确认**：

- [ ] ifindex 查询成功，三个接口都纳入了 area 0.0.0.0
- [ ] `router-id` 是 Loopback IP
- [ ] 下发使用 `merge` + `rollback-on-error`
- [ ] `commit` 成功
- [ ] `post_check` 查询到 OSPF 进程存在
- [ ] 快照生成：`state/snapshots/<设备名>_ospf.json`
- [ ] 快照里 `instance_existed_before` 为 `false`（首次下发）

**设备侧验证**：

```
display current-configuration configuration ospf-1
display ospf brief
display ospf interface
```

物理接口应为 P2P（`network_type = 3`），Loopback 为默认类型。

**这是本次重写新增 Atom 抽象的场景**（Python 版是手写在场景脚本里的），所以要特别关注：

- [ ] `pre_check` 的幂等判断是否工作（Python 版没有这一步）
- [ ] `rollback-on-error` 在 Comware 上的实际行为是否与预期一致

## T3.8 OSPF 幂等性

```bash
$CWA deploy-ospf -d <设备名>
```

**需要确认**：

- [ ] `pre_check` 检测到进程已存在
- [ ] 输出提示 `注意: OSPF 进程 1 在本次变更前已存在，回退时将拒绝删除`
- [ ] 不报错

**注意**：这里有个设计上的取舍需要你判断。当前实现在进程已存在时**仍然下发**（merge 幂等），但会把 `instance_existed_before` 记为 `true`，导致后续回退被阻止。

如果你的实际使用场景是"反复下发同一份 OSPF 配置"，这个行为会让回退变得不可用。届时需要讨论：是改为 `Skipped`，还是区分"进程存在但配置不同"和"完全一致"两种情况。

## T3.9 OSPF 回退

```bash
$CWA rollback-ospf -d <设备名>
```

**需要确认**：

- [ ] `rollback_guard` 检查通过（首次下发的快照 `instance_existed_before = false`）
- [ ] 回退计划被打印
- [ ] 删除 XML 只发索引列
- [ ] 顺序是 接口 → 区域 → 进程
- [ ] `continue-on-error` 容忍了部分对象不存在
- [ ] 快照被删除

**设备侧验证**：

```
display current-configuration configuration ospf-1    # 应无输出
display ospf brief                                     # 应无 OSPF 进程
display current-configuration interface LoopBack1      # 确认接口上无 OSPF 残留参数
```

**最后一条很重要**：`summary.md` 第七章记录的 v1/v2/v3 三次失败，都与"接口上残留 OSPF 参数"有关。确认 v4 方案（逐层删除 + 仅索引列）在你的设备上真的清理干净了。

## T3.10 多设备并发

```bash
# 全部设备
$CWA deploy-ip
$CWA deploy-ospf

# 调整并发数
$CWA enable-netconf --workers 3
```

**需要确认**：

- [ ] 各设备日志不交错（`TaskLog` 聚合生效）
- [ ] 汇总正确统计成功/失败数
- [ ] 全部成功时退出码 0，有失败时退出码 1

**注意当前的并发策略**：

| 场景 | 策略 |
| :--- | :--- |
| `console` | 串行（共用串口服务器） |
| `enable-netconf` | 并发（受 `--workers` 控制） |
| `deploy-ip` / `deploy-ospf` | **顺序执行**（非并发） |

IP / OSPF 顺序执行是当前实现的限制（`SceneApi` 不是 `Send`）。3 台设备下可接受。若你的实际规模更大且成为瓶颈，这是明确的优化点。

**部分成功场景**：故意让一台设备不可达（改错 `hostname`），确认：

- [ ] 其他设备正常完成
- [ ] 汇总里明确标出失败设备及原因
- [ ] 退出码为 1

**这里有个已知的边界**：`rollback-on-error` 只保证单设备单次 RPC 原子性，**不会**在一台失败时回滚其他已成功的设备。需要人工判断是否对已成功的设备执行 `rollback-*`。

## T3.11 回退依赖检查（OSPF）

验证 Python 版没有的能力。

```bash
# 步骤 1：正常下发一次，产生 instance_existed_before=false 的快照
$CWA deploy-ospf -d <设备名>

# 步骤 2：手工把快照改成 true，模拟"进程在变更前已存在"
$PY -c "
import json
f='state/snapshots/<设备名>_ospf.json'
d=json.load(open(f)); d['instance_existed_before']=True
json.dump(d,open(f,'w'),indent=2)"

# 步骤 3：尝试回退，应被阻止
$CWA rollback-ospf -d <设备名>
```

**预期**：

```
  回退被阻止:
    - OSPF 进程 1 在本次变更前已存在，非本次创建，拒绝删除

状态: FAILED
```

**需要确认**：

- [ ] 回退被阻止，退出码 1
- [ ] 设备上的 OSPF 配置**未被删除**
- [ ] 快照文件仍然保留

**清理**：把快照改回 `false` 再正常回退，或手工在设备上删除 OSPF 配置。

## T3.12 Console 开局

**风险最高，放最后。** 这是整个迁移里唯一从零实现的部分（`telnet` crate 只提供协议层，提示符状态机、ZTP 中断时序全部自写）。

**前置**：设备处于零配置状态，仅 Console 可达。

```bash
# 先预览
$CWA console --dry-run -d <设备名>

# 实际执行
RUST_LOG=debug $CWA console -d <设备名>
```

**需要确认**：

- [ ] Telnet 连接到串口服务器成功（`netmiko_oob.hostname` / `port`）
- [ ] `wake()` 发 `\r` 后有回显
- [ ] `interrupt_ztp()` 的两次 `\x03` 成功中断 ZTP
- [ ] `Press ENTER` 提示被自动响应
- [ ] 提示符正则匹配：`<HOSTNAME>` 和 `[HOSTNAME]`
- [ ] `system-view` 进入成功
- [ ] 配置逐行下发，无错误
- [ ] `end` + `quit` 正常退出

**最可能出问题的地方**：

| 现象 | 排查方向 |
| :--- | :--- |
| `ConsoleTimeout` | 错误信息带尾部 200 字符回显，看卡在哪个提示符 |
| ZTP 未中断 | `\x03` 次数或间隔可能需要调整（Python 版是 2 次 × 0.5s×3） |
| 提示符不匹配 | 零配置设备的提示符可能是 `<H3C>` 或带其他前缀 |
| 等待时间不够 | 调大 `netmiko_oob.extras.global_delay_factor`（当前 3） |

**调试建议**：`RUST_LOG=debug` 会输出 `检测到 Press ENTER，自动响应` 等关键节点。若卡住，`ConsoleTimeout` 的尾部回显是最有用的线索。

---

# 已知未验证事项汇总

供你测试时重点关注：

| # | 事项 | 相关用例 |
| :--- | :--- | :--- |
| 1 | Comware 在 `GenericVendor` profile 下的 hello 能力协商 | T3.1 |
| 2 | `commit` 时序、`rollback-on-error` 在 Comware 上的实际行为 | T3.7 |
| 3 | `rneter` h3c 提示符正则对真实 SR88 R7171 输出的匹配度 | T3.2 |
| 4 | 80 字符换行问题是否仍需 `sysname` 置末行的规避 | T3.2 |
| 5 | Console Telnet 的 ZTP 中断时序 | T3.12 |
| 6 | Loopback 存在性查询的输出解析（`display interface X brief`） | T3.5 |
| 7 | OSPF 幂等时"仍下发但标记 existed_before"的取舍是否合理 | T3.8 |
| 8 | 接口上 OSPF 参数是否被彻底清理 | T3.9 |

# 与 Python 版行为差异汇总

测试时若发现行为不同，先查是否在此列表内（这些是**有意的差异**）：

| 差异 | Python | Rust | 理由 |
| :--- | :--- | :--- | :--- |
| CLI 下发错误 | 不检测回显，只要不抛异常就报成功 | 检出错误即中止 | 延伸③，Python 版是缺陷 |
| Loopback 存在性 | `get_cli_interface()` 空实现，永远认为不存在 | 真实查询 | 幂等判断失效是缺陷 |
| OSPF 回退 | 无条件删除整个进程 | 进程变更前已存在则拒绝 | 避免误删既有业务 |
| `commit` 失败后 | candidate 残留脏数据 | 自动 `discard_changes` | 避免影响后续操作 |
| 快照位置 | `logs/snapshots_*.json` | `state/snapshots/*.json` | 与调试产物隔离 |
| 快照写入 | 直接 `open(...,"w")` | 临时文件 + 原子重命名 | 避免截断 |
| `ifindex` XML | 每次查询写盘 `logs/ifindex_*.xml` | 不写盘 | 调试产物不混入业务目录 |
| 规划表 | 硬编码在场景脚本，OSPF 反向 import | `plan.yaml`（可选，默认值相同） | 解耦场景脚本 |
| CMD 纯 CLI 路线 | 有 `test-cmd` / `rollback-cmd` | **未实现** | 只实现验证过的混合路线 |
| 入口 | 5 个脚本 | 1 个二进制 + 子命令 | — |

---

# 反馈需要的信息

若某条用例失败，提供以下信息便于定位：

```bash
# 1. 完整的 debug 日志
RUST_LOG=debug $CWA <失败的命令> 2>&1 | tee /tmp/cwa_fail.log

# 2. 设备侧的实际配置
# display current-configuration
# display version

# 3. 快照内容（若已生成）
cat state/snapshots/*.json

# 4. 环境信息
$CWA --version
rustc --version
```

对于 NETCONF 相关失败，`RUST_LOG=debug` 会输出完整的 RPC 请求与响应，这是最关键的信息。
