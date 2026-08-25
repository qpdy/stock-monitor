#!/usr/bin/env bash
# stock-monitor 股票监控任务一键清理脚本
# 用法: bash undeploy.sh            正式清理
#       DRY_RUN=1 bash undeploy.sh  干跑模式，只打印将执行的命令
set -euo pipefail

# 强制 Python IO 走 UTF-8：服务器 locale 常为 C/POSIX，否则中文 argv/输出会解码失败
export PYTHONUTF8=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config/schedule.json"

# ---------- 依赖检查 ----------
if [[ "${DRY_RUN:-0}" != "1" ]]; then
  command -v hermes >/dev/null 2>&1 || { echo "❌ 错误：未找到 hermes 命令，请先安装/登录 hermes CLI"; exit 1; }
fi
command -v python3 >/dev/null 2>&1 || { echo "❌ 错误：未找到 python3，undeploy 脚本依赖 python3 解析 JSON"; exit 1; }
[[ -f "$CONFIG" ]] || { echo "❌ 错误：配置文件不存在 $CONFIG"; exit 1; }

python3 - "$CONFIG" "${DRY_RUN:-0}" <<'PYEOF'
import json, subprocess, sys

config_path, dry_run = sys.argv[1], sys.argv[2] == "1"

try:
    with open(config_path, encoding="utf-8") as f:
        tasks = json.load(f)
except json.JSONDecodeError as e:
    print(f"❌ schedule.json 解析失败: {e}")
    sys.exit(1)

print(f"📋 从 schedule.json 读取到 {len(tasks)} 个任务\n")

# 展开任务名及其 replaces 声明的历史任务名，去重保持顺序
all_names = []
for task in tasks:
    for n in [task["name"]] + task.get("replaces", []):
        if n not in all_names:
            all_names.append(n)

failed, done = [], []
for i, name in enumerate(all_names, 1):
    cmd = ["hermes", "cron", "remove", name]

    print(f"[{i}/{len(all_names)}] {'DRY-RUN' if dry_run else '删除'}: {name}")
    if dry_run:
        print(f"    将执行: {' '.join(cmd)}")
        done.append(name)
        continue

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        print(f"    ✗ 删除失败: hermes 调用超时（>300s），疑似网络/服务挂起")
        failed.append(name)
        continue
    if result.returncode == 0:
        print(f"    ✓ 删除成功")
        done.append(name)
    else:
        err = (result.stderr.strip() or result.stdout.strip() or "未知错误")
        print(f"    ✗ 删除失败: {err}")
        failed.append(name)

print()
if failed:
    print(f"⚠️ 清理结束：成功 {len(done)} 个，失败 {len(failed)} 个（{'、'.join(failed)}）")
    sys.exit(1)
print(f"✅ 全部 {len(done)} 个任务已删除" + ("（DRY_RUN 模式，未实际执行）" if dry_run else ""))
print("提示：可用 `hermes cron list` 复核清理结果")
PYEOF
