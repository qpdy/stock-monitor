#!/usr/bin/env bash
# stock-monitor 配置一致性校验：README 任务表格 vs config/schedule.json
# 用法: bash scripts/validate.sh
# 返回: 0=一致，1=发现漂移（会在输出中标出差异）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$REPO_ROOT/config/schedule.json"
README="$REPO_ROOT/README.md"

command -v python3 >/dev/null 2>&1 || { echo "❌ 错误：未找到 python3"; exit 1; }
[[ -f "$CONFIG" ]] || { echo "❌ 错误：配置文件不存在 $CONFIG"; exit 1; }
[[ -f "$README" ]] || { echo "❌ 错误：README 不存在 $README"; exit 1; }

python3 - "$CONFIG" "$README" "$REPO_ROOT" <<'PYEOF'
import json, os, re, sys

config_path, readme_path, repo_root = sys.argv[1], sys.argv[2], sys.argv[3]

with open(config_path, encoding="utf-8") as f:
    tasks = json.load(f)
with open(readme_path, encoding="utf-8") as f:
    readme = f.read()

# 从 README 任务表格提取 (任务名, cron)——表格行格式: | 任务名 | `cron` | ... |
readme_rows = {}
in_table = False
for line in readme.splitlines():
    line = line.strip()
    if line.startswith("| 任务名"):
        in_table = True
        continue
    if in_table and line.startswith("|") and "`" in line:
        cells = [c.strip() for c in line.split("|")]
        # cells[0] 为空（行首|）, cells[1]=任务名, cells[2]=`cron`
        if len(cells) >= 3:
            name = cells[1]
            cron_m = re.search(r"`([^`]+)`", cells[2])
            if name and cron_m:
                readme_rows[name] = cron_m.group(1)
    elif in_table and not line.startswith("|"):
        in_table = False  # 当前表格结束，但 README 可能有多个任务表格（如股票任务表 + 系统任务表），继续扫描

config_rows = {t["name"]: t["cron"] for t in tasks}

errors = []

# 0. 空表防护：README 表格解析失败（表头改名/表格被删）时 in_table 恒为 False，
#    readme_rows 为空，绝不能走下去——否则全部任务会被误报成"README 表格缺失"
if not readme_rows:
    errors.append("README 中未解析到任务表格（表头必须为 '| 任务名 | ...' 且 cron 列带反引号），请检查 README.md 表格格式")

# 1. 配置里有但 README 表格没有的任务
else:
    for name in config_rows:
        if name not in readme_rows:
            errors.append(f"配置存在但 README 表格缺失: {name} (cron={config_rows[name]})")

# 2. README 表格有但配置里没有的任务
for name in readme_rows:
    if name not in config_rows:
        errors.append(f"README 表格存在但配置缺失: {name} (cron={readme_rows[name]})")

# 3. 同名但 cron 不一致
for name in config_rows:
    if name in readme_rows and config_rows[name] != readme_rows[name]:
        errors.append(f"cron 不一致: {name}  配置={config_rows[name]!r}  README={readme_rows[name]!r}")

# 4. [SILENT] 防护措辞静态校验：凡含 [SILENT] 的 prompt 必须同时具备
#    a) 纯词输出措辞 —— 系统按精确词识别 [SILENT]，删掉该约束会导致休市日误推送
#    b) 故障分支措辞 —— 接口故障/数据异常时严禁静默，删掉会导致监控失效且无法被发现
#    只能防"编辑 prompt 时误删防护分支"的回归；LLM 运行时是否真的只输出 [SILENT]
#    无法在 CI 中断言，不在此范围
silent_word_pat = re.compile(r"仅回复 \[SILENT\] 一个词|仅为 \[SILENT\] 这一个词")
silent_fault_pat = re.compile(r"严禁[^。；\n]{0,30}回复 \[SILENT]")
for t in tasks:
    prompt_path = os.path.join(repo_root, t["prompt_file"])
    if not os.path.isfile(prompt_path):
        errors.append(f"prompt 文件不存在: {t['name']}  {t['prompt_file']}")
        continue
    with open(prompt_path, encoding="utf-8") as f:
        prompt_text = f.read()
    # 5. 事件日历注入校验：任务配置 "event_calendar": true 时，prompt 同目录的
    #    event_calendar.md 必须存在且非空（部署时会注入该文件，缺失/为空会导致部署失败）
    if t.get("event_calendar"):
        cal_path = os.path.join(os.path.dirname(prompt_path), "event_calendar.md")
        if not os.path.isfile(cal_path):
            errors.append(f"event_calendar: true 但日历文件不存在: {t['name']}  {cal_path}")
        elif not open(cal_path, encoding="utf-8").read().strip():
            errors.append(f"事件日历文件为空: {t['name']}  {cal_path}")
    if "[SILENT]" not in prompt_text:
        continue
    if not silent_word_pat.search(prompt_text):
        errors.append(f"prompt 缺少 [SILENT] 纯词输出措辞: {t['name']}  {t['prompt_file']}"
                      "（应含 '仅回复 [SILENT] 一个词' 类表述）")
    if not silent_fault_pat.search(prompt_text):
        errors.append(f"prompt 缺少故障分支措辞: {t['name']}  {t['prompt_file']}"
                      "（应含 '严禁……回复 [SILENT]' 类表述，接口故障时严禁静默）")

if errors:
    print(f"❌ 发现 {len(errors)} 处 README 表格与 schedule.json 漂移：\n")
    for e in errors:
        print(f"  • {e}")
    print("\n请同步修改后重试（README.md 任务一览表  vs  config/schedule.json）")
    sys.exit(1)

n_silent = sum(
    1 for t in tasks
    if os.path.isfile(os.path.join(repo_root, t["prompt_file"]))
    and "[SILENT]" in open(os.path.join(repo_root, t["prompt_file"]), encoding="utf-8").read()
)
print(f"✅ README 任务表格与 schedule.json 一致（{len(config_rows)} 个任务 cron 全部匹配）")
print(f"✅ [SILENT] 防护措辞校验通过（{n_silent} 个含 [SILENT] 的 prompt 均带纯词输出与故障分支措辞）")
PYEOF
