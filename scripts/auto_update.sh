#!/usr/bin/env bash
# 服务器端自动更新：git 有新提交时才重新部署
# 供 crontab 调用（建议每天 08:10，赶在 08:50 盘前情报前生效）：
#   10 8 * * * cd ~/stock-monitor && bash scripts/auto_update.sh
# 设计要点：无新提交时直接跳过，不做无谓的 REPLACE 重建，
# 避免天天删建任务打断 --continuity（盘前情报/收盘复盘的上次输出注入）历史。
set -euo pipefail

cd "$(dirname "$0")/.."
LOG="deploy.log"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  log "无新提交，跳过部署（HEAD=$LOCAL）"
  exit 0
fi

log "检测到新提交 $LOCAL -> $REMOTE，开始更新部署"
if git pull --ff-only origin main >> "$LOG" 2>&1; then
  if REPLACE=1 bash deploy.sh >> "$LOG" 2>&1; then
    log "✅ 自动部署完成"
  else
    log "❌ deploy.sh 执行失败，请人工介入：hermes cron list 复核，必要时手动 REPLACE=1 bash deploy.sh"
  fi
else
  log "❌ git pull 失败（本地可能有分叉提交），请人工处理"
fi
