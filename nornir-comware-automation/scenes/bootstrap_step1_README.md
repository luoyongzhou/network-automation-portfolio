

# bootstrap_step1.py

## 概述

`bootstrap_step1.py` 是 **H3C Comware V7 设备 Console 映射 Telnet 开局配置** 的主入口脚本。

它负责：
1. 自动构建 `groups.yaml`（调用 `scripts/build_inventory.py`）
2. 加载 Nornir Inventory
3. 通过 Jinja2 渲染 `bootstrap.j2` 模板
4. 通过 Console 映射 Telnet 连接设备，中断 ZTP，逐条下发配置


## 使用方法

```bash
# 对所有设备执行 Console 开局配置
python bootstrap_step1.py console

# 对单台设备执行
python bootstrap_step1.py console H3C-SR88-01

# 预览配置（不下发）
python bootstrap_step1.py preview H3C-SR88-01
```


## 核心流程

```
1. 构建 groups.yaml（合并厂商/产线/OS/分支层级）
2. 加载 Nornir Inventory（hosts.yaml + groups.yaml）
3. 渲染 bootstrap.j2 模板（通过继承链查找）
4. 通过 Console 映射 Telnet 连接设备
5. 中断 ZTP（Ctrl+C 两次 + 回车）
6. 进入系统视图，逐条下发命令
7. 退出到用户视图，设备端主动断开，本端立即关闭
```


## 连接选型：为何绕过 Nornir 的 Netmiko？

### 背景：双 IP 场景的挑战

在网络自动化开局场景中，设备处于“零配置”状态，仅能通过 **Console 映射 Telnet** 访问（无认证、无 SSH）。但设备上架后最终需要通过 **SSH 业务管理 IP** 进行日常运维。

因此，在 `hosts.yaml` 中需要同时承载两种连接信息：

```yaml
H3C-SR88-01:
  hostname: 172.12.1.11              # 业务 SSH IP（最终管理地址）
  connection_options:
    netmiko_oob:                      # Console 映射 Telnet（开局临时通道）
      hostname: 127.0.0.1             # 串口服务器 IP
      port: 30001                     # 该设备对应的 Telnet 端口
      extras:
        device_type: hp_comware_telnet
```

理想情况下，我们希望在 Phase 1（开局阶段）使用 `netmiko_oob` 连接，Phase 2/3（日常运维）使用默认的 SSH 连接。但实际测试中发现，**通过 `task.run(connection="netmiko_oob")` 切换连接时，Nornir 并未使用 `connection_options` 中的 `hostname` 和 `port`，而是始终使用顶层 `hostname`（业务 IP）和默认端口 22**，导致连接超时。


### Nornir 连接管理机制解析

要理解这个问题，需要先了解 Nornir 的连接管理架构。

#### 1. 连接参数的三层继承体系

Nornir 的连接参数来源于三个层次：

- **Host 对象顶层属性**：`hostname`、`port`、`username`、`password`、`platform`
- **Group 继承**：设备所属 Group 定义的参数
- **Defaults 全局默认值**：所有设备共享的默认参数

`connection_options` 是一个字典，key 是连接名称（如 `netmiko`、`netmiko_oob`），value 是 `ConnectionOptions` 对象。当调用 `task.run(task=netmiko_send_command, connection="netmiko_oob")` 时，Nornir 会查找名为 `netmiko_oob` 的连接配置。

#### 2. 参数合并的优先级规则

Nornir 的参数合并遵循明确的优先级：**Host > Group > Defaults**。具体到 `connection_options`：

- `extras` 字段在 Host、Group、Defaults 三层中**直接覆盖**，而非深度合并
- 这意味着如果 Host 层定义了 `extras`，Group 和 Defaults 层的 `extras` 会被完全替换

更关键的是，**`connection_options` 中的 `hostname`、`port` 等字段，其优先级低于 Host 对象顶层的同名属性**。Nornir 在构建最终连接参数时，会优先使用 `host.hostname` 和 `host.port`，而非 `connection_options` 中定义的值。

#### 3. 连接建立时机与缓存机制

Nornir 的连接是**懒加载（Lazy Loading）**的：第一次调用 `task.host.get_connection()` 时建立连接，后续调用复用同一个连接对象。这意味着：
- 连接参数在**首次建立连接时**被固化
- 即使在 Task 执行过程中修改了 `host.hostname`，已建立的连接不会自动更新
- 社区讨论中也有用户遇到类似问题：动态分配 IP 后赋值给 `hostname`，但 Netmiko 仍然使用初始值


### 问题复现与根因

我们尝试了多种方式切换连接：

**尝试 1：通过 `connection` 参数切换**
```python
task.run(
    task=netmiko_send_command,
    connection="netmiko_oob",      # 期望使用 OOB 参数
    command_string="system-view",
)
```
**结果**：仍然连接 `172.12.1.11:22`，`connection_options` 中的 `hostname: 127.0.0.1` 和 `port: 30001` 被忽略。

**尝试 2：修改 Host 对象顶层属性**
```python
host.hostname = "127.0.0.1"
host.port = 30001
task.run(task=netmiko_send_command, ...)
```
**结果**：部分生效，但 Nornir 的连接缓存可能导致仍使用旧值。

**尝试 3：在 `task.run()` 中显式传入参数**
```python
task.run(
    task=netmiko_send_command,
    hostname="127.0.0.1",
    port=30001,
    device_type="hp_comware_telnet",
    ...
)
```
**结果**：`netmiko_send_command` 内部调用 `task.host.get_connection()` 时，仍然使用 Host 对象存储的连接，`task.run()` 中的参数并未传递给连接建立过程。

**根因总结**：`nornir_netmiko` 的 `netmiko_send_command` Task 在建立连接时，通过 `task.host.get_connection()` 获取连接，而 `get_connection()` 使用的是 Host 对象在**首次连接时**的参数快照。`connection` 参数仅用于选择使用哪个 `connection_options` 配置，但**无法覆盖 Host 对象顶层的 `hostname` 和 `port`**。


### 解决方案：直接使用原生 Netmiko

鉴于上述机制限制，最终采用 **绕过 Nornir 连接管理，直接使用原生 Netmiko `ConnectHandler`** 的方案。

```python
from netmiko import ConnectHandler

net_connect = ConnectHandler(
    device_type='hp_comware_telnet',
    host=oob_hostname,
    port=oob_port,
    timeout=60,
    username='',
    password='',
)
```

**方案对比**：

| 维度 | Nornir 托管连接 | 原生 Netmiko |
| :--- | :--- | :--- |
| **连接参数控制** | 受三层继承和缓存机制约束，`hostname`/`port` 难以动态切换 | 完全可控，传入什么就用什么 |
| **连接生命周期** | 由 Nornir 管理，跨 Task 复用 | 由脚本管理，用完即断 |
| **多 IP 场景** | 需要复杂的参数覆盖或连接重建逻辑 | 天然支持，每次连接独立指定 |
| **调试难度** | 参数来源复杂（Host/Group/Defaults 三层），排查困难 | 参数透明，易于定位问题 |
| **代码可读性** | 依赖框架隐式行为 | 显式声明，一目了然 |


## 参数传递陷阱与解决

### 1. `auth_no_password` 不兼容

尝试使用 `auth_no_password=True` 跳过认证，但报错：
```
BaseConnection.__init__() got an unexpected keyword argument 'auth_no_password'
```

**原因**：该参数在较新版本 Netmiko 中才存在，当前环境不支持。

**解决**：
- 使用 `username=''` 和 `password=''` 显式传递空字符串。
- 若 `hp_comware_telnet` 仍尝试登录，**自动回退到 `generic_telnet`**（不执行认证交互）。

```python
try:
    net_connect = ConnectHandler(
        device_type='hp_comware_telnet',
        host=oob_hostname,
        port=oob_port,
        timeout=60,
        username='',
        password='',
    )
except Exception:
    net_connect = ConnectHandler(
        device_type='generic_telnet',
        host=oob_hostname,
        port=oob_port,
        timeout=60,
    )
```

### 2. ZTP 中断（Ctrl+C + 回车）

- 首次 Console 连接时，设备处于 ZTP 模式，不接受 CLI。
- 必须发送两次 `Ctrl+C`（`\x03`），再发送回车，才能进入用户视图。
- 使用 `write_channel` 发送（不等待回显），配合 `time.sleep` 保证设备响应。

### 3. `read_until_prompt()` 超时

- Console 连接的回显不稳定，`read_until_prompt()` 可能因提示符不匹配而超时。
- 采用 `write_channel` + `time.sleep` 发送命令，不等待逐条回显。


## 命令重复下发问题与解决

### 问题发现

在实际测试中，发现设备会话日志中存在命令重复执行的现象：

```text
<H3C-SR88-01>system-view           ← 第一次（由 Python 下发）
System View: return to User View with Ctrl+Z.
[H3C-SR88-01]system-view           ← 第二次（由模板渲染）
             ^
 % Wrong parameter found at '^' position.
```

同样的问题也出现在 `end` 命令上。

### 根本原因

模板文件 `bootstrap.j2` 中包含了 `system-view` 和 `end`，而 Python 代码又在外部显式发送了这两条命令。

**重复执行的命令清单**：

| 命令 | 模板中是否存在 | Python 代码是否发送 | 结果 |
| :--- | :--- | :--- | :--- |
| `system-view` | ✅ 存在 | ✅ 发送 | 重复执行 |
| `end` | ✅ 存在 | ✅ 发送 | 重复执行 |

### 解决方案

**原则**：`system-view` 和 `end` 由 Python 代码统一管理，模板只包含**系统视图下执行的配置命令**。

#### 1. 修改 `bootstrap.j2` 模板（移除 `system-view` 和 `end`）

```jinja2
{# 注意：system-view 和 end 由 Python 代码统一发送，模板中不包含 #}

{# 直接从系统视图命令开始 #}
sysname {{ sysname }}
undo info-center enable

{% if mgmt_interface.startswith('Vlan-interface') %}
{{ macros.vlan_config(100, "Management") }}
{% endif %}

{{ macros.interface_config(mgmt_interface, mgmt_ip, mgmt_mask, "Management Interface") }}

...

{# end 由 Python 代码发送，此处不写 #}
```

#### 2. Python 代码负责进入和退出系统视图

```python
# Python 代码负责进入系统视图
net_connect.send_command("system-view", expect_string=r"\[.*?\]")

# 下发模板渲染后的命令（系统视图内执行）
for cmd in config_commands:
    net_connect.send_command(cmd, expect_string=r"\[.*?\]")

# Python 代码负责退出系统视图
net_connect.send_command("end", expect_string=r"<.*?>")
```


## 其他修正内容

### 1. `ssh server rekey-interval 0` 报错

```text
[H3C-SR88-01]ssh server rekey-interval 0
                                       ^
 % Wrong parameter found at '^' position.
```

**原因**：Comware V7 中 `ssh server rekey-interval` 命令参数范围不支持 `0`，或该命令在当前版本中不存在。

**解决**：从模板中移除该命令，或改为有效值（如 `ssh server rekey-interval 60`）。

### 2. `password hash` 报错

```text
[H3C-SR88-01-luser-manage-admin]password hash Ssh@Pass123
Invalid ciphertext password.
```

**原因**：`password hash` 命令需要**加密后的密文**，而非明文密码。

**解决**：修改 `templates/vendors/h3c/_macros.j2` 中的 `ssh_user` 宏：

```jinja2
{# 修改前 #}
password hash {{ password }}

{# 修改后 #}
password simple {{ password }}
```

### 3. 主动断连策略

**原则**：先让设备端主动断开，本端再立即关闭，不等待。

```python
# 设备端主动断开（使用 write_channel）
net_connect.write_channel("quit\r")
# 本端立即关闭，不等待（避免因设备端断开导致 socket 异常）
net_connect.disconnect()
```


## 速度优化：减少不必要的等待

### 问题发现

原始代码中使用了大量 `time.sleep(1)`，单台设备总耗时可达 25-35 秒。

**原始代码（慢）**：

```python
net_connect.write_channel("system-view\r")
time.sleep(1)                # 等待 1 秒

for cmd in config_commands:   # 假设 15 条命令
    net_connect.write_channel(cmd + "\r")
    time.sleep(1)             # 每条命令等待 1 秒，累计 15 秒

net_connect.write_channel("end\r")
time.sleep(1)                 # 再等 1 秒
```

### 根本原因

`write_channel` 只负责发送，不等待回显。为了确保命令执行完成，只能用 `time.sleep` 人为等待。但等待时间固定，效率低下。

### 解决方案：使用 `send_command` + `expect_string`

`send_command` 会**等待设备回显**（通过 `expect_string`），不需要固定的 `time.sleep`：

```python
# 优化前（慢）
net_connect.write_channel("system-view\r")
time.sleep(1)

# 优化后（快）
net_connect.send_command("system-view", expect_string=r"\[.*?\]")
# send_command 在设备返回提示符后立即返回，不会多等 1 秒
```

### 优化后的完整下发流程

```python
# 使用 send_command 下发（无需额外 sleep）
net_connect.send_command("system-view", expect_string=r"\[.*?\]")

for cmd in config_commands:
    net_connect.send_command(cmd, expect_string=r"\[.*?\]")

net_connect.send_command("end", expect_string=r"<.*?>")

# 设备端主动断开（使用 write_channel 发送 quit，不等待回显）
net_connect.write_channel("quit\r")
net_connect.disconnect()
```

### 性能对比

| 模式 | 单台设备耗时 | 3 台设备耗时（并发） |
| :--- | :--- | :--- |
| `write_channel` + `time.sleep(1)` | ~30 秒 | ~30 秒 |
| `send_command` + `expect_string` | ~5 秒 | ~5 秒 |


## Nornir 并发支持

### 当前函数能否并发？

**可以并发**。Nornir 的 `runner` 机制（默认 `threaded`）为每台设备分配独立的执行线程：
- 无全局锁或共享状态
- 每个 Task 持有独立的 `net_connect` 对象
- 设备间互不干扰

**前提条件**：`num_workers` 配置要大于 1。

```yaml
# config.yaml
runner:
  plugin: threaded
  options:
    num_workers: 10   # 同时并发 10 台设备
```

### 并发与 `time.sleep` 的关系

- Nornir 的 `threaded` runner 让多台设备**同时开始**执行
- 但每台设备内部的 `time.sleep()` 是**线程阻塞**的
- 优化前：3 台设备各花 30 秒，总耗时约 30 秒（并发让它们同时慢）
- 优化后：3 台设备各花 5 秒，总耗时约 5 秒（真正的提速）


## 核心代码片段

### 完整 `task_console_deploy` 函数（最终优化版）

```python
def task_console_deploy(task: Task, scene_api: SceneAPI) -> Result:
    """通过 Console 映射 Telnet 下发配置（最终优化版）"""
    host = task.host

    # ---- 1. 提取 OOB 连接参数 ----
    oob_conn = host.connection_options.get("netmiko_oob")
    if not oob_conn:
        return Result(
            host=host,
            failed=True,
            exception=Exception(f"设备 {host.name} 缺少 netmiko_oob 连接配置")
        )
    oob_hostname = oob_conn.hostname
    oob_port = oob_conn.port

    # ---- 2. 连接设备 ----
    try:
        net_connect = ConnectHandler(
            device_type='hp_comware_telnet',
            host=oob_hostname,
            port=oob_port,
            timeout=60,
            username='',
            password='',
        )
    except Exception:
        try:
            net_connect = ConnectHandler(
                device_type='generic_telnet',
                host=oob_hostname,
                port=oob_port,
                timeout=60,
            )
        except Exception as e2:
            return Result(host=host, failed=True, exception=Exception(f"连接失败: {e2}"))

    # ---- 3. 唤醒 + ZTP 中断 ----
    net_connect.write_channel("\r")
    time.sleep(0.5)

    for _ in range(2):
        net_connect.write_channel("\x03")
        time.sleep(0.5)
    net_connect.write_channel("\r")
    time.sleep(1)

    try:
        output = net_connect.read_until_prompt()
        if "Press ENTER" in output:
            net_connect.write_channel("\r")
            time.sleep(0.5)
            output = net_connect.read_until_prompt()
    except Exception:
        pass

    # ---- 4. 渲染配置模板 ----
    config_text, error = scene_api.bootstrap(host)
    if error:
        net_connect.disconnect()
        return Result(host=host, failed=True, exception=Exception(error.get("detail", "渲染失败")))
    config_commands = [
        cmd.strip() for cmd in config_text.splitlines()
        if cmd.strip() and not cmd.strip().startswith("#")
    ]
    if not config_commands:
        net_connect.disconnect()
        return Result(host=host, result="SKIPPED: 无配置命令")

    # ---- 5. 下发配置 ----
    net_connect.send_command("system-view", expect_string=r"\[.*?\]")

    for cmd in config_commands:
        net_connect.send_command(cmd, expect_string=r"\[.*?\]")

    net_connect.send_command("end", expect_string=r"<.*?>")

    # ---- 6. 设备端主动断开，本端立即关闭 ----
    net_connect.write_channel("quit\r")
    net_connect.disconnect()

    print(f"✅ {host.name} Console 配置下发完成")
    return Result(host=host, result="OK")
```


## 依赖

- Python 3.8+
- Nornir 3.x
- Netmiko 4.x
- Jinja2 3.x
- PyYAML


## 常见问题

### Q1：连接失败 `Login failed`

**原因**：Console 映射 Telnet 不需要认证，但 Netmiko 默认尝试登录。

**解决**：显式传递 `username=''` 和 `password=''`，或回退到 `generic_telnet`。

### Q2：命令下发后无回显或超时

**原因**：`read_until_prompt()` 可能因提示符不匹配而超时。

**解决**：使用 `write_channel` + `time.sleep` 发送命令，不等待回显，最后统一读取或断开。

### Q3：`netmiko_oob` 配置不生效

**原因**：Nornir 参数优先级问题（见上文“连接选型”章节）。

**解决**：绕过 Nornir，直接使用原生 Netmiko。

### Q4：`system-view` 或 `end` 重复执行

**原因**：模板和 Python 代码中同时包含了这些命令。

**解决**：从模板中移除，由 Python 代码统一管理。

### Q5：`password hash` 报 `Invalid ciphertext password`

**原因**：`password hash` 需要密文，传入明文会报错。

**解决**：在 `_macros.j2` 中将 `password hash` 改为 `password simple`。

### Q6：下发速度很慢

**原因**：`write_channel` + `time.sleep(1)` 每条命令都等 1 秒。

**解决**：使用 `send_command` + `expect_string` 替代，单台设备耗时从 30 秒降到 5 秒。


## 扩展

- 如需支持 SSH 或 NETCONF，可参考相同模式：**在任务中直接使用相应库**（如 `netmiko` 或 `ncclient`），绕过 Nornir 连接管理。
- 模板继承链支持多厂商、多版本、多补丁，通过 `SceneAPI` 统一渲染。


## 维护者

[Your Name/Team]


*最后更新：2026-09-02*