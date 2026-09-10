# Nornir Comware Automation

基于 Nornir + Netmiko + ncclient 的 H3C Comware V7（SR88 系列）网络设备全生命周期自动化框架：从设备零配置开局，到 SSH/NETCONF 双通道使能，再到接口 IP、OSPF 等业务配置的原子化下发与精确回退，构成一条完整的闭环。项目在真实实验拓扑（见 `实验topo.jpg`）上完成了三台 H3C SR88 设备的验证测试。

## 这个项目解决什么问题

传统网络配置自动化脚本常见的问题是：厂商差异、版本差异靠散落的 `if/else` 硬编码；模板和数据混在一起；配置下发失败后不知道怎么干净地撤回；不同协议（CLI/NETCONF）的下发逻辑各写一套、互不复用。本项目用四个层次的抽象来系统性地解决这些问题：

1. **分层继承的设备清单**（`inventory/`）：厂商 → 产线 → OS 版本 → 分支 → 热补丁，逐层收窄差异，避免每台设备重复写完整连接参数
2. **对称的模板继承树**（`templates/`）：模板目录结构与 Group 继承链一一对应，CLI（`cmd/`）与 NETCONF（`netconf/`）协议物理隔离，模板通过 `{% extends %}` 复用公共部分、只声明差异
3. **原子化的配置变更单元**（`atoms/`）：每个业务操作（配置IP、创建Loopback）封装为"幂等检查 → 下发 → 校验 → 回退"四阶段的 Atom 类，快照驱动回退，删除前做依赖检查
4. **场景化的执行入口**（`scenes/`、`scripts/`）：三个递进的开局/业务场景（Console开局 → SSH使能NETCONF → IP/OSPF业务配置），每个场景都支持预览（不下发）、下发、回退三种模式

## 快速定位：我该看哪份文档

项目里几乎每个子目录、每个脚本都配有独立的详细 README，本文件只做全局导航，具体设计细节请进入对应文档：

| 我想了解… | 去看 |
| :--- | :--- |
| 设备清单怎么分层组织、怎么新增一台设备/一个厂商 | [`inventory/README.md`](./inventory/README.md) |
| 模板目录结构、继承规则、宏怎么写 | [`templates/README.md`](./templates/README.md) |
| 原子操作（Atom）的生命周期设计、怎么新增一个场景的 Atom | [`atoms/README.md`](./atoms/README.md) |
| 模板渲染引擎（`PathResolver`/`SceneAPI`）具体怎么实现的 | [`scripts/preview_bootstrap_README.md`](./scripts/preview_bootstrap_README.md) |
| Group 继承合并算法（`deep_merge`/循环检测）细节 | [`scripts/build_inventory_README.md`](./scripts/build_inventory_README.md) |
| 阶段一：Console 零配置开局怎么做的 | [`scenes/bootstrap_step1_README.md`](./scenes/bootstrap_step1_README.md) |
| 阶段二：SSH 下发使能 NETCONF 怎么做的 | [`scenes/bootstrap_step2_README.md`](./scenes/bootstrap_step2_README.md) |
| 阶段三之一：接口 IP 下发与回退（CLI/NETCONF 双路线） | [`scenes/test_ip_deploy_full_README.md`](./scenes/test_ip_deploy_full_README.md) |
| 阶段三之二：OSPF 下发与回退、YANG 模型选型的完整调试过程 | [`scenes/test_ospf_deploy_full_README.md`](./scenes/test_ospf_deploy_full_README.md)，更详细的 42000 字过程记录见 [`scenes/summary.md`](./scenes/summary.md) |
| 架构设计中还没解决 / 需要讨论的开放问题 | [`need_to_discuss_prob.md`](./need_to_discuss_prob.md) |
| 新增厂商（华为/Cisco）需要同步改哪些地方，完整清单 | [`docs/vendor_onboarding_checklist.md`](./docs/vendor_onboarding_checklist.md) |

## 三阶段闭环总览

```
┌──────────────────────┐     ┌──────────────────────────┐     ┌─────────────────────────────────┐
│  Step 1: Console开局  │ --> │ Step 2: SSH使能NETCONF     │ --> │  Step 3: 业务配置下发/回退         │
│  bootstrap_step1.py   │     │  bootstrap_step2.py        │     │  test_ip_deploy_full.py         │
│                        │     │                            │     │  test_ospf_deploy_full.py       │
│  设备零配置状态         │     │  设备已有SSH业务IP          │     │  设备已支持SSH+NETCONF双通道       │
│  仅Console/OOB Telnet  │     │  通过SSH下发命令开启        │     │  IP: CLI创建接口+NETCONF配IP     │
│  可达，无认证           │     │  NETCONF服务(端口830)      │     │  OSPF: NETCONF merge下发,        │
│  绕过Nornir用原生       │     │  支持独立连接块处理         │     │  rollback-on-error保证原子性,    │
│  Netmiko中断ZTP         │     │  交互式确认命令             │     │  逐层删除+索引列规则实现精确回退   │
└──────────────────────┘     └──────────────────────────┘     └─────────────────────────────────┘
        │                              │                                    │
        ▼                              ▼                                    ▼
   渲染 cmd/bootstrap.j2        渲染 cmd/netconf_cmd.j2          渲染 cmd/ip_address_cmd.j2 /
   （模板路径通过 H3C 补丁                                        netconf/_fragments/*.j2 /
    路径解析器按继承链查找）                                        netconf/ospf_xml.j2
```

每个阶段都依赖同一套底层设施：`scripts/build_inventory.py` 合并生成的 `inventory/groups.yaml`、`scripts/preview_bootstrap.py` 提供的模板渲染引擎。Step 3 的两个脚本额外依赖 `atoms/utils.py` 的 NETCONF 会话与 ifindex 查询工具，`test_ospf_deploy_full.py` 还直接复用 `test_ip_deploy_full.py` 的设备 IP 规划表（`DEVICE_CONFIG`）。

## 目录结构

```
.
├── config.yaml                      # Nornir 主配置（inventory 插件、并发 worker 数）
├── inventory/                        # 设备清单（分层继承，见 inventory/README.md）
│   ├── hosts.yaml                   # 设备清单（含真实实验拓扑的连接信息）
│   ├── groups.yaml                  # 生成产物，由 build_inventory.py 输出，不手工维护
│   ├── defaults.yaml                # 全局默认连接参数
│   └── groups/                      # 分层 Group 定义源文件（vendors/product_lines/os_versions）
├── templates/                        # Jinja2 模板（对称继承树，见 templates/README.md）
│   ├── _base/ vendors/ product_lines/ os_versions/   # 五层继承结构
│   └── README.md
├── atoms/                            # 原子化配置变更单元（见 atoms/README.md）
│   ├── base.py                      # Atom / CmdAtom / NetconfAtom 抽象基类
│   ├── utils.py                     # NETCONF 会话、ifindex/IP 查询工具
│   └── h3c/ huawei/ cisco/          # 按厂商分（当前仅 h3c 有实现）
├── scripts/                          # 核心引擎（继承合并 + 模板渲染）
│   ├── build_inventory.py           # Group 继承展开引擎
│   └── preview_bootstrap.py         # 模板渲染引擎 + CLI 预览工具
├── scenes/                            # 三阶段场景脚本（各自可独立执行）
│   ├── bootstrap_step1.py           # 阶段一：Console 开局
│   ├── bootstrap_step2.py           # 阶段二：SSH 使能 NETCONF
│   ├── test_ip_deploy_full.py       # 阶段三：接口 IP 下发/回退
│   ├── test_ospf_deploy_full.py     # 阶段三：OSPF 下发/回退
│   └── summary.md                   # OSPF 开发过程的完整技术总结（约42000字）
├── logs/                             # 运行时产物：ifindex XML、配置快照 JSON（.gitignore 排除）
├── verify_inheritance.py             # 手动调试小工具：打印展开后的 Group 完整配置
├── need_to_discuss_prob.md           # 开放问题记录
└── 实验topo.jpg                       # 真实验证拓扑图
```

## 环境要求与安装

```bash
pip install nornir nornir-netmiko netmiko ncclient jinja2 lxml pyyaml
```

- Python 3.8+
- 目标设备：H3C Comware V7（当前验证过 SR88 系列，`R7171` 分支）
- 设备需支持 SSH（CLI 下发）与 NETCONF（端口 830，H3C 私有 YANG 模型）
- 部分场景（`bootstrap_step1.py`）需要 Console 转 Telnet 的终端服务器（如 iTerm/opengear 等），配置见 `inventory/hosts.yaml` 中 `netmiko_oob` 连接选项

## 快速开始

```bash
# 1. 构建 inventory（合并分层 Group 定义）——多数脚本会自动执行这一步，手动执行仅用于调试
python scripts/build_inventory.py

# 2. 预览任意场景会渲染出什么配置，不实际连接设备
python scripts/preview_bootstrap.py --scene ssh_bootstrap H3C-SR88-01

# 3. 按顺序跑通三阶段闭环（需要真实设备或模拟环境）
python scenes/bootstrap_step1.py console H3C-SR88-01
python scenes/bootstrap_step2.py deploy H3C-SR88-01
python scenes/test_ip_deploy_full.py test-netconf H3C-SR88-01
python scenes/test_ospf_deploy_full.py test-ospf H3C-SR88-01

# 4. 验证 Group 继承展开是否符合预期（调试用）
python verify_inheritance.py
```

## 已知的设计取舍与局限（如实记录，非隐藏问题）

项目里每份子文档都尽量如实记录了代码当前的真实状态，而不是理想化描述。这里汇总几处贯穿全局、值得在读代码前就了解的取舍：

- **`atoms/` 的四阶段抽象目前只有 H3C 的接口 IP/Loopback 场景完整落地**，OSPF 场景（`test_ospf_deploy_full.py`）是直接在场景脚本里手写下发/回退逻辑，没有走 `Atom` 范式，两种风格在项目中并存（原因与取舍见 `scenes/test_ospf_deploy_full_README.md` 末节）
- **`atoms/h3c/netconf/interface_loopback.py` 的生命周期实现不完整**，方法签名与 `Atom` 抽象基类不一致，当前无法通过 `execute()` 统一调度（见 `atoms/README.md` 中的一览表）
- **`huawei/`、`cisco/` 分层目录已预留但无实现**，`CiscoPatchExtractor`/`DefaultPatchExtractor` 已写好但目前没有对应的 `PathResolver` 被实际使用
- **CLI 与 NETCONF 在同一场景内混用**（如 Loopback 接口本身用 CLI 创建、IP 地址用 NETCONF 配置），这是在真实设备调试中确认的工程取舍，不是设计洁癖上的"纯 NETCONF"方案（详见 `scenes/test_ip_deploy_full_README.md`"关键设计"章节）
- **回退的原子性边界是单设备单次 RPC**，不是跨设备、跨多次调用的分布式事务——`rollback-on-error` 无法保证"三台设备中两台成功一台失败"时自动回滚已成功的两台（详见 `scenes/summary.md` 第八章）

## 后续规划方向

见 `need_to_discuss_prob.md` 与各子文档"常见问题"章节中提到的扩展点，主要包括：厂商回退逻辑的健壮性增强（continue-on-error 场景下的精细控制）、NETCONF 场景更多借助 netconf-browser 做探索性验证后再固化为原子模板、多设备变更的应用层两阶段提交、`pre_validate_groups` 从空实现补充为真实的结构校验。
