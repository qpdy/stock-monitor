# stock-monitor — hermes 股票监控任务集

基于 hermes CLI 的股票全交易日流程监控：盘前情报 → 集合竞价 → 开盘 → 盘中异动 → 午间舆情 → 尾盘监控 → 收盘复盘 → 晚间风险预警，推送至微信（weixin）。

支持多只股票：每只股票一套 `tasks/<股票代码>/` prompt 目录 + 在 `schedule.json` 中登记该股票的任务。当前已配置 **000582 北部湾港**（9 个任务）。

## 目录结构

```
stock-monitor/
├── README.md                        # 本文件
├── deploy.sh                        # 一键部署：读取配置，批量执行 hermes cron create
├── undeploy.sh                      # 一键清理：批量执行 hermes cron delete
├── tasks/                           # prompt 纯文本，按股票代码分目录
│   ├── _common/
│   │   └── disclaimer.md            # 公共免责声明，部署时自动追加到每个 prompt 末尾
│   └── 000582/                      # 北部湾港
│       ├── pre_market.md            # 盘前情报      08:50
│       ├── auction_init.md          # 竞价初始      09:16
│       ├── auction_result.md        # 竞价结果      09:25
│       ├── opening_trade.md         # 开盘首笔      09:30
│       ├── intraday_monitor.md      # 盘中监控（整点过5分 + 14:50 尾盘共用）
│       ├── noon_sentiment.md        # 午间舆情      12:40
│       ├── closing_review.md        # 收盘复盘      18:00
│       └── evening_alert.md         # 晚间风险预警   21:30
└── config/
    └── schedule.json                # 所有任务的 cron / name / deliver / continuity 配置
```

## 已配置任务一览（000582 北部湾港）

| 任务名 | cron | 说明 | 上下文 |
|---|---|---|---|
| 北部湾港-盘前情报 | `50 8 * * 1,2,3,4,5` | 搜索整理隔夜利好/利空，平陆运河进展优先 | 自身上次输出（--continuity） |
| 北部湾港-竞价初始 | `16 9 * * 1,2,3,4,5` | 集合竞价初始虚拟价格 | — |
| 北部湾港-竞价结果 | `25 9 * * 1,2,3,4,5` | 竞价最终开盘价与量 | — |
| 北部湾港-开盘首笔 | `30 9 * * 1,2,3,4,5` | 连续竞价首笔成交 | — |
| 北部湾港-盘中监控 | `5 10,11,13,14 * * 1,2,3,4,5` | 涨跌幅超 ±5% 才推送，否则 [SILENT] | — |
| 北部湾港-午间舆情 | `40 12 * * 1,2,3,4,5` | 午间利空公告扫描，无则 [SILENT] | — |
| 北部湾港-盘中监控-尾盘 | `50 14 * * 1,2,3,4,5` | 尾盘异动监控，与整点共用同一 prompt | — |
| 北部湾港-收盘复盘 | `0 18 * * 1,2,3,4,5` | 收盘数据 + 涨跌归因 + 明日展望 | 自身上次输出（--continuity） |
| 北部湾港-晚间风险预警 | `30 21 * * 1,2,3,4,5` | 晚间利空公告扫描，无则 [SILENT] | — |

行情数据统一来自腾讯财经接口 `https://qt.gtimg.cn/q=sz000582`（`~` 分隔，fields[3]=现价、fields[4]=昨收、fields[32]=涨跌幅、fields[30]=快照时间戳（交易日判断依据）等）。

## 部署到服务器

本地开发机无需安装 hermes（用 DRY_RUN 校验即可），正式部署在服务器上执行。
代码通过 GitHub 同步（仓库已设公开，HTTPS 匿名可读，服务器无需配置任何凭证）：

```bash
# 0. 确认服务器时区为北京时间（cron 按服务器本地时间执行，UTC 时区会导致全天任务错位）
#    服务器上执行 date 查看，若非 CST/Asia/Shanghai：sudo timedatectl set-timezone Asia/Shanghai

# 1. 服务器上 clone（需已安装并登录 hermes）
git clone https://github.com/qpdy/stock-monitor.git ~/stock-monitor

# 2. 部署 + 复核
cd ~/stock-monitor && bash deploy.sh && hermes cron list
```

日后本地改完代码：先 `git push origin main`，再在服务器上 `cd ~/stock-monitor && git pull && REPLACE=1 bash deploy.sh` 更新（详见下方"修改 prompt 的流程"）。

> **本服务器 hermes 实测（2026-08-25）**：cron 任务创建用位置参数传 prompt（`hermes cron create "<cron>" "<prompt>" --name ...`），
> `--deliver weixin` 合法，删除命令为 `hermes cron remove <任务名|job_id>`（任务名与 job_id 均接受）。
> `deploy.sh` / `undeploy.sh` 已按此适配，不支持 `--context_from`（跨任务上下文），改用 `--continuity`（任务自身上次输出注入）。

## 部署步骤

```bash
bash deploy.sh              # 全新部署
REPLACE=1 bash deploy.sh    # 更新部署：自动删除同名旧任务后重建，可安全重复执行
```

脚本会：
1. 检查 `hermes`、`python3` 依赖；
2. 逐个读取 `config/schedule.json` 中的任务配置；
3. 把 `tasks/` 下对应 `.md` 文件全文作为 prompt（位置参数），连同 `--name`、`--deliver`、`--continuity` 执行 `hermes cron create`；
4. 结束时汇总成功/失败任务数，任一失败以非零码退出。

部署后复核：

```bash
hermes cron list    # 应能看到全部任务
```

## 清理步骤

```bash
bash undeploy.sh
hermes cron list    # 确认任务已删除
```

## 修改 prompt 的流程

1. 直接编辑 `tasks/<股票代码>/` 下对应 `.md` 文件（纯文本，无需关心 shell 转义）；公共免责声明统一改 `tasks/_common/disclaimer.md`，部署时自动追加到每个 prompt 末尾；
2. 本地提交并推送：`git add -A && git commit -m "说明改动" && git push origin main`
3. 服务器上拉取并更新部署：

```bash
cd ~/stock-monitor && git pull && REPLACE=1 bash deploy.sh   # 自动删除同名旧任务后重新创建，无重复风险
```

> hermes 的 cron create 是"新建"语义，直接重复执行 `bash deploy.sh` 可能产生重复任务；更新场景务必用 `REPLACE=1`，或先 `bash undeploy.sh` 清理。
> `REPLACE=1` 可安全重入：某次更新中途失败（部分任务被删后来不及建），直接重跑 `REPLACE=1 bash deploy.sh` 即可补齐，不会重复。

如需调整时间/任务名/推送渠道，改 `config/schedule.json` 对应字段后同样用 `REPLACE=1 bash deploy.sh` 更新。改 cron 时间后记得同步更新上方「已配置任务一览」表格，避免文档与配置漂移。

**任务改名**：若给任务改了名字（如 `北部湾港-盘中监控-整点` → `北部湾港-盘中监控`），在该任务配置中加 `"replaces": ["旧任务名"]`，部署和清理脚本会自动连带删除旧名任务，避免遗留重复任务。

## 添加新股票

以添加 600019 宝钢股份为例：

1. **建 prompt 目录**：新建 `tasks/600019/`，参照 `tasks/000582/` 写 9 个 `.md`（把股票代码、名称、腾讯接口代码改成 `sh600019`，重点关注题材按该股票调整；免责声明不用写，部署时自动追加）；
2. **登记任务**：往 `config/schedule.json` 数组追加该股票的任务，任务名带股票名前缀避免冲突，如 `"宝钢股份-盘前情报"`，`prompt_file` 指向 `tasks/600019/xxx.md`；
3. **部署**：

```bash
REPLACE=1 bash deploy.sh   # 全量更新：旧任务删了重建，新任务直接创建
```

> 注意：`deploy.sh` 部署的是 `schedule.json` 里的**全部**任务。如果想只加新股票不重动旧任务，可手动单独执行新任务的 `hermes cron create` 命令。

## 附：干跑模式（不实际调用 hermes）

```bash
DRY_RUN=1 bash deploy.sh     # 只打印将执行的命令
DRY_RUN=1 bash undeploy.sh
```

用于在未安装 hermes 的机器上校验配置与 prompt 文件是否齐全。

## 注意事项

- 删除任务用 `hermes cron remove <任务名|job_id>`（两者均可），`undeploy.sh` 已按此适配。
- 盘中监控/午间舆情/晚间风险预警为静默型任务：无异常时回复 `[SILENT]` 不推送，收不到消息属正常现象。
- 盘中监控刻意安排在每小时过 5 分而非整点触发：13:00 整恰逢午休结束，接口快照仍是上午收盘数据；整点也是行情接口的访问高峰。
- **交易日判断**：除晚间风险预警外的 8 个任务均内置非交易日防护——接口时间戳（fields[30]）非当日即回复 `[SILENT]`，**节假日/休市日收不到推送是设计行为**，不是故障。
- **[SILENT] 静默机制**：hermes 仅当任务全部输出恰好为 `[SILENT]` 一个词时才不推送。因此所有静默指令均要求"全部输出仅 [SILENT] 一个词"，免责声明也注明 [SILENT] 时不附加——输出中混入任何其他文字（分析过程、免责声明）都会导致整条被推送。
- 所有推送末尾均带固定免责声明，信息仅供参考，不构成投资建议。
