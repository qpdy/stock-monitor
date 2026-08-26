#!/usr/bin/env bash
# stock-monitor 股票监控任务一键部署脚本
# 用法: bash deploy.sh                       正式部署（全新创建）
#       REPLACE=1 bash deploy.sh             更新模式：创建前自动删除同名旧任务
#       DRY_RUN=1 bash deploy.sh             干跑模式，只打印将执行的命令，不实际调用 hermes
#       bash deploy.sh --only <任务名前缀>   选择性部署：只处理任务名以该前缀开头的任务
#                                           （新增股票时不重动旧任务，保护其 --continuity 历史）
# 说明: 若 tasks/_common/disclaimer.md 存在，会自动追加到每个 prompt 末尾
#       （任务配置中 "disclaimer": false 可跳过追加，用于纯数据快照类任务）
#       任务配置 "event_calendar": true 时，prompt 同目录的 event_calendar.md
#       会注入到该任务 prompt 末尾（免责声明之前），事件节点单点维护、按任务 opt-in
set -euo pipefail

# 强制 Python IO 走 UTF-8：服务器 locale 常为 C/POSIX，否则中文 argv/输出会解码失败
export PYTHONUTF8=1

# ---------- 参数解析 ----------
ONLY_PREFIX=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)
      [[ $# -ge 2 && "$2" != -* ]] || { echo "❌ 错误：--only 需要一个任务名前缀参数，如 bash deploy.sh --only 宝钢股份"; exit 1; }
      ONLY_PREFIX="$2"
      shift 2
      ;;
    *)
      echo "❌ 错误：未知参数 $1（仅支持 --only <任务名前缀>）"
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config/schedule.json"

# ---------- 依赖检查 ----------
if [[ "${DRY_RUN:-0}" != "1" ]]; then
  command -v hermes >/dev/null 2>&1 || { echo "❌ 错误：未找到 hermes 命令，请先安装/登录 hermes CLI"; exit 1; }
fi
if [[ -n "${ONLY_PREFIX}" ]]; then
  echo "🔎 --only 模式：仅部署任务名以 '${ONLY_PREFIX}' 开头的任务，其余任务不动（保护其 --continuity 历史）"
fi
command -v python3 >/dev/null 2>&1 || { echo "❌ 错误：未找到 python3，deploy 脚本依赖 python3 解析 JSON"; exit 1; }
[[ -f "$CONFIG" ]] || { echo "❌ 错误：配置文件不存在 $CONFIG"; exit 1; }

# ---------- 时区防护 ----------
# cron 按服务器本地时间执行，时区错误会导致全天任务静默错位（最隐蔽的故障）。
# 用 python3 而非 bash 内建判断：含多字节字符的 if/echo 块在部分 bash 版本 + set -u 下
# 存在解析兼容问题（实测 TZ=UTC 时误报 unbound variable），整个检查+告警由 python 完成彻底绕开。
# 放在依赖检查之后：无 python3 的机器应先报真正的依赖缺失，而非先误报时区警告。
# 仅警告不阻断：DRY_RUN 本地校验、或确有非北京时区需求时不应被卡死。
python3 - <<'PYEOF'
import time
offset = time.strftime("%z")
if offset != "+0800":
    print(f"⚠️ 警告：当前系统时区偏移为 {offset}（非 +0800 北京时间）")
    print("   cron 按服务器本地时间触发，时区错误会导致全天任务执行时间错位")
    print("   如确认服务器应为北京时间，请执行：sudo timedatectl set-timezone Asia/Shanghai")
    print()
PYEOF

# ---------- 配置一致性校验 ----------
# 部署前校验 README 任务表格与 schedule.json 是否漂移，避免文档与配置脱节被推到服务器
# --only 选择性部署时跳过：此时允许只处理部分任务，不应被全量校验阻断
#（README 表格与配置漂移校验仍在 CI 与全量部署时强制）
if [[ -z "$ONLY_PREFIX" ]]; then
  bash "$SCRIPT_DIR/scripts/validate.sh"
else
  echo "ℹ️ --only 模式：跳过 README 表格与 schedule.json 全量一致性校验（CI 与全量部署仍会校验）"
fi

# 用 python3 解析 JSON 并调用 hermes：prompt 中含单引号/双引号/emoji，
# 经 subprocess 列表传参可完全绕开 shell 转义问题
python3 - "$CONFIG" "$SCRIPT_DIR" "${DRY_RUN:-0}" "${REPLACE:-0}" "$ONLY_PREFIX" <<'PYEOF'
import json, os, subprocess, sys

config_path, base_dir = sys.argv[1], sys.argv[2]
dry_run, replace = sys.argv[3] == "1", sys.argv[4] == "1"
only_prefix = sys.argv[5]

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

# --only 选择性部署：只保留任务名以指定前缀开头的任务（新增股票时不重动旧任务，保护其 continuity）
if only_prefix:
    selected = [t for t in tasks if t["name"].startswith(only_prefix)]
    if not selected:
        print(f"❌ --only 前缀 '{only_prefix}' 未匹配到任何任务")
        print("   现有任务名前缀：")
        for n in names:
            print(f"     - {n}")
        sys.exit(1)
    skipped = len(tasks) - len(selected)
    print(f"🔎 --only '{only_prefix}'：选中 {len(selected)} 个任务，跳过其余 {skipped} 个\n")
    tasks = selected

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
    # 事件日历（opt-in）：任务配置 "event_calendar": true 时注入 prompt 同目录的 event_calendar.md，
    # 注入位置在免责声明之前；文件缺失/为空按错误处理，防止静默部署出缺日历的 prompt
    calendar_text = ""
    if task.get("event_calendar"):
        calendar_path = os.path.join(os.path.dirname(prompt_path), "event_calendar.md")
        if not os.path.isfile(calendar_path):
            print(f"[{i}/{len(tasks)}] ✗ {name}: event_calendar: true 但事件日历不存在 {calendar_path}")
            failed.append(name)
            continue
        with open(calendar_path, encoding="utf-8") as f:
            calendar_text = f.read().strip()
        if not calendar_text:
            print(f"[{i}/{len(tasks)}] ✗ {name}: 事件日历文件为空 {calendar_path}")
            failed.append(name)
            continue
    # 默认追加公共免责声明；任务配置 "disclaimer": false 时跳过（纯数据快照任务信噪比考虑）
    use_disclaimer = disclaimer and task.get("disclaimer", True)
    if calendar_text:
        prompt = prompt + "\n\n" + calendar_text
    if use_disclaimer:
        prompt = prompt + "\n\n" + disclaimer

    # hermes cron create 用法: schedule [prompt] —— cron 表达式和 prompt 均为位置参数
    cmd = ["hermes", "cron", "create", task["cron"], prompt, "--name", name]
    if task.get("deliver"):
        cmd += ["--deliver", task["deliver"]]
    if task.get("continuity"):
        cmd += ["--continuity"]

    print(f"[{i}/{len(tasks)}] {'DRY-RUN' if dry_run else '部署'}: {name}  ({task['cron']})")
    if dry_run:
        extra = ""
        if task.get("deliver"):
            extra += f" --deliver {task['deliver']}"
        if task.get("continuity"):
            extra += " --continuity"
        if replace:
            for old in [name] + task.get("replaces", []):
                del_preview = " ".join(["hermes", "cron", "remove", old])
                print(f"    将先执行: {del_preview}")
        tags = ("（含事件日历）" if calendar_text else "") + ("（含公共声明）" if use_disclaimer else "")
        print(f"    将执行: hermes cron create \"{task['cron']}\" \"<{len(prompt)}字{tags}"
              + f" prompt>\" --name \"{name}\"" + extra)
        done.append(name)
        continue

    # REPLACE 模式：先删同名旧任务及 replaces 声明的历史任务名。
    # remove 结果分三类：成功 → 继续创建；非零且无错误输出 → 实测为“任务不存在”，继续创建；
    # 非零但有错误输出或超时 → 视为真失败（网络/服务异常），跳过该任务创建并计入 failed，
    # 否则旧任务残留 + 新任务创建成功会导致同名任务共存、重复推送，破坏 REPLACE 可重入承诺。
    if replace:
        old_names = [name] + task.get("replaces", [])
        remove_failed = False
        for old in old_names:
            del_cmd = ["hermes", "cron", "remove", old]
            try:
                r = subprocess.run(del_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            except subprocess.TimeoutExpired:
                print(f"    ✗ 删除旧任务 {old} 超时（>300s），疑似网络/服务挂起")
                remove_failed = True
                break
            if r.returncode == 0:
                print(f"    ↻ 已删除旧任务: {old}")
            else:
                err = (r.stderr.strip() or r.stdout.strip() or "")
                if err:
                    print(f"    ✗ 删除旧任务 {old} 失败: {err}")
                    remove_failed = True
                    break
                else:
                    print(f"    ↻ 无同名旧任务 {old}，直接创建")
        if remove_failed:
            print(f"    ✗ 跳过创建 {name}：旧任务删除未成功，避免同名任务共存导致重复推送")
            failed.append(name)
            continue

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        print(f"    ✗ 创建失败: hermes 调用超时（>300s），疑似网络/服务挂起")
        failed.append(name)
        continue
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
