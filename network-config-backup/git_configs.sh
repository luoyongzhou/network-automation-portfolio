#!/bin/bash
# git_configs.sh - 网络设备配置 Git 全量备份
# 用法: ./git_configs.sh [--manual "变更说明"]
#
# 将 collect_configs.py 每日采集到本地的设备配置目录，定期整体提交到私有 Git 仓库，
# 形成可追溯的配置变更历史。建议配合 crontab 按固定周期（如每 28 天）触发一次全量备份。

set -euo pipefail

# ---- 以下参数均通过环境变量注入，避免在脚本中硬编码敏感信息 ----
CONFIGS_DIR="${CONFIG_BACKUP_DIR:-/home/network_device_configs}"
REPO_URL="${GIT_BACKUP_REPO_URL:?请设置 GIT_BACKUP_REPO_URL 环境变量，例如 https://user:token@git.example.com/ops/netdevice.git}"
WORK_DIR="${CONFIGS_DIR}/.git_work"
LOG="$CONFIGS_DIR/git_backup.log"
LOCK="/tmp/git_configs.lock"
RETENTION_DAYS="${GIT_BACKUP_RETENTION_DAYS:-90}"
GIT_USER_NAME="${GIT_BACKUP_USER_NAME:-netdevice-backup}"
GIT_USER_EMAIL="${GIT_BACKUP_USER_EMAIL:-netdevice-backup@example.com}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# 防止并发执行导致仓库状态错乱
exec 200>"$LOCK"
flock -n 200 || { log "ERROR: 另一个 git 备份正在运行"; exit 1; }

# 初始化本地工作副本
if [ ! -d "$WORK_DIR/.git" ]; then
    log "初始化: clone repo"
    rm -rf "$WORK_DIR"
    git clone "$REPO_URL" "$WORK_DIR" 2>>"$LOG" || {
        log "Clone 失败，初始化空 repo"
        mkdir -p "$WORK_DIR" && cd "$WORK_DIR"
        git init && git checkout -b main
        git remote add origin "$REPO_URL"
    }
fi

cd "$WORK_DIR"
git config user.name "$GIT_USER_NAME"
git config user.email "$GIT_USER_EMAIL"

# 拉取远端最新，避免与其他来源的提交冲突
git pull origin main --rebase 2>>"$LOG" || true

# 同步配置文件（仅同步按日期命名的目录，及厂商专用目录）
log "同步配置文件..."
rsync -a \
    --exclude='.git/' \
    --include='20*/' --include='20*/**' \
    --exclude='*' \
    "$CONFIGS_DIR/" "$WORK_DIR/" 2>>"$LOG"

# 清理超过保留期的历史目录，控制仓库体积
CUTOFF=$(date -d "${RETENTION_DAYS} days ago" '+%Y-%m-%d')
for d in "$WORK_DIR"/20*/; do
    [ -d "$d" ] || continue
    if [[ "$(basename "$d")" < "$CUTOFF" ]]; then
        log "清理过期: $(basename "$d")"
        rm -rf "$d"
    fi
done

# 检查是否有变更
git add -A
if git diff --cached --quiet 2>/dev/null; then
    log "无变更，跳过"
    exit 0
fi

# 组装 commit message
if [ "${1:-}" = "--manual" ]; then
    MSG="手工备份: ${2:-变更后手工触发} ($(date '+%Y-%m-%d %H:%M'))"
else
    MSG="定时备份: $(date '+%Y-%m-%d') 全量配置"
fi

DEVICE_COUNT=$(find "$WORK_DIR"/20*/ -name "*.txt" 2>/dev/null | wc -l)
LATEST_DATE=$(ls -d "$WORK_DIR"/20*/ 2>/dev/null | xargs -I{} basename {} | sort | tail -1)

git commit -m "$MSG

最新配置日期: $LATEST_DATE
设备配置文件数: $DEVICE_COUNT" 2>>"$LOG"

log "推送到远端仓库..."
if git push -u origin main 2>>"$LOG"; then
    log "成功: $MSG ($DEVICE_COUNT 个配置文件)"
else
    log "ERROR: push 失败，本地 commit 已保留"
    exit 1
fi
