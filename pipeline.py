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

BASE = Path(__file__).resolve().parent
LOCK = threading.Lock()
# 最近一次（或正在进行中的）流水线运行，供前端轮询展示进度
RUN: dict = {"running": False, "level": "", "started": "", "result": None}

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


def freshness() -> dict:
    """各环节时效与建议动作。轻量：只读 meta json，不读 CSV 全表。"""
    cal = _calendar()
    if not cal:
        return {"ok": False, "error": "没有行情缓存，先去「股票池」预热"}
    latest = cal[-1]
    now = dt.datetime.now()
    today = now.strftime("%Y%m%d")
    # A 股 15:00 收盘，留 10 分钟给数据源入库
    quote_stale = bool(now.weekday() < 5 and now.strftime("%H%M") >= "1510"
                       and latest < today)

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
    # 因子集版本变了必须重训：推理列与模型训练时不一致，新因子等于白加
    feat_drift = bool(mmeta.get("ok")) and \
        mmeta.get("feature_version") not in (None, screener.FEATURE_VERSION)

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
    retrain_due = bool(not mmeta.get("ok") or feat_drift or
                       (train_age is not None and train_age > RETRAIN_CAL_DAYS))
    if retrain_due:
        if not mmeta.get("ok"):
            why = "还没训练过模型"
        elif feat_drift:
            why = "因子集已升级到 v%s，模型还是 v%s" % (screener.FEATURE_VERSION,
                                                     mmeta.get("feature_version"))
        else:
            why = "模型训练于 %s（%s 天前）" % (trained_at[:10] if trained_at else "—", train_age)
        actions.append({"key": "model", "level": "warn", "auto": False,
                        "text": why + "；建议按月重训一次，不必天天重训"})

    warns = [a for a in actions if a["level"] == "warn"]
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
        "quote_stale": quote_stale,
        "quote_cached": len(list(screener.CACHE_DIR.glob("*.csv"))),
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
                  "retrain_due": bool(retrain_due)},
        "actions": actions,
        "stale": bool(warns),
        "retrain_cal_days": RETRAIN_CAL_DAYS,
        "run": {"running": RUN["running"], "level": RUN["level"], "started": RUN["started"],
                "ok": (RUN["result"] or {}).get("ok"),
                "steps": (RUN["result"] or {}).get("steps") or []},
    }


def _wait_bulk(timeout: int = 1800) -> dict:
    """等增量行情跑完（bulkdata 是异步线程，pipeline 需要同步语义）。"""
    import bulkdata
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = bulkdata.status()
        if not st.get("running"):
            return st
        time.sleep(3)
    return {"ok": False, "error": "增量行情超过 %d 秒仍未结束" % timeout}


def run(level: str = "auto", quote: bool = False, workers: int | None = None) -> dict:
    """执行流水线。

    level: quick=只重算快照 / full=快照+训练集+模型 / auto=按 freshness 决定
    quote: 是否先做增量行情（auto 下若行情过期会自动打开）
    """
    if not LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有流水线任务在跑，等它结束再试"}
    try:
        before = freshness()
        if not before.get("ok"):
            return before
        need = {a["key"] for a in before["actions"] if a["auto"]}
        if level == "auto":
            do_snap = "snapshot" in need
            do_ds = "dataset" in need
            # 模型到期 = 训练太久 或 因子集版本变了（版本变了必须重训，否则推理列对不上）
            m = before["model"]
            do_train = bool(m["retrain_due"] or m.get("feature_drift"))
            if not m["trained"]:
                do_ds = True          # 没模型时先把训练集备齐
            do_train = do_train and do_ds
            quote = quote or "quote" in need
        else:
            do_snap = True
            do_ds = level == "full"
            do_train = level == "full"

        steps = []

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
            import bulkdata
            r = bulkdata.start("incremental", None, "all", workers)
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
                steps.append({"name": "增量行情", "ok": not err,
                              "detail": (err or detail)})

        if do_snap:
            _step("重算快照", lambda: screener.build_snapshot("cached", None, 1500))
        if do_ds:
            d = before["dataset"]
            _step("重建训练集", lambda: screener.build_dataset(
                start="20220101", horizon=int(d.get("horizon") or 10), step=5,
                max_symbols=300, scope="cached",
                # 训练集口径固定用出厂实测最优：原始因子 + 横截面分位标签。
                # 行业中性化留作界面上的一键可切口径，不做出厂默认（实测 RankIC 更低）。
                neutral_mode="none",
                label_mode=d.get("label_mode") or "rank"))
        if do_train:
            _step("训练模型", lambda: mlmodel.train(None))

        after = freshness()
        return {"ok": all(s["ok"] for s in steps) if steps else True,
                "level": level, "quote": quote, "steps": steps,
                "before": before, "after": after}
    finally:
        LOCK.release()


def run_async(level: str = "auto", quote: bool = False) -> dict:
    """后台跑流水线，立即返回。前端轮询 freshness() 的 run 字段看进度。"""
    if RUN["running"] or LOCK.locked():
        return {"ok": False, "error": "已有流水线任务在跑（%s）" % (RUN["level"] or "未知阶段")}
    RUN.update({"running": True, "level": level,
                "started": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": None})

    def _go():
        try:
            RUN["result"] = run(level, quote)
        except Exception as e:                          # noqa: BLE001
            RUN["result"] = {"ok": False, "error": str(e)[:200], "steps": []}
        finally:
            RUN["running"] = False

    threading.Thread(target=_go, name="pipeline", daemon=True).start()
    return {"ok": True, "started": True, "level": level}


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
