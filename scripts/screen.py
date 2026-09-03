#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock-monitor 买入前排雷确定性脚本（排雷任务唯一数据来源）。

背景：docs/trading-common.md 附录的排雷清单此前靠人工查 F10 / 巨潮，逐票半小时且
红线判定易被"差不多"带过。本脚本把取数与红线判定全部下沉到代码（quote.py 同款
哲学）：每项输出 PASS / FAIL / WARN / MANUAL / NA + 数值 + 报告期 + 数据源，
人工只做 MANUAL 项复核与最终签发，不再自行取数判定。每次联网运行把完整原始
响应追加到 data/screens.jsonl（审计日志，轮转规则同 quotes.jsonl），任何推送
数值事后可对账。

排雷七项（trading-mid-term.md 2.2，红线取自该文档；长线层选股四指标取自
trading-long-term.md 1.2）：
  1 质押率 > 50% 不碰（30%~50% WARN）
  2 商誉/归母净资产 > 30% 不碰（15%~30% WARN）
  3 立案调查 / 审计非标 —— 公告标题扫描，命中 FAIL，未检出仍 MANUAL（非全量源）
  4 连续亏损逼近退市红线不碰（近 3 年年报全亏 FAIL；亏损+营收低于板块红线 FAIL）
  5 大股东减持计划不碰（近 180 天"减持+计划/拟"标题 FAIL；其他减持标题 WARN 列出）
  6 未来 3 个月解禁量 > 流通盘 10% 不碰（5%~10% WARN）
  7 存货/应收增速远超营收增速不碰（≥50% 且超营收 30pct FAIL；30%/15pct WARN）
长线四指标：股息率(TTM) ≥ 5%、连续派息 ≥ 5 年、分红覆盖 ≥ 1.5、分红率 > 80% 警惕。
另附：财报窗口（预约披露日前 7 天禁开新仓，trading-mid-term.md 2.3）。

判定枚举：PASS=过 / FAIL=命中红线 / WARN=接近红线或疑点，人工判断 /
MANUAL=数据源覆盖不全须人工复核（未检出≠安全）/ NA=不适用（如金融股无存货应收）。

数据源（全部东财公开 JSON API，2026-09-03 实测；仅标准库）：
  三大报表快照    datacenter RPT_DMSK_FN_INCOME/CASHFLOW（营收/净利/经营现金流）
  完整资产负债表  emweb zcfzbAjaxNew（商誉/归母净资产——DMSK 精简表无此二列，
                  银行/券商/保险需 companyType 1/2/3，接口按返回空自动重试）
  逐笔质押明细    datacenter RPTA_APP_ACCUMDETAILS（未解押 PF_TSR 加总=质押率，
                  口径为中登质押登记笔数加总，与 F10 页面一致）
  限售解禁        datacenter RPT_LIFT_STAGE（ABLE_FREE_SHARES 解禁量，单位万股，
                  已对账；分母用 push2 实时流通股本，不用接口 FREE_RATIO——
                  那是"占解禁前流通盘"口径，与文档"当前流通盘"红线不一致）
  分红明细        datacenter RPT_SHAREBONUS_DET（每股派息 PRETAX_BONUS_RMB 含税）
  公告标题        np-anotice-stock（400 条 ≈ 2~3 年，标题关键词扫描）
  预约披露        datacenter RPT_PUBLIC_BS_APPOIN
  现价/股本       push2 f43/f84/f85（现价/总股本/流通股本，股）

用法：
  screen.py check <code> [--json]     # code 如 600000 / 000582（纯数字，自动判交易所）
  screen.py selftest

红线阈值集中在 THRESH 常量区；调整阈值须盘后冷静时改代码，禁止为迁就单笔买入临时改。
"""
import argparse
import json
import os
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DC_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
          "?reportName={rn}&columns=ALL&filter={flt}&pageNumber=1&pageSize={ps}"
          "&sortTypes={st}&sortColumns={sc}")
F10_ZCFZB_URL = ("https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
                 "zcfzbAjaxNew?companyType={ct}&reportDateType=0&reportType=1"
                 "&dates={dates}&code={mcode}")
ANN_URL = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
           "?sr=-1&page_size=50&page_index={page}&ann_type=A&client_source=web"
           "&stock_list={code}&f_node=0&s_node=0")
PUSH2_URL = ("https://push2.eastmoney.com/api/qt/stock/get"
             "?secid={secid}&fields=f43,f84,f85,f58")   # secid 1.沪/0.深（quote.py 同款口径）
TENCENT_SNAP_URL = "https://qt.gtimg.cn/q={sym}"       # sym 如 sz000582（quote.py 主源，GBK 编码）

LINE_SCRIPT_ERROR = "⚠️ 排雷脚本异常，请人工检查"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
AUDIT_FILE = os.path.join(DATA_DIR, "screens.jsonl")
AUDIT_MAX_BYTES = 20 * 1024 * 1024
AUDIT_GENERATIONS = 4

# ---- 红线阈值（trading-mid-term.md 2.2 / trading-long-term.md 1.2；单位均为百分比数值） ----
THRESH = {
    "pledge_fail": 50.0, "pledge_warn": 30.0,          # 质押率（占总股本）
    "goodwill_fail": 30.0, "goodwill_warn": 15.0,      # 商誉/归母净资产
    "unlock_days": 92, "unlock_fail": 10.0, "unlock_warn": 5.0,   # 解禁窗口/占流通盘
    "revenue_floor_main": 3.0, "revenue_floor_gem": 1.0,  # 退市营收红线（主板/创业板科创板，亿元）
    "ar_inv_fail": 50.0, "ar_inv_fail_gap": 30.0,      # 存货或应收增速 ≥50% 且超营收 30pct
    "ar_inv_warn": 30.0, "ar_inv_warn_gap": 15.0,      #                ≥30% 且超营收 15pct
    "div_yield_min": 5.0, "div_yield_warn": 4.0,       # 长线：股息率 TTM
    "div_years_min": 5,                                # 长线：连续派息年数
    "div_cover_min": 1.5, "div_cover_warn": 1.0,       # 长线：经营现金流/年度分红
    "div_ratio_alert": 80.0,                           # 长线：分红率警惕线
    "report_gap_days": 7,                              # 财报窗口（披露前 N 天禁开仓）
    "reduce_window_days": 180,                         # 减持计划公告扫描窗口
}
# 立案/非标关键词（标题含即 FAIL）。审计意见词形须排除"标准无保留意见"——
# 年度审计公告标题必含"无保留意见"，直接子串匹配会把全部正常报告误判为非标。
KW_INVESTIGATION = ("立案",)
KW_NONSTANDARD = ("非标准", "非标意见", "无法表示意见", "带强调事项段")


def is_nonstd_title(title):
    if any(k in title for k in KW_NONSTANDARD):
        return True
    return "保留意见" in title and "无保留意见" not in title
# 减持计划类：标题同时含"减持"与计划词形 → FAIL；仅含"减持"（结果/进展公告）→ WARN
KW_REDUCE = "减持"
KW_REDUCE_PLAN = ("计划", "拟", "预披露")

CST = timezone(timedelta(hours=8))


def now_bj():
    return datetime.now(CST).replace(tzinfo=None)


def http_get(url, headers=None, timeout=10, retries=1, encoding="utf-8"):
    """GET → 文本。失败抛 URLError，由 mode 层统一兜底为 script_error。
    腾讯接口为 GBK 编码，经 encoding 参数指定。"""
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            if headers:
                req.add_header("Referer", headers.get("Referer", ""))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode(encoding, "replace")
        except Exception as e:            # noqa: BLE001 记录末次异常统一上抛
            last = e
    raise last


def _jget(obj, *keys, default=None):
    """安全取嵌套键，任一层缺失返回 default。"""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def audit_append(record):
    """追加审计日志（轮转规则同 quote.py：20MB × 保留 4 代）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if (os.path.exists(AUDIT_FILE)
            and os.path.getsize(AUDIT_FILE) > AUDIT_MAX_BYTES):
        for i in range(AUDIT_GENERATIONS, 0, -1):
            src = AUDIT_FILE if i == 1 else "%s.%d" % (AUDIT_FILE, i - 1)
            dst = "%s.%d" % (AUDIT_FILE, i)
            if os.path.exists(src):
                os.replace(src, dst)
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 数据获取 ----

def market_of(code):
    """纯数字代码 → (交易所前缀, 板块)。北交所退市规则未覆盖，check 内降级提示。"""
    if code.startswith(("60", "00")):
        return ("sh" if code.startswith("60") else "sz", "main")
    if code.startswith("300") or code.startswith("301"):
        return ("sz", "gem")            # 创业板
    if code.startswith("688"):
        return ("sh", "star")           # 科创板
    if code.startswith(("8", "4", "92")):
        return ("bj", "bj")
    raise ValueError("无法识别的代码: %s" % code)


def fetch_dc(report, code, sort_col, ps=60, desc=True):
    """datacenter 通用取数：返回 data 行列表（空表返回 []，字段缺失由调用方判定）。"""
    flt = '(SECURITY_CODE="%s")' % code
    url = DC_URL.format(rn=report, flt=urllib.parse.quote(flt), ps=ps,
                        st=-1 if desc else 1, sc=sort_col)
    obj = json.loads(http_get(url))
    return _jget(obj, "result", "data", default=[]) or []


def fetch_f10_balance(mcode, dates):
    """F10 完整资产负债表。金融股 companyType 敏感：4 通用 → 空则按 1 银行/2 券商/3 保险重试。"""
    for ct in (4, 1, 2, 3):
        url = F10_ZCFZB_URL.format(ct=ct, dates=",".join(dates), mcode=mcode)
        try:
            rows = _jget(json.loads(http_get(url, headers={
                "Referer": "https://emweb.securities.eastmoney.com/"})),
                "data", default=[]) or []
        except Exception:               # noqa: BLE001 单 ct 失败试下一个
            continue
        if rows:
            return rows, ct
    return [], None


def fetch_announcements(code, pages=8):
    """公告标题列表（新→旧）。400 条约覆盖 2~3 年，立案调查不限时的扫描上限即此。"""
    out = []
    for p in range(1, pages + 1):
        obj = json.loads(http_get(ANN_URL.format(page=p, code=code)))
        lst = _jget(obj, "data", "list", default=[]) or []
        out.extend((a.get("title", ""), a.get("notice_date", "")[:10]) for a in lst)
        if len(lst) < 50:
            break
    return out


def fetch_quote_fallback(code):
    """push2 限流时的备用价/股本源：腾讯快照（quote.py 的主数据源，长期验证稳定）。
    字段（2026-09-03 实测对账，000582）：3 现价 / 44 流通市值(亿) / 45 总市值(亿)，
    股本 = 市值×1e8÷现价（297.02 亿 ÷ 11.80 = 25.2 亿股，与 push2 f84 一致）。"""
    ex, _ = market_of(code)
    try:
        body = http_get(TENCENT_SNAP_URL.format(sym=ex + code), encoding="gbk")
        f = body.split("~")
        price = float(f[3])
        return {"price": price,
                "total_shares": round(float(f[45]) * 1e8 / price),
                "float_shares": round(float(f[44]) * 1e8 / price),
                "name": f[1] or None}
    except Exception:                   # noqa: BLE001 兜底源也挂才真正降级
        return {}


def fetch_push2(code):
    """现价（元）/ 总股本（股）/ 流通股本（股）/ 名称。secid 用市场数字前缀并带
    Referer（实测不带 Referer 或 sh/sz 前缀返回 rc:102 data:null）。
    push2 偶发断连限流（2026-09-03 微信会话两次触发两次断连），三败后切
    腾讯快照兜底；兜底也挂才返回空 dict——降级项走 MANUAL，不阻塞其余项。"""
    ex, _ = market_of(code)
    secid = ("1." if ex == "sh" else "0.") + code
    for _ in range(3):
        try:
            obj = json.loads(http_get(PUSH2_URL.format(secid=secid), retries=2,
                                      headers={"Referer": "https://quote.eastmoney.com/"}))
            d = _jget(obj, "data", default={}) or {}
            price = d.get("f43")
            price = price / 100.0 if isinstance(price, (int, float)) else None   # 价格 ×100 缩放（quote.py 同款）
            if price:
                return {"price": price, "total_shares": d.get("f84"),
                        "float_shares": d.get("f85"), "name": d.get("f58")}
        except Exception:               # noqa: BLE001 限流抖动重试，末次切兜底源
            continue
    return fetch_quote_fallback(code)


# ---------------------------------------------------------------- 判定逻辑 ----

def check_pledge(rows):
    """项 1 质押率：未解押笔数 PF_TSR 加总（口径：中登质押登记，与 F10 页面一致）。"""
    active = [r for r in rows if str(r.get("UNFREEZE_STATE", "")).find("未解押") >= 0]
    ratio = sum(float(r["PF_TSR"] or 0) for r in active if r.get("PF_TSR"))
    if ratio > THRESH["pledge_fail"]:
        return "FAIL", ratio, "未解押 %d 笔加总" % len(active)
    if ratio > THRESH["pledge_warn"]:
        return "WARN", ratio, "未解押 %d 笔加总" % len(active)
    return "PASS", ratio, "未解押 %d 笔加总" % len(active)


def check_goodwill(bal_rows):
    """项 2 商誉/归母净资产（最新报告期；商誉 null=0；归母净资产 ≤0 资不抵债直接 FAIL）。"""
    if not bal_rows:
        return "MANUAL", None, "F10 资产负债表无数据"
    r = bal_rows[0]
    gw, eq = float(r.get("GOODWILL") or 0), r.get("TOTAL_PARENT_EQUITY")
    if eq is None or eq <= 0:
        return "FAIL", None, "归母净资产缺失或 ≤0（资不抵债），报告期 %s" % r.get("REPORT_DATE", "?")[:10]
    pct = gw / eq * 100.0
    status = ("FAIL" if pct > THRESH["goodwill_fail"]
              else "WARN" if pct > THRESH["goodwill_warn"] else "PASS")
    return status, pct, "商誉 %.0f 万 / 归母净资产 %.0f 亿，报告期 %s" % (
        gw / 1e4, eq / 1e8, r.get("REPORT_DATE", "?")[:10])


def _match_kw(titles, kws):
    hit = [(t, d) for (t, d) in titles if any(k in t for k in kws)]
    return hit


def check_investigation_nonstd(titles):
    """项 3 立案/审计非标：公告标题扫描。未检出仍 MANUAL——公告源非全量，巨潮复核。"""
    inv = _match_kw(titles, KW_INVESTIGATION)
    if inv:
        return "FAIL", len(inv), "标题含「立案」%d 条，最近: %s" % (len(inv), inv[0][0][:40])
    ns = [(t, d) for (t, d) in titles if is_nonstd_title(t)]
    if ns:
        return "FAIL", len(ns), "标题含非标意见类 %d 条，最近: %s" % (len(ns), ns[0][0][:40])
    return "MANUAL", 0, "扫描 %d 条公告未检出（非全量源，须巨潮人工复核）" % len(titles)


def is_annual(row):
    """年报判定：REPORT_DATE 形如 '2025-12-31 00:00:00'——末尾是时分秒，
    用切片取 MM-DD（直接 endwith('12-31') 恒 False，年报会全部漏判）。"""
    return str(row.get("REPORT_DATE", ""))[5:10] == "12-31"


def check_delisting(income_rows, board):
    """项 4 退市红线：近 3 个年报归母净利全亏 FAIL；最近年报亏损且营收低于板块红线 FAIL。"""
    annuals = [r for r in income_rows if is_annual(r)][:3]
    if len(annuals) < 3:
        return "MANUAL", None, "年报数据不足 3 期"
    np_ = [float(r.get("PARENT_NETPROFIT") or 0) for r in annuals]
    rev_last = float(annuals[0].get("TOTAL_OPERATE_INCOME") or 0) / 1e8
    if all(x < 0 for x in np_):
        return "FAIL", sum(np_) / 1e8, "近 3 年年报连亏（%s）" % "/".join(
            "%.0f亿" % (x / 1e8) for x in np_)
    floor = (THRESH["revenue_floor_gem"] if board in ("gem", "star")
             else THRESH["revenue_floor_main"])
    if np_[0] < 0 and rev_last < floor:
        return "FAIL", None, "最近年报亏损 %.1f 亿且营收 %.1f 亿 < 板块红线 %.0f 亿" % (
            np_[0] / 1e8, rev_last, floor)
    if np_[0] < 0:
        return "WARN", None, "最近年报亏损 %.1f 亿（营收 %.1f 亿未触组合红线）" % (
            np_[0] / 1e8, rev_last)
    return "PASS", None, "最近年报盈利 %.1f 亿" % (np_[0] / 1e8)


def check_reduce_plan(titles, today):
    """项 5 减持计划：窗口内"减持+计划词形"FAIL；窗口外或仅结果公告 WARN 列出。"""
    since = today - timedelta(days=THRESH["reduce_window_days"])
    recent = [(t, d) for (t, d) in titles if d >= since.strftime("%Y-%m-%d")]
    plan = [(t, d) for (t, d) in recent
            if KW_REDUCE in t and any(k in t for k in KW_REDUCE_PLAN)]
    if plan:
        return "FAIL", len(plan), "近 %d 天减持计划类 %d 条，最近: %s" % (
            THRESH["reduce_window_days"], len(plan), plan[0][0][:40])
    other = [(t, d) for (t, d) in recent if KW_REDUCE in t]
    if other:
        return "WARN", len(other), "近 %d 天其他减持标题 %d 条（结果/进展），最近: %s" % (
            THRESH["reduce_window_days"], len(other), other[0][0][:40])
    return "MANUAL", 0, "近 %d 天未检出减持标题（非全量源，须巨潮复核）" % (
        THRESH["reduce_window_days"])


def check_unlock(rows, float_shares, today):
    """项 6 解禁：未来 3 个月解禁量合计 / 当前流通股本。ABLE_FREE_SHARES 单位万股（已对账）。"""
    if not float_shares:
        return "MANUAL", None, "流通股本缺失"
    horizon = today + timedelta(days=THRESH["unlock_days"])
    upcoming = [r for r in rows
                if r.get("FREE_DATE") and today.strftime("%Y-%m-%d")
                <= str(r["FREE_DATE"])[:10] <= horizon.strftime("%Y-%m-%d")]
    vol_wan = sum(float(r.get("ABLE_FREE_SHARES") or 0) for r in upcoming)
    pct = vol_wan * 1e4 / float_shares * 100.0
    desc = "%d 个批次合计 %.2f 亿股 / 现流通盘" % (len(upcoming), vol_wan / 1e4)
    if upcoming:
        desc += "，最近 %s" % str(upcoming[0]["FREE_DATE"])[:10]
    if pct > THRESH["unlock_fail"]:
        return "FAIL", pct, desc
    if pct > THRESH["unlock_warn"]:
        return "WARN", pct, desc
    return "PASS", pct, desc


def check_ar_inventory(bal_now, bal_prev, inc_now, inc_prev):
    """项 7 存货/应收增速 vs 营收增速（金融股字段为 null → NA）。同比取去年同期报告期。"""
    def yoy(now, prev):
        return None if not (now and prev) else (now - prev) / abs(prev) * 100.0
    inv, ar = yoy(bal_now.get("INVENTORY"), bal_prev.get("INVENTORY")), \
        yoy(bal_now.get("ACCOUNTS_RECE"), bal_prev.get("ACCOUNTS_RECE"))
    rev = yoy(inc_now.get("TOTAL_OPERATE_INCOME"), inc_prev.get("TOTAL_OPERATE_INCOME"))
    if inv is None and ar is None:
        return "NA", None, "存货/应收字段为空（金融股报表结构），此项不适用"
    parts = []
    worst = ("PASS", 0.0)
    for label, v in (("存货", inv), ("应收", ar)):
        if v is None:
            parts.append("%s 无同比" % label)
            continue
        parts.append("%s %+d%%" % (label, v))
        if rev is not None:
            gap = v - rev
            if (v >= THRESH["ar_inv_fail"] and gap >= THRESH["ar_inv_fail_gap"]):
                worst = ("FAIL", max(worst[1], gap))
            elif (v >= THRESH["ar_inv_warn"] and gap >= THRESH["ar_inv_warn_gap"]):
                worst = ("WARN" if worst[0] != "FAIL" else worst[0], max(worst[1], gap))
    rev_s = "营收 %+d%%" % rev if rev is not None else "营收无同比（上市未满一年？）"
    if worst[0] == "PASS" and rev is None:
        return "MANUAL", None, "营收无同比，增速对比不成立：%s" % "、".join(parts)
    return worst[0], worst[1], "%s vs %s" % ("、".join(parts), rev_s)


def check_dividend(div_rows, cash_rows, income_rows, bal_rows, price, total_shares):
    """长线四指标：股息率 TTM / 连续派息年数 / 分红覆盖 / 分红率。返回指标列表。"""
    out = []

    # 股息率 TTM：近 12 个月除息日落的每股现金分红合计 / 现价（送转不计）。
    # PRETAX_BONUS_RMB 为 10 股口径（IMPL_PLAN_PROFILE「10派X元」），每股须 ÷10。
    today = now_bj()
    t12 = (today - timedelta(days=365)).strftime("%Y-%m-%d")
    dps_ttm = sum(float(r.get("PRETAX_BONUS_RMB") or 0) / 10.0 for r in div_rows
                  if r.get("EX_DIVIDEND_DATE") and str(r["EX_DIVIDEND_DATE"])[:10] >= t12
                  and "派" in str(r.get("IMPL_PLAN_PROFILE") or ""))
    if price:
        y = dps_ttm / price * 100.0
        st = ("PASS" if y >= THRESH["div_yield_min"]
              else "WARN" if y >= THRESH["div_yield_warn"] else "FAIL")
        out.append(("股息率(TTM)", st, y, "近12月每股派息 %.3f 元 / 现价 %.2f 元" % (dps_ttm, price)))
    else:
        out.append(("股息率(TTM)", "MANUAL", None, "现价缺失"))

    # 连续派息年数：有实施分配记录的连续年份（自最新年往前数）
    years = sorted({str(r.get("REPORT_DATE", ""))[:4] for r in div_rows
                    if r.get("REPORT_DATE") and "派" in str(r.get("IMPL_PLAN_PROFILE") or "")},
                   reverse=True)
    n = 0
    if years:
        y0 = int(years[0])
        for yy in range(y0, y0 - 31, -1):     # 上限 31 年防御脏数据（老股真实连发可超 20 年）
            if str(yy) in years:
                n += 1
            else:
                break
    out.append(("连续派息", "PASS" if n >= THRESH["div_years_min"] else "FAIL", n,
                "连续 %d 年（%s）" % (n, years[0] if years else "无记录")))

    # 分红覆盖：最近年报经营现金流 / 该年度分红总额（每股派息×总股本）
    annual_cash = next((r for r in cash_rows if is_annual(r)), None)
    div_year = str(_jget(annual_cash, "REPORT_DATE", default=""))[:4]
    total_div = (sum(float(r.get("PRETAX_BONUS_RMB") or 0) / 10.0 for r in div_rows
                     if str(r.get("REPORT_DATE", ""))[:4] == div_year
                     and "派" in str(r.get("IMPL_PLAN_PROFILE") or ""))
                 * (total_shares or 0))
    ocf = float((annual_cash or {}).get("NETCASH_OPERATE") or 0)
    if annual_cash and total_div > 0:
        cover = ocf / total_div
        st = ("PASS" if cover >= THRESH["div_cover_min"]
              else "WARN" if cover >= THRESH["div_cover_warn"] else "FAIL")
        out.append(("分红覆盖", st, cover, "%s 年报经营现金流 %.0f 亿 / 分红总额 %.0f 亿" % (
            div_year, ocf / 1e8, total_div / 1e8)))
    else:
        out.append(("分红覆盖", "MANUAL", None, "年报现金流或分红数据缺失"))

    # 分红率：最近年报分红总额 / 归母净利润（>80% 警惕，非否决）
    annual_np = next((r for r in income_rows if is_annual(r)), None)
    npv = float((annual_np or {}).get("PARENT_NETPROFIT") or 0)
    if annual_np and total_div > 0 and npv > 0:
        dr = total_div / npv * 100.0
        out.append(("分红率", "WARN" if dr > THRESH["div_ratio_alert"] else "PASS", dr,
                    "%s 年报分红 %.0f 亿 / 归母净利 %.0f 亿%s" % (
                        div_year, total_div / 1e8, npv / 1e8,
                        "，> 80% 警惕（借钱分红/不可持续风险）" if dr > THRESH["div_ratio_alert"] else "")))
    else:
        out.append(("分红率", "MANUAL", None, "年报利润或分红数据缺失"))
    return out


def check_report_window(appoint_rows, today):
    """财报窗口：各报告期预约/实际披露日，未来 7 天内将披露 → WARN 禁开仓。
    预约表无未来日期（三季报预约日通常 9 月中下旬才录入）→ 显式 PASS 而非
    静默省略，让读者分得清"已查、无窗口"与"未检查"；接口无返回 → MANUAL。"""
    if not appoint_rows:
        return "MANUAL", "预约披露数据缺失（接口无返回）"
    futures = []
    for r in appoint_rows:
        d = str(r.get("ACTUAL_PUBLISH_DATE")
                or r.get("FIRST_APPOINT_DATE") or "")[:10]
        if d >= today.strftime("%Y-%m-%d"):
            futures.append((d, "%s 年报" % r.get("REPORT_YEAR", "?")))
    futures.sort()
    if not futures:
        return "PASS", "预约表无未来披露日——已预约报告期均已披露，下批预约日待交易所更新"
    d, label = futures[0]
    days = (datetime.strptime(d, "%Y-%m-%d") - today).days
    if days <= THRESH["report_gap_days"]:
        return "WARN", "%s %d 天后披露（%s）→ 财报窗口内禁开新仓" % (label, days, d)
    return "PASS", "最近披露 %s（%s，%d 天后）" % (label, d, days)


# ---------------------------------------------------------------- 主流程 ----

def fmt_val(v, unit="%"):
    """数值 → 定宽文本；None → —；整数（年数/条数）不带单位前缀的百分号。"""
    if v is None:
        return "—"
    if isinstance(v, int):
        return "%d%s" % (v, unit if unit != "%" else "")
    return ("%.2f%s" % (v, unit)) if abs(v) < 1000 else ("%.1f%s" % (v, unit))


def run_check(code, as_json=False):
    today = now_bj()
    ex, board = market_of(code)
    mcode = ("%s%s" % (ex.upper(), code))
    meta = fetch_push2(code)

    # 取数（每步原始响应入审计）
    raw_store = {}
    def step(name, fn):
        raw_store[name] = fn()
        return raw_store[name]

    pledge_rows = step("pledge", lambda: fetch_dc(
        "RPTA_APP_ACCUMDETAILS", code, "NOTICE_DATE", ps=200))
    unlock_rows = step("unlock", lambda: fetch_dc(
        "RPT_LIFT_STAGE", code, "FREE_DATE", ps=60, desc=False))
    div_rows = step("dividend", lambda: fetch_dc(
        "RPT_SHAREBONUS_DET", code, "EX_DIVIDEND_DATE", ps=120))
    income_rows = step("income", lambda: fetch_dc(
        "RPT_DMSK_FN_INCOME", code, "REPORT_DATE", ps=24))
    cash_rows = step("cashflow", lambda: fetch_dc(
        "RPT_DMSK_FN_CASHFLOW", code, "REPORT_DATE", ps=24))
    appoint_rows = step("appoint", lambda: fetch_dc(
        "RPT_PUBLIC_BS_APPOIN", code, "REPORT_DATE", ps=20))

    # 资产负债表：最新季报 + 去年同期（存货/应收同比）；F10 表按日期倒序
    dates = sorted({str(r.get("REPORT_DATE", ""))[:10] for r in income_rows}, reverse=True)
    def _pick_pair():
        for d in dates:
            prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
            near = [p for p in dates if abs((datetime.strptime(p, "%Y-%m-%d")
                    - datetime.strptime(prev, "%Y-%m-%d")).days) <= 10]
            if near:
                return [d, near[0]]
        return dates[:1]
    bal_dates = _pick_pair()
    bal_rows, ct_used = step("f10_balance", lambda: fetch_f10_balance(mcode, bal_dates))
    titles = step("announcements", lambda: fetch_announcements(code))
    raw_store["push2"] = meta

    audit_append({"ts": today.strftime("%Y-%m-%d %H:%M:%S"), "code": code,
                  "raw": raw_store})

    # 判定
    inc_by_date = {str(r.get("REPORT_DATE", ""))[:10]: r for r in income_rows}
    bal_by_date = {str(r.get("REPORT_DATE", ""))[:10]: r for r in bal_rows}
    d_now = bal_dates[0] if bal_dates else None
    d_prev = bal_dates[1] if len(bal_dates) > 1 else None

    checks = [
        ("1 质押率", *check_pledge(pledge_rows)),
        ("2 商誉/净资产", *check_goodwill(bal_rows)),
        ("3 立案/非标", *check_investigation_nonstd(titles)),
        ("4 退市红线", *check_delisting(income_rows, board)),
        ("5 减持计划", *check_reduce_plan(titles, today)),
        ("6 限售解禁", *check_unlock(unlock_rows, meta.get("float_shares"), today)),
        ("7 存货/应收", *check_ar_inventory(
            bal_by_date.get(d_now, {}), bal_by_date.get(d_prev, {}),
            inc_by_date.get(d_now, {}), inc_by_date.get(d_prev, {}))),
    ]
    dividend = check_dividend(div_rows, cash_rows, income_rows, bal_rows,
                              meta.get("price"), meta.get("total_shares"))
    report_win = check_report_window(appoint_rows, today)

    counts = {}
    for _, st, _, _ in checks:
        counts[st] = counts.get(st, 0) + 1
    summary = " / ".join("%d %s" % (counts.get(k, 0), k)
                         for k in ("FAIL", "WARN", "MANUAL", "PASS") if counts.get(k))

    if as_json:
        return {"code": code, "name": meta.get("name"), "date": today.strftime("%Y-%m-%d"),
                "checks": [{"item": i, "status": s, "value": v, "detail": d}
                           for (i, s, v, d) in checks],
                "dividend": [{"item": i, "status": s, "value": v, "detail": d}
                             for (i, s, v, d) in dividend],
                "report_window": report_win, "summary": summary,
                "board": board, "data_notice": "公告扫描非全量源；北交所退市口径未覆盖"
                if board == "bj" else "公告扫描非全量源"}

    # 文本报告
    lines = ["排雷报告 %s %s · %s" % (code, meta.get("name") or "", today.strftime("%Y-%m-%d")),
             "口径：财务数据取最新定期报告；公告扫描非全量源，MANUAL 项须巨潮人工复核"]
    for item, st, v, detail in checks:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "⚠ WARN",
                "MANUAL": "☐ MANUAL", "NA": "N/A "}[st]
        lines.append("%-14s %-9s %8s  %s" % (item, mark, fmt_val(v), detail))
    lines.append("── 长线红利层选股指标（trading-long-term.md 1.2，非中线必查项）")
    for item, st, v, detail in dividend:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "⚠ WARN",
                "MANUAL": "☐ MANUAL", "NA": "N/A "}[st]
        val = fmt_val(v, "倍" if item == "分红覆盖" else ("年" if item == "连续派息" else "%"))
        lines.append("%-14s %-9s %8s  %s" % (item, mark, val, detail))
    if report_win:
        lines.append("── 财报窗口: %s" % report_win[1])
    lines.append("结论: %s" % summary)
    if counts.get("FAIL"):
        lines.append("⛔ 存在 FAIL 项——按纪律任一命中不买（trading-mid-term.md 2.2）")
    return "\n".join(lines)


# ---------------------------------------------------------------- selftest ----

def _run_selftest():
    """离线判定逻辑测试（构造固定行数据，不联网）。"""
    today = datetime(2026, 9, 3)
    fails = []
    n_checks = [0]

    def eq(name, got, want):
        n_checks[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    # 质押：两笔未解押 30+25 → 55 FAIL；全解押 → PASS 0
    rows = [{"UNFREEZE_STATE": "未解押", "PF_TSR": 30.0},
            {"UNFREEZE_STATE": "未解押", "PF_TSR": 25.0},
            {"UNFREEZE_STATE": "已解押", "PF_TSR": 40.0}]
    eq("pledge-fail", check_pledge(rows)[0], "FAIL")
    eq("pledge-zero", check_pledge([rows[2]])[0], "PASS")

    # 商誉：1.5 亿/10 亿 = 15% 边界（>15 warn 阈值为开区间 → 15.0 PASS）
    eq("gw-pass", check_goodwill(
        [{"GOODWILL": 1.5e8, "TOTAL_PARENT_EQUITY": 1e9, "REPORT_DATE": "2026-06-30"}])[0], "PASS")
    eq("gw-warn", check_goodwill(
        [{"GOODWILL": 2e8, "TOTAL_PARENT_EQUITY": 1e9, "REPORT_DATE": "2026-06-30"}])[0], "WARN")
    eq("gw-fail-neg-eq", check_goodwill(
        [{"GOODWILL": 0, "TOTAL_PARENT_EQUITY": -1e8, "REPORT_DATE": "2026-06-30"}])[0], "FAIL")
    eq("gw-none", check_goodwill([])[0], "MANUAL")

    # 立案/非标：命中词形与未检出
    titles = [("某公司:关于收到中国证监会立案调查通知的公告", "2026-08-01"),
              ("某公司:2025年年度审计报告为标准无保留意见", "2026-04-01")]
    eq("inv-fail", check_investigation_nonstd(titles)[0], "FAIL")
    eq("inv-manual", check_investigation_nonstd([titles[1]])[0], "MANUAL")

    # 退市：近 3 年报（倒序）连亏 FAIL；单亏+低营收 FAIL；单亏 WARN
    def inc(d, npv, rev):
        return {"REPORT_DATE": "%s-12-31" % d, "PARENT_NETPROFIT": npv,
                "TOTAL_OPERATE_INCOME": rev}
    eq("del-3loss", check_delisting(
        [inc("2025", -1e8, 2e8), inc("2024", -1e8, 2e8), inc("2023", -1e8, 2e8)], "main")[0], "FAIL")
    eq("del-loss-lowrev", check_delisting(
        [inc("2025", -1e8, 2e8), inc("2024", 1e8, 5e8), inc("2023", 1e8, 5e8)], "main")[0], "FAIL")
    eq("del-gem-floor", check_delisting(
        [inc("2025", -1e8, 2e8), inc("2024", 1e8, 5e8), inc("2023", 1e8, 5e8)], "gem")[0], "WARN")
    eq("del-ok", check_delisting(
        [inc("2025", 1e8, 5e8), inc("2024", 1e8, 5e8), inc("2023", 1e8, 5e8)], "main")[0], "PASS")

    # 减持：窗口内计划 FAIL、结果公告 WARN、无 → MANUAL
    eq("red-plan", check_reduce_plan(
        [("甲:关于股东减持计划的预披露公告", "2026-09-01")], today)[0], "FAIL")
    eq("red-done", check_reduce_plan(
        [("甲:关于股东减持股份结果公告", "2026-09-01")], today)[0], "WARN")
    eq("red-old", check_reduce_plan(
        [("甲:关于股东减持计划的预披露公告", "2025-01-01")], today)[0], "MANUAL")

    # 解禁：1.2 亿股/10 亿流通 = 12% FAIL；口径万吨→股换算
    unlock = [{"FREE_DATE": "2026-10-01", "ABLE_FREE_SHARES": 12000}]
    eq("unlk-fail", check_unlock(unlock, 1e9, today)[0], "FAIL")
    eq("unlk-past", check_unlock(
        [{"FREE_DATE": "2026-01-01", "ABLE_FREE_SHARES": 12000}], 1e9, today)[0], "PASS")
    eq("unlk-nofloat", check_unlock(unlock, None, today)[0], "MANUAL")

    # 存货/应收：应收 +80% vs 营收 +20%（gap 60 ≥30 且 ≥50）FAIL；金融股 NA
    bn, bp = {"INVENTORY": 1e8, "ACCOUNTS_RECE": 1.8e8}, {"INVENTORY": 1e8, "ACCOUNTS_RECE": 1e8}
    i_n, i_p = {"TOTAL_OPERATE_INCOME": 1.2e9}, {"TOTAL_OPERATE_INCOME": 1e9}
    eq("ar-fail", check_ar_inventory(bn, bp, i_n, i_p)[0], "FAIL")
    eq("ar-na", check_ar_inventory({}, {}, i_n, i_p)[0], "NA")
    eq("ar-warn", check_ar_inventory(
        {"ACCOUNTS_RECE": 1.4e8}, {"ACCOUNTS_RECE": 1e8}, i_n, i_p)[0], "WARN")

    # 财报窗口：3 天后披露 WARN；60 天后 PASS；
    # 无未来披露日（已披露+下批预约未出）显式 PASS；接口空 → MANUAL
    eq("rw-warn", check_report_window(
        [{"REPORT_YEAR": "2026", "ACTUAL_PUBLISH_DATE": "2026-09-06"}], today)[0], "WARN")
    eq("rw-pass", check_report_window(
        [{"REPORT_YEAR": "2026", "ACTUAL_PUBLISH_DATE": "2026-11-02"}], today)[0], "PASS")
    eq("rw-none-future", check_report_window(
        [{"REPORT_YEAR": "2026", "ACTUAL_PUBLISH_DATE": "2026-08-18"}], today)[0], "PASS")
    eq("rw-manual-empty", check_report_window([], today)[0], "MANUAL")

    # 连续派息：2020-2025 六连发（构造进 check_dividend 太重，只测年份数列逻辑外的快速路径）
    eq("market-60", market_of("600000"), ("sh", "main"))
    eq("market-gem", market_of("300750"), ("sz", "gem"))
    eq("market-star", market_of("688981"), ("sh", "star"))

    if fails:
        print("SELFTEST FAILED (%d):" % len(fails))
        for f in fails:
            print("  " + f)
        return 1
    print("selftest OK: 排雷判定 %d 项边界全部通过" % n_checks[0])
    return 0


# ---------------------------------------------------------------- 入口 ----

def parse_args(argv):
    p = argparse.ArgumentParser(description="买入前排雷确定性脚本")
    sub = p.add_subparsers(dest="mode")
    c = sub.add_parser("check", help="跑一只票的排雷报告")
    c.add_argument("code", help="6 位股票代码，如 600000")
    c.add_argument("--json", action="store_true", help="输出 JSON（供任务转述）")
    sub.add_parser("selftest", help="离线判定逻辑自测")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.mode == "selftest":
            return _run_selftest()
        if args.mode == "check":
            out = run_check(args.code, as_json=args.json)
            print(json.dumps(out, ensure_ascii=False, indent=1) if args.json else out)
            return 0
        return 2
    except Exception:                   # noqa: BLE001 崩溃兜底固定话术（同 quote.py）
        traceback.print_exc(file=sys.stderr)
        print(LINE_SCRIPT_ERROR)
        return 1


if __name__ == "__main__":
    sys.exit(main())
