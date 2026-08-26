#!/usr/bin/env bash
# 服务器端自动更新：git 有新提交时才重新部署
# 供 crontab 调用（建议每天 05:10 凌晨检查，离 08:50 首个任务有 3 小时以上缓冲）：
#   10 5 * * * cd ~/stock-monitor && bash -l scripts/auto_update.sh >> deploy.log 2>&1
#（bash -l 不可省：crontab 默认 PATH 不含 hermes 所在的用户级目录，login shell 才能加载）
# 交易时段心跳（可选，弥补每天 05:10 才检测一次的空窗，hermes 挂掉最迟 1 小时被发现）：
#   35 8-14 * * 1-5 cd ~/stock-monitor && bash -l scripts/auto_update.sh --guard-only >> deploy.log 2>&1
# 设计要点：无新提交时直接跳过，不做无谓的 REPLACE 重建，
# 避免天天删建任务打断 --continuity（盘前情报/收盘复盘的上次输出注入）历史。
set -euo pipefail

cd "$(dirname "$0")/.."
LOG="deploy.log"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# ---- hermes 活性旁路检测 ----
# 为什么需要：所有监控任务（含"部署健康检查"）都是 hermes cron 任务，hermes 一死全部静默，
# 且微信端"收不到消息"与静默型任务的 [SILENT] 无法区分——本检测是 hermes 死亡时唯一能送出的告警。
# 告警走独立推送通道（Server酱/pushplus，任配其一，key 放 config/alert_key.sh，已 gitignore），
# 不依赖 hermes；未配置 key 时降级为仅写 deploy.log（至少留有排障痕迹）。
send_side_alert() {
  local msg="$1"
  # shellcheck disable=SC1091
  [ -f config/alert_key.sh ] && . config/alert_key.sh || true
  if [ -n "${SERVERCHAN_KEY:-}" ]; then
    curl -s -m 10 "https://sctapi.ftqq.com/${SERVERCHAN_KEY}.send" \
      --data-urlencode "title=${msg}" >/dev/null 2>&1 || true
  elif [ -n "${PUSHPLUS_TOKEN:-}" ]; then
    curl -s -m 10 -X POST "https://www.pushplus.plus/send" \
      -H 'Content-Type: application/json' \
      -d "{\"token\":\"${PUSHPLUS_TOKEN}\",\"title\":\"stock-monitor 告警\",\"content\":\"${msg}\"}" >/dev/null 2>&1 || true
  fi
}

hermes_guard() {
  # timeout 60 防 hermes 网络半开挂起（挂住 60s 视同失败）；无 timeout 命令的环境（如 macOS）退化为直接调用
  local guard_cmd="hermes cron list"
  command -v timeout >/dev/null 2>&1 && guard_cmd="timeout 60 hermes cron list"
  if $guard_cmd >/dev/null 2>&1; then
    return 0
  fi
  log "❌ hermes 活性检测失败：hermes cron list 不可用，全部 cron 任务（含健康检查）已停摆，请 SSH 检查 hermes 进程/登录态"
  send_side_alert "⚠️ stock-monitor：hermes 已停摆（cron list 调用失败），全部监控任务停止，请检查服务器。"
}

hermes_guard
# --guard-only：仅做活性检测，不动 git/部署，供交易时段高频心跳 crontab 调用
if [ "${1:-}" = "--guard-only" ]; then
  exit 0
fi

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

# 变更影响面检测：只有任务 prompt、配置、部署脚本变化才需要重建任务；
# 纯文档/脚本变更（README、validate.sh 等）重建会白白打断 --continuity 历史。
if git diff --quiet "$LOCAL" "$REMOTE" -- tasks/ config/ deploy.sh undeploy.sh; then
  log "检测到新提交 $LOCAL -> $REMOTE，但仅文档/校验脚本变更，跳过重建"
  if git pull --ff-only origin main >> "$LOG" 2>&1; then
    log "✅ 已同步代码（未重建任务）"
  else
    log "❌ git pull 失败（本地可能有分叉提交，或工作区有未提交修改），请人工处理：cd ~/stock-monitor && git status"
  fi
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
