---
name: china-stock-due-diligence
description: "Use when user 排雷 a specific A-share code. Run stock-monitor screen.py and transcribe verbatim."
version: 2.0.0
author: stock-monitor
license: MIT
---

# A 股个股排雷（stock-monitor 脚本版）

> 部署方式：本文件由仓库管理，服务器上以软链生效——
> `~/.hermes/skills/china-stock-due-diligence -> ~/stock-monitor/skills/china-stock-due-diligence`
> （hermes 的 skill 触发词「排雷」精准匹配本 skill；2026-09-03 曾被 hermes-curator
> 自动生成过"上网尽调"版本，导致三轮排雷全部绕过脚本自取数据，故收编进仓库管控）

## When to Use

触发词：`排雷 600000`、`排雷检查 sh000582`、`排雷报告 300750`、多代码如 `排雷 600000 000582`。
不适用于：闲聊、行业分析、无代码的泛泛提问。

## 铁律（违反任何一条 = 执行失败）

1. **数值只来自脚本 stdout**：禁止上网搜索行情/财务数据；禁止使用记忆里的数字（含「XX 常年 X%」、行业均值、个股横向对比）；禁止心算估算。
2. **逐字转述**脚本输出：不改写、不总结、不重新排版成表格，保留检查项/数值/判定（PASS/FAIL/WARN/MANUAL/NA）/结论行原样。
3. 不加免责声明段（用户已知悉），不给买卖建议。
4. 数据缺失（MANUAL）就如实转述缺失原因，禁止用任何其他来源补全，不要「帮忙拉数据」。

## 执行流程

1. 从用户消息提取股票代码，转成 **6 位纯数字**（剥掉 sh/sz/SH/SZ 前缀：`排雷 sh600000` → `600000`）。用户发中文名或位数不对 → 回复请用户提供 6 位代码，禁止自行搜索转换。
2. 执行（terminal 工具，一条命令）：
   `cd /home/ubuntu/stock-monitor && python3 scripts/screen.py check <代码>`
3. 把脚本 stdout 逐字转述给用户，**以普通消息文本发送、不要用代码块包裹**（微信里代码块字体小且折行错位）。
4. 多代码请求：逐个执行、逐个完整转述，不合并摘要。

## 转述后允许的补充（可选，不引入新数字）

- 解释各项检查的含义
- 提醒 MANUAL 项须巨潮资讯网（cninfo.com.cn）人工复核

## 异常处理

- 脚本输出含 ⚠️ 异常话术：原样转述，自行重跑至多 1 次；仍异常 → 请用户人工检查
- 脚本跑不起来（目录/文件不存在）：如实告知，**禁止 fallback 到上网搜索**
