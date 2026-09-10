# bootstrap_step2.py

## 概述

`bootstrap_step2.py` 是 **阶段二：SSH 下发 NETCONF 初始化配置** 的独立入口脚本。

它负责：
1. 自动构建 `groups.yaml`（调用 `scripts/build_inventory.py`）
2. 加载 Nornir Inventory
3. 通过 Jinja2 渲染 `netconf_cmd.j2` 模板
4. 通过 **SSH 连接** 逐条下发配置，开启 NETCONF 服务（端口 830）
5. 支持 **独立连接块**（`#INDEPENDENT:START/END`）处理交互式命令

**与阶段一的本质区别**：
- 阶段一（Console）设备刚上电，无认证、无 SSH，需绕过 Nornir 使用原生 Netmiko
- 阶段二（SSH）设备已具备 SSH 能力，Nornir 的 Netmiko 连接管理可正常工作


## 使用方法

```bash
# 预览 NETCONF 配置（不下发）
python bootstrap_step2.py preview

# 对单台设备预览
python bootstrap_step2.py preview H3C-SR88-01

# 对所有设备下发 NETCONF 配置
python bootstrap_step2.py deploy

# 对单台设备下发
python bootstrap_step2.py deploy H3C-SR88-01
```


## 核心流程

```
1. 构建 groups.yaml（合并厂商/产线/OS/分支层级）
2. 加载 Nornir Inventory（hosts.yaml + groups.yaml）
3. 渲染 netconf_cmd.j2 模板（通过继承链查找）
4. 解析模板中的执行单元（普通命令块 + 独立块）
5. 按原始顺序执行：
   a. 普通命令块 → Nornir 托管连接批量下发
   b. 独立块 → 独立 SSH 连接逐条发送
6. 独立块采用“发送即忘”策略，不解析回显
7. 断开连接
```


## 独立连接块机制

### 什么是独立连接块？

在模板中通过 `#INDEPENDENT:START` 和 `#INDEPENDENT:END` 标记的命令块，会在**独立的 SSH 连接**中执行，而不是使用 Nornir 的托管连接。

### 为什么需要独立连接块？

Nornir 的连接复用机制在普通命令场景下非常高效，但遇到**交互式命令**（如 `public-key local create` 需要输入 `y` 和 `512`）时无法处理。独立连接块通过在单独的 SSH 会话中执行，绕过了 Nornir 的连接复用限制。

### 模板示例

```jinja2
{# ---- 普通命令（Nornir 托管连接） ---- #}
system-view
netconf ssh server enable
netconf ssh server port 830

{# ---- 独立块：交互式命令 ---- #}
#INDEPENDENT:START
public-key local create rsa
y
512
#INDEPENDENT:END

#INDEPENDENT:START
public-key local create dsa
y
512
#INDEPENDENT:END

#INDEPENDENT:START
public-key local create ecdsa secp256r1
y
#INDEPENDENT:END
```

### 独立块的执行策略

**“发送即忘”**：
- 逐行发送命令，不等待特定提示符
- 使用 `expect_string=r".*"` 匹配任意输出，立即返回
- 使用 `time.sleep(0.2)` 控制发送节奏，避免发送过快导致设备“吞掉”输入
- 执行完毕后立即关闭连接，设备在后台继续处理耗时操作（如密钥生成）

**适用场景**：
- 密钥创建（RSA/DSA/ECDSA）
- 证书导入
- 软件升级
- 任何需要多行交互的配置命令


## 阶段一 vs 阶段二

| 维度 | 阶段一（Console） | 阶段二（SSH） |
| :--- | :--- | :--- |
| **脚本** | `bootstrap_step1.py` | `bootstrap_step2.py` |
| **目标** | 开局配置（主机名、管理 IP、SSH） | NETCONF 初始化（开启 830 端口） |
| **模板** | `bootstrap.j2` | `netconf_cmd.j2` |
| **场景 API** | `scene_api.bootstrap()` | `scene_api.ssh_bootstrap()` |
| **连接方式** | Console 映射 Telnet（无认证） | SSH（用户名 + 密码） |
| **连接管理** | 原生 Netmiko（绕过 Nornir） | **Nornir 托管 + 独立块** |
| **ZTP 中断** | ✅ 需要（Ctrl+C） | ❌ 不需要 |
| **交互式命令** | 需特殊处理 | 通过独立块处理 |


## 为什么阶段二能用 Nornir 托管连接？

| 条件 | 阶段一 | 阶段二 |
| :--- | :--- | :--- |
| 设备有 SSH 服务 | ❌ 尚未配置 | ✅ 已由 Phase 1 开启 |
| 有用户名/密码 | ❌ 尚未创建 | ✅ 已由 Phase 1 创建 |
| 管理 IP 可达 | ❌ 尚未配置 | ✅ 已由 Phase 1 配置 |
| Nornir 连接参数 | 需要 OOB 覆盖 | **顶层属性直接使用** |

**核心**：阶段二设备的 SSH 认证信息已完整，`host.hostname`、`host.username`、`host.password` 均可正常使用，Nornir 的连接管理无需特殊处理。


## 命令与模板映射

| 命令 | 作用 | 模板场景 |
| :--- | :--- | :--- |
| `python bootstrap_step2.py preview` | 预览 `netconf_cmd.j2` | `scene_api.ssh_bootstrap()` |
| `python bootstrap_step2.py deploy` | 下发 `netconf_cmd.j2` | `scene_api.ssh_bootstrap()` |


## 模板继承链（`netconf_cmd.j2`）

```
os_versions/h3c/SR88_Comware_V7/R7171/H02/netconf_cmd.j2
    extends → R7171/netconf_cmd.j2
        extends → V7/netconf_cmd.j2
            extends → SR88/netconf_cmd.j2
                extends → vendors/h3c/netconf_cmd.j2
                    imports → _macros.j2
```

实际加载的模板取决于设备的 Group 继承链（由 `hosts.yaml` 中的 `groups` 字段决定）。


## 前置条件

| 条件 | 说明 |
| :--- | :--- |
| **Phase 1 已完成** | 设备已配置管理 IP、SSH 服务、SSH 用户 |
| **SSH 可达** | `hosts.yaml` 中 `hostname` 为可达的业务管理 IP |
| **认证信息** | `defaults.yaml` 或 `hosts.yaml` 中配置了正确的 `username`/`password` |
| **模板存在** | `netconf_cmd.j2` 在模板继承链中完整存在 |


## 配置示例

### `hosts.yaml`

```yaml
H3C-SR88-01:
  hostname: 172.12.1.11           # SSH 可达的业务管理 IP
  groups:
    - h3c_sr88_comware_v7_r7171   # 决定模板继承链
  data:
    location: "IDC-A"
    role: core_router
```

### `defaults.yaml`

```yaml
username: admin
password: Ssh@Pass123
```


## 核心代码片段

### 解析器（`parse_preserving_order`）

```python
def parse_preserving_order(config_text: str) -> list:
    """解析配置文本，返回按原始顺序排列的执行单元列表"""
    units = []
    normal_buffer = []
    in_block = False
    current_block = []

    for line in config_text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("#INDEPENDENT:START"):
            if normal_buffer:
                units.append({"type": "normal", "commands": normal_buffer})
                normal_buffer = []
            in_block = True
            current_block = []
            continue

        if stripped.startswith("#INDEPENDENT:END"):
            if current_block:
                units.append({"type": "block", "lines": current_block})
            in_block = False
            continue

        if stripped.startswith("#"):
            continue

        if in_block:
            current_block.append(stripped)
        else:
            normal_buffer.append(stripped)

    if normal_buffer:
        units.append({"type": "normal", "commands": normal_buffer})

    return units
```

### 独立块执行（“发送即忘”策略）

```python
with ConnectHandler(
    device_type='hp_comware',
    host=host.hostname,
    port=host.port or 22,
    username=host.username,
    password=host.password,
    timeout=60,
) as net_connect:
    # 进入系统视图
    if lines and not lines[0].startswith("system-view"):
        net_connect.send_command("system-view", expect_string=r"\[.*?\]")
    
    # 逐行发送，不等待特定提示符
    for line in lines:
        if line.startswith("system-view"):
            continue
        net_connect.send_command(line, expect_string=r".*", delay_factor=1)
        time.sleep(0.2)  # 控制发送节奏，确保交互被正确处理
```


## 依赖

- Python 3.8+
- Nornir 3.x
- Netmiko 4.x
- Jinja2 3.x
- PyYAML


## 常见问题

### Q1：独立块中的命令执行失败？

**原因**：可能是交互序列不完整，或发送节奏过快/过慢。

**解决**：
1. 检查模板中 `#INDEPENDENT:START/END` 块内的命令序列是否完整
2. 调整 `time.sleep(0.2)` 的间隔值

### Q2：下发失败，提示认证错误

**原因**：`username`/`password` 配置不正确，或设备 SSH 服务未开启。

**解决**：
1. 检查 `defaults.yaml` 或 `hosts.yaml` 中的认证信息
2. 确认 Phase 1 已成功执行（SSH 服务已开启、用户已创建）

### Q3：预览正常，下发失败

**原因**：预览只渲染模板，不涉及网络连接。下发失败通常是网络或认证问题。

**解决**：
1. 手动 SSH 登录设备验证认证信息
2. 检查 `config.yaml` 中的 `num_workers` 是否过大导致并发连接被拒绝
3. 查看 Nornir 日志定位具体错误

### Q4：独立块与普通命令块之间能保证执行顺序吗？

**能。** `parse_preserving_order()` 严格按照模板中的原始顺序解析，独立块和普通命令块交替执行，不会出现顺序错乱。

### Q5：独立块执行失败会影响其他设备吗？

**不会。** 每台设备独立处理，单台设备失败不影响其他设备（Nornir 默认行为）。


## 三阶段总览

| 阶段 | 脚本 | 命令 | 连接方式 | 模板 | 目标 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Phase 1** | `bootstrap_step1.py` | `console` | Console Telnet | `bootstrap.j2` | 开局配置（IP、SSH） |
| **Phase 2** | `bootstrap_step2.py` | `deploy` | SSH | `netconf_cmd.j2` | NETCONF 初始化（830 端口） |
| **Phase 3** | 待开发 | `netconf` | NETCONF | `*_xml.j2` | 业务配置下发（XML） |


## 扩展

- 如需添加新的 SSH 场景，在 `SceneAPI` 中新增方法并注册即可
- 模板继承链支持多厂商、多版本、多补丁，通过 `SceneAPI` 统一渲染
- 独立块机制可处理任意交互式命令，只需在模板中正确编写交互序列


## 维护者

[Your Name/Team]


*最后更新：2026-09-02*