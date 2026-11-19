# -*- coding: utf-8 -*-
"""QuantDesk 内置定时器 · 收盘后自动把数据链推到最新

为什么放在进程内而不是系统计划任务：这套东西只在你开着工作台时才有意义，
用 Windows 计划任务反而会出现「服务没开 → 任务空跑一遍写坏文件」的坑。
进程内一个 daemon 线程，随服务启停，配置存在 data/scheduler.json 里，
界面上一目了然，不用去翻任务计划程序。

调度规则（刻意保守）：
  * 只在配置的星期几、且在设定时刻之后的补跑窗口内触发；
  * 同一天最多自动跑一次（失败也不反复重试，避免整点轰炸数据源）；
  * 已经在跑（手动点了「更新到最新」）就跳过，不叠加；
  * 只跑 1~3 段（行情/快照/训练集），模型重训仍按月提醒、手动确认 —— 见 pipeline.py。
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "data" / "scheduler.json"

DEFAULT = {
    "enabled": True,
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


def _json_read(p: Path, d):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return d


def _json_write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
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
        # 当天已经自动跑过，就不再排今天
        last = cfg.get("last") or {}
        if last.get("date") == d.strftime("%Y-%m-%d") and last.get("trigger") == "auto":
            continue
        return t
    return None


def _due(cfg: dict, now: dt.datetime) -> tuple[bool, str]:
    """是否该在本次 tick 触发，以及不触发的原因。"""
    if not cfg.get("enabled"):
        return False, "定时任务已关闭"
    if now.isoweekday() not in cfg["weekdays"]:
        return False, "今天不是计划日"
    t = _target(now.date(), cfg["time"])
    if now < t:
        return False, "未到 %s" % cfg["time"]
    if (now - t).total_seconds() > cfg["window_min"] * 60:
        return False, "已过补跑窗口（%d 分钟）" % cfg["window_min"]
    last = cfg.get("last") or {}
    if last.get("date") == now.strftime("%Y-%m-%d") and last.get("trigger") == "auto":
        if last.get("ok") is not False:
            return False, "今天已经跑过"
        # 失败给一次重试机会（无人值守时数据源抽风很常见），间隔至少半小时，
        # 但绝不无限重试 —— 整点反复敲门会把数据源限流。
        if int(last.get("tries") or 1) >= 2:
            return False, "今天已跑过（含一次重试）"
        try:
            at = dt.datetime.strptime(str(last.get("at") or ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            at = None
        if at and (now - at).total_seconds() < 1800:
            return False, "上次失败，半小时后重试"
    # 周一至周五也可能休市（国庆/春节等）：确认今天真开市再拉数据，
    # 否则节假日会空跑一轮全市场增量，白白消耗数据源配额。
    try:
        import pipeline
        if pipeline._is_trading_day(now.strftime("%Y%m%d")) is False:
            return False, "今天休市"
    except Exception:
        pass
    return True, ""


def _record(**kw):
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
    _record(date=day, at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trigger=trigger, ok=None, brief="运行中", ms=0, tries=tries)
    try:
        r = pipeline.run_async(cfg.get("level") or "auto", bool(cfg.get("quote")),
                               trigger="scheduled")
        if not r.get("ok"):
            _record(ok=False, brief=str(r.get("error") or "启动失败")[:120],
                    ms=int((time.time() - t0) * 1000))
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
    except Exception as e:                              # noqa: BLE001
        _record(ok=False, brief=str(e)[:120], ms=int((time.time() - t0) * 1000))
    finally:
        _running = False


def _tick():
    """每 30 秒看一眼是否到点。轻量：只读一个小 json。"""
    global _running
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        import pipeline
        if _running or pipeline.RUN.get("running") or pipeline.LOCK.locked():
            return
        cfg = load()
        due, _why = _due(cfg, dt.datetime.now())
        if not due:
            return
        _running = True
        threading.Thread(target=_go, args=(cfg, "auto"), name="sched-auto", daemon=True).start()
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
