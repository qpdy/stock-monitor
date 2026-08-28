#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock-monitor 确定性行情取数脚本（监控任务唯一数据来源）。

背景：此前取数/解析/自洽校验全部写在任务 prompt 里由模型"自觉执行"，模型可编造
一对自洽的假数字通过校验（2026-08-27 竞价初始任务事故）。本脚本把数字的产生与
校验全部下沉到代码：任务 prompt 只允许运行本脚本并逐字转述输出，严禁模型自行
取数、心算或估算。每次联网运行会把完整原始响应与解析结果追加到 data/quotes.jsonl
（审计日志，单文件超 20MB 自动轮转为 quotes.jsonl.1、保留一代），任何推送数值事后可对账。
脚本内一切时间判定（交易日、竞价/早盘窗口、快照时效）固定按北京时间 UTC+8 计算，
与服务器本地时区无关。

用法：
  quote.py snap <sym...> [--premarket] [--min-time HH:MM] [--max-age-min N] [--with-kline[=N]]
                         [--event-anchor YYYY-MM-DD] [--minute-recap HH:MM]
  quote.py kline <sym> [--days N] [--since YYYY-MM-DD] [--this-week] [--this-month] [--prev-month]
  quote.py minute <sym> [--closing]
  quote.py audit [--this-week]
  quote.py selftest

snap 符号如 sz000582 / sh000001；多标的按行首代码（v_sz000582）匹配，与行序无关。
verdict 枚举与优先级：source_error > inconsistent > not_trading_day > halt_suspected
> stale_snapshot > no_data_preopen > ok（模式门控）；崩溃兜底 script_error（exit 1，
verdict_line 为固定脚本异常话术，不依赖任务 prompt 的兜底规则也能被原样转述）。
verdict_line 为固定话术，任务原样复制；not_trading_day / no_data_preopen 的分支动作
由任务 prompt 定义（[SILENT] 或固定行）。

仅依赖 python3 标准库（服务器 python3 >= 3.6）。
"""
import argparse
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import traceback
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TENCENT_SNAPSHOT_URL = "https://qt.gtimg.cn/q={symbols}"
TENCENT_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                     "?param={code},day,,,{count},qfq")  # qfq 不可省略（省略返回空）
TENCENT_MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
EASTMONEY_URL = ("https://push2.eastmoney.com/api/qt/stock/get"
                 "?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f58,f60,f170")

# 腾讯快照字段索引（2026-08-27 实盘核对）
F_NAME, F_CODE = 1, 2
F_PRICE, F_PREV, F_OPEN = 3, 4, 5
F_TS = 30          # 快照时间戳 YYYYMMDDHHMMSS
F_CHANGE, F_PCT = 31, 32
F_HIGH, F_LOW = 33, 34
F_VOL, F_AMT = 36, 37   # 成交量(手)、成交额(万元)
F_TURNOVER, F_AMPLITUDE = 38, 43
F_LIMIT_UP, F_LIMIT_DOWN, F_VOLRATIO = 47, 48, 49

# 已知标的显示名（备用源无名称字段时兜底）
SYMBOL_LABELS = {
    "sz000582": "北部湾港",
    "sh000001": "上证指数",
    "sz001872": "招商港口",
    "sh600018": "上港集团",
    "sh600368": "五洲交通",
}

# 固定话术（任务推送原样使用；script_error 话术与各任务 prompt 第 4 条兜底文案
# 刻意一致——脚本崩溃时模型无论走 verdict_line 转述还是走 prompt 兜底，输出同一句话）
LINE_SOURCE_ERROR = "⚠️ 数据源异常，请人工检查"
LINE_SCRIPT_ERROR = "⚠️ 数据脚本异常，请人工检查"
LINE_STALE_AUCTION = ("⚠️ 快照停在 {hhmm}（早于9:25撮合），本次读数可能为撮合前虚拟价，"
                      "开盘价请以9:30开盘首笔推送为准")
LINE_HALT_FROZEN = "⚠️ 疑似盘中临时停牌/数据冻结（快照停留在 {hhmm}），请人工核实"
LINE_HALT_ZERO = "⚠️ 疑似停牌/数据冻结（时间戳更新但零成交），请人工核实"
LINE_CLOSE_FAULT = "⚠️ 数据源异常（收盘数据不可用/未回补），请人工检查"
LINE_CLOSE_NOT_READY = "⚠️ 数据源异常（收盘竞价分时数据未回补），请人工检查"
LINE_SOURCE_NOTICE = "⚠️ 主数据源异常，已切换备用源（无快照时间戳，交易日/时效校验失效）"
LINE_NO_DATA_PREOPEN = ("📊 集合竞价数据尚未产生：现价=昨收 {prev}元、成交量0、快照 {ts}，"
                        "竞价虚拟价以9:25竞价结果推送为准（9:20前可撤单，虚拟数据仅供参考）")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
AUDIT_FILE = os.path.join(DATA_DIR, "quotes.jsonl")
AUDIT_ROTATED = AUDIT_FILE + ".1"      # 轮转一代（覆盖式，只保留最近一代）
AUDIT_MAX_BYTES = 20 * 1024 * 1024     # 单文件 20MB 上限（约数月量级，audit 汇总两代合并读取）

# 行情时间体系固定为北京时间（UTC+8）：cron 触发、交易所时钟、接口快照时间戳均按
# 北京时间对齐，脚本判定不随部署环境本地时区漂移
CST = timezone(timedelta(hours=8))


def now_bj():
    """当前北京时间（naive datetime，与 parse_ts 产出同构，可直接比较/相减）。"""
    return datetime.now(CST).replace(tzinfo=None)

ROW_RE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')


# ---------------------------------------------------------------- 基础工具

def to_f(v):
    """字符串安全转 float；空/非法返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pick(fields, idx):
    return fields[idx].strip() if idx < len(fields) else ""


def parse_ts(raw):
    """YYYYMMDDHHMMSS（或 YYYYMMDDHHMM）→ datetime；非法返回 None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not re.fullmatch(r"\d{12,14}", s):
        return None
    try:
        if len(s) >= 14:
            return datetime.strptime(s[:14], "%Y%m%d%H%M%S")
        return datetime.strptime(s[:12], "%Y%m%d%H%M")
    except ValueError:
        return None


def http_get(url, headers=None, timeout=10, retries=1):
    """GET 返回 bytes；重试 retries 次后仍失败抛最后异常。"""
    last = None
    for _ in range(retries + 1):
        try:
            h = {"User-Agent": UA}
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last = e
    raise last


def decode_tencent(body):
    """腾讯行情接口返回 GBK 编码（现行 prompt 从未处理，是隐性故障源）。"""
    if isinstance(body, bytes):
        return body.decode("gbk", errors="replace")
    return body


def num_str(v):
    """1561.0→'1561'，1561.25→'1561.25'，None→''。"""
    if v is None:
        return ""
    s = ("%.2f" % float(v)).rstrip("0").rstrip(".")
    return s or "0"


def price_str(v):
    """价格固定两位小数（9.7→'9.70'）；None→''。"""
    return ("%.2f" % v) if v is not None else ""


def clean(obj):
    """剔除内部字段（下划线前缀）后可 JSON 化。"""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    return obj


def out_json(obj):
    print(json.dumps(clean(obj), ensure_ascii=False, indent=2, default=str))


def audit_append(record):
    """追加审计日志；当前文件超上限先轮转为 .1（覆盖旧一代）；写失败仅告警，绝不影响数据路径。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            if os.path.getsize(AUDIT_FILE) > AUDIT_MAX_BYTES:
                try:
                    os.remove(AUDIT_ROTATED)
                except OSError:
                    pass
                os.rename(AUDIT_FILE, AUDIT_ROTATED)
        except OSError:
            pass  # 轮转失败不阻塞本次写入
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(clean(record), ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        sys.stderr.write("⚠️ 审计日志写入失败（不影响本次数据）: %s\n" % e)


def audit_files():
    """审计日志文件列表（轮转旧代在前保证时间顺序）；两代均不存在返回空表。"""
    return [p for p in (AUDIT_ROTATED, AUDIT_FILE) if os.path.isfile(p)]


def market_phase(now):
    t = now.time()
    if t < dtime(9, 15):
        return "pre_open"
    if t < dtime(9, 25):
        return "call_auction"
    if t < dtime(9, 30):
        return "auction_matched"
    if t <= dtime(11, 30):
        return "morning_continuous"
    if t < dtime(13, 0):
        return "lunch_break"
    if t <= dtime(15, 0):
        return "afternoon_continuous"
    return "after_close"


def in_continuous(now):
    t = now.time()
    return dtime(9, 30) <= t <= dtime(11, 30) or dtime(13, 0) <= t <= dtime(15, 0)


def sign(x):
    if x is None or x == 0:
        return 0
    return 1 if x > 0 else -1


# ---------------------------------------------------------------- 解析（纯函数）

def parse_tencent_rows(text):
    """腾讯多标的响应 → {code: fields_list}。按行首代码匹配，严禁按行序。"""
    rows = {}
    for m in ROW_RE.finditer(text or ""):
        rows[m.group(1)] = m.group(2).split("~")
    return rows


def symbol_from_tencent(code, fields):
    ts_raw = pick(fields, F_TS)
    ts = parse_ts(ts_raw)
    sym = {
        "code": code,
        "name": pick(fields, F_NAME) or SYMBOL_LABELS.get(code, code),
        "source": "tencent",
        "price": to_f(pick(fields, F_PRICE)),
        "prev_close": to_f(pick(fields, F_PREV)),
        "open": to_f(pick(fields, F_OPEN)),
        "high": to_f(pick(fields, F_HIGH)),
        "low": to_f(pick(fields, F_LOW)),
        "change": to_f(pick(fields, F_CHANGE)),
        "pct_reported": to_f(pick(fields, F_PCT)),
        "volume_hand": to_f(pick(fields, F_VOL)),
        "amount_wan": to_f(pick(fields, F_AMT)),
        "turnover_pct": to_f(pick(fields, F_TURNOVER)),
        "amplitude_pct": to_f(pick(fields, F_AMPLITUDE)),
        "limit_up": to_f(pick(fields, F_LIMIT_UP)),
        "limit_down": to_f(pick(fields, F_LIMIT_DOWN)),
        "vol_ratio": to_f(pick(fields, F_VOLRATIO)),
        "ts_raw": ts_raw,
        "ts_date": ts.strftime("%Y-%m-%d") if ts else None,
        "ts_time": (ts.strftime("%H:%M:%S") if ts and len(ts_raw.strip()) >= 14
                    else (ts.strftime("%H:%M") if ts else None)),
        "_ts_dt": ts,
        "limitations": [],
    }
    return sym


def symbol_from_eastmoney(code, data):
    """东财备用源：价格类字段为放大100倍整数需 ÷100；f47 成交量(手)；f48 成交额(元)。
    无快照时间戳 → 交易日/时效/停牌校验失效（以 limitations 标注）。"""
    def f100(k):
        v = to_f(data.get(k))
        return v / 100.0 if v is not None else None
    price, prev, pct = f100("f43"), f100("f60"), f100("f170")
    amt_yuan = to_f(data.get("f48"))
    name = str(data.get("f58") or "").strip() or SYMBOL_LABELS.get(code, code)
    sym = {
        "code": code,
        "name": name,
        "source": "eastmoney",
        "price": price,
        "prev_close": prev,
        "open": f100("f46"),
        "high": f100("f44"),
        "low": f100("f45"),
        "change": None,
        "pct_reported": pct,
        "volume_hand": to_f(data.get("f47")),
        "amount_wan": amt_yuan / 1e4 if amt_yuan is not None else None,
        "turnover_pct": None,
        "amplitude_pct": None,
        "limit_up": None,
        "limit_down": None,
        "vol_ratio": None,
        "ts_raw": None,
        "ts_date": None,
        "ts_time": None,
        "_ts_dt": None,
        "limitations": ["no_timestamp", "no_volratio", "no_limit_prices"],
    }
    return sym


def parse_kline_bars(obj, code):
    """日K JSON → bars[{date,open,close,high,low,volume_hand}]。条目=[日期,开,收,高,低,量]。"""
    node = (obj.get("data") or {}).get(code) or {}
    arr = node.get("qfqday") or node.get("day") or []
    bars = []
    for b in arr:
        try:
            bars.append({
                "date": str(b[0]).strip(),
                "open": to_f(b[1]),
                "close": to_f(b[2]),
                "high": to_f(b[3]),
                "low": to_f(b[4]),
                "volume_hand": to_f(b[5]),
            })
        except (IndexError, TypeError):
            continue
    return bars


def parse_minute(obj, code):
    """分时 JSON → (date, entries[(hhmm, price, cumvol)])。"""
    node = (obj.get("data") or {}).get(code) or {}
    d = node.get("data") or {}
    entries_raw = d.get("data") or []
    date = str(d.get("date") or node.get("date") or "").strip()
    entries = []
    for s in entries_raw:
        parts = str(s).split()
        if len(parts) >= 2 and re.fullmatch(r"\d{4}", parts[0]):
            entries.append((parts[0], to_f(parts[1]),
                            to_f(parts[2]) if len(parts) > 2 else None))
    return date, entries


# ---------------------------------------------------------------- 评估（纯函数）

def evaluate_symbol(sym, now, premarket=False, min_time=None, max_age_min=None):
    """就地填充派生字段与 verdict。所有数值判定在此完成，模型只转述。"""
    p, prev, pct = sym["price"], sym["prev_close"], sym["pct_reported"]

    def fail(verdict, line, detail=None):
        sym["verdict"] = verdict
        sym["verdict_line"] = line
        if detail:
            sym["verdict_detail"] = detail
        return sym

    # 1) 关键字段必须可解析
    if p is None or p <= 0 or prev is None or prev <= 0 or pct is None:
        return fail("source_error", LINE_SOURCE_ERROR, "关键字段缺失或非法（现价/昨收/涨跌幅）")

    # 2) 自洽校验：(现价−昨收)/昨收×100 与 fields[32] 偏差 ≤0.2 个百分点
    pct_computed = (p - prev) / prev * 100.0
    dev = abs(pct_computed - pct)
    sym["pct_computed"] = round(pct_computed, 4)
    sym["pct_dev_pp"] = round(dev, 4)
    if dev > 0.2:
        return fail("inconsistent", LINE_SOURCE_ERROR,
                    "自洽校验不过：computed=%.4f reported=%.4f dev=%.4f" % (pct_computed, pct, dev))

    # 通用判定布尔（不受 verdict 影响，供任务直接引用）
    if sym["open"] is not None and sym["open"] > 0:
        d = sym["open"] - prev
        sym["open_direction"] = "up" if d > 0.005 else ("down" if d < -0.005 else "flat")
    else:
        sym["open_direction"] = None
    if sym["limit_up"] is not None and sym["limit_down"] is not None:
        sym["is_limit_up"] = abs(p - sym["limit_up"]) < 0.005
        sym["is_limit_down"] = abs(p - sym["limit_down"]) < 0.005
    else:
        sym["is_limit_up"] = sym["is_limit_down"] = None
    sym["alert_abs"] = abs(pct) > 5
    early = dtime(9, 30) <= now.time() < dtime(10, 0)
    vr_thr = 4 if early else 3  # 早盘量比阈值上调为 4（分母样本极短噪声大）
    sym["alert_vol_threshold"] = vr_thr
    sym["alert_vol"] = (sym["vol_ratio"] is not None and abs(pct) > 3
                        and sym["vol_ratio"] > vr_thr)
    sym["one_word_board"] = (sym["high"] is not None and sym["low"] is not None
                             and abs(sym["high"] - sym["low"]) < 0.005
                             and abs(sym["high"] - p) < 0.005 and abs(pct) > 5)

    # 3) 时间戳类校验（东财备用源无时间戳，跳过，verdict 即 ok 带 limitations）
    ts = sym["_ts_dt"]
    if sym["source"] == "eastmoney":
        sym["verdict"] = "ok"
        sym["verdict_line"] = None
        return sym
    if ts is None:
        return fail("source_error", LINE_SOURCE_ERROR, "快照时间戳缺失或无法解析")

    if ts.date() != now.date():
        if premarket:
            # 盘前任务（08:50）快照必为最近交易日，不按非交易日处理
            sym["snapshot_trade_date"] = sym["ts_date"]
            sym["verdict"] = "ok"
            sym["verdict_line"] = None
            return sym
        return fail("not_trading_day", None,
                    "快照日期 %s 非今日（休市或数据未更新）" % sym["ts_date"])

    age = (now - ts).total_seconds() / 60.0
    sym["ts_age_min"] = round(age, 1)

    # 4) 撮合前守卫（--min-time，竞价结果任务）
    if min_time is not None and ts.time() < min_time:
        return fail("stale_snapshot", LINE_STALE_AUCTION.format(hhmm=ts.strftime("%H:%M")),
                    "快照时间早于 %s" % min_time.strftime("%H:%M"))

    # 5) 停牌形态一：快照冻结（原始钟差，午休不计调整——沿用现行任务语义）
    if max_age_min is not None and age > max_age_min:
        return fail("halt_suspected", LINE_HALT_FROZEN.format(hhmm=ts.strftime("%H:%M")),
                    "frozen_snapshot：快照滞后 %.0f 分钟" % age)

    # 6) 停牌形态二：时间戳新鲜但连续竞价时段零成交且全天无波动、非涨跌停价
    vol, amt = sym["volume_hand"], sym["amount_wan"]
    o, h, l = sym["open"], sym["high"], sym["low"]
    flat = (o is not None and h is not None and l is not None and o > 0
            and abs(h - l) < 0.005 and abs(h - o) < 0.005)
    zero_turnover = (in_continuous(now) and age <= 30
                     and (vol is not None and vol == 0) and (amt is not None and amt == 0)
                     and flat and sym["is_limit_up"] is not None
                     and not sym["is_limit_up"] and not sym["is_limit_down"])
    if zero_turnover:
        return fail("halt_suspected", LINE_HALT_ZERO, "zero_turnover：时间戳更新但零成交")

    # 7) 竞价数据未产生（盘前/竞价初期：现价=昨收、量0）
    if (not premarket and now.time() < dtime(9, 25)
            and vol is not None and vol == 0 and abs(p - prev) < 0.005):
        return fail("no_data_preopen",
                    LINE_NO_DATA_PREOPEN.format(prev=price_str(prev), ts=sym["ts_time"] or ""),
                    "竞价数据尚未产生")

    sym["verdict"] = "ok"
    sym["verdict_line"] = None
    return sym


def evaluate_kline(bars, today):
    """日K派生：close_pct/振幅逐条、近20日日均振幅 A、昨日量、≥5% 漏报对账幅度。"""
    enriched = []
    for i, b in enumerate(bars):
        prev_close = bars[i - 1]["close"] if i > 0 else None
        b2 = dict(b)
        b2["prev_close"] = prev_close
        if prev_close:
            b2["close_pct"] = round((b["close"] - prev_close) / prev_close * 100.0, 2)
            b2["amplitude_pct"] = round((b["high"] - b["low"]) / prev_close * 100.0, 2)
        else:
            b2["close_pct"] = b2["amplitude_pct"] = None
        enriched.append(b2)
    completed = [b for b in enriched if b["date"] != today]
    amps = [b["amplitude_pct"] for b in completed if b["amplitude_pct"] is not None][-20:]
    if len(amps) >= 10:
        a, degraded = round(sum(amps) / len(amps), 2), False
    else:
        a, degraded = 2.0, True  # 降级取常态波动，不告警不阻塞（沿用现行任务语义）
    ge5 = []
    for b in completed:
        if b["prev_close"] is None:
            continue
        pc = b["prev_close"]
        up = round((b["high"] - pc) / pc * 100.0, 2)
        down = round((pc - b["low"]) / pc * 100.0, 2)
        ge5.append({"date": b["date"], "close_pct": b["close_pct"],
                    "up_move": up, "down_move": down,
                    "hit_abs": up >= 5 or down >= 5})
    return {
        "bars": enriched,
        "last_bar_is_today": bool(bars) and bars[-1]["date"] == today,
        "prev_day_volume_hand": completed[-1]["volume_hand"] if completed else None,
        "avg_amplitude_20d": a,
        "a_degraded": degraded,
        "a_source": "degraded_default" if degraded else "kline20",
        "ge5_move": ge5,
    }


def event_anchor_gain(enriched, anchor, price):
    """事件锚点累计涨幅：现价相对「锚点日前一交易日收盘」的涨幅（=自锚点日起持有的
    累计收益）。锚点定位取首个 date≥anchor 的 bar——锚点为休市日时自然落到其后首个
    交易日，其前一根 close 即基准。锚点早于/晚于日K范围、基准或现价缺失 → 数值字段
    置 None 并附 error 说明，任务省略该行即可，不阻塞。"""
    out = {"event_anchor": anchor, "event_anchor_base": None,
           "event_anchor_base_date": None, "event_gain_pct": None,
           "event_anchor_error": None}
    idx = next((i for i, b in enumerate(enriched) if b["date"] >= anchor), None)
    if idx is None:
        out["event_anchor_error"] = "锚点晚于日K最新日期（anchor=%s）" % anchor
        return out
    if idx == 0:
        out["event_anchor_error"] = "锚点早于日K取数范围，基准前收缺失（anchor=%s）" % anchor
        return out
    base = enriched[idx - 1]["close"]
    if not base or base <= 0 or price is None or price <= 0:
        out["event_anchor_error"] = "基准价或现价缺失，无法计算累计涨幅"
        return out
    out["event_anchor_base"] = base
    out["event_anchor_base_date"] = enriched[idx - 1]["date"]
    out["event_gain_pct"] = round((price - base) / base * 100.0, 2)
    return out


def _anchor_kline_days(anchor, now):
    """锚点日所需日K条数估算：自然日差×5/7 + 缓冲（fetch_kline 内再 +4 兜底；
    区间含长假只会多取不会少取）。"""
    diff = (now.date() - datetime.strptime(anchor, "%Y-%m-%d").date()).days
    if diff < 0:
        diff = 0
    return diff * 5 // 7 + 5


def minute_prices(entries):
    """price_1430=精确'1430'（缺失取≤1430最后一条）；price_1500 只认精确'1500'，
    严禁取数组末条（收盘后有 1528/1529/1530 盘后延伸条目）。"""
    price_1430 = None
    for hhmm, px, _ in entries:
        if px is None:
            continue
        if hhmm == "1430":
            price_1430 = px
            break
        if hhmm < "1430":
            price_1430 = px
    price_1500, has_1500 = None, False
    for hhmm, px, _ in entries:
        if hhmm == "1500" and px is not None:
            price_1500, has_1500 = px, True
            break
    return price_1430, price_1500, has_1500


def evaluate_minute_recap(entries, since_str, prev_close, pct_now,
                          alert_abs=False, alert_vol=False):
    """snap --minute-recap：自 since 时分以来的日内区间回放。采样式快照只看采样点
    瞬时涨跌幅，「冲高回落/探底回升」的极值发生在两次采样之间时会整个漏掉
    （如 9:35 冲 +7%、9:42 采样只见 +2.9%），本函数用分时把区间极值捞回来。
    仅统计 since<=hhmm<='1500' 的条目（盘后延伸条目不计）；高低点幅度相对昨收，
    与盘中绝对档及周复盘 ge5 对账同基准。基准/现价缺失或窗口内无条目 → None
    （增强信息，降级不阻塞）。"""
    if prev_close is None or prev_close <= 0 or pct_now is None:
        return None
    since = since_str.replace(":", "")
    hi = lo = None  # (hhmm, price)，首触极值时点
    for hhmm, px, _ in entries:
        if px is None or px <= 0 or not since <= hhmm <= "1500":
            continue
        if hi is None or px > hi[1]:
            hi = (hhmm, px)
        if lo is None or px < lo[1]:
            lo = (hhmm, px)
    if hi is None or lo is None:
        return None

    def _pct(p):
        return round((p - prev_close) / prev_close * 100.0, 2)

    hi_pct, lo_pct = _pct(hi[1]), _pct(lo[1])
    hit_high, hit_low = hi_pct >= 5, lo_pct <= -5
    off_high = round(hi_pct - pct_now, 2)
    off_low = round(pct_now - lo_pct, 2)
    fmt_t = lambda h: h[:2] + ":" + h[2:]
    line = ("日内自%s 高%s(%+.2f%%)@%s 低%s(%+.2f%%)@%s 现%+.2f%% 较高点%+.2fpp 较低点%+.2fpp"
            % (since_str, price_str(hi[1]), hi_pct, fmt_t(hi[0]),
               price_str(lo[1]), lo_pct, fmt_t(lo[0]), pct_now, off_high, off_low))
    return {
        "since": since_str,
        "high_time": fmt_t(hi[0]), "high_price": hi[1], "high_pct": hi_pct,
        "low_time": fmt_t(lo[0]), "low_price": lo[1], "low_pct": lo_pct,
        "off_high_pp": off_high, "off_low_pp": off_low,
        "recap_hit_abs": bool(hit_high or hit_low),
        "hit_side": ("both" if hit_high and hit_low
                     else ("high" if hit_high else ("low" if hit_low else None))),
        # 曾达绝对档幅度而当前快照未触发任何异动档 → 冲高回落/探底回升形态简讯
        "retrace_signal": bool((hit_high or hit_low) and not alert_abs and not alert_vol),
        "recap_line": line,
    }


def evaluate_closing(sym, kres, entries, minute_date, now):
    """minute --closing 合成评估：快照+分时+日K 在代码内合流，不经模型中转。
    沿用收盘竞价任务语义：ts<14:57 未就绪、'1500'须已回补、位移分层档、极值半档。"""
    out = {"mode_extra": "closing"}
    ts = sym.get("_ts_dt")
    if sym.get("verdict") == "inconsistent" or sym.get("verdict") == "source_error":
        return dict(out, verdict="source_error", verdict_line=LINE_CLOSE_FAULT,
                    verdict_detail=sym.get("verdict_detail"))
    if ts is None or ts.date() != now.date():
        return dict(out, verdict="not_trading_day", verdict_line=None,
                    verdict_detail="快照日期非今日")
    if ts.time() < dtime(14, 57):
        return dict(out, verdict="stale_snapshot", verdict_line=LINE_CLOSE_FAULT,
                    verdict_detail="快照时分早于14:57，收盘竞价结果未就绪/快照未更新")
    if not entries or minute_date and minute_date != now.strftime("%Y%m%d"):
        return dict(out, verdict="minute_error", verdict_line=LINE_CLOSE_FAULT,
                    verdict_detail="分时数据缺失或日期非今日")
    price_1430, price_1500, has_1500 = minute_prices(entries)
    if price_1430 is None:
        return dict(out, verdict="minute_error", verdict_line=LINE_CLOSE_FAULT,
                    verdict_detail="分时无14:30基准条目")
    if ts.time() >= dtime(15, 0) and not has_1500:
        return dict(out, verdict="not_ready_1500", verdict_line=LINE_CLOSE_NOT_READY,
                    verdict_detail="快照已过15:00但分时无1500条目（撮合结果未回补）")
    if not has_1500:
        return dict(out, verdict="minute_error", verdict_line=LINE_CLOSE_FAULT,
                    verdict_detail="分时无1500条目")

    p1500 = price_1500
    disp = abs(p1500 - price_1430) / price_1430 * 100.0
    direction = "up" if p1500 > price_1430 else ("down" if p1500 < price_1430 else "flat")
    a = kres["avg_amplitude_20d"]
    tier = 1.0 if a <= 2 else 1.5  # A≤2（含降级取2）→T=1%；A>2→T=1.5%
    snap_price = sym["price"]
    cross_dev = abs(p1500 - snap_price) / snap_price * 100.0
    day_high, day_low = sym.get("high"), sym.get("low")
    at_extreme = ((day_high is not None and abs(p1500 - day_high) < 0.005)
                  or (day_low is not None and abs(p1500 - day_low) < 0.005))
    out.update({
        "price_1430": price_1430,
        "price_1500": p1500,
        "has_1500": True,
        "pct_reported": sym["pct_reported"],
        "pct_close": round((p1500 - sym["prev_close"]) / sym["prev_close"] * 100.0, 2),
        "prev_close": sym["prev_close"],
        "displacement_pct": round(disp, 2),
        "direction": direction,
        "avg_amplitude_20d": a,
        "a_degraded": kres["a_degraded"],
        "tier_used": tier,
        "abs2": disp >= 2.0,                     # 绝对档：位移≥2%，不受分层降权
        "layered": disp >= tier,                 # 分层档
        "extreme_half": at_extreme and disp >= tier / 2.0,  # 极值放宽档
        "is_day_high": bool(day_high is not None and abs(p1500 - day_high) < 0.005),
        "is_day_low": bool(day_low is not None and abs(p1500 - day_low) < 0.005),
        "is_close_limit": bool(
            (sym.get("limit_up") is not None and abs(p1500 - sym["limit_up"]) < 0.005)
            or (sym.get("limit_down") is not None and abs(p1500 - sym["limit_down"]) < 0.005)),
        "cross_dev_pct": round(cross_dev, 3),
        "use_minute": cross_dev > 0.2,           # 分时与快照偏差>0.2% → 以分时为准并标注
        "snapshot_ts": sym["ts_time"],
        "verdict": "ok",
        "verdict_line": None,
    })
    return out


# ---------------------------------------------------------------- 渲染

def fmt_pack(sym):
    pct = sym.get("pct_reported")
    chg = sym.get("change")
    return {
        "price": ("%.2f" % sym["price"]) if sym.get("price") is not None else None,
        "pct": ("%+.2f%%" % pct) if pct is not None else None,
        "change": ("%+.2f" % chg) if chg is not None else None,
        "volume": (num_str(sym["volume_hand"]) + "手")
                  if sym.get("volume_hand") is not None else None,
        "amount": (num_str(sym["amount_wan"]) + "万")
                  if sym.get("amount_wan") is not None else None,
        "vol_ratio": num_str(sym.get("vol_ratio")) or None,
    }


def render_data_line(sym):
    """预格式化数据行：推送必须原样嵌入（含快照时间，不取数不可知——防伪锚点）。"""
    f = fmt_pack(sym)
    parts = []
    if sym.get("source") == "eastmoney":
        parts.append("［备用源］")
    parts.append(sym["code"])
    parts.append(sym.get("name") or sym["code"])
    if f["price"]:
        parts.append(f["price"] + "元")
    if f["pct"]:
        parts.append(f["pct"])
    if sym.get("prev_close") is not None:
        parts.append("昨收" + price_str(sym["prev_close"]))
    if sym.get("open") is not None:
        parts.append("今开" + price_str(sym["open"]))
    if sym.get("high") is not None:
        parts.append("高" + price_str(sym["high"]))
    if sym.get("low") is not None:
        parts.append("低" + price_str(sym["low"]))
    if f["volume"]:
        parts.append("量" + f["volume"])
    if f["amount"]:
        parts.append("额" + f["amount"])
    if sym.get("turnover_pct") is not None:
        parts.append("换手" + num_str(sym["turnover_pct"]) + "%")
    if f["vol_ratio"]:
        parts.append("量比" + f["vol_ratio"])
    if sym.get("limit_up") is not None:
        parts.append("涨停" + price_str(sym["limit_up"]))
    if sym.get("limit_down") is not None:
        parts.append("跌停" + price_str(sym["limit_down"]))
    if sym.get("ts_date"):
        parts.append("快照%s %s" % (sym["ts_date"], sym["ts_time"] or ""))
    else:
        parts.append("快照缺失")
    return " ".join(parts)


def render_compare_line(by_code):
    """对照行：上证/招商港口/上港集团/五洲交通（缺失的跳过）。"""
    order = [("sh000001", "上证"), ("sz001872", "招商港口"),
             ("sh600018", "上港集团"), ("sh600368", "五洲交通")]
    parts = []
    for code, label in order:
        s = by_code.get(code)
        if s and s.get("pct_reported") is not None:
            parts.append(label + "%+.2f%%" % s["pct_reported"])
    return " ".join(parts)


# ---------------------------------------------------------------- 网络封装

def fetch_tencent_snapshot(symbols):
    text = decode_tencent(http_get(TENCENT_SNAPSHOT_URL.format(symbols=",".join(symbols))))
    return text, parse_tencent_rows(text)


def fetch_eastmoney_symbol(code):
    secid = ("0." if code.startswith("sz") else "1.") + code[2:]
    body = http_get(EASTMONEY_URL.format(secid=secid),
                    headers={"Referer": "https://quote.eastmoney.com/"})
    obj = json.loads(body.decode("utf-8", errors="replace"))
    data = obj.get("data") or {}
    if not data:
        raise ValueError("eastmoney 返回空 data")
    return data


def fetch_kline(code, count):
    body = http_get(TENCENT_KLINE_URL.format(code=code, count=count))
    obj = json.loads(body.decode("utf-8", errors="replace"))
    bars = parse_kline_bars(obj, code)
    if not bars:
        raise ValueError("日K返回空数据")
    return bars


def fetch_minute(code):
    body = http_get(TENCENT_MINUTE_URL.format(code=code))
    obj = json.loads(body.decode("utf-8", errors="replace"))
    return parse_minute(obj, code)


def attach_kline(stock, code, days, now, raw_store, anchor=None):
    """snap --with-kline/--event-anchor：A/昨日量/竞价量占比/事件锚点涨幅附到个股
    符号上；日K失败降级不阻塞（锚点字段同样置 None 附 error，任务省略该行即可）。"""
    try:
        bars = fetch_kline(code, days + 4)
        kres = evaluate_kline(bars, now.strftime("%Y-%m-%d"))
        raw_store["kline_bars"] = len(bars)
    except Exception as e:
        stock["kline_error"] = str(e)
        kres = {"avg_amplitude_20d": 2.0, "a_degraded": True, "a_source": "degraded_default",
                "prev_day_volume_hand": None, "ge5_move": [], "bars": [],
                "last_bar_is_today": None}
        if anchor:
            stock.update({"event_anchor": anchor, "event_anchor_base": None,
                          "event_anchor_base_date": None, "event_gain_pct": None,
                          "event_anchor_error": "日K获取失败，锚点基准缺失"})
    else:
        if anchor:
            stock.update(event_anchor_gain(kres["bars"], anchor, stock.get("price")))
    stock["avg_amplitude_20d"] = kres["avg_amplitude_20d"]
    stock["a_degraded"] = kres["a_degraded"]
    stock["a_source"] = kres["a_source"]
    stock["prev_day_volume_hand"] = kres["prev_day_volume_hand"]
    vol, pvol = stock.get("volume_hand"), kres["prev_day_volume_hand"]
    stock["auction_vol_ratio_pct"] = (round(vol / pvol * 100.0, 1)
                                      if vol and pvol else None)
    if stock.get("alert_vol") and kres["avg_amplitude_20d"] > 2:
        stock["vol_alert_downgrade"] = True  # 高波动环境：价量共振档降为一行简讯
    else:
        stock["vol_alert_downgrade"] = False
    return kres


def attach_minute_recap(stock, code, since_str, now, raw):
    """snap --minute-recap：主标的（首个符号）分时区间回放附到个股符号上。
    分时请求失败/日期非今日/窗口内无条目 → minute_recap=None 附 error 说明，
    降级不阻塞（增强信息，与日K降级同语义——快照本身的 verdict 不受影响）。"""
    recap, err = None, None
    try:
        body = http_get(TENCENT_MINUTE_URL.format(code=code))
        mdate, entries = parse_minute(json.loads(body.decode("utf-8", errors="replace")),
                                      code)
        raw["minute"] = {"date": mdate, "entries": len(entries)}  # 概要入审计
        if mdate and mdate != now.strftime("%Y%m%d"):
            raise ValueError("分时日期非今日（%s）" % mdate)
        recap = evaluate_minute_recap(entries, since_str, stock.get("prev_close"),
                                      stock.get("pct_reported"),
                                      alert_abs=bool(stock.get("alert_abs")),
                                      alert_vol=bool(stock.get("alert_vol")))
        if recap is None:
            raise ValueError("窗口内无有效分时条目或基准缺失")
    except Exception as e:
        recap, err = None, str(e)
    if recap is not None:
        stock["minute_recap"] = recap
    else:
        stock["minute_recap"] = None
        stock["minute_recap_error"] = err


# ---------------------------------------------------------------- 模式实现

def mode_snap(args):
    # 兼容两种传参：snap sz000582 sh000001 … 与 snap sz000582,sh000001,…
    symbols = [c for a in args.symbols for c in a.split(",") if c]
    args.symbols = symbols
    now = now_bj()
    opts = dict(premarket=args.premarket,
                min_time=(datetime.strptime(args.min_time, "%H:%M").time()
                          if args.min_time else None),
                max_age_min=args.max_age_min)
    raw = {}
    rows, fetch_err = {}, None
    try:
        text, rows = fetch_tencent_snapshot(args.symbols)
        raw["tencent"] = text
    except Exception as e:
        fetch_err = str(e)

    symbols_out = []
    for code in args.symbols:
        sym = None
        flds = rows.get(code)
        if flds:
            sym = symbol_from_tencent(code, flds)
            evaluate_symbol(sym, now, **opts)
        # 备用源自动切换：腾讯请求失败 / 解析失败 / 自洽不过（与现行任务触发条件一致；
        # stale/no_data/not_trading_day 不触发切换）
        if sym is None or sym["verdict"] in ("source_error", "inconsistent"):
            reason = "tencent_request_failed" if sym is None else "tencent_" + sym["verdict"]
            fb = None
            try:
                data = fetch_eastmoney_symbol(code)
                fb = symbol_from_eastmoney(code, data)
                evaluate_symbol(fb, now, **opts)
                raw.setdefault("eastmoney", {})[code] = data
            except Exception as e:
                raw.setdefault("eastmoney_errors", {})[code] = str(e)
            if fb is not None and fb["verdict"] == "ok":
                fb["fallback_reason"] = reason
                fb["source_notice"] = LINE_SOURCE_NOTICE
                sym = fb
            elif sym is None:
                sym = {"code": code, "name": SYMBOL_LABELS.get(code, code), "source": "none",
                       "verdict": "source_error", "verdict_line": LINE_SOURCE_ERROR,
                       "error": fetch_err or "腾讯返回中无该标的行"}
            else:
                sym["verdict"] = "source_error"
                sym["verdict_line"] = LINE_SOURCE_ERROR
                sym["fallback_attempted"] = reason
        symbols_out.append(sym)

    by_code = {s["code"]: s for s in symbols_out}
    stock = symbols_out[0]

    # 多标的对照派生（intraday 任务用；仅当对照标的在场时给出）
    idx = by_code.get("sh000001")
    zhao, shang, wu = by_code.get("sz001872"), by_code.get("sh600018"), by_code.get("sh600368")
    if idx is not None and idx.get("pct_reported") is not None \
            and stock.get("pct_reported") is not None:
        stock["excess_vs_index_pp"] = round(
            stock["pct_reported"] - idx["pct_reported"], 2)
    if zhao and shang:
        same_dir = (sign(zhao.get("pct_reported")) == sign(shang.get("pct_reported")) != 0)
        stock["sector_active"] = bool(
            same_dir and abs(zhao.get("pct_reported") or 0) >= 2
            and abs(shang.get("pct_reported") or 0) >= 2)
    if wu:
        stock["chain_active"] = bool(abs(wu.get("pct_reported") or 0) >= 3)
    if idx:
        stock["index_abs_ge1"] = bool(abs(idx.get("pct_reported") or 0) >= 1)
        stock["index_abs_lt1"] = bool(abs(idx.get("pct_reported") or 0) < 1)
        if zhao:
            stock["index_same_dir_ge1"] = bool(
                stock.get("sector_active")
                and sign(idx.get("pct_reported")) == sign(zhao.get("pct_reported")) != 0
                and stock["index_abs_ge1"])

    kres = None
    if args.with_kline or args.event_anchor:
        kdays = args.with_kline or 0
        if args.event_anchor:
            kdays = max(kdays, _anchor_kline_days(args.event_anchor, now))
        kres = attach_kline(stock, stock["code"], kdays, now, raw,
                            anchor=args.event_anchor)

    # 分时区间回放：仅在主标的 verdict=ok 时取（其余分支任务在 verdict 处已退出，
    # 不必多打一次分时请求）
    if args.minute_recap and stock["verdict"] == "ok":
        attach_minute_recap(stock, stock["code"], args.minute_recap, now, raw)

    for s in symbols_out:
        s["data_line"] = render_data_line(s)
        s["fmt"] = fmt_pack(s)

    result = {
        "mode": "snap",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "server_time": now.strftime("%H:%M:%S"),
        "phase": market_phase(now),
        "in_early_session": dtime(9, 30) <= now.time() < dtime(10, 0),
        "in_tail_session": now.time() >= dtime(14, 45),
        "verdict": stock["verdict"],
        "symbols": symbols_out,
    }
    if len(symbols_out) > 1:
        result["compare_line"] = render_compare_line(by_code)
    if stock.get("ts_date"):
        result["stamp_line"] = "【%s %s】" % (stock["ts_date"], (stock["ts_time"] or "")[:5])
    out_json(result)
    audit_append({"ts": result["generated_at"], "host": socket.gethostname(),
                  "mode": "snap", "argv": sys.argv[1:], "verdicts":
                  {s["code"]: s["verdict"] for s in symbols_out},
                  "raw": raw, "parsed": symbols_out})
    return 0


def _date_range(args, now):
    """kline 日期过滤窗口：--since / --this-week / --this-month / --prev-month。"""
    if args.since:
        return args.since, None
    if args.this_week:
        monday = (now - timedelta(days=now.weekday())).date()
        return monday.strftime("%Y-%m-%d"), None
    if args.this_month:
        return now.replace(day=1).strftime("%Y-%m-%d"), None
    if args.prev_month:
        first = now.replace(day=1).date()
        prev_end = first - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        return prev_start.strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d")
    return None, None


def mode_kline(args):
    now = now_bj()
    today = now.strftime("%Y-%m-%d")
    result = {"mode": "kline", "symbol": args.symbol,
              "generated_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    raw = ""
    try:
        body = http_get(TENCENT_KLINE_URL.format(code=args.symbol, count=args.days + 4))
        raw = body.decode("utf-8", errors="replace")
        bars = parse_kline_bars(json.loads(raw), args.symbol)
        if not bars:
            raise ValueError("日K返回空数据")
    except Exception as e:
        result.update({"verdict": "kline_error", "verdict_line": LINE_SOURCE_ERROR,
                       "error": str(e)})
        out_json(result)
        audit_append({"ts": result["generated_at"], "host": socket.gethostname(),
                      "mode": "kline", "argv": sys.argv[1:], "verdict": "kline_error",
                      "raw": raw, "parsed": None})
        return 0
    kres = evaluate_kline(bars, today)
    start, end = _date_range(args, now)
    if start:
        kres["bars"] = [b for b in kres["bars"] if start <= b["date"] <= (end or "9999-12-31")]
        kres["ge5_move"] = [g for g in kres["ge5_move"] if start <= g["date"] <= (end or "9999-12-31")]
        result["filter"] = {"start": start, "end": end}
    kres.pop("prev_close", None)
    result.update({"verdict": "ok", "today": today}, **kres)
    out_json(result)
    audit_kline_parsed = {k: v for k, v in kres.items() if k != "bars"}
    audit_kline_parsed["bar_count"] = len(kres["bars"])
    audit_append({"ts": result["generated_at"], "host": socket.gethostname(),
                  "mode": "kline", "argv": sys.argv[1:], "verdict": "ok",
                  "raw": raw, "parsed": audit_kline_parsed})
    return 0


def mode_minute(args):
    now = now_bj()
    today_compact = now.strftime("%Y%m%d")
    result = {"mode": "minute", "symbol": args.symbol,
              "generated_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    raw = {}
    snap = None
    if args.closing:
        # 合成模式：快照+分时+日K 全部在代码内取齐（不经模型中转）；
        # 收盘任务不设备用源（分时依赖，沿用现行设计）。
        # 直接调 http_get 而非封装函数：原始响应体须完整入审计日志（对账真值）
        try:
            text = decode_tencent(
                http_get(TENCENT_SNAPSHOT_URL.format(symbols=args.symbol)))
            raw["tencent"] = text
            flds = parse_tencent_rows(text).get(args.symbol)
            if not flds:
                raise ValueError("快照返回中无该标的行")
            snap = symbol_from_tencent(args.symbol, flds)
            evaluate_symbol(snap, now)
        except Exception as e:
            raw["snap_error"] = str(e)
        kres = None
        try:
            raw["kline"] = http_get(
                TENCENT_KLINE_URL.format(code=args.symbol, count=25)
            ).decode("utf-8", errors="replace")
            bars = parse_kline_bars(json.loads(raw["kline"]), args.symbol)
            if not bars:
                raise ValueError("日K返回空数据")
            kres = evaluate_kline(bars, now.strftime("%Y-%m-%d"))
            raw["kline_bars"] = len(bars)
        except Exception as e:
            raw["kline_error"] = str(e)
            kres = {"avg_amplitude_20d": 2.0, "a_degraded": True,
                    "a_source": "degraded_default"}
    try:
        raw["minute"] = http_get(
            TENCENT_MINUTE_URL.format(code=args.symbol)
        ).decode("utf-8", errors="replace")
        mdate, entries = parse_minute(json.loads(raw["minute"]), args.symbol)
        raw["minute_date"] = mdate
        raw["minute_entries"] = len(entries)
    except Exception as e:
        mdate, entries = "", []
        raw["minute_error"] = str(e)

    if args.closing:
        if snap is None:
            closing = {"verdict": "source_error", "verdict_line": LINE_CLOSE_FAULT}
        else:
            closing = evaluate_closing(snap, kres, entries, mdate, now)
        result["snapshot"] = {k: v for k, v in (snap or {}).items()
                              if not k.startswith("_")} if snap else None
        if snap:
            result["snapshot"]["data_line"] = render_data_line(snap)
            result["snapshot"]["fmt"] = fmt_pack(snap)
        result.update({k: v for k, v in closing.items() if k != "mode_extra"})
        if closing.get("verdict") == "ok":
            d = "拉升" if closing["direction"] == "up" else (
                "回落" if closing["direction"] == "down" else "持平")
            result["data_line"] = (
                "%s %s 收盘(分时1500)%s元 %s 14:30基准%s 位移%.2f%%%s A=%.2f 档位%s%% 快照%s %s"
                % (args.symbol, result["snapshot"]["name"],
                   price_str(closing["price_1500"]),
                   result["snapshot"]["fmt"]["pct"],
                   price_str(closing["price_1430"]),
                   closing["displacement_pct"], d,
                   closing["avg_amplitude_20d"],
                   num_str(closing["tier_used"]),
                   snap["ts_date"], snap["ts_time"] or ""))
    else:
        price_1430, price_1500, has_1500 = minute_prices(entries)
        result.update({"date": mdate, "price_1430": price_1430,
                       "price_1500": price_1500, "has_1500": has_1500,
                       "entry_count": len(entries),
                       "verdict": ("minute_error" if not entries else "ok"),
                       "verdict_line": (LINE_SOURCE_ERROR if not entries else None)})
    out_json(result)
    audit_append({"ts": result["generated_at"], "host": socket.gethostname(),
                  "mode": "minute" + ("--closing" if args.closing else ""),
                  "argv": sys.argv[1:], "verdict": result.get("verdict"),
                  "raw": raw,
                  "parsed": {k: v for k, v in result.items()
                             if k in ("price_1430", "price_1500", "has_1500",
                                      "displacement_pct", "direction", "verdict")}})
    return 0


def audit_aggregate(rec, days, start=None):
    """单条审计记录聚合进 days{date: slot}；日期缺失/早于 start 返回 False 不计入。
    verdicts 展平：snap 记录只有顶层 verdicts dict（{code: verdict}），直接 str(dict)
    会产出整串不可读 key——逐标的计成 "snap:sz000582=ok" 形态；其余记 "mode:verdict"。"""
    d = str(rec.get("ts") or "")[:10]
    if not d or (start and d < start):
        return False
    slot = days.setdefault(d, {"date": d, "runs": 0, "modes": {},
                               "verdicts": {}, "snap_points": []})
    slot["runs"] += 1
    m = str(rec.get("mode") or "?")
    slot["modes"][m] = slot["modes"].get(m, 0) + 1
    v_raw = rec.get("verdict") or rec.get("verdicts") or "?"
    keys = (["%s:%s=%s" % (m, code, cv) for code, cv in sorted(v_raw.items())]
            if isinstance(v_raw, dict) else ["%s:%s" % (m, v_raw)])
    for key in keys:
        slot["verdicts"][key] = slot["verdicts"].get(key, 0) + 1
    if m.startswith("snap") and isinstance(rec.get("parsed"), list):
        for s in rec["parsed"]:
            if isinstance(s, dict) and s.get("code") == "sz000582":
                slot["snap_points"].append({
                    "ts": s.get("ts_date", "") + " " + (s.get("ts_time") or ""),
                    "price": s.get("price"), "pct": s.get("pct_reported"),
                    "run_at": str(rec.get("ts") or "")[11:19]})
                break
    return True


def mode_audit(args):
    now = now_bj()
    files = audit_files()
    if not files:
        out_json({"mode": "audit", "verdict": "no_data",
                  "note": "审计日志尚不存在：%s" % AUDIT_FILE})
        return 0
    start = None
    if args.this_week:
        monday = (now - timedelta(days=now.weekday())).date()
        start = monday.strftime("%Y-%m-%d")
    days = {}
    for path in files:  # 两代合并（旧代在前），轮转不丢历史
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                audit_aggregate(rec, days, start)
    out_json({"mode": "audit", "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
              "filter_from": start, "days": sorted(days.values(), key=lambda x: x["date"]),
              "note": "只读汇总：与 hermes 推送记录并列陈列做事实对账，不做任何推算"})
    return 0


# ---------------------------------------------------------------- selftest（离线）

def _tf(price="10.81", prev="10.78", open_="10.83", high="10.85", low="10.72",
        vol="14475", amt="1561.2", turnover="0.06", limit_up="11.86", limit_down="9.70",
        volratio="1.56", ts="20260827093503", pct=None, code="sz000582", stock_name="北部湾港"):
    """构造腾讯快照 fields（保证索引正确），pct 缺省按自洽计算。"""
    f = [""] * 55
    f[F_NAME], f[F_CODE] = stock_name, code
    f[F_PRICE], f[F_PREV], f[F_OPEN] = price, prev, open_
    f[F_TS] = ts
    try:
        p, pv = float(price), float(prev)
        f[F_CHANGE] = "%+.2f" % (p - pv)
        f[F_PCT] = pct if pct is not None else "%+.2f" % ((p - pv) / pv * 100)
    except (TypeError, ValueError):
        f[F_CHANGE], f[F_PCT] = "", (pct if pct is not None else "")
    f[F_HIGH], f[F_LOW] = high, low
    f[F_VOL], f[F_AMT] = vol, amt
    f[F_TURNOVER], f[F_AMPLITUDE] = turnover, "1.20"
    f[F_LIMIT_UP], f[F_LIMIT_DOWN], f[F_VOLRATIO] = limit_up, limit_down, volratio
    return f


def _wrap(fields, code="sz000582"):
    return 'v_%s="%s";' % (code, "~".join(fields))


def _mk_bars(n, today, close=10.0, span=0.2, last_vol=20000):
    """n 条已完成日K（每条振幅 = span/close*100），外加今日残条。"""
    bars = []
    d = datetime(2026, 7, 31)
    added = 0
    while added < n:
        if d.weekday() < 5:
            bars.append({"date": d.strftime("%Y-%m-%d"), "open": close, "close": close,
                         "high": close + span, "low": close, "volume_hand": 10000.0})
            added += 1
        d += timedelta(days=1)
    bars.append({"date": today, "open": close, "close": close + 0.05,
                 "high": close + 0.1, "low": close, "volume_hand": 5000.0})
    if last_vol != 10000.0:
        bars[-2]["volume_hand"] = last_vol
    return bars


def _run_selftest():
    now_intr = datetime(2026, 8, 27, 10, 5, 0)
    now_pre = datetime(2026, 8, 27, 9, 16, 0)
    now_early = datetime(2026, 8, 27, 9, 45, 0)
    now_close = datetime(2026, 8, 27, 15, 2, 0)
    today = "2026-08-27"
    fails = []
    passed = [0]

    def check(name, cond):
        if cond:
            passed[0] += 1
        else:
            fails.append(name)

    # 1) 真实形态快照：解析+自洽+ok+data_line
    sym = symbol_from_tencent("sz000582", _tf())
    evaluate_symbol(sym, now_intr)
    check("snap_ok", sym["verdict"] == "ok")
    check("snap_price", sym["price"] == 10.81 and sym["prev_close"] == 10.78)
    check("snap_dev", sym["pct_dev_pp"] < 0.2)
    dl = render_data_line(sym)
    check("data_line_ts", "快照2026-08-27 09:35:03" in dl and "北部湾港" in dl)
    check("data_line_limit", "涨停11.86" in dl and "量比1.56" in dl)

    # 2) GBK 解码路径
    text = _wrap(_tf(stock_name="北部湾港"))
    decoded = decode_tencent(text.encode("gbk"))
    rows = parse_tencent_rows(decoded)
    check("gbk_decode", rows.get("sz000582") is not None
          and rows["sz000582"][F_NAME] == "北部湾港")

    # 3) 多标的乱序+额外行：按行首代码匹配
    multi = "\n".join([
        _wrap(_tf(price="3.10", prev="3.05", code="sh600018", stock_name="上港集团"), "sh600018"),
        _wrap(_tf(), "sz000582"),
        _wrap(_tf(price="3100.5", prev="3080.2", code="sh000001", stock_name="上证指数",
                  limit_up="", limit_down="", volratio=""), "sh000001"),
        _wrap(_tf(price="8.8", prev="8.7", code="sz000001", stock_name="平安银行"), "sz000001"),
    ])
    rows = parse_tencent_rows(multi)
    check("row_match", rows["sh600018"][F_PRICE] == "3.10"
          and rows["sz000582"][F_PRICE] == "10.81"
          and rows["sh000001"][F_PRICE] == "3100.5"
          and "sz000001" in rows)

    # 4) 盘前竞价未产生：no_data_preopen
    sym = symbol_from_tencent("sz000582", _tf(price="10.78", open_="", high="", low="",
                                              vol="0", amt="0", volratio="",
                                              limit_up="11.86", limit_down="9.70",
                                              ts="20260827091100"))
    evaluate_symbol(sym, now_pre)
    check("no_data_preopen", sym["verdict"] == "no_data_preopen"
          and "竞价数据尚未产生" in (sym["verdict_line"] or ""))

    # 5) 时间戳为昨日：not_trading_day
    sym = symbol_from_tencent("sz000582", _tf(ts="20260826160000"))
    evaluate_symbol(sym, now_intr)
    check("not_trading_day", sym["verdict"] == "not_trading_day")

    # 5b) --premarket：ts 为昨日也判 ok 并给出锚点日期
    sym = symbol_from_tencent("sz000582", _tf(ts="20260826160000"))
    evaluate_symbol(sym, datetime(2026, 8, 27, 8, 50), premarket=True)
    check("premarket_ok", sym["verdict"] == "ok"
          and sym.get("snapshot_trade_date") == "2026-08-26")

    # 6) 自洽不过：inconsistent
    sym = symbol_from_tencent("sz000582", _tf(pct="+0.78"))
    evaluate_symbol(sym, now_intr)
    check("inconsistent", sym["verdict"] == "inconsistent"
          and sym["pct_dev_pp"] > 0.2)

    # 7) 东财备用源：÷100 换算 + limitations + data_line 标注
    em = symbol_from_eastmoney("sz000582", {
        "f43": 1081, "f44": 1085, "f45": 1072, "f46": 1083, "f47": 14475,
        "f48": 156100000, "f58": "北部湾港", "f60": 1078, "f170": 28})
    evaluate_symbol(em, now_intr)
    check("em_scale", em["price"] == 10.81 and em["pct_reported"] == 0.28
          and em["volume_hand"] == 14475 and em["amount_wan"] == 15610.0)
    check("em_ok_limited", em["verdict"] == "ok" and "no_timestamp" in em["limitations"])
    emdl = render_data_line(em)
    check("em_line", emdl.startswith("［备用源］") and "快照缺失" in emdl
          and "涨停" not in emdl and "量比" not in emdl)

    # 8) 异动档位：|pct|>5 绝对档；|pct|>3+量比 早盘阈值4/常规3（严格大于）
    sym = symbol_from_tencent("sz000582", _tf(price="11.32", pct="+5.05", volratio="2.0"))
    evaluate_symbol(sym, now_intr)
    check("alert_abs", sym["alert_abs"] is True and sym["alert_vol"] is False)
    sym = symbol_from_tencent("sz000582", _tf(price="11.12", pct="+3.20", volratio="3.50"))
    evaluate_symbol(sym, now_early)
    check("early_volratio_4", sym["alert_vol"] is False)  # 3.50 ≤ 4
    evaluate_symbol(sym, now_intr)
    check("normal_volratio_3", sym["alert_vol"] is True)  # 3.50 > 3
    sym = symbol_from_tencent("sz000582", _tf(price="11.32", pct="+5.00"))
    evaluate_symbol(sym, now_intr)
    check("strict_gt5", sym["alert_abs"] is False)

    # 9) 一字板：高=低=现价 且 |pct|>5
    sym = symbol_from_tencent("sz000582", _tf(price="11.86", high="11.86", low="11.86",
                                              pct="+10.02"))
    evaluate_symbol(sym, now_intr)
    check("one_word_board", sym["one_word_board"] is True
          and sym["is_limit_up"] is True)

    # 9b) 涨跌停价仅一侧可解析：不崩溃、两判定均 None（limit_down 为空曾致 TypeError）
    sylim = symbol_from_tencent("sz000582", _tf(limit_up="11.86", limit_down=""))
    evaluate_symbol(sylim, now_intr)
    check("limit_one_side_none", sylim["verdict"] == "ok"
          and sylim["is_limit_up"] is None and sylim["is_limit_down"] is None)

    # 10) 停牌形态一：快照冻结 >30 分钟（--max-age-min 30）
    sym = symbol_from_tencent("sz000582", _tf(ts="20260827093503"))
    evaluate_symbol(sym, datetime(2026, 8, 27, 10, 20, 0), max_age_min=30)
    check("halt_frozen", sym["verdict"] == "halt_suspected"
          and "疑似盘中临时停牌" in sym["verdict_line"])

    # 11) 停牌形态二：时间戳新鲜但零成交无波动、非涨跌停
    sym = symbol_from_tencent("sz000582", _tf(price="10.78", open_="10.78", high="10.78",
                                              low="10.78", vol="0", amt="0",
                                              ts="20260827100400"))
    evaluate_symbol(sym, now_intr)
    check("halt_zero", sym["verdict"] == "halt_suspected"
          and "零成交" in sym["verdict_line"])
    sym2 = symbol_from_tencent("sz000582", _tf(price="10.78", open_="10.78", high="10.78",
                                               low="10.78", vol="0", amt="0.5",
                                               ts="20260827100400"))
    evaluate_symbol(sym2, now_intr)
    check("halt_zero_neg", sym2["verdict"] == "ok")  # 成交额>0 → 低流动性而非停牌

    # 12) 撮合前守卫 --min-time 09:24
    sym = symbol_from_tencent("sz000582", _tf(ts="20260827091600"))
    evaluate_symbol(sym, datetime(2026, 8, 27, 9, 25, 0), min_time=dtime(9, 24))
    check("stale_snapshot", sym["verdict"] == "stale_snapshot"
          and "早于9:25撮合" in sym["verdict_line"])

    # 13) 板块/产业链/大盘布尔与对照行（fixture 价格与涨跌幅自洽）
    by = {"sz000582": symbol_from_tencent("sz000582", _tf(price="10.91", pct="+1.20")),
          "sh000001": symbol_from_tencent("sh000001", _tf(price="3100.5", prev="3090.2",
                                                          pct="+0.33", code="sh000001",
                                                          stock_name="上证指数")),
          "sz001872": symbol_from_tencent("sz001872", _tf(price="10.0", prev="9.77",
                                                          pct="+2.35", code="sz001872",
                                                          stock_name="招商港口")),
          "sh600018": symbol_from_tencent("sh600018", _tf(price="3.10", prev="3.03",
                                                          pct="+2.31", code="sh600018",
                                                          stock_name="上港集团")),
          "sh600368": symbol_from_tencent("sh600368", _tf(price="6.20", prev="5.99",
                                                          pct="+3.51", code="sh600368",
                                                          stock_name="五洲交通"))}
    for s in by.values():
        evaluate_symbol(s, now_intr)
    zhao, shang, wu, idx = by["sz001872"], by["sh600018"], by["sh600368"], by["sh000001"]
    same = sign(zhao["pct_reported"]) == sign(shang["pct_reported"]) != 0
    sector = same and abs(zhao["pct_reported"]) >= 2 and abs(shang["pct_reported"]) >= 2
    chain = abs(wu["pct_reported"]) >= 3
    idx_ge1 = abs(idx["pct_reported"]) >= 1
    check("sector_bool", sector is True and chain is True and idx_ge1 is False)
    cl = render_compare_line(by)
    check("compare_line", "招商港口+2.35%" in cl and "上证+0.33%" in cl
          and "五洲交通+3.51%" in cl)

    # 14) 日K：A 计算、昨日量跳过今日残条、降级、close_pct
    kres = evaluate_kline(_mk_bars(22, today, close=10.0, span=0.2, last_vol=20000), today)
    check("kline_A", kres["avg_amplitude_20d"] == 2.0 and kres["a_degraded"] is False)
    check("kline_prev_vol", kres["prev_day_volume_hand"] == 20000.0
          and kres["last_bar_is_today"] is True)
    check("kline_close_pct", kres["bars"][1]["close_pct"] == 0.0
          and kres["bars"][0]["close_pct"] is None)
    kres8 = evaluate_kline(_mk_bars(8, today), today)
    check("kline_degraded", kres8["avg_amplitude_20d"] == 2.0 and kres8["a_degraded"] is True)

    # 14b) 事件锚点累计涨幅：休市日锚点顺延、锚点恰为交易日同基准、
    #      锚点早于/晚于日K范围、现价缺失（fixture 全部 close=10.0、自 7/31 起）
    ebars = evaluate_kline(_mk_bars(22, today), today)["bars"]
    eg = event_anchor_gain(ebars, "2026-08-01", 11.0)  # 8/1 周六 → 定位 8/3，基准 7/31
    check("anchor_weekend", eg["event_anchor_base"] == 10.0
          and eg["event_anchor_base_date"] == "2026-07-31"
          and eg["event_gain_pct"] == 10.0 and eg["event_anchor_error"] is None)
    eg2 = event_anchor_gain(ebars, "2026-08-03", 11.0)  # 锚点恰为交易日：基准同为其前收
    check("anchor_tradeday", eg2["event_anchor_base_date"] == "2026-07-31"
          and eg2["event_gain_pct"] == 10.0)
    eg3 = event_anchor_gain(ebars, "2026-07-01", 11.0)  # 早于首条 7/31：无前收可作基准
    check("anchor_too_early", eg3["event_gain_pct"] is None and eg3["event_anchor_error"])
    eg4 = event_anchor_gain(ebars, "2026-09-15", 11.0)  # 晚于全部日K日期
    check("anchor_future", eg4["event_gain_pct"] is None and eg4["event_anchor_error"])
    eg5 = event_anchor_gain(ebars, "2026-08-01", None)  # 现价缺失（停牌/关键字段缺失形态）
    check("anchor_no_price", eg5["event_gain_pct"] is None and eg5["event_anchor_error"])

    # 15) 分时：1500 精确匹配（不取末条）、1430 缺失回退、位移
    entries = [("1425", 10.94, 100), ("1428", 10.96, 110), ("1430", 10.95, 120),
               ("1459", 10.99, 130), ("1500", 11.02, 140), ("1528", 11.03, 145),
               ("1529", 11.03, 146), ("1530", 11.03, 147)]
    p1430, p1500, has = minute_prices(entries)
    check("minute_1500_exact", p1500 == 11.02 and has is True)
    check("minute_1430", p1430 == 10.95)
    p1430b, _, _ = minute_prices([e for e in entries if e[0] != "1430"])
    check("minute_1430_fallback", p1430b == 10.96)

    # 15b) 分钟回放：窗口过滤（since 前条目忽略、盘后延伸不计）、高低点/现价距离、
    #      绝对档触及与 retrace_signal、快照已触发异动时信号关闭、降级路径
    mentries = [("0929", 10.80, 5), ("0930", 10.83, 10), ("0935", 11.53, 120),
                ("0942", 10.95, 200), ("1130", 11.00, 300), ("1500", 11.02, 400),
                ("1528", 11.60, 410)]
    mr = evaluate_minute_recap(mentries, "09:30", 10.78, 2.77)
    check("recap_window", mr["high_price"] == 11.53 and mr["high_time"] == "09:35"
          and mr["low_price"] == 10.83 and mr["low_time"] == "09:30")
    check("recap_pct", mr["high_pct"] == 6.96 and mr["low_pct"] == 0.46
          and mr["off_high_pp"] == 4.19 and mr["off_low_pp"] == 2.31)
    check("recap_hit", mr["recap_hit_abs"] is True and mr["hit_side"] == "high"
          and mr["retrace_signal"] is True)
    mr2 = evaluate_minute_recap(mentries, "09:30", 10.78, 6.50,
                                alert_abs=True, alert_vol=False)
    check("recap_signal_off_when_alert", mr2["retrace_signal"] is False
          and mr2["recap_hit_abs"] is True)
    calm = [("0930", 10.83, 10), ("0942", 10.95, 200), ("1100", 10.90, 260)]
    mr3 = evaluate_minute_recap(calm, "09:30", 10.78, 1.58)
    check("recap_no_hit", mr3["recap_hit_abs"] is False
          and mr3["hit_side"] is None and mr3["retrace_signal"] is False)
    check("recap_line", "09:35" in mr["recap_line"] and "+6.96%" in mr["recap_line"]
          and "11.53" in mr["recap_line"])
    check("recap_degrade",
          evaluate_minute_recap(mentries, "09:30", None, 2.77) is None
          and evaluate_minute_recap([("0925", 10.8, 1)], "09:30", 10.78, 2.77) is None)

    # 16) 收盘合成：位移/分层档/极值档/分时为准
    snap = symbol_from_tencent("sz000582", _tf(price="11.02", open_="10.83", high="11.05",
                                               low="10.70", ts="20260827150003"))
    evaluate_symbol(snap, now_close)
    c = evaluate_closing(snap, kres, entries, "20260827", now_close)
    check("closing_ok", c["verdict"] == "ok")
    check("closing_disp", c["displacement_pct"] == 0.64 and c["direction"] == "up")
    check("closing_tier", c["tier_used"] == 1.0 and c["abs2"] is False
          and c["layered"] is False and c["extreme_half"] is False)
    snap2 = symbol_from_tencent("sz000582", _tf(price="11.02", high="11.05", low="10.70",
                                                ts="20260827150003"))
    evaluate_symbol(snap2, now_close)
    c2 = evaluate_closing(snap2, kres,
                          [("1425", 10.75, 100), ("1430", 10.75, 110), ("1500", 11.02, 140)],
                          "20260827", now_close)
    check("closing_abs2", c2["abs2"] is True and c2["displacement_pct"] == 2.51)
    snap3 = symbol_from_tencent("sz000582", _tf(price="11.10", high="11.10", low="10.70",
                                                ts="20260827150003"))
    evaluate_symbol(snap3, now_close)
    c3 = evaluate_closing(snap3, kres, entries, "20260827", now_close)
    check("closing_use_minute", c3["use_minute"] is True and c3["cross_dev_pct"] > 0.2)
    snap4 = symbol_from_tencent("sz000582", _tf(ts="20260827145600"))
    evaluate_symbol(snap4, now_close)
    c4 = evaluate_closing(snap4, kres, entries, "20260827", now_close)
    check("closing_stale", c4["verdict"] == "stale_snapshot")
    c5 = evaluate_closing(snap, kres, [], "20260827", now_close)
    check("closing_minute_err", c5["verdict"] == "minute_error")
    c6 = evaluate_closing(snap, kres,
                          [("1430", 10.95, 120), ("1459", 10.99, 130)], "20260827", now_close)
    check("closing_not_ready", c6["verdict"] == "not_ready_1500")

    # 17) 畸形输入：不抛异常、落入 source_error
    check("malformed_empty", parse_tencent_rows("") == {}
          and parse_tencent_rows("<html>err</html>") == {})
    symt = symbol_from_tencent("sz000582", ["51"] * 5)  # 截断行
    evaluate_symbol(symt, now_intr)
    check("malformed_trunc", symt["verdict"] == "source_error")
    symn = symbol_from_tencent("sz000582", _tf(price="abc"))
    evaluate_symbol(symn, now_intr)
    check("malformed_price", symn["verdict"] == "source_error")
    symts = symbol_from_tencent("sz000582", _tf(ts="notadate12"))
    evaluate_symbol(symts, now_intr)
    check("malformed_ts", symts["verdict"] == "source_error")

    # 18) no_data_preopen 固定行含昨收与快照时点
    sym = symbol_from_tencent("sz000582", _tf(price="10.78", open_="", high="", low="",
                                              vol="0", amt="0", volratio="",
                                              ts="20260827091100"))
    evaluate_symbol(sym, now_pre)
    check("preopen_line", "10.78" in sym["verdict_line"]
          and "09:11" in sym["verdict_line"])

    # 19) audit 聚合：snap 的 verdicts dict 展平（不产出 str(dict) 乱 key）、
    #     snap_points 提取、日期过滤
    days = {}
    audit_aggregate({"ts": "2026-08-27 10:05:00", "mode": "snap",
                     "verdicts": {"sz000582": "ok", "sh000001": "ok"},
                     "parsed": [{"code": "sz000582", "ts_date": "2026-08-27",
                                 "ts_time": "10:05:03", "price": 10.81,
                                 "pct_reported": 0.28}]}, days)
    audit_aggregate({"ts": "2026-08-27 15:02:00", "mode": "minute--closing",
                     "verdict": "ok"}, days)
    slot27 = days["2026-08-27"]
    check("audit_flatten", slot27["runs"] == 2
          and slot27["verdicts"].get("snap:sz000582=ok") == 1
          and slot27["verdicts"].get("snap:sh000001=ok") == 1
          and slot27["verdicts"].get("minute--closing:ok") == 1
          and "{'sz000582'" not in str(slot27["verdicts"]))
    check("audit_snap_points", slot27["snap_points"][0]["price"] == 10.81
          and slot27["snap_points"][0]["run_at"] == "10:05:00")
    counted = audit_aggregate({"ts": "2026-08-26 09:16:00", "mode": "snap",
                               "verdicts": {"sz000582": "no_data_preopen"}},
                              days, start="2026-08-27")
    check("audit_date_filter", counted is False and "2026-08-26" not in days)

    # 20) 北京时间：now_bj 与 UTC+8 换算一致（±2 分钟容差）——时间判定不随服务器本地时区漂移
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    check("now_bj_utc8",
          abs((now_bj() - (utc_now + timedelta(hours=8))).total_seconds()) < 120)

    # 21) 审计轮转：超上限当前文件转 .1（旧 .1 被覆盖）、新记录写回当前文件；
    #     未超上限不轮转；audit_files 两代合并且顺序正确（旧代在前）
    tmpdir = tempfile.mkdtemp(prefix="quote_selftest_")
    _old_file, _old_rot, _old_max = AUDIT_FILE, AUDIT_ROTATED, AUDIT_MAX_BYTES
    try:
        globals()["AUDIT_FILE"] = os.path.join(tmpdir, "quotes.jsonl")
        globals()["AUDIT_ROTATED"] = globals()["AUDIT_FILE"] + ".1"
        globals()["AUDIT_MAX_BYTES"] = 100
        with open(globals()["AUDIT_FILE"], "w", encoding="utf-8") as f:
            f.write("x" * 150 + "\n")  # 151 字节，已超 100 上限
        audit_append({"ts": "2026-08-27 10:00:00", "mode": "snap", "verdict": "ok"})
        check("audit_rotate",
              os.path.getsize(globals()["AUDIT_ROTATED"]) == 151
              and 0 < os.path.getsize(globals()["AUDIT_FILE"]) < 100
              and audit_files() == [globals()["AUDIT_ROTATED"], globals()["AUDIT_FILE"]])
        audit_append({"ts": "2026-08-27 10:05:00", "mode": "snap", "verdict": "ok"})
        check("audit_no_rotate_below",
              os.path.getsize(globals()["AUDIT_FILE"]) > 100)  # 未超限：两代都在、未再轮转
    finally:
        globals()["AUDIT_FILE"], globals()["AUDIT_ROTATED"], globals()["AUDIT_MAX_BYTES"] = (
            _old_file, _old_rot, _old_max)
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(("✅ selftest 全部通过（%d 项断言）" % passed[0]) if not fails
          else "❌ selftest 失败 %d 项：%s（通过 %d 项）"
               % (len(fails), ", ".join(fails), passed[0]))
    return 0 if not fails else 1


# ---------------------------------------------------------------- 入口

def _valid_anchor(s):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or ""):
        raise argparse.ArgumentTypeError("锚点日期须为 YYYY-MM-DD 格式，如 2026-08-01")
    return s


def _valid_hhmm(s):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s or ""):
        raise argparse.ArgumentTypeError("时分须为 HH:MM 格式，如 09:30")
    return s


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="quote.py",
        description="stock-monitor 确定性行情取数脚本（任务只转述输出，禁止模型自算）")
    sub = p.add_subparsers(dest="mode")
    sp = sub.add_parser("snap", help="行情快照（多标的按行首代码匹配）")
    sp.add_argument("symbols", nargs="+")
    sp.add_argument("--premarket", action="store_true", help="盘前任务：容忍昨日快照")
    sp.add_argument("--min-time", dest="min_time", metavar="HH:MM",
                    help="撮合前守卫：快照时分早于该值判 stale_snapshot")
    sp.add_argument("--max-age-min", dest="max_age_min", type=int, metavar="N",
                    help="快照滞后超过 N 分钟判 halt_suspected(冻结)")
    sp.add_argument("--with-kline", dest="with_kline", nargs="?", const=21,
                    default=None, type=int, metavar="N",
                    help="附带日K派生（A/昨日量/竞价量占比）")
    sp.add_argument("--event-anchor", dest="event_anchor", metavar="YYYY-MM-DD",
                    type=_valid_anchor,
                    help="事件锚点日：附现价相对锚点日前收的累计涨幅 "
                         "event_gain_pct（事件兑现风险的量化锚）")
    sp.add_argument("--minute-recap", dest="minute_recap", metavar="HH:MM",
                    type=_valid_hhmm,
                    help="主标的(首个符号)分时区间回放：自该时分以来的日内高低点/"
                         "recap_hit_abs(曾达绝对档幅度)/retrace_signal(曾达而快照未"
                         "触发异动档，冲高回落盲区补捞)/recap_line；分时失败降级不阻塞")
    kp = sub.add_parser("kline", help="日K线与派生指标")
    kp.add_argument("symbol")
    kp.add_argument("--days", type=int, default=21)
    kp.add_argument("--since", metavar="YYYY-MM-DD")
    kp.add_argument("--this-week", dest="this_week", action="store_true")
    kp.add_argument("--this-month", dest="this_month", action="store_true")
    kp.add_argument("--prev-month", dest="prev_month", action="store_true")
    mp = sub.add_parser("minute", help="分时数据（--closing 为收盘竞价合成评估）")
    mp.add_argument("symbol")
    mp.add_argument("--closing", action="store_true")
    ap = sub.add_parser("audit", help="审计日志只读汇总（对账用）")
    ap.add_argument("--this-week", dest="this_week", action="store_true")
    sub.add_parser("selftest", help="离线自检（无网络，CI/健康检查用）")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "mode", None):
        parse_args(["-h"])
        return 2
    handlers = {"snap": mode_snap, "kline": mode_kline, "minute": mode_minute,
                "audit": mode_audit, "selftest": lambda a: _run_selftest()}
    try:
        return handlers[args.mode](args) or 0
    except SystemExit:
        raise
    except Exception:
        out_json({"mode": getattr(args, "mode", "?"), "verdict": "script_error",
                  "verdict_line": LINE_SCRIPT_ERROR,
                  "error": traceback.format_exc()[-2000:]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
