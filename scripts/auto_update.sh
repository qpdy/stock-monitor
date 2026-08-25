#!/usr/bin/env bash
# 服务器端自动更新：git 有新提交时才重新部署
# 供 crontab 调用（建议每天 05:10 凌晨检查，离 08:50 首个任务有 3 小时以上缓冲）：
#   10 5 * * * cd ~/stock-monitor && bash -l scripts/auto_update.sh
#（bash -l 不可省：crontab 默认 PATH 不含 hermes 所在的用户级目录，login shell 才能加载）
# 设计要点：无新提交时直接跳过，不做无谓的 REPLACE 重建，
# 避免天天删建任务打断 --continuity（盘前情报/收盘复盘的上次输出注入）历史。
set -euo pipefail

cd "$(dirname "$0")/.."
LOG="deploy.log"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# fetch 失败（网络抖动/GitHub 抽风）不属需人工介入的故障：记录带 ❌ 的日志供检索，退出码置 0
# 避免 crontab 误判失败发报警邮件，下次运行自愈即可
if ! git fetch origin main 2>>"$LOG"; then
  log "❌ git fetch 失败（网络异常或 GitHub 不可达？），本次跳过，次日自动重试"
  exit 0
fi
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
  log "❌ git pull 失败（本地可能有分叉提交，或工作区有未提交修改），请人工处理：cd ~/stock-monitor && git status"
fi
