# -*- coding: utf-8 -*-
"""
实盘成交导入与复盘

重要边界：本模块【不联网、不登录、不抓取】任何账户数据。
数据来源只能是用户自己从东方财富 / 券商客户端「历史成交 / 交割单」导出的
CSV / TXT / Excel 复制文本，粘贴或上传后在本机解析，解析结果不写入磁盘。

列名做模糊匹配以兼容不同客户端；已实现盈亏按 FIFO 估算，
未计分红、送转股、红利税与融资融券利息，仅供复盘参考。
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict

ALIASES = {
    "date": ["成交日期", "成交时间", "交易日期", "发生日期", "清算日期", "日期", "时间", "date", "time"],
    "code": ["证券代码", "股票代码", "证券编号", "代码", "code", "symbol", "stockcode"],
    "name": ["证券名称", "股票名称", "证券简称", "名称", "简称", "name"],
    "side": ["买卖方向", "交易方向", "委托方向", "买卖标志", "业务名称", "操作", "方向", "摘要", "side"],
    "price": ["成交价格", "成交均价", "成交价", "成交单价", "价格", "price"],
    "qty": ["成交数量", "成交股数", "成交数量(股)", "数量", "股数", "qty", "volume"],
    "amount": ["成交金额", "成交额", "成交金额(元)", "发生金额", "金额", "amount"],
    "fee": ["手续费", "佣金", "手续费合计", "fee", "commission"],
    "tax": ["印花税", "stamptax"],
    "transfer": ["过户费", "经手费", "规费", "transferfee"],
}

_SIDE_BUY = ("买", "申购", "增持", "买入", "buy", "b")
_SIDE_SELL = ("卖", "赎回", "减持", "卖出", "sell", "s")


def _norm_header(s) -> str:
    s = str(s or "").replace("\ufeff", "").strip()
    s = re.sub(r"[（(].*?[)）]", "", s)
    return re.sub(r"\s+", "", s)


def _num(v):
    s = str(v if v is not None else "").strip()
    s = s.replace(",", "").replace("￥", "").replace("¥", "").replace("元", "").replace("股", "")
    s = s.replace("--", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except Exception:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group()) if m else None


def _date(v):
    s = str(v or "")
    m = re.search(r"(\d{4})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})", s)
    if not m:
        return ""
    return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _side(v):
    s = str(v or "").strip().lower()
    if any(k in s for k in _SIDE_SELL):
        return "卖出"
    if any(k in s for k in _SIDE_BUY):
        return "买入"
    return None


def _split(text: str) -> list[list[str]]:
    lines = [l.strip() for l in str(text).replace("\r", "").split("\n") if l.strip()]
    if not lines:
        return []
    sample = lines[0]
    delim = "\t" if "\t" in sample else ("," if "," in sample else None)
    if delim:
        return [r for r in csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)]
    return [re.split(r"\s{1,}", l) for l in lines]


def _header_map(cells) -> tuple[dict, int]:
    norm = [_norm_header(c) for c in cells]
    m: dict[str, int] = {}
    used: set[int] = set()
    # 先精确匹配，避免「金额」抢占「成交金额」这类列
    for key, aliases in ALIASES.items():
        for i, c in enumerate(norm):
            if c and i not in used and c in aliases:
                m[key] = i
                used.add(i)
                break
    # 再包含匹配
    for key, aliases in ALIASES.items():
        if key in m:
            continue
        for i, c in enumerate(norm):
            if not c or i in used:
                continue
            if any((a in c or c in a) for a in aliases if len(a) >= 2):
                m[key] = i
                used.add(i)
                break
    return m, len([c for c in norm if c])


def parse(text: str) -> dict:
    if not text or not text.strip():
        return {"ok": False, "error": "内容为空，请先粘贴或选择导出的成交文件"}
    grid = _split(text)
    if not grid:
        return {"ok": False, "error": "没有解析出任何行"}

    best_map, best_i, best_n = {}, -1, 0
    for i, cells in enumerate(grid[:12]):
        m, n = _header_map(cells)
        score = len([k for k in ("code", "side", "qty", "price", "amount", "date") if k in m])
        if score > best_n:
            best_map, best_i, best_n = m, i, score
    if best_i < 0 or "code" not in best_map or best_n < 3:
        head = " | ".join(str(x) for x in grid[0][:12])
        return {"ok": False,
                "error": "没能识别出表头字段。请确认粘贴的是「历史成交 / 交割单」导出的表格（需含证券代码、买卖方向、数量或价格等列）。首行内容：" + head[:180]}

    required = ("qty" in best_map) or ("amount" in best_map)
    if not required:
        return {"ok": False, "error": "缺少「成交数量」或「成交金额」列，无法计算盈亏"}

    def cell(row, key):
        i = best_map.get(key)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    rows, skipped, warns = [], 0, []
    for row in grid[best_i + 1:]:
        if not row or not any(str(x).strip() for x in row):
            continue
        code_raw = cell(row, "code")
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", code_raw)
        if not m:
            skipped += 1
            continue
        code = m.group(1)
        sd = _side(cell(row, "side"))
        if sd is None:
            # 没有方向列时，尝试用金额正负号推断；仍无法判断则跳过
            am = _num(cell(row, "amount"))
            if am is None:
                skipped += 1
                continue
            sd = "卖出" if am < 0 else "买入"
            if len([1 for k in ("side",) if k in best_map]) == 0:
                warns.append("未找到「买卖方向」列，已按发生金额正负号推断（买入为负 / 卖出为正）")
        price = _num(cell(row, "price"))
        qty = _num(cell(row, "qty"))
        amount = _num(cell(row, "amount"))
        if amount is not None:
            amount = abs(amount)
        fee = (_num(cell(row, "fee")) or 0.0) + (_num(cell(row, "tax")) or 0.0) + (_num(cell(row, "transfer")) or 0.0)
        if amount is None and price is not None and qty is not None:
            amount = abs(price * qty)
        if amount is None:
            skipped += 1
            continue
        if qty is None or qty <= 0:
            qty = round(amount / price, 2) if price and price > 0 else 0
        if price is None or price <= 0:
            price = round(amount / qty, 3) if qty else 0
        rows.append({"date": _date(cell(row, "date")) or "-",
                     "code": code, "name": cell(row, "name") or code,
                     "side": sd, "price": round(price, 3),
                     "qty": int(qty) if float(qty).is_integer() else round(qty, 2),
                     "amount": round(amount, 2), "fee": round(abs(fee), 2),
                     "pnl": None, "pnl_pct": None})

    if not rows:
        return {"ok": False, "error": "表头识别成功，但没有解析出有效成交行。请检查是否复制了完整的数据区（而非只有表头）"}

    # FIFO 已实现盈亏：买入成本含费用，卖出收入扣费用
    rows.sort(key=lambda r: (r["date"], r["code"]))
    books = defaultdict(list)
    realized = defaultdict(float)
    for r in rows:
        q = float(r["qty"])
        if q <= 0:
            continue
        if r["side"] == "买入":
            unit_cost = (r["amount"] + r["fee"]) / q
            books[r["code"]].append([q, unit_cost])
        else:
            unit_income = (r["amount"] - r["fee"]) / q
            need, pnl, base = q, 0.0, 0.0
            while need > 0 and books[r["code"]]:
                lot = books[r["code"]][0]
                take = min(need, lot[0])
                pnl += take * (unit_income - lot[1])
                base += take * lot[1]
                lot[0] -= take
                need -= take
                if lot[0] <= 1e-9:
                    books[r["code"]].pop(0)
            r["pnl"] = round(pnl, 2)
            r["pnl_pct"] = round(pnl / base * 100, 2) if base > 0 else None
            realized[r["code"]] += pnl

    buy_amt = sum(r["amount"] for r in rows if r["side"] == "买入")
    sell_amt = sum(r["amount"] for r in rows if r["side"] == "卖出")
    fee_sum = sum(r["fee"] for r in rows)
    pnl_sum = sum(v for v in realized.values())
    open_shares = {c: round(sum(lot[0] for lot in lots), 2)
                   for c, lots in books.items() if sum(lot[0] for lot in lots) > 0}
    dates = [r["date"] for r in rows if r["date"] != "-"]
    stats = {"trades": len(rows), "stocks": len({r["code"] for r in rows}),
             "buy_amount": round(buy_amt, 2), "sell_amount": round(sell_amt, 2),
             "net_cash": round(sell_amt - buy_amt - fee_sum, 2),
             "fee": round(fee_sum, 2), "realized_pnl": round(pnl_sum, 2),
             "open_positions": open_shares,
             "date_from": min(dates) if dates else "-", "date_to": max(dates) if dates else "-"}
    if warns:
        warns = list(dict.fromkeys(warns))
    warns.append("已实现盈亏按 FIFO 估算，未计分红、送转股、红利税与融资融券利息；费用口径以导出文件为准，请自行核对。")
    return {"ok": True, "rows": rows, "stats": stats, "skipped": skipped,
            "columns": {k: str(grid[best_i][i]).strip() for k, i in best_map.items()},
            "warnings": warns}
