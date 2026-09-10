#!/bin/bash
# vSphere IP验证系统 - Cron执行脚本
# 建议每天执行1-2次（示例：8:00 和 16:00）

# 项目根目录（请根据实际部署路径修改）
PROJECT_DIR="/opt/vsphere-ip-validator"

cd "$PROJECT_DIR" || exit 1

# 激活虚拟环境
source venv/bin/activate

# 执行脚本（静默模式，仅输出到日志）
python validator.py > /dev/null 2>&1

# 清理超过1天的cron日志
find logs/vsphere_validator/ -name "cron_*.log" -mtime +1 -delete

# 清理超过1天的主日志备份（保留当前日志）
find logs/vsphere_validator/ -name "validator.log.*" -mtime +1 -delete

# 清理超过1天的vSphere数据CSV
find data/vsphere_validator/vsphere/ -name "*.csv" -mtime +1 -delete

# 退出虚拟环境
deactivate
