# -*- coding: utf-8 -*-
"""复盘日记格式回归：旧稿补 ##、生成端输出标准 Markdown。"""
import journal
from emtai import SYSTEM_JOURNAL, local_journal

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def test_normalize_body():
    print("\n[旧稿补格式]")
    old = "今日事实\n总资产 100 元。\n\n过程复盘\n无交易。\n"
    new, changed = journal.normalize_body(old)
    check(changed, "裸节标题被识别")
    check(new.startswith("## 今日事实\n\n总资产"), "节标题加了 ## 且正文另起一段")
    check("## 过程复盘" in new, "四节标题全部转换")

    again, changed2 = journal.normalize_body(new)
    check(not changed2 and again == new, "已格式化的稿子再跑一次不变（幂等）")

    br, cb = journal.normalize_body("【持仓】共 1 只，占 27%。\n")
    check(cb and br.startswith("## 持仓") and "共 1 只，占 27%。" in br,
          "【小节】转成 ## 标题且保留内容")

    plain, cp = journal.normalize_body("今天没有交易，只是观察。\n")
    check(not cp and plain.strip() == "今天没有交易，只是观察。", "普通段落不被误判成标题")


def test_prompt_and_template():
    print("\n[生成端]")
    check("## 今日事实" in SYSTEM_JOURNAL, "AI 提示词要求 ## 二级标题")
    check("**加粗**" in SYSTEM_JOURNAL, "AI 提示词要求关键数字加粗")
    check("`- ` 无序列表" in SYSTEM_JOURNAL, "AI 提示词要求并列用列表")

    board = {
        "date": "2026-11-25",
        "overview": {"total": 10000, "day_pnl": 100, "mv": 3000, "avail": 7000},
        "positions": {"items": [{"code": "000338", "name": "潍柴动力",
                                 "mv": 3000, "mv_share": 30, "pnl": 100}],
                      "count": 1, "top3": 30, "eff_n": 1.0},
        "trades": {"closed": 2, "win_rate": 50, "pl_ratio": 0.8, "avg_hold_days": 10,
                   "fee": 5, "gross": 100, "closed_fee": 4},
        "curve": {"mdd": -1.2},
        "attribution": {"held": 100, "top_gain": {"name": "潍柴动力", "day_pnl": 100},
                        "top_loss": None},
        "diagnose": [{"level": "risk", "title": "单票超限：潍柴动力占总资产 30.0%",
                      "detail": "超过 25% 的单票占比会把组合波动绑在一只股票上。"}],
        "vs_picks": {"ok": True, "batches": 3, "top_n": 10, "hit": [],
                     "hit_rate": 0, "held_perf_avg": 0, "pick_ret_avg": -1},
    }
    text = local_journal(board)
    for sec in ("## 今日事实", "## 过程复盘", "## 需要警惕", "## 明日检查"):
        check(sec in text, "本地模板含 " + sec)
    check("\n- " in text, "本地模板用列表陈述")
    check("**" in text, "本地模板关键数字加粗")


if __name__ == "__main__":
    test_normalize_body()
    test_prompt_and_template()
    print("\n%s" % ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    raise SystemExit(1 if FAILED else 0)
