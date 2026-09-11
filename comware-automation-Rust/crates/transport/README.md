# cwa-transport

CLI(SSH) / NETCONF / Console(Telnet) 三通道传输层。

对应 Python 版 [`atoms/utils.py`](../../../nornir-comware-automation/atoms/utils.py)（NETCONF 会话与查询）、`scenes/*.py` 里的 Netmiko 调用、以及 `bootstrap_step1.py` 的 Console 处理。

## 概述

三条通道，成熟度与风险截然不同：

| 通道 | 底层 crate | 成熟度 | 风险 |
| :--- | :--- | :--- | :--- |
| NETCONF | `rustnetconf` `=0.17.0` | 4857 行 session.rs，有 vSRX 真机测试 | H3C 走 `GenericVendor` 兜底，未验证 |
| CLI over SSH | `rneter` `=0.5.2` | 内置 `h3c_comware` 模板，含错误正则 | 799 次下载，2026 年新项目 |
| Console Telnet | `telnet` `=0.2.5` | **仅协议层，状态机自写** | 整个迁移里唯一无对等库的部分 |

## 供应链隔离（重要）

**所有对第三方 crate 的调用都收敛在本 crate 内部。** 上层 `atoms` / `scenes` 只依赖本 crate 暴露的类型，不直接引用 `rustnetconf` / `rneter` / `telnet` 的任何类型。

唯一的例外是 `ErrorOption`，通过 re-export 暴露：

```rust
pub use rustnetconf::types::ErrorOption;
```

这样做的代价是上层间接依赖了这个枚举的定义，收益是避免为三个变体再包一层同构枚举。若将来需要更换 NETCONF 实现，改动面仍限于本 crate + 一处 re-export。

三个 crate 在 `Cargo.toml` 中**锁定精确版本**（`=0.17.0` 形式），不用 caret range，`Cargo.lock` 已提交。理由见 [`../../MIGRATION_PLAN.md`](../../MIGRATION_PLAN.md) 6.2。

---

# NETCONF 通道

## 使用方法

```rust
use cwa_transport::{NetconfSession, ErrorOption};

let mut nc = NetconfSession::connect(host).await?;

// 查询接口名 → ifindex 映射
let map: Vec<(String, i64)> = nc.ifindex_map().await?;

// 查询指定接口的 IPv4 地址
let ip: Option<String> = nc.interface_ip(1235).await?;

// 下发 + 提交（失败自动 discard，避免脏 candidate）
nc.edit_and_commit(fragment, Some(ErrorOption::RollbackOnError)).await?;

// 或分步
nc.edit_config_fragment(fragment, None).await?;
nc.commit().await?;

nc.close().await?;
```

## 关键差异：`<config>` 外壳

这是整个迁移里最容易出错的一处语义翻转。

### Python 侧（ncclient）

查 ncclient master 的 `operations/edit.py`：

```python
if format == 'xml':
    node.append(validated_element(config, ("config", qualify("config"))))
```

而 `xml_.py` 的 `validated_element()` 在根元素 tag 不在允许列表时抛 `XMLError`。

**即 ncclient 要求 `config` 参数的根元素必须是 `config`。**

由此得出两个结论：

| Python 代码位置 | 写法 | 是否正确 |
| :--- | :--- | :--- |
| `scenes/*.py` | `config_xml = f"<config>{xml_frag}</config>"` | **正确** |
| `atoms/base.py::NetconfAtom._edit_config()` | 直接传 `xml_frag` 裸片段 | **错误，会抛 `XMLError`** |

后者之所以从未暴露，是因为 `NetconfIPAddressAtom` 从未被任何场景实例化调用。这正好回答了 `need_to_discuss_prob.md` 延伸⑥的开放问题——**与语言选择无关，是 Python 代码里的一个待修缺陷**。

### Rust 侧（rustnetconf）

约定完全相反：

```rust
pub async fn edit_config(&mut self, target, config, ...) -> Result<(), NetconfError> {
    rpc::validate_xml_fragment(config)?;
    let wrapped_config = self.vendor_profile.wrap_config(config);   // 库负责包
    ...
}
```

**调用方必须传裸片段。**

### 本 crate 的处理

统一按裸片段处理，并在 `edit_config_fragment()` 里主动剥除误传的外壳：

```rust
fn strip_config_wrapper(fragment: &str) -> &str {
    let t = fragment.trim();
    let Some(rest) = t.strip_prefix("<config>") else { return t };
    match rest.strip_suffix("</config>") {
        Some(inner) => {
            tracing::warn!("edit_config 收到带 <config> 外壳的片段，已自动剥除");
            inner.trim()
        }
        None => t,
    }
}
```

从架构上消除了歧义：无论调用方怎么传，最终都是裸片段。误传时有 warning 提示。

## 会话参数

| 参数 | 来源 | 默认值 | 对应 Python |
| :--- | :--- | :--- | :--- |
| 端口 | `host.connection_options["ncclient"].port` | 830 | `port=830` |
| 超时 | `ncclient.extras.timeout` | 60 s | `timeout=60` |
| 主机密钥校验 | 固定 `AcceptAll` | — | `hostkey_verify=False` |
| vendor profile | 不指定，回落 `GenericVendor` | — | `device_params={"name":"hpcomware"}` |
| `gather_facts` | `false` | — | 无对应 |

### 安全说明

`HostKeyVerification::AcceptAll` 对应 Python 版的 `hostkey_verify=False`。

这在实验/开局场景是必要的（设备密钥尚未分发），但**不适合生产环境**——无法防御中间人攻击。生产部署应改为 `HostKeyVerification::Fingerprint(...)` 或 known_hosts 策略，`rustnetconf` 两者都支持。

`gather_facts(false)` 是刻意的：facts 采集会发额外 RPC，而 H3C 在 `GenericVendor` 下的 facts RPC 未必被支持，关掉可减少一处未知。

### 关于 `GenericVendor`

`rustnetconf` 的 `src/vendor/` 只有 `junos` 和 `generic` 两个 profile，`detect_vendor()` 对非 Junos 设备一律回落 `GenericVendor`。

对本项目**可能**够用，理由：

1. 所有 XML 都在模板里写全了 `xmlns="http://www.h3c.com/netconf/config:1.0"`，不依赖 profile 注入命名空间
2. ncclient 的 `hpcomware` handler 源码只做两件事——注入 nsmap、注册 `cli_display` / `cli_config` / `action` / `rollback` / `save` 五个私有 RPC，**这五个本项目一个都没用**

即换语言不会丢厂商适配逻辑。但"可能够"不等于"验证过"，见测试用例 T3.1。

## 补齐的能力：`discard_changes`

Python 版从未调用 `discard_changes`。一旦 `edit_config` 成功但 `commit` 失败，candidate 会残留脏数据，影响同一会话的后续操作。

本 crate 的 `edit_and_commit()` 在两个失败路径都自动丢弃：

```rust
pub async fn edit_and_commit(&mut self, fragment, error_option) -> Result<(), TransportError> {
    if let Err(e) = self.edit_config_fragment(fragment, error_option).await {
        let _ = self.discard_changes().await;
        return Err(e);
    }
    if let Err(e) = self.commit().await {
        let _ = self.discard_changes().await;
        return Err(e);
    }
    Ok(())
}
```

`candidate_dirty()` 暴露 `rustnetconf` 的脏标记，供场景层判断。

## XML 解析

用 `roxmltree` 只读解析，对应 Python 版的 lxml + 命名空间 XPath。

### BOM 处理

Python 版 `_safe_parse_xml()` 会剥除 UTF-8 BOM：

```python
if xml_str.startswith('\ufeff'):
    xml_str = xml_str[1:]
```

本 crate 保留等价逻辑（`strip_bom()`），因为 H3C 设备的响应确实可能带 BOM。

### 同名节点陷阱

H3C 的 IPv4 地址模型里，**容器与叶子同名**：

```xml
<Ipv4Address>                          <!-- 容器 -->
    <IfIndex>1235</IfIndex>
    <Ipv4Address>11.11.11.11</Ipv4Address>   <!-- 叶子 -->
    <Ipv4Mask>255.255.255.255</Ipv4Mask>
</Ipv4Address>
```

直接按名字匹配会把叶子也当成容器。本 crate 的过滤条件要求节点**必须有 `IfIndex` 子元素**：

```rust
&& n.children().any(|c| c.is_element() && c.tag_name().name() == "IfIndex")
```

### 解析函数

两个函数是 `pub`，可脱离设备直接测试（见 T2.4）：

```rust
pub fn parse_ifindex_map(xml: &str) -> Result<Vec<(String, i64)>, String>;
pub fn parse_interface_ip(xml: &str, ifindex: i64) -> Result<Option<String>, String>;
```

### 不再写盘

Python 版 `get_ifindex_map()` 每次查询都覆盖写 `logs/ifindex_<device>.xml`，属调试产物混入业务目录（新发现③）。本 crate 不写盘，需要留痕由调用方通过 `cwa_atoms::DebugStore` 决定。

---

# CLI over SSH 通道

## 使用方法

```rust
use cwa_transport::{CliSession, mode};

let cli = CliSession::connect(host).await?;

// 单条命令，指定模式
let out = cli.run(mode::ENABLE, "display interface brief").await?;

// 系统视图下批量下发（任一条报错即中止）
cli.send_config(&lines).await?;
cli.send_config_text(&rendered_text).await?;
```

`mode::ENABLE` = `"Enable"`（用户视图 `<HOSTNAME>`），`mode::CONFIG` = `"Config"`（系统视图 `[HOSTNAME]`），对应 `rneter` h3c 模板定义的状态机节点。

## 补齐的能力：下发错误检测

这是 Python 版最实质的缺失（`need_to_discuss_prob.md` 延伸③）。

### Python 侧的问题

```python
net_connect.send_command(config_text, expect_string=expect_string)
return {"status": "success", "detail": None}
```

**对返回值不做任何检查**。设备返回 `% Unrecognized command` 时，只要连接层没抛异常就报成功。

### 本 crate 的双重校验

```rust
// ① 库层的 success 标志（rneter h3c 模板的 error_regex 已含 .+%.+ 、.+\^.+ 等）
if !output.success { return Err(...) }

// ② 本地显式扫描，不依赖库内部行为的变化
if let Some(err_line) = check_output_for_errors(&output.content) { return Err(...) }
```

`check_output_for_errors()` 的模式列表：

| 正则 | 匹配对象 |
| :--- | :--- |
| `^\s*%\s*\S` | H3C 错误统一以 `%` 开头 |
| `\^\s*$` | `^` 标记出错位置 |
| `(?i)unrecognized command` | 命令不识别 |
| `(?i)wrong parameter` | 参数错误 |
| `(?i)incomplete command` | 命令不完整 |
| `(?i)too many parameters` | 参数过多 |
| `(?i)permission denied` | 权限不足 |
| `(?i)doesn't exist` / `does not exist` | 对象不存在 |
| `(?i)failed to apply` | 应用失败 |
| `(?i)invalid input` | 输入非法 |

**关键设计：`^\s*%\s*\S` 要求 `%` 在行首**（允许前导空白）。这样才能区分：

```
% Unrecognized command found at '^' position.    → 检出错误
 ip address 1.1.1.1 255.255.255.0                → 正常配置行，不误报
```

若写成 `.+%.+`（`rneter` 内置模板的写法），任何含 `%` 的正常回显都会误报。本 crate 在库层之外再加一层更精确的判定，正是为此。

### 行为收紧

`send_config()` 任一条命令检出错误即**中止并返回错误，不继续下发后续命令**。

这与 Python 版"无论回显如何都继续"不同，是有意的。理由：配置命令通常有顺序依赖，前一条失败后继续执行会产生更难恢复的中间状态。

## 独立块解析（`parse_preserving_order`）

对应 Python 版 `bootstrap_step2.py::parse_preserving_order()`。

模板中用标记切分需要独立连接的块：

```jinja
{# ---- 独立块：RSA 密钥（含交互输入 512） ---- #}
#INDEPENDENT:START
public-key local create rsa
y
512
#INDEPENDENT:END
```

`public-key local create rsa` 需要回答 `y` 和密钥长度 `512`，这些交互应答行本身不是合法命令，且不产生标准提示符，所以要独立连接、采用"发送即忘"策略。

解析规则与 Python 版一致：

| 输入行 | 处理 |
| :--- | :--- |
| 空行 | 跳过 |
| `#INDEPENDENT:START` | 先把缓冲的普通命令收成一个 `Normal` 单元，进入块模式 |
| `#INDEPENDENT:END` | 把当前块收成一个 `Independent` 单元，退出块模式 |
| 其他 `#` 开头 | 视为注释，跳过 |
| 其他 | 按当前模式追加到普通缓冲或块缓冲 |

```rust
pub enum ExecUnit {
    Normal { commands: Vec<String> },
    Independent { lines: Vec<String> },
}
```

这是**纯字符串处理，可完全离线验证**（见 T2.3）。

---

# Console Telnet 通道

## 这是唯一从零实现的部分

`telnet` crate 只提供协议层。以下全部自写：

| Python (Netmiko) | 本 crate |
| :--- | :--- |
| `device_type='hp_comware_telnet'` 驱动 | 直接走 telnet 协议 |
| `write_channel("\r")` 唤醒 | `wake()` |
| `write_channel("\x03")` ×2 中断 ZTP | `interrupt_ztp()` |
| `read_until_prompt()` + 检测 `Press ENTER` | `read_until()` 内自动处理 |
| `send_command(cmd, expect_string=r"\[.*?\]")` | `send_expect()` |
| `global_delay_factor: 3` | `delay_factor` 字段，`scaled()` 放大等待 |

## 使用方法

```rust
use cwa_transport::ConsoleSession;

// 注意：同步阻塞 API，调用方需放进 spawn_blocking
let mut console = ConsoleSession::connect(host)?;
console.wake()?;
console.interrupt_ztp()?;
console.enter_system_view()?;
console.send_config_lines(&lines)?;
console.finish()?;
```

`ConsoleSession` 是**同步阻塞**的（`telnet` crate 无 async 版本）。`scenes/step1_console.rs` 把它放进 `tokio::task::spawn_blocking`，避免阻塞运行时。

## 连接参数

全部取自 `host.connection_options["netmiko_oob"]`：

| 参数 | 来源 | 默认值 |
| :--- | :--- | :--- |
| 串口服务器地址 | `netmiko_oob.hostname` | **必填**，缺失报错 |
| Telnet 端口 | `netmiko_oob.port` | 23 |
| 延迟放大系数 | `netmiko_oob.extras.global_delay_factor` | 1 |
| 读超时 | `netmiko_oob.extras.timeout` | 120 s |

缺少 `netmiko_oob` 时报 `MissingConnectionOption`，对应 Python 版对该连接选项的检查。

## 提示符状态机

三条正则：

```rust
const RE_ENABLE: &str      = r"<[^>\r\n]+>\s*$";      // 用户视图 <HOSTNAME>
const RE_CONFIG: &str      = r"\[[^\]\r\n]+\]\s*$";   // 系统视图 [HOSTNAME]
const RE_PRESS_ENTER: &str = r"(?i)press\s+enter";    // 需按回车继续
```

`read_until()` 的循环逻辑：

```
每 500ms 读一次，直到 read_timeout
    │
    ├── 读到数据 → 追加到缓冲
    │       ├── 匹配 Press ENTER → 自动发 \r，清空缓冲，继续
    │       └── 匹配 matcher     → 返回缓冲内容
    │
    └── 超时     → 若 matcher 已匹配则返回，否则继续等
            │
            └── 总超时 → ConsoleTimeout（携带尾部 200 字符便于排查）
```

`ConsoleTimeout` 刻意携带尾部回显，因为 Console 场景最难排查的就是"卡在哪个提示符上"。

## ZTP 中断时序

与 Python 版严格一致：

```rust
for _ in 0..2 {
    self.write_raw("\x03")?;              // Ctrl-C
    std::thread::sleep(self.scaled(500)); // ×delay_factor
}
self.write_raw("\r")?;
std::thread::sleep(self.scaled(1000));
// 读一次，吸收横幅与可能的 Press ENTER
let _ = self.read_until(&|s| re_enable.is_match(s) || re_config.is_match(s));
```

`scaled(ms)` = `ms × delay_factor`，对应 Netmiko 的 `global_delay_factor`。H3C Console 在 Group 里配的是 `global_delay_factor: 3`，即实际等待 1.5 s / 3 s。

最后那次 `read_until` 忽略结果（`let _ =`），因为此时可能还没有稳定提示符，目的只是清空缓冲区里的 ZTP 横幅。

## 错误检测复用

`send_expect()` 复用 CLI 通道的 `check_output_for_errors()`，Console 下发同样有错误检测——Python 版 Console 路径也完全没有这层。

---

## 强约束：绝对禁止行为

### 禁止 1：在上层 crate 直接 `use rustnetconf::` / `use rneter::`

破坏供应链隔离。所有第三方类型必须经本 crate 转译或 re-export。

### 禁止 2：给 `edit_config_fragment()` 传带 `<config>` 外壳的片段

虽然会被自动剥除并告警，但正确做法是模板直接输出裸片段。依赖自动剥除等于把约定藏进实现。

### 禁止 3：在 tokio 运行时里直接调用 `ConsoleSession` 的方法

它是同步阻塞的，会卡住整个运行时的工作线程。必须包在 `spawn_blocking` 里。

### 禁止 4：放宽 `check_output_for_errors()` 的 `%` 行首约束

改成 `.+%.+` 会让任何含 `%` 的正常回显误报为错误。这个约束是精确性的关键。

### 禁止 5：在生产环境沿用 `HostKeyVerification::AcceptAll`

实验环境可以，生产必须改为 `Fingerprint` 或 known_hosts。

## 弱约束：推荐做法

### 推荐 1：优先用 `edit_and_commit()` 而非分步调用

它包含了失败时的 `discard_changes`，避免脏 candidate。

### 推荐 2：新增 NETCONF 查询时，把解析逻辑写成 `pub fn` 纯函数

像 `parse_ifindex_map` / `parse_interface_ip` 那样，接收 XML 字符串返回结果。这样可以用构造的响应离线验证，不需要设备。

### 推荐 3：Console 场景串行执行

多台设备共用一台串口服务器时，并发连接容易互相干扰。`scenes` 层已对 Console 场景采用串行。

## 常见问题

### Q1：为什么 NETCONF 不用连接池？

`rustnetconf` 有 `src/pool/`，但当前场景每台设备一次操作就关闭会话，池化收益不大。设备规模上升后可以考虑。

### Q2：`rneter` 的 `h3c_comware` 模板具体做了什么？

读它的 `src/templates/network/h3c.rs`：

- 提示符区分 Enable `<...>` / Config `[...]`，且处理了 `RBM_P` / `RBM_S`（IRF 主备前缀）
- Config 正则刻意排除 `[Y/N]`，避免确认框被误判为提示符
- `error_regex` 含 `.+%.+`、`.+\^.+`、`Permission denied\.`、`Failed to apply .+` 等
- `more_regex` 处理 `---- More ----` 分页
- `after_connect` hook 自动发 `screen-length disable`
- `input_rule` 处理保存确认、"保留原文件名请回车"、密码过期提示

覆盖度看起来不错，但对真实 SR88 R7171 输出的匹配度未验证，见 T3.2。

### Q3：80 字符换行问题会不会在 Rust 侧复现？

Python 项目的 `bootstrap.j2` 刻意把 `sysname {{ sysname }}` 放在最后一行，注释说明是"避免过早修改设备名称，触发 80 字符换行，导致 netmiko 原生命令校验失败"。

模板原样复制，所以这个规避手法保留了。但 `rneter` 是否需要同样的规避、或是否会以别的形式表现，**未验证**。真机测试时若发现不需要，可以考虑放开——但要先在 Python 侧同步确认。

### Q4：Console 通道为什么不用 async？

`telnet` crate（14.7 万下载）只有同步 API。可选方案是用 `tokio::net::TcpStream` 自己实现 telnet 协商，但那会把"唯一从零实现的部分"扩大到协议层，风险更高。

当前用 `spawn_blocking` 包装，代价是每台设备占一个阻塞线程。Console 开局是低频操作且已串行，可接受。

### Q5：如果 `rustnetconf` 或 `rneter` 停更了怎么办？

预案见 `MIGRATION_PLAN.md` 6.2：两者均为 MIT / Apache-2.0，最坏情况可 vendor 进仓库自行维护。因为调用被隔离在本 crate 内，改动面可控。

### Q6：怎么在没有设备的情况下测这一层？

三类：

1. **纯函数**：`parse_ifindex_map` / `parse_interface_ip` / `check_output_for_errors` / `parse_preserving_order` 都可直接喂构造数据（T2.3、T2.4、T2.5）
2. **连接失败路径**：指向不存在的地址，验证错误类型与提示信息（T2.6）
3. **`rneter` 的 testkit**：它提供 `FakeSshDevice` + `DevicePersona::builtin("h3c_comware")`，可做 CLI 通道的离线 E2E。本项目未引入（按要求未写测试代码），但真机测试前若想进一步降险，这是可选路径
