#!/usr/bin/env bash
# stock-monitor 股票监控任务一键部署脚本
# 用法: bash deploy.sh              正式部署（全新创建）
#       REPLACE=1 bash deploy.sh    更新模式：创建前自动删除同名旧任务
#       DRY_RUN=1 bash deploy.sh    干跑模式，只打印将执行的命令，不实际调用 hermes
# 说明: 若 tasks/_common/disclaimer.md 存在，会自动追加到每个 prompt 末尾
set -euo pipefail

# 强制 Python IO 走 UTF-8：服务器 locale 常为 C/POSIX，否则中文 argv/输出会解码失败
export PYTHONUTF8=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config/schedule.json"

# ---------- 依赖检查 ----------
if [[ "${DRY_RUN:-0}" != "1" ]]; then
  command -v hermes >/dev/null 2>&1 || { echo "❌ 错误：未找到 hermes 命令，请先安装/登录 hermes CLI"; exit 1; }
fi
command -v python3 >/dev/null 2>&1 || { echo "❌ 错误：未找到 python3，deploy 脚本依赖 python3 解析 JSON"; exit 1; }
[[ -f "$CONFIG" ]] || { echo "❌ 错误：配置文件不存在 $CONFIG"; exit 1; }

# 若你的 hermes 版本删除任务需要 --name 参数，执行 HERMES_DELETE_ARGS=--name bash deploy.sh
DELETE_ARGS="${HERMES_DELETE_ARGS:-}"

# 用 python3 解析 JSON 并调用 hermes：prompt 中含单引号/双引号/emoji，
# 经 subprocess 列表传参可完全绕开 shell 转义问题
python3 - "$CONFIG" "$SCRIPT_DIR" "${DRY_RUN:-0}" "${REPLACE:-0}" "$DELETE_ARGS" <<'PYEOF'
import json, os, subprocess, sys

config_path, base_dir = sys.argv[1], sys.argv[2]
dry_run, replace, delete_args = sys.argv[3] == "1", sys.argv[4] == "1", sys.argv[5]

try:
    with open(config_path, encoding="utf-8") as f:
        tasks = json.load(f)
except json.JSONDecodeError as e:
    print(f"❌ schedule.json 解析失败: {e}")
    sys.exit(1)

# 公共免责声明（可选）：存在则自动追加到每个 prompt 末尾
disclaimer = ""
disclaimer_path = os.path.join(base_dir, "tasks", "_common", "disclaimer.md")
if os.path.isfile(disclaimer_path):
    with open(disclaimer_path, encoding="utf-8") as f:
        disclaimer = f.read().strip()

print(f"📋 从 schedule.json 读取到 {len(tasks)} 个任务"
      + ("，REPLACE 更新模式" if replace else "") + "\n")

# 重名检测：REPLACE 模式下同名任务会互相覆盖（后者删掉前者刚建的），必须前置拦截
names = [t["name"] for t in tasks]
dup = sorted({n for n in names if names.count(n) > 1})
if dup:
    print(f"❌ schedule.json 存在重名任务：{'、'.join(dup)}")
    print("   同名任务在 REPLACE 模式下会互相覆盖，请修正配置后重试")
    sys.exit(1)

failed, done = [], []
for i, task in enumerate(tasks, 1):
    name = task["name"]
    prompt_path = os.path.join(base_dir, task["prompt_file"])

    if not os.path.isfile(prompt_path):
        print(f"[{i}/{len(tasks)}] ✗ {name}: prompt 文件不存在 {task['prompt_file']}")
        failed.append(name)
        continue
    with open(prompt_path, encoding="utf-8") as f:
        prompt = f.read().strip()
    if not prompt:
        print(f"[{i}/{len(tasks)}] ✗ {name}: prompt 文件为空 {task['prompt_file']}")
        failed.append(name)
        continue
    if disclaimer:
        prompt = prompt + "\n\n" + disclaimer

    cmd = ["hermes", "cron", "create", task["cron"], "--prompt", prompt, "--name", name]
    if task.get("deliver"):
        cmd += ["--deliver", task["deliver"]]
    if task.get("context_from"):
        cmd += ["--context_from", task["context_from"]]

    print(f"[{i}/{len(tasks)}] {'DRY-RUN' if dry_run else '部署'}: {name}  ({task['cron']})")
    if dry_run:
        extra = ""
        if task.get("deliver"):
            extra += f" --deliver {task['deliver']}"
        if task.get("context_from"):
            extra += f" --context_from \"{task['context_from']}\""
        if replace:
            for old in [name] + task.get("replaces", []):
                del_preview = " ".join(["hermes", "cron", "delete"] + ([delete_args] if delete_args else []) + [old])
                print(f"    将先执行: {del_preview}")
        print(f"    将执行: hermes cron create \"{task['cron']}\" --prompt <{len(prompt)}字"
              + ("（含公共声明）" if disclaimer else "") + f"> --name \"{name}\"" + extra)
        done.append(name)
        continue

    # REPLACE 模式：先删同名旧任务及 replaces 声明的历史任务名；删除失败不阻断（旧任务可能本来就不存在）
    if replace:
        old_names = [name] + task.get("replaces", [])
        for old in old_names:
            del_cmd = ["hermes", "cron", "delete"] + ([delete_args] if delete_args else []) + [old]
            r = subprocess.run(del_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode == 0:
                print(f"    ↻ 已删除旧任务: {old}")
            else:
                err = (r.stderr.strip() or r.stdout.strip() or "")
                if err:
                    print(f"    ⚠️ 删除旧任务 {old} 失败（若为任务不存在可忽略）: {err}")
                else:
                    print(f"    ↻ 无同名旧任务 {old}，直接创建")

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode == 0:
        print(f"    ✓ 创建成功")
        done.append(name)
    else:
        err = (result.stderr.strip() or result.stdout.strip() or "未知错误")
        print(f"    ✗ 创建失败: {err}")
        failed.append(name)

print()
if failed:
    print(f"⚠️ 部署结束：成功 {len(done)} 个，失败 {len(failed)} 个（{'、'.join(failed)}）")
    sys.exit(1)
print(f"✅ 全部 {len(done)} 个任务部署成功" + ("（DRY_RUN 模式，未实际执行）" if dry_run else ""))
PYEOF
