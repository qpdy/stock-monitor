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

python3 - "$CONFIG" "$README" <<'PYEOF'
import json, re, sys

config_path, readme_path = sys.argv[1], sys.argv[2]

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
        break  # 表格结束

config_rows = {t["name"]: t["cron"] for t in tasks}

errors = []

# 1. 配置里有但 README 表格没有的任务
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

if errors:
    print(f"❌ 发现 {len(errors)} 处 README 表格与 schedule.json 漂移：\n")
    for e in errors:
        print(f"  • {e}")
    print("\n请同步修改后重试（README.md 任务一览表  vs  config/schedule.json）")
    sys.exit(1)

print(f"✅ README 任务表格与 schedule.json 一致（{len(config_rows)} 个任务 cron 全部匹配）")
PYEOF
