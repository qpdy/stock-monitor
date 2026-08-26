# stock-monitor — hermes 股票监控任务集

基于 hermes CLI 的股票全交易日流程监控：盘前情报 → 集合竞价 → 开盘 → 盘中异动 → 午间舆情 → 尾盘监控 → 收盘复盘 → 晚间风险预警，推送至微信（weixin）。

支持多只股票：每只股票一套 `tasks/<股票代码>/` prompt 目录 + 在 `schedule.json` 中登记该股票的任务。当前已配置 **000582 北部湾港**（10 个任务）。

## 目录结构

```
stock-monitor/
├── README.md                        # 本文件
├── deploy.sh                        # 一键部署：读取配置，批量执行 hermes cron create
├── undeploy.sh                      # 一键清理：批量执行 hermes cron delete
├── scripts/
│   ├── auto_update.sh               # 服务器 crontab 自动更新：有新提交才重新部署
│   └── validate.sh                  # 配置一致性校验：README 任务表格 vs schedule.json（deploy.sh 部署前自动执行）
├── .github/
│   └── workflows/
│       └── validate.yml             # CI：push/PR 时自动跑 validate.sh + deploy/undeploy 干跑
├── tasks/                           # prompt 纯文本，按股票代码分目录
│   ├── _common/
│   │   └── disclaimer.md            # 公共免责声明，部署时自动追加（任务可配 "disclaimer": false 跳过）
│   ├── _system/
│   │   └── deploy_health_check.md   # 部署健康检查（监控系统自监控）   08:45
│   └── 000582/                      # 北部湾港
│       ├── pre_market.md            # 盘前情报      08:50
│       ├── auction_init.md          # 竞价初始      09:16
│       ├── auction_result.md        # 竞价结果      09:25
│       ├── opening_trade.md         # 开盘首笔      09:30
│       ├── intraday_monitor.md      # 盘中监控（9:42 早盘 + 整点过5分 + 14:50 尾盘共用）
│       ├── noon_sentiment.md        # 午间舆情      12:40
│       ├── closing_review.md        # 收盘复盘      18:00
│       ├── evening_alert.md         # 晚间风险预警   21:30
│       └── event_calendar.md        # 事件日历（平陆运河节点 + 确定性日历〔吞吐量/定期报告/除权/解禁〕，单点维护，按任务 opt-in 注入）
└── config/
    └── schedule.json                # 所有任务的 cron / name / deliver / continuity 配置
```

## 已配置任务一览（000582 北部湾港）

| 任务名 | cron | 说明 | 上下文 |
|---|---|---|---|
| 北部湾港-盘前情报 | `50 8 * * 1,2,3,4,5` | 搜索整理隔夜利好/利空，平陆运河进展优先 | 自身上次输出（--continuity） |
| 北部湾港-竞价初始 | `16 9 * * 1,2,3,4,5` | 集合竞价初始虚拟价格 | — |
| 北部湾港-竞价结果 | `25 9 * * 1,2,3,4,5` | 竞价最终开盘价、量与承接分型（含快照时效校验，读到撮合前虚拟价时 ⚠️ 标注） | — |
| 北部湾港-开盘首笔 | `30 9 * * 1,2,3,4,5` | 连续竞价首笔成交 | — |
| 北部湾港-盘中监控-早盘 | `42 9 * * 1,2,3,4,5` | 早盘定型窗口（9:30–10:00）定性采样，与整点共用同一 prompt | 自身上次输出（--continuity） |
| 北部湾港-盘中监控 | `5 10,11,13,14 * * 1,2,3,4,5` | 涨跌幅超 ±5% 绝对档、或超 ±3% 且量比>3 价量档才推送（早盘 9:30–10:00 时段量比阈值上调为 4，开盘初期量比天然偏高；按近20日日均振幅分层：波动中枢抬升时价量档降为一行简讯防刷屏）；板块β（招商港口+上港集团）与运河产业链β（五洲交通）对照，对照已动而个股未动出一行提示；同日同向无新变化去重；交易时段快照冻结超30分钟判疑似停牌推送 ⚠️ | 自身上次输出（--continuity，输出首行带日期时间戳防跨日误去重） |
| 北部湾港-午间舆情 | `40 12 * * 1,2,3,4,5` | 午间利空/利好公告扫描，均无则 [SILENT] | — |
| 北部湾港-盘中监控-尾盘 | `50 14 * * 1,2,3,4,5` | 尾盘异动监控，与整点共用同一 prompt | 自身上次输出（--continuity） |
| 北部湾港-收盘复盘 | `0 18 * * 1,2,3,4,5` | 收盘数据 + 涨跌归因 + 明日展望 | 自身上次输出（--continuity） |
| 北部湾港-晚间风险预警 | `30 21 * * 1,2,3,4,5` | 晚间利空/利好公告扫描 + 龙虎榜补查（仅上榜时提及，未上榜只字不提），均无则 [SILENT] | 自身上次输出（--continuity，链式去重防长假重复推送） |

**系统任务（与股票无关，每天运行含周末）**

| 任务名 | cron | 说明 | 上下文 |
|---|---|---|---|
| 部署健康检查 | `45 8 * * *` | 检查 deploy.log 当日 ❌ 记录、实际任务与 schedule.json 任务名逐名比对、auto_update 是否运行，异常推送 ⚠️，正常 [SILENT] | — |

行情数据统一来自腾讯财经接口 `https://qt.gtimg.cn/q=sz000582`（`~` 分隔，fields[3]=现价、fields[4]=昨收、fields[32]=涨跌幅、fields[30]=快照时间戳（交易日判断依据）等）；盘中监控为 `q=sz000582,sh000001,sz001872,sh600018,sh600368` 一次请求附带上证指数（大盘β）、招商港口+上港集团（港口板块β）、五洲交通（平陆运河产业链事件β——产业链异动而个股未动时提示"产业链已动、标的未跟"）作多重对照，收盘复盘为 `q=sz000582,sh000001`。盘中监控另调日K接口 `web.ifzq.gtimg.cn/appstock/app/fqkline/get`（近20日、剔除当日）自算日均振幅 A 作波动基准：±5% 为绝对档（任何波动环境出完整报告），±3% 且量比>3 的价量档在 A>2%（事件窗口波动中枢抬升）时降为一行放量简讯，K线接口失败降级 A=2 按常态处理。
腾讯接口异常时，竞价初始/竞价结果/开盘首笔/盘中监控四个任务内置东方财富 `push2.eastmoney.com` 应急备用源（切换后推送会标注 ⚠️；备用源无快照时间戳字段，交易日判断与停牌检测随之失效）。多标的请求（个股+大盘/对照股）按**行首代码字段**（如 `v_sz000582`）匹配各行标的，不按行序对应，防接口行序变化导致数据互换；所有行情任务均内置数据自洽校验（现价、昨收、涨跌幅须满足 (现价−昨收)/昨收×100≈涨跌幅，偏差 ≤0.2 个百分点），字段索引漂移/解析错位同样按接口故障输出 ⚠️。

## 部署到服务器

本地开发机无需安装 hermes（用 DRY_RUN 校验即可），正式部署在服务器上执行。
代码通过 GitHub 同步（仓库已设公开，HTTPS 匿名可读，服务器无需配置任何凭证）：

```bash
# 0. 确认服务器时区为北京时间（cron 按服务器本地时间执行，UTC 时区会导致全天任务错位；
#    deploy.sh 已内置时区防护：检测到非 +0800 时区会打警告提示，但不阻断部署）
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

1. 直接编辑 `tasks/<股票代码>/` 下对应 `.md` 文件（纯文本，无需关心 shell 转义）；公共免责声明统一改 `tasks/_common/disclaimer.md`，部署时自动追加到每个 prompt 末尾（纯数据快照类任务如竞价/开盘首笔，在配置中加 `"disclaimer": false` 可跳过，提高信噪比）。注意：改 `disclaimer.md` 等于动了所有任务的 prompt，下次部署会全量重建、打断所有 `--continuity` 历史，非必要不改。事件日历（如平陆运河节点）单点维护在 `tasks/<股票代码>/event_calendar.md`，任务配置 `"event_calendar": true` 才注入（opt-in，纯行情/快照任务不注入以保信噪比），改日历只需 `REPLACE=1 bash deploy.sh --only <股票名>` 重建该股票任务，不影响其他任务；
2. 本地提交并推送：`git add -A && git commit -m "说明改动" && git push origin main`
3. 服务器上拉取并更新部署：

```bash
cd ~/stock-monitor && git pull && REPLACE=1 bash deploy.sh   # 自动删除同名旧任务后重新创建，无重复风险
```

> hermes 的 cron create 是"新建"语义，直接重复执行 `bash deploy.sh` 可能产生重复任务；更新场景务必用 `REPLACE=1`，或先 `bash undeploy.sh` 清理。
> `REPLACE=1` 可安全重入：某次更新中途失败（部分任务被删后来不及建），直接重跑 `REPLACE=1 bash deploy.sh` 即可补齐，不会重复。

如需调整时间/任务名/推送渠道，改 `config/schedule.json` 对应字段后同样用 `REPLACE=1 bash deploy.sh` 更新。改 cron 时间后记得同步更新上方「已配置任务一览」表格，避免文档与配置漂移——`deploy.sh` 会在部署前自动执行 `scripts/validate.sh` 校验两者一致性，发现漂移会中断部署并标出差异。

**任务改名**：若给任务改了名字（如 `北部湾港-盘中监控-整点` → `北部湾港-盘中监控`），在该任务配置中加 `"replaces": ["旧任务名"]`，部署和清理脚本会自动连带删除旧名任务，避免遗留重复任务。

### 自动更新（可选）

服务器上配一次 crontab，之后每天 05:10（凌晨）自动检查 GitHub 是否有新提交，**有才执行** `git pull + REPLACE=1 bash deploy.sh`（无新提交时跳过，不会天天删建任务、打断 `--continuity` 历史）。提交未触及 `tasks/ config/ deploy.sh undeploy.sh` 时（如纯 README 改动），只同步代码不重建任务，进一步避免打断 continuity。选凌晨是为了离 08:50 首个任务留足 3 小时以上缓冲——即使部署失败，也有充足时间人工介入，当天任务最坏情况跑旧 prompt、无实际损害；且「部署健康检查」任务会在 08:45 主动推送部署失败告警：

```bash
crontab -e
# 添加一行（deploy.log 已在 .gitignore 中）：
# 10 5 * * * cd ~/stock-monitor && bash -l scripts/auto_update.sh >> deploy.log 2>&1
#（bash -l 不可省：crontab 默认 PATH 不含 hermes 所在的用户级目录，login shell 才能加载）
# 可选再加一行：交易时段每小时心跳，弥补每天 05:10 才检测一次的空窗（hermes 挂掉最迟 1 小时被发现）：
# 35 8-14 * * 1-5 cd ~/stock-monitor && bash -l scripts/auto_update.sh --guard-only >> deploy.log 2>&1
```

**hermes 活性旁路告警**：`auto_update.sh` 每次运行先做 hermes 活性检测——所有监控任务（含「部署健康检查」）都是 hermes cron 任务，hermes 一死全部静默，且微信端「收不到消息」与静默型任务的 [SILENT] 无法区分，此旁路是 hermes 死亡时唯一能送出的告警。检测失败（`hermes cron list` 不可用）时经**独立推送通道**告警（不依赖 hermes）：在服务器上创建 `config/alert_key.sh`（已在 .gitignore），写入 Server酱 或 pushplus 任一 key：

```bash
echo 'SERVERCHAN_KEY=SCTxxxxxxxx' > config/alert_key.sh    # Server酱（sctapi.ftqq.com）
# 或：echo 'PUSHPLUS_TOKEN=xxxxxxxx' > config/alert_key.sh  # pushplus（pushplus.plus）
```

未配置 key 时旁路降级为仅写 deploy.log ❌ 记录（留有排障痕迹，但 hermes 停摆不会主动送达通知）。

自动部署记录统一写在 `~/stock-monitor/deploy.log`，日志出现 ❌ 时登录服务器按提示人工处理。本地 push 完想立即生效等不及次日 05:10 的话，手动跑一次 `git pull && REPLACE=1 bash deploy.sh` 即可。

## 添加新股票

以添加 600019 宝钢股份为例：

1. **建 prompt 目录**：新建 `tasks/600019/`，参照 `tasks/000582/` 写 9 个 `.md`（把股票代码、名称、腾讯接口代码改成 `sh600019`，重点关注题材按该股票调整；免责声明不用写，部署时自动追加）；
2. **登记任务**：往 `config/schedule.json` 数组追加该股票的任务，任务名带股票名前缀避免冲突，如 `"宝钢股份-盘前情报"`，`prompt_file` 指向 `tasks/600019/xxx.md`；
3. **部署**：

```bash
bash deploy.sh --only 宝钢股份   # 只创建新股票任务，已有任务不受影响（推荐，保护旧任务 continuity）
# 或 REPLACE=1 bash deploy.sh   # 全量更新：旧任务删了重建，新任务直接创建
```

> 注意：不带 `--only` 时 `deploy.sh` 部署的是 `schedule.json` 里的**全部**任务。`--only` 前缀匹配未命中任何任务时会报错并列出现有任务名，防止静默零部署。

## 附：干跑模式（不实际调用 hermes）

```bash
DRY_RUN=1 bash deploy.sh     # 只打印将执行的命令
DRY_RUN=1 bash undeploy.sh
```

用于在未安装 hermes 的机器上校验配置与 prompt 文件是否齐全。

## 附：选择性部署（--only）

```bash
bash deploy.sh --only 北部湾港            # 只部署/更新名称以此前缀开头的任务
REPLACE=1 bash deploy.sh --only 北部湾港  # 配合 REPLACE 只更新该前缀任务，其余任务完全不动
```

用于新增股票或只改单只股票配置时不重动其他任务（`REPLACE=1` 全量重建会打断所有任务的 `--continuity` 历史）。`--only` 模式下会跳过 README 表格与 schedule.json 的全量一致性校验（CI 与全量部署仍会校验）。

## 注意事项

- 删除任务用 `hermes cron remove <任务名|job_id>`（两者均可），`undeploy.sh` 已按此适配。
- 盘中监控/午间舆情/晚间风险预警为静默型任务：无异常时回复 `[SILENT]` 不推送，收不到消息属正常现象。
- **盘中异动去重（链内）**：三个盘中监控任务均开启 `--continuity`。所有非 [SILENT] 输出的第一行为日期时间戳（取快照 fields[30] 换算）；注入上次输出先核对时间戳——**日期非今日一律视为无锚点、按首次触发处理**，防止周一/长假后首日因上一交易日同向异动报告而误静默，时间戳缺失无法判断时保守不去重（宁误推不漏推）。上次输出为今日异动报告/放量简讯/封板确认行且同向无新变化（涨跌幅变化 <1 个百分点、封板未开板、未反向穿越触发阈值）→ [SILENT]；上次输出为 [SILENT] 且当前已封板（现价==涨停/跌停价，fields[47]/[48]）但非一字板 → 只推一行封板确认——continuity 只注入上一次输出，[SILENT] 会丢失更早报告的锚点，此规则防隔小时链式重复推送；开板/回封/反向穿越为新变化，正常推送。停牌提醒与故障告警不受去重约束，每次必推。上次输出为 ⚠️ 告警类（停牌/故障/极端行情）时走兜底分支：告警持续则继续推送；告警解除（复牌/数据恢复）推一行'✅ 前次 ⚠️ 已解除，当前无异动'后回归正常判断。
- **跨任务去重（一字板特判）**：早盘/整点/尾盘三个盘中任务各自独立注入上次输出，互不可见，continuity 无法跨任务去重；一字封板日（最高=最低=现价 且 |涨跌幅|>5%）按「更早任务必然已首报」反推 → [SILENT]。两个豁免：早盘时段（9:30–10:00）不适用——今日尚无更早任务，本次即首报；尾盘时段（14:45 后）不适用——收盘前封板状态与次日开盘指向有独立价值，保留推送。非一字板的盘中封板走上方链内去重的封板确认行；开板（最高≠最低）特判自动失效。
- 盘中监控刻意安排在每小时过 5 分而非整点触发：13:00 整恰逢午休结束，接口快照仍是上午收盘数据；整点也是行情接口的访问高峰。
- **9:42 早盘采样**：开盘 15 分钟（9:30–9:45）是 A 股当日节奏定型窗口，「竞价高开→开盘冲高→9:40 回落」是最高频的追高被套形态，故在首笔与 10:05 之间加一次定性采样。
- **盘中停牌检测**：盘中临时停牌时行情快照冻结，纯涨跌幅判断会静默漏掉（冻结在 +8% 则每小时重复推送）。盘中监控已内置「交易时段内快照时间戳时分早于当前 30 分钟 → 推送 ⚠️ 疑似停牌」分支。
- **交易日判断**：除晚间风险预警外的 8 个任务均内置非交易日防护——接口时间戳（fields[30]）非当日即回复 `[SILENT]`，**节假日/休市日收不到推送是设计行为**，不是故障。
- **[SILENT] 静默机制**：hermes 仅当任务全部输出恰好为 `[SILENT]` 一个词时才不推送。因此所有静默指令均要求"全部输出仅 [SILENT] 一个词"，免责声明也注明 [SILENT] 时不附加——输出中混入任何其他文字（分析过程、免责声明）都会导致整条被推送。
- 除配置 `"disclaimer": false` 的任务（竞价初始/竞价结果/开盘首笔/部署健康检查为纯数据快照；三个盘中监控与晚间风险预警为保持链式去重上下文干净而将声明内联在自身 prompt 中）外，所有推送末尾均带固定免责声明，信息仅供参考，不构成投资建议。
- **接口故障与休市可区分**：所有行情类任务均已内置"接口请求失败/返回为空/无法解析 → 输出 ⚠️ 数据源异常，严禁 [SILENT]"分支——若该推送的日子收不到任何消息且确认是交易日，说明数据源（腾讯接口）故障，需人工介入，而非监控静默失效。
- **排障提示（continuity）**：怀疑误去重/误推送时，第一步查该任务上一次运行的实际输出（`hermes cron runs` + 任务 output 目录）——continuity 只注入任务自身上次输出，去重锚点异常多源于上次输出的实际内容（如某次任务失败/超时产生了非预期输出被注入为锚点）。
- **公开仓库纪律**：本仓库公开匿名可读，严禁提交任何个人持仓、成本价、账户信息；此类本地文件（如后续规划的 position.md 持仓文件）一律加入 `.gitignore`，部署时本地注入。
- **事件日历单点维护**：平陆运河等事件节点（9/3 起兑现前两周反转风险窗口、9/17—21 试通航、9/22 起兑现后监控重点切换、年底商业化运营）统一维护在 `tasks/000582/event_calendar.md`，部署时注入到配置了 `"event_calendar": true` 的任务（盘前情报/午间舆情/收盘复盘/晚间预警）；prompt 正文不再硬编码事件日期，节点临近或过后只改该文件并 `REPLACE=1 bash deploy.sh --only 北部湾港` 重建生效。日历写法为自纠偏式（按当前日期判断阶段、以最新搜索为准），重大节点过后仍建议人工复核一次日历是否需要更新。日历同时维护**确定性日历**（月度吞吐量披露节奏〔次月 3–6 日〕、定期报告预约披露日、分红除权提醒、解禁季度核对）——注意：除权日条目仅作信息提醒，行情接口昨收字段在除权日返回除权参考价、与涨跌幅同基准，数据自洽校验在除权日天然成立，**无需也不应**添加任何除权日校验降级逻辑；解禁截至 2026-08 核查无已排期大额解禁，按季度核对更新即可。
