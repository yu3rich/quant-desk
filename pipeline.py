# -*- coding: utf-8 -*-
"""QuantDesk 流水线 · 时效检查与一键更新

四段流水线，越往后越慢、也越不该高频跑：

  1) 行情缓存    每日增量（baostock），首次全量约 1 小时，日常增量 1~8 分钟
  2) 横截面快照  选股每次都用，行情一变就该重算；十几秒，可以天天跑
  3) 训练集      含未来 N 日标签，必须等行情补齐；十几秒到 1 分钟
  4) 模型        建议按月重训（walk-forward），**不要因为最近亏了临时重训** —— 那是过拟合

分工的依据是行业惯例而不是手感：数据新鲜度必须跟上（否则打分用的是过期截面），
模型参数则要稳定（频繁重训会让参数在噪声里乱跳，反而不如不训）。
所以自动更新只负责 1~3 段，第 4 段按月提醒 + 一键执行，不自动触发。
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import threading
import time
from pathlib import Path

import screener
import mlmodel
import store

from paths import BASE  # 打包后 = exe 目录；开发时 = 项目根
DATA = BASE / "data"
TRADE_CAL = DATA / "trade_cal.json"
LOCK = threading.Lock()


class _ProcLock:
    """跨进程互斥锁：threading.Lock 只在一个进程内有效。

    定时任务、页面上的「更新到最新」、以及双击 pipeline_daily.bat 是三个
    不同进程。没有这把锁时它们会各起 6 个行情子进程，同时覆盖
    snapshot.csv / dataset.csv，还会重复写 runs。拿不到就立即返回，不阻塞。
    """

    def __init__(self, path):
        self.path = path
        self.f = None

    def acquire(self) -> bool:
        import os
        DATA.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    f.write(b"0")
                    f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.f = f
        return True

    def release(self):
        if not self.f:
            return
        try:
            import os
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self.f.close()
            except Exception:
                pass
            self.f = None


PROC_LOCK = _ProcLock(DATA / ".pipeline.lock")
# 最近一次（或正在进行中的）流水线运行，供前端轮询展示进度。
# 运行中必须能回答「现在在跑第几步、这一步做到哪了」——否则界面上只有一个
# 「运行中」的胶囊，用户点完更新后几分钟内看不到任何动静，会以为卡死了。
# 以前 steps 只在每步**结束后**才 append，运行中永远是空数组，前端自然没东西可画。
RUN: dict = {
    "running": False, "level": "", "started": "", "result": None,
    # 当前正在执行的段：{"index":1,"total":3,"key":"quote","name":"行情增量",
    #                    "detail":"...","done":1234,"total_n":5579}
    "step": None,
    # 已完成的段（逐步追加），运行中前端就能画出「第 1/3 步已完成」
    "steps": [],
}
# 交易日历缓存：只在「工作日 15:10 后、行情还没到最新」这个需要判断休市的时刻拉一次
_TCAL: dict = {"days": None, "at": 0.0}
# 离线兜底：akshare 首次拉日历失败时，至少能识别 2024–2026 的工作日节假日。
# 每年年初更新一次（直接从交易所公告或 akshare 重新生成）；历史日期另由本地 K 线补齐。
_STATIC_HOLIDAYS = {
    "2024-01-01", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14",
    "2024-02-15", "2024-02-16", "2024-04-04", "2024-04-05", "2024-05-01",
    "2024-05-02", "2024-05-03", "2024-06-10", "2024-09-16", "2024-09-17",
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-03", "2025-02-04", "2025-04-04", "2025-05-01", "2025-05-02",
    "2025-05-05", "2025-06-02", "2025-10-01", "2025-10-02", "2025-10-03",
    "2025-10-06", "2025-10-07", "2025-10-08", "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-23", "2026-04-06", "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-09-25", "2026-10-01", "2026-10-02", "2026-10-05",
    "2026-10-06", "2026-10-07",
}
_STATIC_FROM = dt.date(2024, 1, 1)
_STATIC_TO = dt.date(2026, 12, 31)

# 模型重训的日历上限：超过这么多天就提醒一次（行业惯例是月度）
RETRAIN_CAL_DAYS = 35
# 训练集落后多少个交易日才算「该重建」（最后 horizon+1 天本来就没有标签，属正常）
DATASET_WARN_LAG = 25
# 上市名单落后多少个交易日才提醒。名单不是「每天必须」的东西 —— 新股进来会被
# 当作「名单外放行」，退市股晚几天判定也不影响当天的截面 —— 所以滞后 1~2 天
# 不必吵；但超过一周还没同步，回放的样本池就开始失真了。
LISTING_WARN_LAG = 3
# 资金流（两融/龙虎榜/大宗）落后多少个交易日算「这条链断了」。
# 注意两融是 T+1 才发布的，所以 0 天的要求本来就不可能 —— 判据用「龙虎榜/大宗
# 当日就有」的 _all.end，允许落后 1 天才提示。
CAPITAL_WARN_LAG = 5
# baostock 日线约 17:30 入库，留 15 分钟余量。在这之前，「今天的数据没到」
# 本来就是正常的，不能算落后 —— 否则盘中每次打开工作台都会白喊一次。
DATA_READY_HM = "1745"
# Tushare 的 daily_basic 通常 19:30 之后才更新当日。晚于这个点才把「今天」
# 算作估值的应到日，否则每天傍晚都会挂一条「估值落后 1 天」的假待办。
VAL_READY_HM = "1930"


def _ymd(v) -> str:
    return str(v or "").replace("-", "")[:8]


def dash(v) -> str:
    """20261119 / 2026-11-19 → 2026-11-19；空值给「—」。"""
    d = _ymd(v)
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 else "—"


def _calendar() -> list[str]:
    """兜底交易日历：交易日历不可用时，退回某只主板大票的日线日期。

    注意这只是兜底 —— 正常判断必须用 _market_days()。拿本地缓存里的 K 线日期
    当「交易日历」，缓存落后时日历会跟着落后，等于自己证明自己是最新的。
    """
    try:
        d = screener.load_history("600000")
        if d is None or len(d) < 10:
            return []
        return sorted({_ymd(x) for x in d["date"]})
    except Exception:
        return []


def _market_days(upto: str | None = None) -> list[str]:
    """交易所交易日历（升序），upto 给定时只取到那天为止。

    与本地缓存无关：缓存停在 11-18 时，这里仍然知道 11-19 是交易日，
    于是「11-19 没抓到」才能被发现。
    """
    days = {d for d in _trade_days() if d}
    if not days:
        return _calendar()
    if upto:
        days = {d for d in days if d <= _ymd(upto)}
    return sorted(days)


def _expected_day(now: dt.datetime | None = None) -> str:
    """按「数据源什么时候入库」推算此刻应该已经拿到的最新交易日（2026-11-19 这种写法）。

    盘中（17:45 之前）今天的日线本来就取不到，应到日算上一交易日；
    周末/节假日会自动落到最近一个交易日，不必再单独判断休市。
    """
    now = now or dt.datetime.now()
    ref = now.strftime("%Y%m%d")
    if now.strftime("%H%M") < DATA_READY_HM:
        ref = (now - dt.timedelta(days=1)).strftime("%Y%m%d")
    days = _market_days(ref)
    if not days:
        return ""
    d = days[-1]
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:8])


def _trade_lag(day: str, ref: str) -> int | None:
    """day 比 ref 落后几个交易日；数据已到 ref 或更新时返回 0。"""
    d, r = _ymd(day), _ymd(ref)
    if len(d) < 8 or len(r) < 8:
        return None
    if d >= r:
        return 0
    return len([x for x in _market_days(r) if d < x <= r])



def _lag(cal: list[str], day: str) -> int | None:
    """day 距离最新交易日差多少个交易日。day 早于日历起点时返回 None。"""
    d = _ymd(day)
    if not d or len(d) < 8 or not cal:
        return None
    i = bisect.bisect_right(cal, d) - 1
    if i < 0:
        return None
    return max(0, len(cal) - 1 - i)


def _bar_trade_days() -> set[str]:
    """从本地 K 线抽一批股票的公共日期，作为离线历史交易日。

    bars 主键是 (code,date)，按 code 前缀取 64 只再取 DISTINCT date 走索引，
    不会全表扫描；几百个日期足够让「落后几天」的判断不依赖网络。
    """
    try:
        codes = store.cached_codes()
        if not codes:
            return set()
        step = max(1, len(codes) // 64)
        sample = codes[::step][:64]
        q = ("SELECT DISTINCT date FROM bars WHERE code IN (%s)"
             % ",".join("?" * len(sample)))
        return {_ymd(r[0]) for r in store.conn().execute(q, sample) if r[0]}
    except Exception:
        return set()


def _fallback_trade_days() -> set[str]:
    """akshare 不可用时的离线日历：静态节假日 + 本地 K 线实际出现过的日期。"""
    days = _bar_trade_days()
    d = _STATIC_FROM
    while d <= _STATIC_TO:
        if d.weekday() < 5 and d.isoformat() not in _STATIC_HOLIDAYS:
            days.add(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return days


def _trade_days() -> set[str]:
    """全年交易日集合（含未来日期），用于区分「周末/节假日」与「行情真的没入库」。

    优先读 data/trade_cal.json；过期或缺失时联网拉 akshare；仍失败则用本地静态
    节假日表 + K 线日期兜底，避免断网时把国庆/春节误判成「行情落后」。
    """
    if _TCAL["days"] is not None:
        today = _ymd(dt.date.today().isoformat())
        cached = _TCAL["days"]
        # 覆盖到今天就直接用。没覆盖（日历到期 / 上次联网失败）半小时后重试，
        # 否则一次网络抖动会把这个进程锁死在「失明」状态，直到重启才恢复。
        if cached and min(cached) <= today <= max(cached):
            return cached
        if time.time() - float(_TCAL.get("at") or 0) < 1800:
            return cached
    days: set[str] = set()
    try:
        j = json.loads(TRADE_CAL.read_text(encoding="utf-8")) or {}
        days = {str(x)[:10] for x in (j.get("days") or []) if x}
    except Exception:
        days = set()
    today = _ymd(dt.date.today().isoformat())
    covered = bool(days) and min(days) <= today <= max(days)
    if not covered:
        try:
            import akshare as ak
            d = ak.tool_trade_date_hist_sina()
            days = {_ymd(x) for x in d["trade_date"] if _ymd(x)}
            DATA.mkdir(parents=True, exist_ok=True)
            tmp = TRADE_CAL.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"updated": _now(), "days": sorted(days)},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(TRADE_CAL)
            covered = bool(days) and min(days) <= today <= max(days)
        except Exception:
            pass
    if not covered:
        # 不把兜底结果落盘：下次启动仍会重试 akshare，拿到正式日历后覆盖。
        days |= _fallback_trade_days()
    _TCAL.update(days=days, at=time.time())
    return days


def _calendar_span() -> dict:
    """交易日历的覆盖范围与「够不够用」。

    日历到期是一件很危险的事：应到日算不出来时，时效检查会拿本地 K 线跟本地
    K 线比，于是又变回「自己证明自己最新」，而且没有任何提示。所以这里要单独
    把「日历是否覆盖今天」暴露出去，让待办能够明说「我不知道今天该不该更新」。
    """
    days = _trade_days()
    today = _ymd(dt.date.today().isoformat())
    lo = min(days) if days else ""
    hi = max(days) if days else ""
    covered = bool(lo and hi and lo <= today <= hi)
    # 提醒的提前量：日历到期前 10 天开始提示，免得元旦那天才发现。
    days_left = None
    if hi:
        try:
            days_left = (dt.datetime.strptime(hi, "%Y%m%d").date() - dt.date.today()).days
        except Exception:
            days_left = None
    return {"from": lo, "to": hi, "today": today, "covered": covered,
            "days_left": days_left}


def _is_trading_day(day: str) -> bool | None:
    """True/False=确定是/不是交易日；None=日历未覆盖或暂不可用。"""
    d = _ymd(day)
    days = _trade_days()
    if not d or not days or not (min(days) <= d <= max(days)):
        return None
    return d in days


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _val_expected(now: dt.datetime | None = None) -> str:
    """估值的应到日。

    有快照源（东财/腾讯）时收盘 5 分钟就有当日估值；只开 Tushare 时得等 19:30。
    口径由 valsrc 统一算，避免两处各写一套「几点算新」。
    """
    now = now or dt.datetime.now()
    try:
        import valsrc
        exp = valsrc.target_date(now)
        if exp:
            return dash(exp)
    except Exception:
        pass
    exp = _expected_day(now)
    if not exp:
        return ""
    if now.strftime("%H%M") < VAL_READY_HM and exp == now.strftime("%Y-%m-%d"):
        prev = [d for d in _market_days(_ymd(exp)) if d < _ymd(exp)]
        return dash(prev[-1]) if prev else ""
    return exp


def _valuation_state(expected: str) -> dict:
    """Tushare 估值的时效（只读 sources.json 里的 last_date，不扫 CSV）。

    估值文件里有几千行不代表它是新的：PE/PB/市值可以停在两周前而页面毫无提示。
    """
    try:
        import valsrc
        if not valsrc.any_enabled():
            return {"configured": False}
        st = valsrc.status()
        pre = st.get("precheck") or {}
        got = str(st.get("last_date") or "")
        return {"configured": True, "source": st.get("last_source") or "",
                "source_name": valsrc.name_of(st.get("last_source") or ""),
                "last_date": got, "last_fetch": str(st.get("last_at") or ""),
                "last_try": str(st.get("last_at") or ""),
                # 下次能自动取到的时刻（盘中 15:05 / 退避结束）：待办里直接告诉用户
                "next": pre.get("until") or "", "wait_sec": int(pre.get("wait_sec") or 0),
                "blocked": bool(pre and not pre.get("can")),
                "why": pre.get("reason") or "",
                "lag": _trade_lag(got, expected) if (got and expected) else None,
                "stale": bool(got and expected and (_trade_lag(got, expected) or 0) > 0)}
    except Exception:
        return {"configured": False}


def freshness() -> dict:
    """各环节时效与建议动作。轻量：只读 meta json + bar_meta 聚合，不读 K 线全表。"""
    now = dt.datetime.now()
    today = now.strftime("%Y%m%d")
    if not store.cached_count():
        return {"ok": False, "error": "没有行情缓存，先去「股票池」预热"}
    # 数据日与应到日必须各自独立：数据日取「多数股票都到齐的那一天」，
    # 应到日取交易所日历。以前两者都从本地 K 线推，缓存落后时日历跟着落后，
    # 于是「昨天收盘后没更新」这件事永远查不出来。
    cal_state = _calendar_span()
    latest = store.data_day()
    # 日历不覆盖今天时，应到日干脆留空：宁可说「我不知道」，
    # 也不要拿本地 K 线推一个假的应到日，把落后伪装成「已是最新」。
    expected = _expected_day(now) if cal_state["covered"] else ""
    quote_lag = _trade_lag(latest, expected)
    cal = _market_days(expected or (cal_state["to"] or today))
    if not cal:
        cal = _calendar()
    if not latest:
        latest = cal[-1] if cal else ""
    trading_day = _is_trading_day(today) if latest and latest < today else None
    quote_stale = bool((quote_lag or 0) > 0)
    cover = store.data_day_cover(latest)

    smeta = screener._json_read(screener.SNAPSHOT_META, {}) or {}
    dmeta = screener._json_read(screener.DATASET_META, {}) or {}
    mmeta = mlmodel._meta()

    snap_day = _ymd(smeta.get("as_of") or smeta.get("date_max"))
    ds_day = _ymd(dmeta.get("date_max"))
    model_day = _ymd(mmeta.get("train_end") or
                     (mmeta.get("dataset") or {}).get("end"))
    trained_at = str(mmeta.get("trained") or "")

    snap_lag, ds_lag, model_lag = _lag(cal, snap_day), _lag(cal, ds_day), _lag(cal, model_day)
    train_age = None
    if trained_at:
        try:
            train_age = (now - dt.datetime.fromisoformat(trained_at)).days
        except Exception:
            train_age = None
    # 因子集版本变了，或模型压根没用上当前因子集里的列，都必须重训：
    # 推理列与模型训练时不一致，新因子等于白加。只看版本号会漏掉旧模型。
    mfeat = set(mmeta.get("features") or [])
    missing_feats = [k for k in screener.FEATURE_KEYS if k not in mfeat]
    feat_drift = bool(mmeta.get("ok")) and (
        mmeta.get("feature_version") not in (None, screener.FEATURE_VERSION)
        or bool(missing_feats))
    # 训练集在模型训练之后重建过：行数/尾部日期可能已变，模型却是旧样本训出来的。
    # 只看训练时间发现不了这种情况（同一天重建训练集、忘了重训模型）。
    ds_generated = str(dmeta.get("generated") or "")
    model_ds_generated = str(mmeta.get("dataset_generated") or
                             (mmeta.get("dataset") or {}).get("generated") or "")
    dataset_newer = bool(
        mmeta.get("ok") and ds_generated and (
            (model_ds_generated and ds_generated > model_ds_generated) or
            # 旧模型没记数据集指纹时，退化为「训练集生成时间 > 模型训练时间」，
            # 至少能发现「前一天重建了训练集、模型还是更早训练的」。
            (not model_ds_generated and trained_at and ds_generated > trained_at)))

    actions = []
    # 日历到期 / 拉不到：这不是「数据落后」，而是「没有判断依据」，
    # 必须显式说出来，否则它会表现成一片绿油油的「已是最新」。
    if not cal_state["covered"]:
        if cal_state["to"] and cal_state["to"] < today:
            text = ("交易日历只覆盖到 %s，无法判断今天是否该更新行情（请在数据源页联网刷新日历）"
                    % cal_state["to"])
        else:
            text = "读不到交易日历，无法判断行情是否最新（请在数据源页联网刷新）"
        actions.append({"key": "calendar", "level": "warn", "auto": False, "text": text})
    elif (cal_state.get("days_left") is not None and cal_state["days_left"] <= 10):
        actions.append({"key": "calendar", "level": "info", "auto": False,
                        "text": "交易日历只到 %s（还剩 %d 天），到期前请刷新一次，否则时效检查会失效"
                                % (cal_state["to"], cal_state["days_left"])})
    if quote_stale:
        if quote_lag == 1 and _ymd(expected) == today:
            text = "今日行情还没入库（缓存到 %s），先做增量再重算截面" % latest
        elif quote_lag == 1:
            text = ("行情落后 1 个交易日（缓存到 %s，应到 %s）：上次收盘后没跑成更新，"
                    "不补的话榜单和昨天一模一样" % (latest, expected))
        else:
            text = "行情落后 %d 个交易日（缓存到 %s，应到 %s），先做增量再重算截面" % (
                quote_lag, latest, expected)
        actions.append({"key": "quote", "level": "warn", "auto": True, "text": text})
    # 数据日取的是「多数股票到齐」那天，所以总有一小撮票掉队却不影响判断。
    # 掉队的数量多到影响截面时（< 97%）要单独提示，否则选股样本会悄悄缩水。
    if latest and not quote_stale and 0 < cover < 0.97:
        try:
            miss = store.conn().execute(
                "SELECT COUNT(*) FROM bar_meta WHERE last_date<>? AND last_date<>''",
                (latest,)).fetchone()[0]
        except Exception:
            miss = 0
        actions.append({"key": "cover", "level": "warn", "auto": True,
                        "text": "行情覆盖 %.1f%%（%d 只停在更早的日期），重跑一次增量即可补齐"
                                % (cover * 100, miss)})
    if snap_lag is None:
        actions.append({"key": "snapshot", "level": "warn", "auto": True,
                        "text": "还没有横截面快照，选股前需要先建一次"})
    elif snap_lag > 0 and not quote_stale:
        # 行情落后时快照必然跟着落后，自动更新一步就把两件事都办了，
        # 拆成两条待办只会让用户点两次。
        actions.append({"key": "snapshot", "level": "warn", "auto": True,
                        "text": "快照落后 %d 个交易日（%s），重算只要十几秒" % (snap_lag, snap_day)})

    if ds_lag is None:
        actions.append({"key": "dataset", "level": "warn", "auto": True,
                        "text": "还没有训练集，模型无法训练"})
    elif ds_lag > DATASET_WARN_LAG:
        actions.append({"key": "dataset", "level": "warn", "auto": True,
                        "text": "训练集落后 %d 个交易日，迟到的新样本没进训练" % ds_lag})
    elif ds_lag > 0:
        actions.append({"key": "dataset", "level": "info", "auto": True,
                        "text": "训练集落后 %d 个交易日（正常，尾部 %s 天要等未来收益才能打标签）"
                                % (ds_lag, int(dmeta.get("horizon") or 10) + 1)})
    # 到期判据只看「训练时间」和「因子集版本」，不看拟合截止日：
    # 模型刻意只用前 75% 的样本拟合，train_end 天生落后验证期约四分之一的时间跨度，
    # 拿它跟最新交易日比会永远显示「该重训」。
    retrain_due = bool(not mmeta.get("ok") or feat_drift or dataset_newer or
                       (train_age is not None and train_age > RETRAIN_CAL_DAYS))
    if retrain_due:
        if not mmeta.get("ok"):
            why = "还没训练过模型"
        elif feat_drift:
            if missing_feats:
                why = "模型缺少当前因子集里的 %d 个因子（%s）" % (
                    len(missing_feats), "、".join(missing_feats[:4]))
            else:
                why = "因子集已升级到 v%s，模型还是 v%s" % (screener.FEATURE_VERSION,
                                                         mmeta.get("feature_version"))
        elif dataset_newer:
            why = "训练集已于 %s 重建，模型还是 %s 训练的" % (
                ds_generated[:16], (trained_at[:10] or "此前"))
        else:
            why = "模型训练于 %s（%s 天前）" % (trained_at[:10] if trained_at else "—", train_age)
        actions.append({"key": "model", "level": "warn", "auto": False,
                        "text": why + "；建议按月重训一次，不必天天重训"})

    vexp = _val_expected(now)
    vas = dict(_valuation_state(vexp), expected=vexp)
    if vas.get("stale") and vas.get("lag"):
        got = dash(vas["last_date"])
        src = vas.get("source_name") or "本地缓存"
        tried_today = str(vas.get("last_try") or vas.get("last_fetch") or "")[:10] == now.strftime("%Y-%m-%d")
        # 落后一天是否属正常，取决于开了哪几路源：
        #   只开 Tushare → 它 19:30 才更新，而定时任务 18:10 跑，天然落后一天；
        #   开了东财/腾讯 → 收盘 5 分钟就有，还落后一天说明确实没取到，该提醒。
        try:
            import valsrc
            snap = valsrc.has_snapshot()
        except Exception:
            snap = False
        level = "info" if (vas["lag"] <= 1 and not snap) else "warn"
        if vas["lag"] <= 1 and not snap:
            text = ("估值 %s（%s），比行情晚一天（Tushare 19:30 才更新当日），属正常" % (got, src))
        elif vas.get("blocked") and vas.get("next"):
            # 盘中 / 退避中：能取的时刻是确定的，就别让用户反复手点
            text = ("估值停在 %s（应到 %s，落后 %d 个交易日）：%s 后系统会自动取，现在不用手动点；"
                    "PE/PB/市值暂用旧值" % (got, vexp, vas["lag"], vas["next"]))
        elif tried_today:
            text = ("估值只到 %s（应到 %s，落后 %d 个交易日）：今天各源都试过了%s；"
                    "PE/PB/市值暂用旧值" % (got, vexp, vas["lag"],
                                     "（%s）" % src if src else ""))
        else:
            text = ("估值停在 %s（来源 %s），落后 %d 个交易日，PE/PB/市值因子在用旧值"
                    "（去数据源拉一次）" % (got, src, vas["lag"]))
        actions.append({"key": "valuation", "level": level, "auto": False, "text": text})

    # ---- 2026-11 新增的三个数据源：上市名单 / 退市股行情 / 资金流 ----------------
    # 这三块当初只做了「数据中枢里能点按钮」，没接进待办，于是数据断了页面一片绿，
    # 只有真去翻中枢才看得见。它们的共同点是**不阻塞选股**，所以允许 info 级；
    # 但一律带 todo=True —— 前端只渲染 warn 级，不带这个标记的 info 会被直接丢掉。
    # auto 一律给 False：它们不进「更新到最新」的自动链（除了名单在管道里逐步同步），
    # 所以不该把顶栏的「数据待更新」点亮。
    lst = {}
    try:
        import listing
        lst = listing.status()
    except Exception:
        lst = {}
    if not lst.get("ok"):
        actions.append({
            "key": "listing", "level": "warn", "auto": False, "todo": True,
            "text": "读不到上市名单，回放无法按「那天真实在市」取样本（去数据链同步一次）"})
    else:
        lday = _ymd(lst.get("updated"))
        llag = _lag(cal, lday) if lday else None
        if not lday:
            actions.append({
                "key": "listing", "level": "warn", "auto": False, "todo": True,
                "text": "还没有上市名单（point-in-time），回放会踏回幸存者偏差；"
                        "去数据链同步一次，约 3 秒"})
        elif (llag or 0) >= LISTING_WARN_LAG:
            actions.append({
                "key": "listing", "level": "warn", "auto": False, "todo": True,
                "text": "上市名单停在 %s（落后 %d 个交易日）：新股按「名单外」放行、"
                        "退市股判定也会滞后" % (dash(lday), llag)})
        # 退市股行情：只有近两年退市的才影响当前回放窗口。
        # 阈值取 3 而不是 1 —— 接口对部分老票已经查无此股，补不全是常态，
        # 差一两只就挂待办等于永远消不掉。
        try:
            cov = listing.coverage()
        except Exception:
            cov = {}
        miss = int(cov.get("recent_missing") or 0)
        if miss >= 3 and not (lst.get("prewarm") or {}).get("running"):
            actions.append({
                "key": "listing_prewarm", "level": "info", "auto": False, "todo": True,
                "text": "近两年退市的 %d 只票还没有行情，补上回放样本才完整"
                        "（更早退市的接口已查无此股，补不全属正常）" % miss})

    cap = {}
    try:
        import capital
        cap = capital.fresh()
    except Exception:
        cap = {}
    if cap.get("ok"):
        cend = str(cap.get("end") or "")
        if not cap.get("total"):
            actions.append({
                "key": "capital", "level": "info", "auto": False, "todo": True,
                "text": "还没同步过资金流（两融 / 龙虎榜 / 大宗），因子体检里这一组会一直是空的"})
        elif not cap.get("running"):
            clag = _lag(cal, cend)
            # 两融天然 T+1 发布，落后 1 天属正常；开在模型里时落后就意味着打分用了旧值，
            # 那种情况下要更早喊出来。
            hard = 1 if cap.get("in_model") else CAPITAL_WARN_LAG
            if (clag or 0) >= hard:
                actions.append({
                    "key": "capital", "level": "warn", "auto": False, "todo": True,
                    "text": "资金流停在 %s（落后 %d 个交易日）：%s" % (
                        dash(cend), clag,
                        "它已开进模型，打分正在用旧值" if cap.get("in_model")
                        else "这一组因子会缺样本，默认不进模型、不影响选股")})
            elif (clag or 0) >= 1:
                actions.append({
                    "key": "capital", "level": "info", "auto": False, "todo": True,
                    "text": "资金流落后 %d 个交易日（%s），补上只是让体检样本连续"
                            % (clag, dash(cend))})

    # ---- 全 A 名单 / 实时快照 ------------------------------------------------
    # 这两块以前只有股票池页的两个按钮，过期了页面只是悄悄显示旧价，
    # 于是没人点、待办里也永远看不到。判据一律对齐「应到行情日」而不是自然日：
    # 周末/长假名单停在周五是正常的，按自然日比会天天误报「不是今天更新的」。
    # 名单进自动链（行情段里会顺带刷新），所以 auto=True；实时快照只影响
    # 页面上的最新价，不阻塞选股，给 info 级免得天天喊。
    um = {}
    try:
        import universe
        um = universe.meta()
    except Exception:
        um = {}
    universe_lag = None
    if um.get("ok"):
        uday = _ymd(um.get("updated"))
        if expected and uday and expected:
            universe_lag = _trade_lag(uday, expected)
        if not um.get("total"):
            actions.append({
                "key": "universe", "level": "warn", "auto": True, "todo": True,
                "text": "还没有全 A 名单：股票池没法批量加入，实时价也没有标的（刷新一次约 3 秒）"})
        elif (universe_lag or 0) > 0:
            actions.append({
                "key": "universe", "level": "warn", "auto": True, "todo": True,
                "text": "全 A 名单停在 %s（落后 %d 个交易日）：新上市的票进不来，"
                        "退市票也还留在名单里" % (dash(uday), universe_lag)})

    sp = {}
    try:
        import spot
        sp = spot.status()
    except Exception:
        sp = {}
    spot_lag = None
    if sp.get("ok"):
        sday = _ymd(sp.get("updated"))
        if expected and sday:
            spot_lag = _trade_lag(sday, expected)
        if not sp.get("count"):
            actions.append({
                "key": "spot", "level": "info", "auto": True, "todo": True,
                "text": "还没抓过全市场实时价：名单页的最新价/涨跌幅用的是名单里的旧值"})
        elif (spot_lag or 0) > 0:
            actions.append({
                "key": "spot", "level": "info", "auto": True, "todo": True,
                "text": "实时价停在 %s（落后 %d 个交易日）：名单页显示的是旧价，"
                        "点「刷新实时价」十几秒可补" % (dash(sday), spot_lag)})

    warns = [a for a in actions if a["level"] == "warn"]
    # 行情/快照/训练集是「数据链待更新」，可以一键自动补齐；模型重训是人工待办，
    # 自动更新并不会清掉它。两者混在一起会让顶栏一直显示「数据待更新」，
    # 用户点了更新也消不掉，所以单独给一个 data_stale。
    data_stale = any(a.get("auto") for a in warns)
    src = {}
    try:
        import bulkdata
        src = bulkdata.source_now()
    except Exception:
        src = {}
    return {
        "ok": True,
        "as_of": latest,                     # 行情缓存数据日（多数股票到齐的那天）
        "expected": expected,                # 按数据源入库时间，此刻应该到哪一天
        "quote_lag": quote_lag,              # 落后几个交易日
        "today": today,
        "trading_day": trading_day,
        "quote_stale": quote_stale,
        "quote_cached": store.cached_count(),
        # 交易日历覆盖情况：covered=False 时上面所有「落后 N 日」都不可信
        "calendar": cal_state,
        # 估值时效（多源；一路都没开时 configured=False，不参与判断）
        # expected 用估值的口径（19:30 才更新当日），跟行情应到日不是同一天
        "valuation": dict(vas, last_date=vas.get("last_date") and dash(vas["last_date"]) or ""),
        "quote_cover": cover,
        # 全 A 名单与实时快照：股票池页两个按钮的口径，中枢也一并暴露，
        # 免得「页面说旧、待办不说」两套判断各说各话
        "universe": {"updated": _ymd(um.get("updated")), "lag": universe_lag,
                     "total": um.get("total", 0), "cached": um.get("cached", 0)},
        "spot": {"updated": str(sp.get("updated") or ""), "lag": spot_lag,
                 "count": sp.get("count", 0)},
        # 最近一次更新用的数据源：spot 说明 baostock 连不上、当日 K 线是快照补的
        "quote_source": src.get("source", ""),
        "quote_error": src.get("error", ""),
        "quote_blocked": bool(src.get("blocked")),
        "snapshot": {"as_of": snap_day, "lag": snap_lag,
                     "generated": smeta.get("generated", ""),
                     "count": smeta.get("count", 0)},
        "dataset": {"date_max": ds_day, "lag": ds_lag,
                    "rows": dmeta.get("rows", 0), "generated": dmeta.get("generated", ""),
                    "label_mode": dmeta.get("label_mode", ""),
                    "horizon": dmeta.get("horizon", 10)},
        "model": {"trained": trained_at, "age_days": train_age,
                  "train_end": model_day, "fit_end_lag": model_lag,
                  "engine": mmeta.get("engine", ""),
                  "ic": (mmeta.get("metrics") or {}).get("rank_ic"),
                  "ic_ir": (mmeta.get("metrics") or {}).get("ic_ir"),
                  # 头部超额：IC 正不代表按榜单买能赚（全市场下跌时排序仍成立、
                  # 但头部相对池子的优势会消失）。这两个数才是「照着买会怎样」。
                  "head10": (mmeta.get("metrics") or {}).get("head10"),
                  "head10_win": (mmeta.get("metrics") or {}).get("head10_win"),
                  "feature_version": mmeta.get("feature_version"),
                  "feature_drift": feat_drift,
                  "missing_features": missing_feats,
                  "dataset_newer": dataset_newer,
                  "dataset_generated": ds_generated,
                  "trained_dataset_generated": model_ds_generated,
                  "retrain_due": bool(retrain_due)},
        "actions": actions,
        "stale": bool(warns),
        "data_stale": bool(data_stale),
        "retrain_cal_days": RETRAIN_CAL_DAYS,
        "run": {"running": RUN["running"], "level": RUN["level"], "started": RUN["started"],
                "ok": (RUN["result"] or {}).get("ok"),
                # 运行中：step=当前段（含 index/total/detail/pct），steps=已完成的段。
                # 运行结束后 result 里才有完整 steps，两者都返回让前端统一处理。
                # 必须拷贝：流水线线程会往同一个 dict 里加 "done" 键，
                # jsonify 正在遍历时加键会抛 dictionary changed size during iteration。
                "step": dict(RUN["step"]) if RUN.get("step") else None,
                "steps": (RUN["result"] or {}).get("steps") or list(RUN.get("steps") or []),
                "elapsed": (int(time.time() - RUN["t0"]) if RUN.get("t0") else None)},
    }


def _wait_bulk(timeout: int = 1800, on_tick=None) -> dict:
    """等增量行情跑完（bulkdata 是异步线程，pipeline 需要同步语义）。

    超时不等于完成：bulkdata 可能还在逐只写库。此时必须明确返回 running=True，
    调用方跳过后续快照/训练集，避免在一半新、一半旧的行情上算截面。

    on_tick(st)：每 3 秒回调一次 bulkdata 的实时状态，供流水线上报进度。
    行情增量是全流程里最慢的一段（全市场几分钟），没有这个回调，
    用户在界面上只能看到一个「运行中」，不知道到底走到第几只。
    """
    import bulkdata
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = bulkdata.status()
        if on_tick is not None:
            try:
                on_tick(st)
            except Exception:
                pass
        if not st.get("running"):
            return st
        time.sleep(3)
        st = bulkdata.status()
        if not st.get("running"):
            return st
        time.sleep(3)
    st = bulkdata.status()
    return {"ok": False, "running": bool(st.get("running")),
            "done": st.get("done"), "total": st.get("total"),
            "error": "增量行情超过 %d 秒仍未结束（%s/%s），已跳过后续快照与训练集"
                     % (timeout, st.get("done") or 0, st.get("total") or 0)}


def _dataset_opts() -> dict:
    """重建训练集时沿用上一次的口径。

    自动更新/完整重建只负责「把新行情补进训练集」，不应该把用户手动调过的
    起始日、采样步长、股票上限、中性化方式悄悄改回出厂默认值。
    """
    d = screener._json_read(screener.DATASET_META, {}) or {}
    neutral = str(d.get("neutral") or "none")
    if neutral not in screener.NEUTRAL_MODES:
        neutral = "none"
    label = str(d.get("label_mode") or "rank")
    if label not in screener.LABEL_MODES:
        label = "rank"
    try:
        req = d.get("max_symbols")
        # 800 而不是 300：抽样 300 只时换手率（模型最重要因子）的截面标准差
        # 比全市场高 42%，同一只票的 z 会被压缩，模型学到的阈值与推理口径错配。
        # 实测 800 只可把该偏差压到 2%，Top20 与全市场口径完全一致。
        symbols = int(req) if req else max(800, int(d.get("symbols") or 0))
    except Exception:
        symbols = 800
    return {
        "start": str(d.get("start") or "20220101"),
        "horizon": int(d.get("horizon") or 10),
        "step": int(d.get("step") or 5),
        "max_symbols": max(10, symbols),
        "scope": str(d.get("scope") or "cached"),
        "neutral_mode": neutral,
        "label_mode": label,
    }


def run(level: str = "auto", quote: bool = False, workers: int | None = None,
        trigger: str = "manual", only: list | None = None) -> dict:
    """执行流水线，并把本次运行落进 store.runs（数据中枢统一更新记录）。

    level: quick=只重算快照 / full=快照+训练集+模型 / auto=行情+快照+训练集（不自动重训模型）
    quote: 是否先做增量行情（auto 下若行情过期会自动打开）
    trigger: manual / scheduled / shortcut，记录触发来源
    only: 显式选段 ["quote","snapshot","dataset","model"]，给了就忽略 level/auto 判断
    """
    if not LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有流水线任务在跑，等它结束再试"}
    if not PROC_LOCK.acquire():
        LOCK.release()
        return {"ok": False,
                "error": "另一个进程正在跑流水线（定时任务或 pipeline_daily.bat），等它结束再试"}
    try:
        rid = store.run_start("pipeline", trigger, level)
    except Exception:
        # run_start 在下面的 try 之外，抛错会把两把锁一起泄漏
        PROC_LOCK.release()
        LOCK.release()
        raise
    RUN["t0"] = time.time()
    RUN["steps"] = []
    RUN["step"] = None
    try:
        before = freshness()
        if not before.get("ok"):
            store.run_finish(rid, False, [], error=before.get("error", ""))
            return before
        # 只有 warn 级才是真正待办：训练集落后 1~3 个交易日属正常（尾部要等未来收益），
# info 级也带 auto=True，若一起算进来，每天的自动更新都会重建一次训练集。
        need = {a["key"] for a in before["actions"]
                if a.get("auto") and a.get("level") == "warn"}
        if only:
            sel = set(only)
            quote = "quote" in sel
            do_snap = "snapshot" in sel
            do_ds = "dataset" in sel
            do_train = "model" in sel
        elif level == "auto":
            # 先只决定要不要拉行情。快照/训练集是否落后，必须等行情落库后重新判断：
            # 更新前快照可能正好是最新的，但行情增量后就会落后一天。
            # cover（覆盖率不足）也走增量：它就是「有票没跟上数据日」。
            # universe / spot 与行情增量同属「每日全市场刷新」那一段（见下面的
            # quote 分支），所以它们过期时也要把这一段打开，否则点「更新到最新」
            # 只重算快照，名单和实时价还是旧的。
            quote = quote or bool(need & {"quote", "cover", "universe", "spot"})
            do_snap = do_ds = False
            do_train = False          # 自动更新只做 1~3 段；模型按月提醒、手动重训
        else:
            do_snap = True
            do_ds = level == "full"
            do_train = level == "full"

        steps = []
        quote_failed = False
        # 运行中就把已完成的段同步到 RUN["steps"]，前端轮询能画出「第 1/3 步已完成」。
        # 只写进局部 steps 的话，整个运行期间 RUN 里没有任何增量信息。
        RUN["steps"] = steps

        def _mark(key, name, total):
            """登记「现在开始跑第 i 段」，前端据此显示当前段与进度。

            total 是本次预计算的段数（见上面 _quote_steps）；万一实际跑的段数
            超过预估（某个可选段临时打开了），用 max 兜底，避免出现「第 6/2 步」
            这种自相矛盾的进度。
            """
            idx = len(steps) + 1
            RUN["step"] = {"index": idx,
                           "total": max(int(total or 0), idx),
                           "key": key, "name": name, "detail": "", "pct": None}

        def _progress(detail, pct=None):
            """当前段内部的细粒度进度（如行情 1234/5579）。"""
            s = RUN.get("step")
            if s is not None:
                s["detail"] = detail
                if pct is not None:
                    s["pct"] = max(0.0, min(1.0, float(pct)))

        # 本次实际会跑几段，决定进度分母（用户点「更新到最新」时最关心还剩几步）。
        # 行情分支最多带 6 个子步骤（名单/上市名单/实时快照/估值/资金流/增量行情），
        # 其中估值和资金流要看配置是否启用 —— 事先尽量数准，数不准时 _mark 会兜底。
        _quote_steps = 0
        if quote:
            _quote_steps = 3                       # 名单 / 上市名单 / 实时快照
            try:
                import valsrc
                if valsrc.any_enabled():
                    _quote_steps += 1              # 全市场估值
            except Exception:
                pass
            try:
                import capital as _cap_probe
                _cap_probe.fresh()
                _quote_steps += 1                  # 资金流
            except Exception:
                pass
            _quote_steps += 1                      # 增量行情
        _total = _quote_steps + (1 if do_snap else 0) + (1 if do_ds else 0) + (1 if do_train else 0)
        if _total <= 0:
            _total = 1

        def _step(name, fn, soft=False, key=""):
            t0 = time.time()
            _mark(key, name, _total)
            try:
                r = fn()
                ok = bool(r.get("ok", True))
                detail = _brief(r)
                # 拉到旧数据不算失败，但要在更新记录里留痕：否则整条记录一片绿，
                # 「估值停在哪天」没人看得出来，下次照旧复用旧值。
                if isinstance(r, dict) and r.get("stale"):
                    detail = ("⚠ " + str(r.get("message") or detail))[:200]
                steps.append({"name": name, "ok": ok, "soft": soft,
                              "ms": int((time.time() - t0) * 1000),
                              "detail": detail})
                return r
            except Exception as e:                     # noqa: BLE001
                steps.append({"name": name, "ok": False, "soft": soft,
                              "ms": int((time.time() - t0) * 1000),
                              "detail": str(e)[:200]})
                # 步骤异常以前只留在这一步的 detail 里，跑完就找不到了；
                # 系统日志留一份带上下文的，排查「这步一直失败」时能对上时间。
                try:
                    import store
                    store.log("pipeline", "%s 失败：%s" % (name, str(e)[:180]),
                              level="error", act=name, detail=str(e)[:1500])
                except Exception:
                    pass
                return {"ok": False, "error": str(e)}


        if quote:
            # 名单 / 实时快照 / 估值与行情增量是同一套「每日全市场刷新」：
            # 并入管道后定时任务一次跑齐，不必再去股票池页分别点按钮。
            import universe
            _step("全 A 名单", lambda: universe.fetch_all())
            # 上市名单跟着每日刷新走：一次请求、当日已同步会直接返回，成本近零。
            # 它是回放正确性的地基 —— 没有 out_date，退市股永远不会从样本里剔除。
            import listing
            # soft：baostock 被限流时这一步会失败，但行情能由快照补齐，
            # 不该因此把整条「更新到最新」判成失败（待办里会单独提示名单没更新）。
            _step("上市名单", lambda: listing.sync(), soft=True)
            import spot
            _step("实时快照", lambda: spot.refresh())
            try:
                import valsrc
                import datasource
                if valsrc.any_enabled():
                    _step("全市场估值", lambda: datasource.fetch_daily_basic())
            except Exception:
                pass
            # 资金流跟着每日刷新走。非 force 会跳过已入库的日期，所以日常只拉新增那一天；
            # 一次都没同步过时用 25 天把体检样本铺起来。
            # soft：东财公开接口偶发限流/改字段，不该因此把整条更新判成失败。
            try:
                import capital as _capital
                _cf = _capital.fresh()
                _cdays = 25 if not _cf.get("total") else 5
                _step("资金流", lambda: _capital.sync(days=_cdays), soft=True)
            except Exception:
                pass
            import bulkdata
            r = bulkdata.start("incremental", None, "all", workers, log=False)
            if not r.get("ok"):
                steps.append({"name": "增量行情", "ok": False, "detail": r.get("error", "")})
            else:
                _mark("quote", "增量行情", _total)

                def _tick(st):
                    d, t = int(st.get("done") or 0), int(st.get("total") or 0)
                    _progress("%d / %d 只 · 成功 %s · 跳过 %s · 失败 %s"
                              % (d, t, st.get("ok") or 0, st.get("skip") or 0,
                                 st.get("fail") or 0),
                              (d / t) if t else None)

                st = _wait_bulk(on_tick=_tick)
                detail = "更新 %s 只，失败 %s 只，缓存共 %s 只" \
                         % (st.get("ok") or 0, st.get("fail", ""), st.get("cached_total", ""))
                # rows=0 说明本轮没下载到新行（行情源没出当天日线、或已是最新），
                # 此时把「对账后确认已到最新」的只数说出来，否则「更新 0 只」像没干活
                if not st.get("rows") and st.get("fresh"):
                    detail = "已到最新 %s 只，失败 %s 只，缓存共 %s 只" \
                             % (st.get("fresh"), st.get("fail", ""), st.get("cached_total", ""))
                src = str(st.get("source") or "")
                if src == "spot" and st.get("blocked"):
                    detail = "baostock 登录被限流（黑名单用户），当日 K 线全部由快照补齐 · " + detail
                elif src == "spot":
                    detail = "baostock 连不上，已用当日快照补日线 · " + detail
                elif src.startswith("spot+"):
                    tp = st.get("topup") or {}
                    detail = "baostock 未出当日日线，用快照补齐 %s（%s 只） · %s" \
                             % (tp.get("day", ""), tp.get("updated", 0), detail)
                elif st.get("blocked"):
                    detail = "baostock 登录被限流（黑名单用户）· " + detail
                err = st.get("error") or ""
                quote_failed = bool(err)
                steps.append({"name": "增量行情", "ok": not err,
                              "detail": (err or detail)})

        # 行情还在写库时绝不能往下算截面：让本次流水线以失败结束，
        # 前端会显示增量超时，用户可等它跑完再点「更新到最新」。
        if quote_failed:
            after = freshness()
            msg = next((s.get("detail", "") for s in steps if not s.get("ok") and not s.get("soft")), "增量行情未完成")
            store.run_finish(rid, False, steps, data_day=after.get("as_of", ""), error=msg)
            return {"ok": False, "level": level, "quote": quote, "steps": steps,
                    "before": before, "after": after, "error": msg}

        if level == "auto" and not only:
            # 行情已经落库（或本来就不需要更新），这时再看快照/训练集是否落后。
            # 这样「行情增量成功但快照没跟上」不会漏掉；行情没变也不会白跑一遍。
            mid = freshness()
            mid_need = {a["key"] for a in mid.get("actions", [])
                        if a.get("auto") and a.get("level") == "warn"}
            do_snap = "snapshot" in mid_need
            do_ds = "dataset" in mid_need

        if do_snap:
            # 0 = 全市场；定时管道作为日常口径，跟界面默认保持一致
            _step("重算快照", lambda: screener.build_snapshot("cached", None, 0))
        if do_ds:
            opts = _dataset_opts()
            _step("重建训练集", lambda: screener.build_dataset(**opts))
        if do_train:
            _step("训练模型", lambda: mlmodel.train(None))

        after = freshness()
        # soft 步骤（如上市名单）失败只影响它自己，不能拖垮整条流水线
        ok = all(s["ok"] for s in steps if not s.get("soft")) if steps else True
        err = next((s.get("detail", "") for s in steps
                    if not s["ok"] and not s.get("soft")), "")
        store.run_finish(rid, ok, steps, data_day=after.get("as_of", ""), error=err)
        return {"ok": ok, "level": level, "quote": quote, "steps": steps,
                "before": before, "after": after}
    except Exception as e:                              # noqa: BLE001
        store.run_finish(rid, False, [], error=str(e)[:300])
        raise
    finally:
        PROC_LOCK.release()
        LOCK.release()


def run_async(level: str = "auto", quote: bool = False, trigger: str = "manual",
              only: list | None = None, workers: int | None = None) -> dict:
    """后台跑流水线，立即返回。前端轮询 freshness() 的 run 字段看进度。"""
    if RUN["running"] or LOCK.locked():
        return {"ok": False, "error": "已有流水线任务在跑（%s）" % (RUN["level"] or "未知阶段")}
    RUN.update({"running": True, "level": level,
                "started": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": None, "step": None, "steps": [], "t0": time.time()})

    def _go():
        try:
            RUN["result"] = run(level, quote, workers, trigger, only)
        except Exception as e:                          # noqa: BLE001
            RUN["result"] = {"ok": False, "error": str(e)[:200], "steps": []}
        finally:
            RUN["running"] = False
            # 保留 step/steps 供「刚跑完」的一瞬间展示，但把 step 标成结束态，
            # 否则前端会一直画着最后一段的「进行中」进度条。
            if isinstance(RUN.get("step"), dict):
                RUN["step"]["done"] = True
            RUN["t0"] = None

    threading.Thread(target=_go, name="pipeline", daemon=True).start()
    return {"ok": True, "started": True, "level": level}


def history(limit: int = 60) -> list[dict]:
    """数据中枢「更新记录」：流水线与各页快捷统一日志，最新在前。"""
    return store.runs_list(limit)


def _brief(r: dict) -> str:
    if not isinstance(r, dict):
        return ""
    if r.get("error"):
        return str(r["error"])[:160]
    bits = []
    if r.get("source_name"):
        bits.append("源=%s" % r["source_name"])      # 估值多源：记录这次用的是哪一路
    for k in ("as_of", "count", "rows", "cross_sections"):
        v = r.get(k)
        # rows 在快照接口里是明细列表，不能直接塞进一行摘要
        if isinstance(v, (int, float, str)) and v not in (None, "", 0):
            bits.append("%s=%s" % (k, v))
    if r.get("engine"):
        bits.append(str(r["engine"]))
    m = r.get("metrics") or {}
    if m.get("rank_ic") is not None:
        bits.append("RankIC=%.3f" % float(m["rank_ic"]))
    if m.get("ic_ir") is not None:
        bits.append("IR=%.2f" % float(m["ic_ir"]))
    # 训练步骤的 warning 是口径说明而非故障，排在数字后面，避免看起来像报错
    if bits:
        return " · ".join(bits)[:160]
    return str(r.get("warning") or "")[:160]


if __name__ == "__main__":
    import sys
    args = set(sys.argv[1:])
    if "--check" in args or not (args & {"--quick", "--full", "--auto"}):
        print(json.dumps(freshness(), ensure_ascii=False, indent=1))
    else:
        lv = "quick" if "--quick" in args else ("full" if "--full" in args else "auto")
        out = run(lv, quote="--data" in args)
        print(json.dumps(out, ensure_ascii=False, indent=1))
