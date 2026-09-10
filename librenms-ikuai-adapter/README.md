# LibreNMS iKuai Adapter

LibreNMS 官方 Docker 部署的两项增强改动：补全独立的 `snmptrapd` 容器，并适配 iKuai（爱快）设备的私有 MIB 识别与轮询采集。改动已在实验环境（`lab-librenms`，隔离网段 `172.31.0.0/24`）中验证生效。

## 解决的问题

LibreNMS 官方 `docker-compose` 示例默认不包含独立的 SNMP Trap 接收组件，且对非主流厂商（如国产路由设备 iKuai）没有内置的设备识别与 MIB 支持——这类设备会被识别为通用 `generic` 类型，只能采集到最基础的 Linux/UCD 风格指标，无法拿到设备真实的私有性能数据。本仓库把这两项改动沉淀为可复用的配置文件集合。

## 仓库内容

1. **`snmptrapd` 独立容器补全**——`docker-compose.yml` 里新增了 `snmptrapd` 服务定义，与 `librenms` 主容器、`dispatcher` 共享同一份 `/data` 挂载，能独立接收和处理 SNMP Trap 而不影响主容器的 Web/API 服务。
2. **iKuai 私有 MIB 轮询适配**——让 LibreNMS 能正确识别 iKuai 设备并按私有 MIB 采集轮询数据，包含设备识别定义文件（`os_detection` / `os_discovery`）、专属图标、私有 MIB 文件。

> 注意：本仓库**不包含** SNMP Trap 场景下的 iKuai 专属 Handler（CPU/内存/温度数值化处理）。那部分改动在测试环境中通过 `docker cp` 写入容器内部、未做持久化，容器重建后已丢失，不在本次同步范围内。当前仓库内容只保证：iKuai 设备能被正确识别 + 轮询数据正常；iKuai 本身不发送 Trap，因此 `snmptrapd` 未挂载 iKuai 的图标/识别定义文件不影响功能。

## 目录结构

```
.
├── docker-compose.yml               # 完整 compose 定义（db/redis/librenms/snmptrapd/dispatcher）
├── .env.example                     # 环境变量示例（已脱敏，需复制为 .env 并填入真实值）
├── librenms.env                     # LibreNMS 可选环境变量（邮件通知等，按需补充）
├── custom-icons/
│   └── ikuai.png                    # iKuai 设备图标
├── custom-os-definitions/
│   ├── os_detection/ikuai.yaml      # iKuai 设备识别定义（discovery 阶段用）
│   └── os_discovery/ikuai.yaml      # iKuai 设备发现定义
└── librenms/mibs/ikuai/
    ├── private-ikuai.mib            # iKuai 私有 MIB 主文件
    └── IKUAI-AP-MIB.mib             # iKuai AP 相关 MIB
```

## 在其他环境应用步骤

1. 克隆本仓库到目标环境的 LibreNMS 部署目录（与现有 `docker-compose.yml` 同级，或按需合并）。
2. 复制 `.env.example` 为 `.env`，填入你自己环境的真实数据库密码、SNMP community、端口等。**不要直接复用本仓库 `.env.example` 里的占位值。**
3. 如果目标环境的 `docker-compose.yml` 已经存在且与官方部署不同，**不要直接覆盖**，而是参考本仓库 `docker-compose.yml` 中 `snmptrapd` 服务定义和三个容器（`librenms`/`dispatcher`/`snmptrapd`）里跟 `ikuai` 相关的 `volumes` 条目，手动合并到目标环境现有文件中。
4. 确认目标环境 `SNMP_EXTRA_MIB_DIRS` 变量包含 `/data/mibs/ikuai`（如果还有其他厂商 MIB，用 `:` 追加，不要覆盖已有厂商路径）。
5. 执行 `docker compose up -d` 使配置生效，之后触发一次 discovery，确认设备能被正确识别为 iKuai 类型、图标正常显示、轮询数据正常入库。

## 持久化机制说明（重要）

- `librenms`、`dispatcher` 两个容器都需要挂载 iKuai 的图标和识别定义文件，**`snmptrapd` 容器不需要**（`snmptrapd` 进程本身不判断设备 OS 类型、不渲染图标）。这是有意为之的差异，不是遗漏。
- 图标和识别定义文件必须用**独立的 bind mount 精确挂到镜像内部具体路径**（如 `./custom-icons/ikuai.png:/opt/librenms/html/images/os/ikuai.png:ro`），直接 `docker cp` 写入容器内部文件系统不会持久化，容器重建或镜像更新后会丢失。
- 私有 MIB 文件走的是独立 `volumes` 挂载（`./librenms/mibs/ikuai:/opt/librenms/mibs/ikuai:ro`），同时需要配合 `SNMP_EXTRA_MIB_DIRS` 环境变量让 `snmptrapd` 进程的 `-M` 参数包含该路径。

## 验证方式（应用后建议执行）

```bash
# 确认容器都挂载了预期文件
for c in librenms dispatcher; do
  docker exec lab-librenms-$c ls /opt/librenms/resources/definitions/os_detection/ikuai.yaml
done

# 确认 iKuai 设备 discovery 后被正确识别（os 字段应为 ikuai 而非 generic）
# 登录 LibreNMS UI 或查询数据库 devices 表 os 字段确认
```

## 相关文档

排障过程中遇到的具体问题、根因与解决方案，见 [`librenms-troubleshooting-faq`](../librenms-troubleshooting-faq)；SNMP Trap 接收链路与 Housekeeping 机制的原理性说明，见 [`librenms-snmptrap-housekeeping-sop`](../librenms-snmptrap-housekeeping-sop)。

## 免责声明

本项目内容来自实验环境的真实验证结果，应用到生产环境前建议先在低风险设备上验证一次 discovery + poll 流程。仓库内所有凭据均已替换为占位值，`.env.example` 中的值不可直接使用。
