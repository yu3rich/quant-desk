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

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
TRADE_CAL = DATA / "trade_cal.json"
LOCK = threading.Lock()
# 最近一次（或正在进行中的）流水线运行，供前端轮询展示进度
RUN: dict = {"running": False, "level": "", "started": "", "result": None}
# 交易日历缓存：只在「工作日 15:10 后、行情还没到最新」这个需要判断休市的时刻拉一次
_TCAL: dict = {"days": None}
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


def _ymd(v) -> str:
    return str(v or "").replace("-", "")[:8]


def _calendar() -> list[str]:
    """交易日历：取一只从不长期停牌的主板大票的日线日期。

    不用全市场并集是为了速度（5000+ 个文件读一遍要好几秒），
    600000 停牌缺失只会让「落后几天」的估算偏小，不影响是否过期的判断。
    """
    try:
        d = screener.load_history("600000")
        if d is None or len(d) < 10:
            return []
        return sorted({_ymd(x) for x in d["date"]})
    except Exception:
        return []


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
        return _TCAL["days"]
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
    _TCAL["days"] = days
    return days


def _is_trading_day(day: str) -> bool | None:
    """True/False=确定是/不是交易日；None=日历未覆盖或暂不可用。"""
    d = _ymd(day)
    days = _trade_days()
    if not d or not days or not (min(days) <= d <= max(days)):
        return None
    return d in days


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def freshness() -> dict:
    """各环节时效与建议动作。轻量：只读 meta json，不读 CSV 全表。"""
    cal = _calendar()
    if not cal:
        return {"ok": False, "error": "没有行情缓存，先去「股票池」预热"}
    latest = cal[-1]
    now = dt.datetime.now()
    today = now.strftime("%Y%m%d")
    # A 股 15:00 收盘，留 10 分钟给数据源入库。周末/节假日不提醒：
    # 只在「工作日 + 已过入库时间 + 缓存还没到当天」时才去确认今天到底开不开市。
    after_close = bool(now.weekday() < 5 and now.strftime("%H%M") >= "1510")
    trading_day = _is_trading_day(today) if (after_close and latest < today) else None
    quote_stale = bool(after_close and latest < today and trading_day is not False)

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
    if quote_stale:
        actions.append({"key": "quote", "level": "warn", "auto": True,
                        "text": "今日行情还没入库（缓存最新 %s），先做增量再重算截面" % latest})
    if snap_lag is None:
        actions.append({"key": "snapshot", "level": "warn", "auto": True,
                        "text": "还没有横截面快照，选股前需要先建一次"})
    elif snap_lag > 0:
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
        "as_of": latest,                     # 行情缓存最新交易日
        "today": today,
        "trading_day": trading_day,
        "quote_stale": quote_stale,
        "quote_cached": store.cached_count(),
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
                "steps": (RUN["result"] or {}).get("steps") or []},
    }


def _wait_bulk(timeout: int = 1800) -> dict:
    """等增量行情跑完（bulkdata 是异步线程，pipeline 需要同步语义）。

    超时不等于完成：bulkdata 可能还在逐只写库。此时必须明确返回 running=True，
    调用方跳过后续快照/训练集，避免在一半新、一半旧的行情上算截面。
    """
    import bulkdata
    t0 = time.time()
    while time.time() - t0 < timeout:
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
        symbols = int(req) if req else max(300, int(d.get("symbols") or 0))
    except Exception:
        symbols = 300
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
    rid = store.run_start("pipeline", trigger, level)
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
            quote = quote or "quote" in need
            do_snap = do_ds = False
            do_train = False          # 自动更新只做 1~3 段；模型按月提醒、手动重训
        else:
            do_snap = True
            do_ds = level == "full"
            do_train = level == "full"

        steps = []
        quote_failed = False

        def _step(name, fn):
            t0 = time.time()
            try:
                r = fn()
                ok = bool(r.get("ok", True))
                steps.append({"name": name, "ok": ok,
                              "ms": int((time.time() - t0) * 1000),
                              "detail": _brief(r)})
                return r
            except Exception as e:                     # noqa: BLE001
                steps.append({"name": name, "ok": False,
                              "ms": int((time.time() - t0) * 1000),
                              "detail": str(e)[:200]})
                return {"ok": False, "error": str(e)}

        if quote:
            # 名单 / 实时快照 / 估值与行情增量是同一套「每日全市场刷新」：
            # 并入管道后定时任务一次跑齐，不必再去股票池页分别点按钮。
            import universe
            _step("全 A 名单", lambda: universe.fetch_all())
            import spot
            _step("实时快照", lambda: spot.refresh())
            try:
                import datasource
                if datasource.configured():
                    _step("全市场估值", lambda: datasource.fetch_daily_basic())
            except Exception:
                pass
            import bulkdata
            r = bulkdata.start("incremental", None, "all", workers, log=False)
            if not r.get("ok"):
                steps.append({"name": "增量行情", "ok": False, "detail": r.get("error", "")})
            else:
                st = _wait_bulk()
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
            msg = next((s.get("detail", "") for s in steps if not s.get("ok")), "增量行情未完成")
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
        ok = all(s["ok"] for s in steps) if steps else True
        err = next((s.get("detail", "") for s in steps if not s["ok"]), "")
        store.run_finish(rid, ok, steps, data_day=after.get("as_of", ""), error=err)
        return {"ok": ok, "level": level, "quote": quote, "steps": steps,
                "before": before, "after": after}
    except Exception as e:                              # noqa: BLE001
        store.run_finish(rid, False, [], error=str(e)[:300])
        raise
    finally:
        LOCK.release()


def run_async(level: str = "auto", quote: bool = False, trigger: str = "manual",
              only: list | None = None, workers: int | None = None) -> dict:
    """后台跑流水线，立即返回。前端轮询 freshness() 的 run 字段看进度。"""
    if RUN["running"] or LOCK.locked():
        return {"ok": False, "error": "已有流水线任务在跑（%s）" % (RUN["level"] or "未知阶段")}
    RUN.update({"running": True, "level": level,
                "started": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": None})

    def _go():
        try:
            RUN["result"] = run(level, quote, workers, trigger, only)
        except Exception as e:                          # noqa: BLE001
            RUN["result"] = {"ok": False, "error": str(e)[:200], "steps": []}
        finally:
            RUN["running"] = False

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
