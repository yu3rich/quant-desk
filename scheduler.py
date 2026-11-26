# -*- coding: utf-8 -*-
"""QuantDesk 内置定时器 · 收盘后自动把数据链推到最新

**默认关闭**：数据更新与选股都已在「待办」里提示，手动点一次即可；再挂一个
18:10 的班次只会在没人看的时候白跑全市场增量、反复敲数据源。实盘账户的
盘中刷新与收盘日结是另一套定时器（emtlink.py），不受这里的开关影响。

为什么放在进程内而不是系统计划任务：这套东西只在你开着工作台时才有意义，
用 Windows 计划任务反而会出现「服务没开 → 任务空跑一遍写坏文件」的坑。
进程内一个 daemon 线程，随服务启停，配置存在 data/scheduler.json 里，
界面上一目了然，不用去翻任务计划程序。

调度规则（刻意保守）：
  * 计划日、设定时刻之后的补跑窗口内触发（默认 18:10 + 4 小时）；
  * 另外只要「数据链确实落后于应有的交易日」就补跑一次 —— 电脑在 18:10 关着
    是最常见的漏跑原因，第二天打开工作台必须能自己补上，而不是干等下一个 18:10；
  * 同一天最多自动跑一次；失败只给一次重试（间隔半小时），避免整点轰炸数据源；
  * 已经在跑（手动点了「更新到最新」）就跳过，不叠加；
  * 只跑 1~3 段（行情/快照/训练集），模型重训仍按月提醒、手动确认 —— 见 pipeline.py。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "data" / "scheduler.json"

DEFAULT = {
    # 默认关闭：数据更新与选股都已在「待办」里提示，用户手动点一次即可，
    # 再挂一个 18:10 的班次只会在没人看的时候白跑全市场增量、敲数据源。
    # 想恢复自动更新，在「选股设置 → 数据准备」里打开即可（配置会落盘）。
    "enabled": False,
    # 排 18:10 而不是收盘后立刻跑：baostock 日线通常 17:30 后才入库，
    # 15:40 跑出来的增量还是前一日的数据，等于白跑一趟。
    "time": "18:10",
    "weekdays": [1, 2, 3, 4, 5],
    "quote": True,            # 顺带做行情增量，否则快照建在旧数据上
    "level": "auto",          # auto = 按 freshness 决定做哪几段
    "window_min": 240,        # 错过时刻 4 小时内仍补跑（晚开机也不至于当天没数据）
    "last": None,
}

_lock = threading.Lock()
_running = False
_started = False
_tick_lock = threading.Lock()
# 「数据落后几天」查询做了 5 分钟缓存：_tick 每 30 秒醒一次，
# 每次都去算一遍时效没意义，也不能因为频繁查询把 CPU 占住。
_LAG_TTL = 300
_LAG: dict = {"at": 0.0, "lag": 0, "as_of": "", "expected": ""}
# 估值的「现在能不能取」也缓存 60 秒：它要读两个 json + 查交易日历，
# 每 30 秒算一次没必要。估值的真值判断在 valsrc.precheck 里，这里只是省 IO。
_VAL_TTL = 60
_VAL: dict = {"at": 0.0, "can": False}
_val_running = False
_PRUNE_DAY = {"day": ""}          # 系统日志每天清一次（保留 30 天 / 5 万条）


def _prune_once(now: dt.datetime) -> None:
    d = now.strftime("%Y-%m-%d")
    if _PRUNE_DAY["day"] == d or now.hour < 3:
        return
    _PRUNE_DAY["day"] = d
    try:
        import store
        n = store.log_prune()
        if n:
            store.log("scheduler", "清理过期系统日志 %d 条" % n, level="info", act="prune")
    except Exception:
        pass


def _val_can() -> bool:
    """估值现在能不能自动取（盘中/退避/已取到都会给 False）。异常一律 False。"""
    now = time.time()
    if now - float(_VAL.get("at") or 0) < _VAL_TTL:
        return bool(_VAL.get("can"))
    can = False
    try:
        import valsrc
        if valsrc.any_enabled():
            can = bool(valsrc.precheck().get("can"))
    except Exception:
        can = False
    _VAL.update(at=now, can=can)
    return can


def _val_go():
    """只跑估值这一段。跑完清掉缓存，下一个 tick 立刻按新状态判断。"""
    global _val_running
    t0 = time.time()
    try:
        import datasource
        r = datasource.fetch_daily_basic() or {}
        ms = int((time.time() - t0) * 1000)
        # 定时估值也要进「更新记录」：以前只在数据源页手动点才有记录，
        # 15:05 自动取的那次在页面上是隐形的，事后没人知道它跑没跑。
        try:
            import store
            if r.get("blocked"):
                detail = "跳过：" + str(r.get("message") or "")[:180]
            elif r.get("ok"):
                detail = "来源 %s · %s · %s 只" % (
                    r.get("source_name") or "—", r.get("date") or "—", r.get("count") or 0)
            else:
                detail = "失败：" + str(r.get("error") or "")[:180]
            store.run_log("valuation", bool(r.get("ok")),
                          steps=[{"name": "全市场估值（定时）", "ok": bool(r.get("ok")),
                                  "ms": ms, "detail": detail}],
                          trigger="scheduled", data_day=str(r.get("date") or ""),
                          error=str(r.get("error") or "")[:200], ms=ms)
            if r.get("blocked"):
                store.log("scheduler", "估值班次跳过：%s" % (r.get("message") or ""),
                          level="info", act="val")
            elif r.get("ok"):
                store.log("scheduler", "估值班次完成：%s · %s 只 · 来源 %s" %
                          (r.get("date"), r.get("count") or 0, r.get("source_name") or "—"),
                          level="info", act="val", detail=(r.get("message") or "")[:600])
            else:
                store.log("scheduler", "估值班次失败：%s" % (r.get("error") or "未知"),
                          level="error", act="val")
        except Exception:
            pass
    except Exception as e:
        try:
            import store
            store.log("scheduler", "估值班次异常：%s" % e, level="error", act="val",
                      detail=str(e))
        except Exception:
            pass
    finally:
        _val_running = False
        _VAL.update(at=0.0)


def _data_lag() -> int:
    """数据链落后几个交易日（0 = 已是最新）。异常一律当 0，不要自己造提醒。"""
    now = time.time()
    if now - float(_LAG.get("at") or 0) < _LAG_TTL:
        return int(_LAG.get("lag") or 0)
    lag = 0
    try:
        import pipeline
        f = pipeline.freshness() or {}
        if f.get("ok"):
            lag = int(f.get("quote_lag") or 0)
            _LAG.update(as_of=f.get("as_of") or "", expected=f.get("expected") or "")
    except Exception:
        lag = 0
    _LAG.update(at=now, lag=lag)
    return lag


def _json_read(p: Path, d):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return d


_CFG_LOCK = threading.RLock()


def _json_write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名带 pid：Flask 线程与定时线程同时写时，固定名会互相截断
    tmp = p.with_name("%s.%d.tmp" % (p.stem, os.getpid()))
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def load() -> dict:
    cfg = dict(DEFAULT)
    raw = _json_read(CFG_PATH, {}) or {}
    if isinstance(raw, dict):
        for k in DEFAULT:
            if k in raw:
                cfg[k] = raw[k]
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["quote"] = bool(cfg.get("quote"))
    cfg["time"] = _norm_time(cfg.get("time"))
    wd = cfg.get("weekdays")
    cfg["weekdays"] = sorted({int(x) for x in wd if 1 <= int(x) <= 7}) if isinstance(wd, list) else [1, 2, 3, 4, 5]
    if not cfg["weekdays"]:
        cfg["weekdays"] = [1, 2, 3, 4, 5]
    try:
        cfg["window_min"] = max(5, min(720, int(cfg.get("window_min") or 240)))
    except Exception:
        cfg["window_min"] = 240
    if cfg.get("level") not in ("auto", "quick"):
        cfg["level"] = "auto"
    return cfg


def save(cfg: dict) -> dict:
    # load→改→写 必须整体加锁：定时线程同时在写 last，两边都读旧值会互相覆盖
    with _CFG_LOCK:
        cur = load()
        cur.update({k: v for k, v in (cfg or {}).items() if k in DEFAULT})
        cur["time"] = _norm_time(cur.get("time"))
        _json_write(CFG_PATH, cur)
        return load()


def _norm_time(v) -> str:
    s = str(v or "").strip()
    try:
        h, m = s.split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return "%02d:%02d" % (h, m)
    except Exception:
        pass
    return DEFAULT["time"]


def _target(day: dt.date, hm: str) -> dt.datetime:
    h, m = [int(x) for x in hm.split(":")]
    return dt.datetime.combine(day, dt.time(h, m))


def next_run(cfg: dict | None = None, now: dt.datetime | None = None) -> dt.datetime | None:
    """下一次计划执行时间；停用或未来 8 天都没有可用工作日则返回 None。"""
    cfg = cfg or load()
    if not cfg.get("enabled"):
        return None
    now = now or dt.datetime.now()
    for i in range(0, 9):
        d = (now + dt.timedelta(days=i)).date()
        if d.isoweekday() not in cfg["weekdays"]:
            continue
        t = _target(d, cfg["time"])
        if t <= now:
            continue
        # 当天这一班已经跑过（运行时间在计划时刻之后）才不再排今天。
        # 白天补跑不算占位：18:10 那一班还要拿当天收盘的行情。
        last = cfg.get("last") or {}
        if (last.get("date") == d.strftime("%Y-%m-%d")
                and last.get("trigger") == "auto"
                and str(last.get("at") or "") >= d.strftime("%Y-%m-%d ") + cfg["time"]):
            continue
        return t
    return None


def _due(cfg: dict, now: dt.datetime) -> tuple[bool, str]:
    """是否该在本次 tick 触发，以及不触发的原因。"""
    if not cfg.get("enabled"):
        return False, "定时任务已关闭"
    last = cfg.get("last") or {}
    today = now.strftime("%Y-%m-%d")
    auto_today = last.get("date") == today and last.get("trigger") == "auto"
    runs_today = int(last.get("tries") or 0) if auto_today else 0
    # 今天已经成功跑过：只有「数据已经补齐」才算收工。
    # 白天补跑的是昨天的行情，晚上收盘后数据日又会落后一天，18:10 那一班还得照跑，
    # 否则今天收盘的行情要拖到明天才入库 —— 一天最多两次自动运行，防止空转敲门。
    if auto_today and last.get("ok") is not False:
        if _data_lag() <= 0:
            return False, "今天已经跑过"
        if runs_today >= 2:
            return False, "今天已自动跑过 2 次，数据仍落后（可能数据源还没出当天日线）"
    # 今天失败过：最多再试一次，且间隔至少半小时。
    # 无人值守时数据源抽风很常见，但绝不无限重试 —— 反复敲门只会被限流。
    if auto_today and last.get("ok") is False:
        if runs_today >= 2:
            return False, "今天已跑过（含一次重试）"
        try:
            at = dt.datetime.strptime(str(last.get("at") or ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            at = None
        if at and (now - at).total_seconds() < 1800:
            return False, "上次失败，半小时后重试"
    # 一、正常班次：计划日 + 到点 + 在补跑窗口内 + 今天确实开市。
    # 周一至周五也可能休市（国庆/春节等）：确认今天真开市再拉数据，
    # 否则节假日会空跑一轮全市场增量，白白消耗数据源配额。
    planned = now.isoweekday() in cfg["weekdays"]
    t = _target(now.date(), cfg["time"])
    if planned and t <= now <= t + dt.timedelta(minutes=cfg["window_min"]):
        try:
            import pipeline
            if pipeline._is_trading_day(now.strftime("%Y%m%d")) is False:
                return False, "今天休市"
        except Exception:
            pass
        return True, ""
    # 二、漏跑补跑：18:10 那会儿电脑没开，是最常见的漏跑原因。第二天上午打开
    # 工作台，按原逻辑既不报待办也不补数据，用户看到的还是前天选出来的票。
    # 这里改成「只要数据链确实落后于应有交易日就补一次」，不再看几点、不看星期几
    # （周末补周五的行情同样有意义）；同一自然日最多一次，失败一次后半小时再试。
    if _data_lag() > 0:
        return True, ""
    if planned and now < t:
        return False, "未到 %s" % cfg["time"]
    return False, "数据已是最新"


def _record(**kw):
    with _CFG_LOCK:
        cfg = load()
        last = dict(cfg.get("last") or {})
        last.update(kw)
        cfg["last"] = last
        _json_write(CFG_PATH, cfg)


def _go(cfg: dict, trigger: str):
    """触发一次流水线，等它结束再把结果写回配置（前端靠 last 展示）。"""
    global _running
    import pipeline
    t0 = time.time()
    day = dt.datetime.now().strftime("%Y-%m-%d")
    prev = load().get("last") or {}
    # 同一天同一触发源的第二次执行记为「重试」，_due 靠它限制最多两次
    tries = int(prev.get("tries") or 0) + 1 \
        if (prev.get("date") == day and prev.get("trigger") == trigger) else 1
    # 这条「运行中」记录在 try 之外，写盘失败（Windows 上 tmp.replace 撞到
    # 杀软扫描/文件占用）会直接冲出 daemon 线程，下面的 finally 不执行，
    # _running 永久为 True —— 定时任务直到重启服务才恢复。包一层吞掉写盘错误。
    try:
        _record(date=day, at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                trigger=trigger, ok=None, brief="运行中", ms=0, tries=tries)
    except Exception:
        pass
    try:
        import store
        store.log("scheduler", "定时任务触发（%s，第 %d 次）" % (cfg.get("level") or "auto", tries),
                  level="info", act="trigger",
                  detail="行情增量=%s · 补跑窗口=%s 分钟" % (cfg.get("quote"), cfg.get("window_min")))
    except Exception:
        pass
    try:
        r = pipeline.run_async(cfg.get("level") or "auto", bool(cfg.get("quote")),
                               trigger="scheduled")
        if not r.get("ok"):
            _record(ok=False, brief=str(r.get("error") or "启动失败")[:120],
                    ms=int((time.time() - t0) * 1000))
            try:
                import store
                store.log("scheduler", "流水线启动失败：%s" % (r.get("error") or ""),
                          level="error", act="trigger")
            except Exception:
                pass
            return
        # run_async 把进度写在 pipeline.RUN，这里跟着等，前端看到的是同一份状态
        while pipeline.RUN.get("running"):
            time.sleep(2)
        res = pipeline.RUN.get("result") or {}
        steps = res.get("steps") or []
        bad = [s for s in steps if not s.get("ok")]
        if not steps:
            brief = "无需更新，数据链已是最新"
        elif bad:
            brief = "%d 步失败：%s" % (len(bad), bad[0].get("name", ""))
        else:
            brief = "、".join(s.get("name", "") for s in steps) + " 完成"
        _record(ok=bool(res.get("ok", not bad)), brief=brief[:120],
                ms=int((time.time() - t0) * 1000))
        try:
            import store
            if bad:
                store.log("scheduler", "定时任务有 %d 步失败：%s" % (len(bad), brief),
                          level="error", act="finish",
                          detail="；".join("%s：%s" % (s.get("name"), s.get("detail"))
                                          for s in bad)[:1500])
            else:
                store.log("scheduler", "定时任务完成：%s" % brief, level="info", act="finish",
                          detail="耗时 %.0f 秒" % (time.time() - t0))
        except Exception:
            pass
    except Exception as e:                              # noqa: BLE001
        _record(ok=False, brief=str(e)[:120], ms=int((time.time() - t0) * 1000))
        try:
            import store
            store.log("scheduler", "定时任务异常退出：%s" % e, level="error", act="finish",
                      detail=str(e))
        except Exception:
            pass
    finally:
        _running = False
        # 跑完立刻作废「落后几天」的缓存，下一次 tick 按新数据重新判断
        _LAG.update(at=0.0, lag=0)


def _tick():
    """每 30 秒看一眼是否到点。轻量：只读一个小 json。"""
    global _running, _val_running
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        import pipeline
        if _running or pipeline.RUN.get("running") or pipeline.LOCK.locked():
            return
        now = dt.datetime.now()
        _prune_once(now)
        cfg = load()
        due, _why = _due(cfg, now)
        if due:
            _running = True
            threading.Thread(target=_go, args=(cfg, "auto"), name="sched-auto", daemon=True).start()
            return
        # 估值有自己的到点时刻：开快照源时 15:05 就有当日 PE/PB，没必要跟行情一起
        # 等到 18:10（baostock 日线的入库时间）。否则 15:05~18:10 这三小时里，
        # 页面上的估值一直是昨天的，用户还得手动点 —— 而盘中手动点必然失败。
        # 必须跟总开关联动：关了定时却还偷偷拉估值，等于开关没生效。
        if not _val_running and cfg.get("enabled") and _val_can():
            _val_running = True
            threading.Thread(target=_val_go, name="sched-val", daemon=True).start()
    except Exception:
        pass
    finally:
        _tick_lock.release()


def _loop():
    # 启动后先等 20 秒，避开服务刚起来时的初始化请求高峰
    time.sleep(20)
    while True:
        try:
            _tick()
        except Exception:
            pass
        time.sleep(30)


def start():
    """随服务启动，重复调用安全（只起一个线程）。"""
    global _started
    if _started:
        return False
    _started = True
    if not CFG_PATH.exists():          # 首次启动落一份默认配置，界面上直接可改
        try:
            _json_write(CFG_PATH, dict(DEFAULT))
        except Exception:
            pass
    threading.Thread(target=_loop, name="scheduler", daemon=True).start()
    try:
        import store
        n = store.log_prune()          # 开机顺手清一次，别等 3 点
        cfg = load()
        store.log("scheduler", "调度线程已启动",
                  level="info", act="boot",
                  detail="计划 %s（周 %s）· 行情增量=%s · 估值到点自动取%s" %
                         (cfg.get("time"), ",".join(str(x) for x in cfg.get("weekdays") or []),
                          cfg.get("quote"), ("· 清理旧日志 %d 条" % n) if n else ""))
    except Exception:
        pass
    return True


def status() -> dict:
    cfg = load()
    now = dt.datetime.now()
    due, why = _due(cfg, now)
    nr = next_run(cfg, now)
    last = cfg.get("last") or {}
    return {
        "ok": True,
        "enabled": cfg["enabled"],
        "time": cfg["time"],
        "weekdays": cfg["weekdays"],
        "quote": cfg["quote"],
        "level": cfg["level"],
        "window_min": cfg["window_min"],
        "last": last,
        "running": bool(_running),
        "due_now": due,
        "state": ("运行中" if _running else
                  ("待执行" if due else why)),
        "next_run": nr.strftime("%Y-%m-%d %H:%M") if nr else "",
        "next_in_min": int((nr - now).total_seconds() // 60) if nr else None,
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
