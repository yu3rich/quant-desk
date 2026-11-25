# -*- coding: utf-8 -*-
"""SQLite 存储层：K线行情 / 选股记录 / 实盘账本的统一数据库。

为什么是 SQLite：Python 标准库自带（零新依赖）、单文件、WAL 模式下
读写并发安全、事务原子写入（不再需要 tmp+replace 防半截文件）。

表设计按访问模式来：
- bars 用 WITHOUT ROWID，主键 (code, date) 即聚簇索引 ——
  「取某只股票最近 n 根」是一次连续范围扫描，比 5000 个 CSV 的 seek 尾读更稳，
  且横截面查询（按日期跨股票）一条 SQL 就能做。
- bar_meta 每股一行（复权口径/来源/末日），增量更新挑股票时一条 SQL 全拿，
  不再逐只打开 5000 个 json。
- picks 主键 (as_of, recorded_at, code)：同一数据日多次选股各存一批，
  按批次评估 T+N，不会把不同参数/口径的实验混成一个均值。
- ledger_days 每日快照整体存 JSON（嵌套结构：overview/positions/orders/snaps）。
- fills/orders/flows 用「去重键 k 做主键 + payload JSON」：
  去重语义与旧 _upsert 完全一致（键函数在 emtlink），INSERT OR IGNORE 天然幂等。

旧 CSV/JSON 首次启动自动迁入（flags 表记迁移完成），原文件保留不动作备份。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import qdpaths

import pandas as pd

BASE = Path(__file__).resolve().parent
DATA = qdpaths.data_path()
DB_PATH = DATA / "quant.db"
LEGACY_CACHE = DATA / "cache"
LEGACY_PICKS = DATA / "screen" / "picks.csv"

COLS = ("date", "open", "high", "low", "close", "volume", "amount", "pct", "turnover")
_NUM_COLS = COLS[1:]
_KV_TABLES = {"fills", "orders", "flows"}

_local = threading.local()
_init_lock = threading.Lock()
_ready = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL, pct REAL, turnover REAL,
  PRIMARY KEY(code, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bar_meta(
  code TEXT PRIMARY KEY, basis TEXT, source TEXT, updated TEXT,
  last_date TEXT, start TEXT, rows INTEGER);
CREATE TABLE IF NOT EXISTS picks(
  as_of TEXT NOT NULL, recorded_at TEXT NOT NULL, code TEXT NOT NULL,
  name TEXT, industry TEXT, rank INTEGER, score REAL, close REAL,
  PRIMARY KEY(as_of, recorded_at, code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS picks_meta(
  as_of TEXT NOT NULL, recorded_at TEXT NOT NULL, code TEXT NOT NULL,
  payload TEXT,
  PRIMARY KEY(as_of, recorded_at, code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pick_archive(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT, recorded_at TEXT, at TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS pick_archive_at ON pick_archive(id DESC);
CREATE TABLE IF NOT EXISTS pick_bear(
  as_of TEXT NOT NULL, recorded_at TEXT NOT NULL,
  at TEXT, mode TEXT, payload TEXT,
  PRIMARY KEY(as_of, recorded_at)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pick_llm(
  as_of TEXT NOT NULL, recorded_at TEXT NOT NULL,
  at TEXT, mode TEXT, model TEXT, payload TEXT,
  PRIMARY KEY(as_of, recorded_at)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS pick_llm_asof ON pick_llm(as_of);
CREATE TABLE IF NOT EXISTS ledger_days(day TEXT PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS fills(k TEXT PRIMARY KEY, date TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS orders(k TEXT PRIMARY KEY, date TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS flows(k TEXT PRIMARY KEY, date TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS flags(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, trigger TEXT, level TEXT,
  started TEXT, finished TEXT, ok INTEGER,
  data_day TEXT, steps TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS runs_id_desc ON runs(id DESC);
CREATE INDEX IF NOT EXISTS runs_kind ON runs(kind, id DESC);
CREATE TABLE IF NOT EXISTS syslog(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, level TEXT NOT NULL, src TEXT NOT NULL,
  act TEXT, msg TEXT, detail TEXT, ref TEXT);
CREATE INDEX IF NOT EXISTS syslog_id_desc ON syslog(id DESC);
CREATE INDEX IF NOT EXISTS syslog_level ON syslog(level, id DESC);
CREATE INDEX IF NOT EXISTS syslog_src ON syslog(src, id DESC);
CREATE TABLE IF NOT EXISTS capital(
  code TEXT NOT NULL, date TEXT NOT NULL,
  lhb_net REAL, lhb_amt REAL, blk_amt REAL, blk_prem REAL,
  margin_bal REAL, margin_net REAL, north_ratio REAL, north_chg REAL,
  PRIMARY KEY(code, date)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS capital_date ON capital(date);
CREATE TABLE IF NOT EXISTS stock_report(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL, name TEXT, as_of TEXT, at TEXT,
  mode TEXT, model TEXT, score REAL, rank INTEGER, price REAL, pct REAL,
  payload TEXT);
CREATE INDEX IF NOT EXISTS stock_report_code ON stock_report(code, id DESC);
-- 风险预算按交易日留一行，用于看「预算使用率」随时间的走势。
-- 只保留最近 400 行（约一年半交易日），避免体检每刷新一次就无限长。
CREATE TABLE IF NOT EXISTS risk_daily(
  date TEXT PRIMARY KEY, total REAL, budget REAL, usage REAL,
  top_code TEXT, top_name TEXT, top_risk REAL,
  exposure REAL, n_pos INTEGER, at TEXT);
-- 目标持仓计划：生成时存档，用来对比之后实际成交（计划-执行偏差）
CREATE TABLE IF NOT EXISTS plan(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, as_of TEXT, payload TEXT);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        DATA.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.c = c
    ensure()
    return c


@contextlib.contextmanager
def _migration_lock(timeout: float = 120.0):
    """跨进程互斥：首次启动时多个进程（主服务 / 行情子进程）可能同时迁移。

    SQLite 自身能串行写，但旧 CSV 迁移是「读文件 → 多批 INSERT」，没有文件锁时
    两个进程会重复扫 5000 个 CSV，并在低超时下互相抢写锁。这里用系统文件锁把
    整个 ensure 串起来；进程异常退出时锁由操作系统释放，不会留下死锁。
    """
    DATA.mkdir(parents=True, exist_ok=True)
    f = open(DATA / ".migrate.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            t0 = time.time()
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    if time.time() - t0 > timeout:
                        raise RuntimeError("等待数据库迁移锁超时")
                    time.sleep(0.2)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def ensure() -> None:
    """建表 + 一次性自动迁移（进程内只跑一次，多进程靠文件锁 + flags 表幂等）。"""
    global _ready
    if _ready:
        return
    with _init_lock:
        if _ready:
            return
        with _migration_lock():
            c = _local.c
            c.executescript(_SCHEMA)
            c.commit()
            if not _flag(c, "picks_multibatch"):
                _migrate_picks_pk(c)
            if not _flag(c, "bars_migrated"):
                _migrate_bars(c)
            if not _flag(c, "picks_migrated"):
                _migrate_picks(c)
        _ready = True


def _flag(c, k: str) -> bool:
    r = c.execute("SELECT v FROM flags WHERE k=?", (k,)).fetchone()
    return bool(r and r[0] == "1")


def _set_flag(c, k: str) -> None:
    c.execute("INSERT OR REPLACE INTO flags(k, v) VALUES(?, '1')", (k,))
    c.commit()


def migration_done(key: str) -> bool:
    """公开迁移状态查询：调用方据此跳过旧文件扫描，不必自己再判一遍。"""
    return _flag(conn(), key)


# --------------------------------------------------------------------------
# 旧数据迁移
# --------------------------------------------------------------------------

def _migrate_bars(c) -> None:
    files = sorted(LEGACY_CACHE.glob("*.csv")) if LEGACY_CACHE.exists() else []
    if not files:
        _set_flag(c, "bars_migrated")
        return
    failed = []
    for fp in files:
        code = fp.stem
        try:
            d = pd.read_csv(fp, dtype={"date": str})
            d["date"] = d["date"].astype(str).str.slice(0, 10)
            for col in _NUM_COLS:
                if col not in d.columns:
                    d[col] = None
                d[col] = pd.to_numeric(d[col], errors="coerce")
            d = d[(d["close"] > 0) & (d["open"] > 0)]
            d = d.sort_values("date").drop_duplicates("date")
            rows = [(code, r["date"],
                     *[None if pd.isna(r[col]) else float(r[col]) for col in _NUM_COLS])
                    for r in d.to_dict("records")]
            meta = {}
            mp = fp.with_suffix(".json")
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8")) or {}
                except Exception:
                    meta = {}
            if rows:
                c.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            c.execute("INSERT OR REPLACE INTO bar_meta VALUES(?,?,?,?,?,?,?)",
                      (code, meta.get("basis", ""), meta.get("source", ""),
                       meta.get("updated", ""), meta.get("last_date", "") or (rows[-1][1] if rows else ""),
                       meta.get("start", ""), len(rows)))
            c.commit()
        except Exception:
            c.rollback()
            failed.append(fp.stem)
    if failed:
        # 不置迁移标记：下次启动会重试（INSERT OR REPLACE 幂等），避免静默丢数据
        raise RuntimeError("K线迁移失败 %d 只：%s ..." % (len(failed), ",".join(failed[:5])))
    _set_flag(c, "bars_migrated")


def _migrate_picks_pk(c) -> None:
    """旧表主键是 (as_of, code)，同一天再选会覆盖上一批。

    改成 (as_of, recorded_at, code) 后，同一数据日可以保留多次选股实验，
    记录页的「批次」才和实际语义一致。SQLite 改主键必须重建表。
    """
    try:
        info = c.execute("PRAGMA table_info(picks)").fetchall()
        pk = [r[1] for r in sorted(info, key=lambda x: x[5]) if r[5]]
    except Exception:
        pk = []
    if pk != ["as_of", "code"]:
        _set_flag(c, "picks_multibatch")
        return
    try:
        c.execute("ALTER TABLE picks RENAME TO picks_legacy_pk")
        c.execute(
            "CREATE TABLE picks("
            "as_of TEXT NOT NULL, recorded_at TEXT NOT NULL, code TEXT NOT NULL,"
            "name TEXT, industry TEXT, rank INTEGER, score REAL, close REAL,"
            "PRIMARY KEY(as_of, recorded_at, code)) WITHOUT ROWID")
        c.execute(
            "INSERT OR REPLACE INTO picks(as_of,recorded_at,code,name,industry,rank,score,close) "
            "SELECT as_of,COALESCE(recorded_at,''),code,name,industry,rank,score,close "
            "FROM picks_legacy_pk")
        c.execute("DROP TABLE picks_legacy_pk")
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    _set_flag(c, "picks_multibatch")


def _migrate_picks(c) -> None:
    if LEGACY_PICKS.exists():
        try:
            d = pd.read_csv(LEGACY_PICKS, dtype={"code": str, "as_of": str})
            rows = []
            for r in d.to_dict("records"):
                rows.append((str(r.get("as_of") or "")[:10], str(r.get("recorded_at") or ""),
                             str(r.get("code") or "").zfill(6),
                             r.get("name") or "", r.get("industry") or "",
                             _i(r.get("rank")), _f(r.get("score")), _f(r.get("close"))))
            c.executemany(
                "INSERT OR REPLACE INTO picks(as_of,recorded_at,code,name,industry,rank,score,close) "
                "VALUES(?,?,?,?,?,?,?,?)", rows)
            c.commit()
        except Exception:
            c.rollback()
    _set_flag(c, "picks_migrated")


def migrate_ledger(days: list[tuple[str, dict]],
                   kv: dict[str, list[tuple[str, str, dict]]]) -> None:
    """emtlink 专用：每日快照 + fills/orders/flows（键由 emtlink 的去重函数算好）。"""
    c = conn()
    if _flag(c, "ledger_migrated"):
        return
    for day, rec in days:
        c.execute("INSERT OR REPLACE INTO ledger_days VALUES(?,?)",
                  (day, json.dumps(rec, ensure_ascii=False)))
    for table, items in kv.items():
        if table not in _KV_TABLES:
            continue
        c.executemany("INSERT OR IGNORE INTO %s VALUES(?,?,?)" % table,
                      [(k, day, json.dumps(r, ensure_ascii=False)) for k, day, r in items])
    c.commit()
    _set_flag(c, "ledger_migrated")


def _f(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return None if x is None else int(x)


# --------------------------------------------------------------------------
# K线
# --------------------------------------------------------------------------

def bars_tail(code: str, n: int) -> list[tuple]:
    """最近 n 根有效日线（升序）。主键范围扫描，与历史总量无关。"""
    rows = conn().execute(
        "SELECT date,open,high,low,close,volume,amount,pct,turnover "
        "FROM bars WHERE code=? AND close>0 AND open>0 ORDER BY date DESC LIMIT ?",
        (str(code).zfill(6), int(n))).fetchall()
    return [tuple(r) for r in reversed(rows)]


def bars_frame(code: str, n: int | None = None) -> pd.DataFrame | None:
    """整段（或尾部 n 根）日线 DataFrame，列与旧 CSV 完全一致。"""
    code = str(code).zfill(6)
    if n is None:
        rows = conn().execute(
            "SELECT date,open,high,low,close,volume,amount,pct,turnover "
            "FROM bars WHERE code=? AND close>0 AND open>0 ORDER BY date", (code,)).fetchall()
        rows = [tuple(r) for r in rows]
    else:
        rows = bars_tail(code, n)
    if not rows:
        return None
    return pd.DataFrame(rows, columns=COLS)


def _drop_chart_cache() -> None:
    """行情落库后清图表缓存。

    chartdata 的日线缓存 TTL 180 秒：补完当日 K 线后立刻看个股图，
    最长三分钟内还是旧图，表现成「更新完了图没变」。延迟 import 避免循环依赖。
    """
    try:
        import chartdata
        chartdata.invalidate()
    except Exception:
        pass


def bars_write(code: str, df: pd.DataFrame, source: str, basis: str,
               start: str | None = None) -> None:
    """整只覆盖写（一个事务：要么全见要么全不见，快照/回测不会读到半截数据）。"""
    code = str(code).zfill(6)
    d = df.copy()
    d["date"] = d["date"].astype(str).str.slice(0, 10)
    for col in _NUM_COLS:
        if col not in d.columns:
            d[col] = None
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d[(d["close"] > 0) & (d["open"] > 0)].sort_values("date").drop_duplicates("date")
    rows = [(code, r["date"],
             *[None if pd.isna(r[col]) else float(r[col]) for col in _NUM_COLS])
            for r in d.to_dict("records")]
    c = conn()
    try:
        c.execute("DELETE FROM bars WHERE code=?", (code,))
        if rows:
            c.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO bar_meta VALUES(?,?,?,?,?,?,?)",
                  (code, basis, source, dt.date.today().isoformat(),
                   rows[-1][1] if rows else "", str(start)[:10] if start else "", len(rows)))
        c.commit()
    except Exception:
        # 不让半截 DELETE 留在连接事务里，避免后续 commit 把数据写丢
        try:
            c.rollback()
        except Exception:
            pass
        raise
    _drop_chart_cache()


def bars_append(code: str, bar: dict, meta: dict) -> None:
    """追加单根 bar（快照补当日线用）并更新 meta。"""
    code = str(code).zfill(6)
    c = conn()
    try:
        c.execute("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (code, str(bar["date"])[:10],
                   *[_f(bar.get(col)) for col in _NUM_COLS]))
        c.execute("INSERT OR REPLACE INTO bar_meta VALUES(?,?,?,?,?,?,?)",
                  (code, meta.get("basis", ""), meta.get("source", ""), meta.get("updated", ""),
                   meta.get("last_date", ""), meta.get("start", ""), int(meta.get("rows") or 0)))
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    _drop_chart_cache()


def meta_of(code: str) -> dict:
    r = conn().execute("SELECT basis,source,updated,last_date,start,rows FROM bar_meta WHERE code=?",
                       (str(code).zfill(6),)).fetchone()
    if not r:
        return {}
    return {"code": str(code).zfill(6), "basis": r[0], "source": r[1], "updated": r[2],
            "last_date": r[3], "start": r[4], "rows": r[5]}


def meta_map() -> dict[str, dict]:
    """全部 meta 一次取回（增量挑股票 / 限量优先级排序用）。"""
    out = {}
    for r in conn().execute("SELECT code,basis,source,updated,last_date,start,rows FROM bar_meta"):
        out[r[0]] = {"code": r[0], "basis": r[1], "source": r[2], "updated": r[3],
                     "last_date": r[4], "start": r[5], "rows": r[6]}
    return out


def cached_codes() -> list[str]:
    return [r[0] for r in conn().execute("SELECT code FROM bar_meta ORDER BY code")]


def cached_count() -> int:
    return int(conn().execute("SELECT COUNT(*) FROM bar_meta").fetchone()[0])


def data_day() -> str:
    """全市场「数据日」：多数股票都到齐的那个交易日。

    不能用 MAX(date) 当数据日：北交所走腾讯通道、个别股票被快照单独补齐时，
    会零星写进一两根当天（或次日）的 bar，全市场其实还停在昨天，
    MAX 却已经跳到新的一天 —— 时效检查据此判断就永远显示「已是最新」。
    取众数（并列取较早的一天）才代表「这一天的截面是全市场都有的」。
    """
    rows = conn().execute(
        "SELECT last_date, COUNT(*) FROM bar_meta WHERE last_date<>'' GROUP BY last_date"
    ).fetchall()
    if not rows:
        return ""
    best = max(int(n) for _d, n in rows)
    days = sorted(str(d)[:10] for d, n in rows if int(n) == best)
    return days[0] if days else ""


def data_day_cover(day: str) -> float:
    """某一天在缓存里的覆盖率（0~1），用来说明「这一天到底算不算齐」。

    分母只算「当前名单里还在市」的代码，和 bulkdata.status() 的口径保持一致。

    为什么不能拿 bar_meta 的总数当分母：bar_meta 里永远留着一批已退市 / 改码的
    历史遗留代码（本项目实测 68 只，最早停在 2018 年）。它们永远不可能跟到最新
    交易日，等于分母永久虚高 —— 数据明明齐了，覆盖率也上不了 100%，侧栏那条
    「重跑一次增量即可补齐」就一直挂着，而重跑一万次也补不上。
    bulkdata 早就发现了这个问题并只按当前名单算，这里没跟上，于是同一份数据
    在「股票池页」和「侧栏待办」显示了两个不同的覆盖率。
    """
    day = str(day or "")[:10]
    if not day:
        return 0.0
    alive = _alive_codes()
    if alive is None:
        total = cached_count()
        if not total:
            return 0.0
        n = conn().execute("SELECT COUNT(*) FROM bar_meta WHERE last_date=?",
                           (day,)).fetchone()[0]
        return round(int(n) / total, 4)
    if not alive:
        return 0.0
    ph = ",".join("?" * len(alive))
    n = conn().execute(
        "SELECT COUNT(*) FROM bar_meta WHERE last_date=? AND code IN (%s)" % ph,
        (day, *alive)).fetchone()[0]
    return round(int(n) / len(alive), 4)


# 在市名单的短缓存：覆盖率会被前端反复轮询，每次读 5000 行 CSV 不划算。
_ALIVE = {"day": "", "codes": None}


def _alive_codes() -> list[str] | None:
    """当前名单里在市（未退市）的代码；名单不可用就返回 None，调用方退回旧口径。

    两个数据源要合起来用，谁都不能单独当分母：
      - listing.csv（baostock query_stock_basic）有 ipo/out_date，能判退市，
        但 **baostock 完全不覆盖北交所**，357 只 920xxx 一行都没有；
      - universe.csv（东财 clist）覆盖全市场含北交所，但只有「当前时点」，无退市信息。

    所以口径是：以 universe 为主干（才是「当前全 A」），再减掉 listing 里
    明确标了 out_date 的。listing 里查不到的（北交所）一律算在市 ——
    未知不误杀，和 listing.as_of 的取舍一致。
    """
    try:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        if _ALIVE["day"] == today and _ALIVE["codes"] is not None:
            return _ALIVE["codes"]
        import csv
        p = DATA / "universe.csv"
        if not p.exists():
            return None
        with open(p, encoding="utf-8-sig") as f:
            codes = [str(r.get("code") or "").zfill(6) for r in csv.DictReader(f)]
        codes = [c for c in codes if c and c != "000000"]
        if not codes:
            return None
        try:
            import listing
            rows = listing._rows() or {}
            if rows:
                dead = {c for c, r in rows.items()
                        if str(r.get("out_date") or "").strip()
                        or str(r.get("status") or "") == "0"}
                if dead:
                    codes = [c for c in codes if c not in dead]
        except Exception:
            pass
        if not codes:
            return None
        _ALIVE["day"] = today
        _ALIVE["codes"] = codes
        return codes
    except Exception:
        return None


def has_code(code: str) -> bool:
    return conn().execute("SELECT 1 FROM bar_meta WHERE code=?",
                          (str(code).zfill(6),)).fetchone() is not None


def clear_bars() -> int:
    c = conn()
    n = cached_count()
    c.execute("DELETE FROM bars")
    c.execute("DELETE FROM bar_meta")
    c.commit()
    return n


# --------------------------------------------------------------------------
# 选股记录
# --------------------------------------------------------------------------

def picks_replace(as_of: str, rows: list[dict]) -> tuple[int, int, int]:
    """写入一个选股批次；同一 (as_of, recorded_at) 重跑时覆盖该批。

    不同批次（同一数据日改了参数再选）会各自保留，记录页按批次展示；
    T+N 后验也按批次聚合，避免把几次不同实验混成一期。
    """
    as_of = str(as_of)[:10]
    if not rows:
        return 0, 0, picks_count()
    rec = str(rows[0].get("recorded_at") or "")
    data = [(as_of, rec, str(r.get("code") or "").zfill(6), r.get("name") or "",
             r.get("industry") or "", _i(r.get("rank")), _f(r.get("score")),
             _f(r.get("close"))) for r in rows]
    c = conn()
    old = int(c.execute("SELECT COUNT(*) FROM picks WHERE as_of=? AND recorded_at=?",
                        (as_of, rec)).fetchone()[0])
    try:
        c.execute("DELETE FROM picks WHERE as_of=? AND recorded_at=?", (as_of, rec))
        c.executemany(
            "INSERT OR REPLACE INTO picks(as_of,recorded_at,code,name,industry,rank,score,close) "
            "VALUES(?,?,?,?,?,?,?,?)", data)
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    total = int(c.execute("SELECT COUNT(*) FROM picks").fetchone()[0])
    return len(data), old, total


def picks_delete(as_of: str, recorded_at: str) -> int:
    """删除一个批次（as_of + recorded_at 唯一定位），返回删除条数。

    同一批的空头辩护一起删：它解释的就是「这一批为什么留这几只」，
    批次没了还留着理由，下次同 as_of 的新批次会看到一份对不上的结论。
    """
    c = conn()
    cur = c.execute("DELETE FROM picks WHERE as_of=? AND recorded_at=?",
                    (str(as_of)[:10], str(recorded_at)))
    try:
        c.execute("DELETE FROM pick_bear WHERE as_of=? AND recorded_at=?",
                  (str(as_of)[:10], str(recorded_at)))
    except Exception:
        pass
    c.commit()
    return int(cur.rowcount)


def bear_put(as_of: str, recorded_at: str, payload: dict, mode: str = "") -> int:
    """把一批候选的空头辩护结果随批次存下来，返回写入条数（0/1）。

    和 `pick_archive` 的区别：档案记「选了什么、被哪层漏斗淘汰」，
    这里单独存辩护全文（每只票 ≥3 条理由 + 触发条件 + 裁决理由），
    因为它是另一个模型产出的、可能要重跑，混在档案里会让档案越来越大。
    """
    as_of = str(as_of)[:10]
    rec = str(recorded_at or "")
    if not rec or not isinstance(payload, dict):
        return 0
    c = conn()
    c.execute("INSERT OR REPLACE INTO pick_bear(as_of,recorded_at,at,mode,payload) "
              "VALUES(?,?,?,?,?)",
              (as_of, rec, dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               str(mode or payload.get("mode") or ""),
               json.dumps(payload, ensure_ascii=False, default=_jsonable)))
    c.commit()
    return 1


def bear_get(as_of: str = "", recorded_at: str = "") -> dict | None:
    """读一批的空头辩护；只给 as_of 时取该数据日最近存的一份。"""
    c = conn()
    if recorded_at:
        row = c.execute("SELECT payload FROM pick_bear WHERE as_of=? AND recorded_at=?",
                        (str(as_of)[:10], str(recorded_at))).fetchone()
    elif as_of:
        row = c.execute("SELECT payload FROM pick_bear WHERE as_of=? "
                        "ORDER BY recorded_at DESC LIMIT 1", (str(as_of)[:10],)).fetchone()
    else:
        row = c.execute("SELECT payload FROM pick_bear ORDER BY as_of DESC, recorded_at DESC "
                        "LIMIT 1").fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def llm_put(as_of: str, recorded_at: str, payload: dict, mode: str = "",
            model: str = "") -> int:
    """把一批候选的 LLM 重排结果（分数 / 理由 / 名次）随批次存库。

    以前只写 data/screen/llm_last.json 单个文件，只能记住「最近一次」：
    换一批选股就整体覆盖，恢复时又要求候选集完全相等，于是改了 TopN 就恢复不出来，
    界面上表现为「刷新一下 LLM 分析就没了」。按 (as_of, recorded_at) 入库后，
    每一批都能各自取回，互不覆盖。
    """
    as_of = str(as_of)[:10]
    rec = str(recorded_at or "")
    if not rec or not isinstance(payload, dict):
        return 0
    items = payload.get("items") or payload.get("rows") or []
    if not items:
        return 0
    body = {"as_of": as_of, "mode": payload.get("mode", "local"),
            "model": payload.get("model", ""),
            "saved_at": payload.get("saved_at") or _now(),
            "items": items}
    c = conn()
    c.execute("INSERT OR REPLACE INTO pick_llm(as_of,recorded_at,at,mode,model,payload) "
              "VALUES(?,?,?,?,?,?)",
              (as_of, rec, _now(), str(mode or body["mode"]), str(model or body["model"]),
               json.dumps(body, ensure_ascii=False, default=_jsonable)))
    c.commit()
    return 1


def llm_get(as_of: str = "", recorded_at: str = "") -> dict | None:
    """读一批的 LLM 重排结果；只给 as_of 时取该数据日最近存的一份。"""
    c = conn()
    if recorded_at:
        row = c.execute("SELECT payload FROM pick_llm WHERE as_of=? AND recorded_at=?",
                        (str(as_of)[:10], str(recorded_at))).fetchone()
    elif as_of:
        row = c.execute("SELECT payload FROM pick_llm WHERE as_of=? "
                        "ORDER BY recorded_at DESC LIMIT 1", (str(as_of)[:10],)).fetchone()
    else:
        row = c.execute("SELECT payload FROM pick_llm ORDER BY as_of DESC, recorded_at DESC "
                        "LIMIT 1").fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def llm_latest(as_of: str = "") -> dict | None:
    """取最新一份重排结果（优先同一数据日，否则取全库最近一份）。

    恢复候选榜用这个：翻页/刷新时数据日可能已经往前走了，但上一批的解读仍然有效，
    只要按代码取交集套回去即可。
    """
    p = llm_get(as_of=as_of) if as_of else None
    return p or llm_get()


def picks_frame() -> pd.DataFrame:
    rows = conn().execute(
        "SELECT as_of,code,name,industry,rank,score,close,recorded_at FROM picks").fetchall()
    d = pd.DataFrame([tuple(r) for r in rows],
                     columns=["as_of", "code", "name", "industry", "rank", "score",
                              "close", "recorded_at"])
    if len(d):
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["as_of"] = d["as_of"].astype(str)
        d["recorded_at"] = d["recorded_at"].astype(str)
    return d


def picks_count() -> int:
    return int(conn().execute("SELECT COUNT(*) FROM picks").fetchone()[0])


def picks_version() -> tuple:
    """记录页缓存键：条数 + 最近记录时间（替代旧 CSV 的 mtime/size）。"""
    r = conn().execute("SELECT COUNT(*), MAX(recorded_at) FROM picks").fetchone()
    return (int(r[0]), r[1] or "")


# --------------------------------------------------------------------------
# 选股附注（因子值 / 漏斗层 / LLM 裁决）与决策档案
# --------------------------------------------------------------------------

def picks_meta_put(as_of: str, recorded_at: str, rows: list[dict]) -> int:
    """写入一个批次的附注。rows: [{code, ...任意字段}]。

    与 picks 分开存的原因：picks 的列要稳定（历史批次共用），而附注的字段会
    随分析维度演进（今天加漏斗层、明天加 LLM 裁决），用 JSON 才不会每次改表。
    """
    as_of = str(as_of)[:10]
    rec = str(recorded_at or "")
    if not rows or not rec:
        return 0
    data = []
    for r in rows:
        code = str(r.get("code") or "").zfill(6)
        if not code:
            continue
        payload = {k: v for k, v in r.items() if k != "code"}
        data.append((as_of, rec, code,
                     json.dumps(payload, ensure_ascii=False, default=_jsonable)))
    if not data:
        return 0
    c = conn()
    try:
        c.execute("DELETE FROM picks_meta WHERE as_of=? AND recorded_at=?", (as_of, rec))
        c.executemany("INSERT OR REPLACE INTO picks_meta(as_of,recorded_at,code,payload) "
                      "VALUES(?,?,?,?)", data)
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    return len(data)


def _jsonable(v):
    """numpy 标量 / NaN 转成 JSON 安全值（附注里全是因子值，最容易踩这个）。"""
    try:
        if isinstance(v, float) and v != v:
            return None
        if hasattr(v, "item"):
            return v.item()
    except Exception:
        pass
    return str(v) if v is not None else None


def picks_meta_map(as_of: str = "", recorded_at: str = "") -> dict:
    """(as_of, recorded_at, code) → 附注 dict。带参数时只取该批次。"""
    sql = "SELECT as_of,recorded_at,code,payload FROM picks_meta"
    args: tuple = ()
    if as_of and recorded_at:
        sql += " WHERE as_of=? AND recorded_at=?"
        args = (str(as_of)[:10], str(recorded_at))
    out = {}
    for r in conn().execute(sql, args).fetchall():
        try:
            out[(str(r[0]), str(r[1]), str(r[2]))] = json.loads(r[3] or "{}")
        except Exception:
            out[(str(r[0]), str(r[1]), str(r[2]))] = {}
    return out


def archive_put(as_of: str, recorded_at: str, payload: dict) -> int:
    """存一份完整决策快照（TopN 因子值 / 模型分 / LLM 理由 / 否决理由）。

    半年后复盘「当时为什么选它」时，只有这份档案能回答；runs/syslog 只记了动作。
    """
    c = conn()
    cur = c.execute("INSERT INTO pick_archive(as_of,recorded_at,at,payload) VALUES(?,?,?,?)",
                    (str(as_of)[:10], str(recorded_at or ""),
                     dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     json.dumps(payload, ensure_ascii=False, default=_jsonable)))
    c.commit()
    return int(cur.lastrowid or 0)


def archive_meta_list(limit: int = 20, as_of: str = "") -> list[dict]:
    """只取摘要列，不解析 payload。

    resume 要在 40 份存档里找「recorded_at 匹配」的那一份，每份 payload 可能
    几 MB（含 3000 条淘汰明细）。先把摘要查出来定位 id，再单独读那一份，
    比一次拉 40 份完整 JSON 快一个数量级。
    """
    sql = "SELECT id,as_of,recorded_at,at FROM pick_archive"
    args: tuple = ()
    if as_of:
        sql += " WHERE as_of=?"
        args = (str(as_of)[:10],)
    sql += " ORDER BY id DESC LIMIT ?"
    return [{"id": r[0], "as_of": r[1], "recorded_at": r[2], "at": r[3]}
            for r in conn().execute(sql, args + (int(limit),)).fetchall()]


def archive_list(limit: int = 20, as_of: str = "") -> list[dict]:
    sql = "SELECT id,as_of,recorded_at,at,payload FROM pick_archive"
    args: tuple = ()
    if as_of:
        sql += " WHERE as_of=?"
        args = (str(as_of)[:10],)
    sql += " ORDER BY id DESC LIMIT ?"
    out = []
    for r in conn().execute(sql, args + (int(limit),)).fetchall():
        try:
            payload = json.loads(r[4] or "{}")
        except Exception:
            payload = {}
        out.append({"id": r[0], "as_of": r[1], "recorded_at": r[2], "at": r[3],
                    "payload": payload})
    return out


def archive_get(aid: int) -> dict:
    r = conn().execute("SELECT id,as_of,recorded_at,at,payload FROM pick_archive WHERE id=?",
                       (int(aid),)).fetchone()
    if not r:
        return {}
    try:
        payload = json.loads(r[4] or "{}")
    except Exception:
        payload = {}
    return {"id": r[0], "as_of": r[1], "recorded_at": r[2], "at": r[3], "payload": payload}


# --------------------------------------------------------------------------
# 单股分析存档：每次分析一份，列表要轻量，所以摘要字段单独成列
# --------------------------------------------------------------------------

def report_put(code: str, as_of: str, payload: dict, mode: str = "",
               model: str = "", name: str = "", score=None, rank=None,
               price=None, pct=None) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO stock_report(code,name,as_of,at,mode,model,score,rank,price,pct,payload)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (str(code).zfill(6), str(name or ""), str(as_of)[:10],
         dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         str(mode or ""), str(model or "")[:80],
         _f(score), _i(rank), _f(price), _f(pct),
         json.dumps(payload, ensure_ascii=False, default=_jsonable)))
    c.commit()
    return int(cur.lastrowid or 0)


def report_list(code: str = "", limit: int = 60) -> list[dict]:
    """历史列表只取摘要列，不解析 payload（每次分析几 KB，列表页没必要全读）。"""
    sql = "SELECT id,code,name,as_of,at,mode,model,score,rank,price,pct FROM stock_report"
    args: tuple = ()
    if code:
        sql += " WHERE code=?"
        args = (str(code).zfill(6),)
    sql += " ORDER BY id DESC LIMIT ?"
    return [{"id": r[0], "code": r[1], "name": r[2], "as_of": r[3], "at": r[4],
             "mode": r[5], "model": r[6], "score": r[7], "rank": r[8],
             "price": r[9], "pct": r[10]}
            for r in conn().execute(sql, args + (int(limit),)).fetchall()]


def report_get(rid: int) -> dict:
    r = conn().execute(
        "SELECT id,code,name,as_of,at,mode,model,score,rank,price,pct,payload"
        " FROM stock_report WHERE id=?", (int(rid),)).fetchone()
    if not r:
        return {}
    try:
        payload = json.loads(r[11] or "{}")
    except Exception:
        payload = {}
    return {"id": r[0], "code": r[1], "name": r[2], "as_of": r[3], "at": r[4],
            "mode": r[5], "model": r[6], "score": r[7], "rank": r[8],
            "price": r[9], "pct": r[10], "payload": payload}


def report_delete(rid: int) -> int:
    c = conn()
    n = c.execute("DELETE FROM stock_report WHERE id=?", (int(rid),)).rowcount
    c.commit()
    return int(n or 0)


def report_clear(code: str) -> int:
    c = conn()
    n = c.execute("DELETE FROM stock_report WHERE code=?",
                  (str(code).zfill(6),)).rowcount
    c.commit()
    return int(n or 0)


# --------------------------------------------------------------------------
# 风险预算日表：一天一行，看使用率走势
# --------------------------------------------------------------------------

def risk_daily_put(d: dict) -> int:
    """按账本日期 upsert。同一天多次刷新只更新数值，不新增行。"""
    day = str(d.get("date") or "")[:10]
    if not day:
        return 0
    c = conn()
    c.execute(
        "INSERT INTO risk_daily(date,total,budget,usage,top_code,top_name,top_risk,"
        "exposure,n_pos,at) VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(date) DO UPDATE SET total=excluded.total,budget=excluded.budget,"
        " usage=excluded.usage,top_code=excluded.top_code,top_name=excluded.top_name,"
        " top_risk=excluded.top_risk,exposure=excluded.exposure,n_pos=excluded.n_pos,"
        " at=excluded.at",
        (day, _f(d.get("total")), _f(d.get("budget")), _f(d.get("usage")),
         str(d.get("top_code") or "")[:12], str(d.get("top_name") or "")[:40],
         _f(d.get("top_risk")), _f(d.get("exposure")), _i(d.get("n_pos")),
         dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    c.execute("DELETE FROM risk_daily WHERE date NOT IN "
              "(SELECT date FROM risk_daily ORDER BY date DESC LIMIT 400)")
    c.commit()
    return 1


def risk_daily_list(limit: int = 30) -> list[dict]:
    """按日期升序返回，前端画趋势时不用再倒序。"""
    rows = conn().execute(
        "SELECT date,total,budget,usage,top_code,top_name,top_risk,exposure,n_pos,at"
        " FROM risk_daily ORDER BY date DESC LIMIT ?", (int(limit),)).fetchall()
    out = [{"date": r[0], "total": r[1], "budget": r[2], "usage": r[3],
            "top_code": r[4], "top_name": r[5], "top_risk": r[6],
            "exposure": r[7], "n_pos": r[8], "at": r[9]} for r in rows]
    out.reverse()
    return out


# --------------------------------------------------------------------------
# 目标持仓计划：最近一份用于对比实际成交
# --------------------------------------------------------------------------

def plan_put(payload: dict) -> int:
    """存一份计划，只保留最近 60 份 —— 计划是过程记录，不是长期资产。"""
    c = conn()
    cur = c.execute("INSERT INTO plan(at,as_of,payload) VALUES(?,?,?)",
                    (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     str(payload.get("date") or "")[:10],
                     json.dumps(payload, ensure_ascii=False, default=_jsonable)))
    c.execute("DELETE FROM plan WHERE id NOT IN "
              "(SELECT id FROM plan ORDER BY id DESC LIMIT 60)")
    c.commit()
    return int(cur.lastrowid or 0)


def plan_latest() -> dict:
    r = conn().execute("SELECT id,at,as_of,payload FROM plan ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return {}
    try:
        payload = json.loads(r[3] or "{}")
    except Exception:
        payload = {}
    return {"id": r[0], "at": r[1], "as_of": r[2], "payload": payload}


# --------------------------------------------------------------------------
# 实盘账本
# --------------------------------------------------------------------------

def ledger_put(day: str, rec: dict) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO ledger_days VALUES(?,?)",
              (str(day)[:10], json.dumps(rec, ensure_ascii=False)))
    c.commit()


def ledger_get(day: str) -> dict:
    r = conn().execute("SELECT payload FROM ledger_days WHERE day=?", (str(day)[:10],)).fetchone()
    if not r:
        return {}
    try:
        return json.loads(r[0]) or {}
    except Exception:
        return {}


def ledger_days() -> list[str]:
    return [r[0] for r in conn().execute("SELECT day FROM ledger_days ORDER BY day DESC")]


def ledger_all(limit: int = 800) -> list[dict]:
    out = []
    for r in conn().execute("SELECT payload FROM ledger_days ORDER BY day DESC LIMIT ?",
                            (int(limit),)):
        try:
            d = json.loads(r[0])
            if d:
                out.append(d)
        except Exception:
            continue
    return out


def kv_upsert(table: str, items: list[tuple[str, str, dict]]) -> int:
    """按去重键幂等写入，返回新增条数。items: (key, date, row)。"""
    if table not in _KV_TABLES or not items:
        return 0
    c = conn()
    before = c.total_changes
    c.executemany("INSERT OR IGNORE INTO %s VALUES(?,?,?)" % table,
                  [(k, d, json.dumps(r, ensure_ascii=False)) for k, d, r in items])
    c.commit()
    return int(c.total_changes - before)


def kv_rows(table: str) -> list[dict]:
    if table not in _KV_TABLES:
        return []
    out = []
    for r in conn().execute("SELECT payload FROM %s ORDER BY date, k" % table):
        try:
            out.append(json.loads(r[0]))
        except Exception:
            continue
    return out


def kv_count(table: str) -> int:
    if table not in _KV_TABLES:
        return 0
    return int(conn().execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])


# --------------------------------------------------------------------------
# 更新记录（数据中枢统一日志：流水线多步 + 各页快捷单步）
# --------------------------------------------------------------------------

def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_start(kind: str, trigger: str = "manual", level: str = "") -> int:
    """开一条运行记录（ok=NULL 表示进行中），返回 id。"""
    c = conn()
    cur = c.execute("INSERT INTO runs(kind,trigger,level,started,ok) VALUES(?,?,?,?,NULL)",
                    (kind, trigger, level, _now()))
    c.commit()
    return int(cur.lastrowid)


def run_finish(rid: int, ok: bool, steps=None, data_day: str = "", error: str = "") -> None:
    c = conn()
    c.execute("UPDATE runs SET finished=?, ok=?, steps=?, data_day=?, error=? WHERE id=?",
              (_now(), 1 if ok else 0, json.dumps(steps or [], ensure_ascii=False),
               str(data_day or "")[:10], str(error or "")[:300], int(rid)))
    c.commit()


def run_log(kind: str, ok: bool, steps=None, trigger: str = "shortcut", level: str = "",
            data_day: str = "", error: str = "", started: str = "", ms: int = 0) -> int:
    """同步快捷操作一次性落一条记录（started/finished 同刻，耗时由 ms 给出）。"""
    c = conn()
    at = started or _now()
    cur = c.execute(
        "INSERT INTO runs(kind,trigger,level,started,finished,ok,data_day,steps,error) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (kind, trigger, level, at, at, 1 if ok else 0, str(data_day or "")[:10],
         json.dumps(steps or [], ensure_ascii=False), str(error or "")[:300]))
    c.commit()
    return int(cur.lastrowid)


def _run_row(r) -> dict:
    try:
        steps = json.loads(r[8]) if r[8] else []
    except Exception:
        steps = []
    ms = 0
    try:
        t0 = dt.datetime.strptime(r[4], "%Y-%m-%d %H:%M:%S")
        t1 = dt.datetime.strptime(r[5], "%Y-%m-%d %H:%M:%S") if r[5] else t0
        ms = int((t1 - t0).total_seconds() * 1000)
    except Exception:
        ms = 0
    if not ms and steps:
        ms = sum(int(s.get("ms") or 0) for s in steps)
    return {"id": r[0], "kind": r[1], "trigger": r[2], "level": r[3],
            "started": r[4], "finished": r[5],
            "ok": (None if r[6] is None else bool(r[6])),
            "data_day": r[7], "steps": steps, "error": r[9], "ms": ms}


def runs_list(limit: int = 60) -> list[dict]:
    return [_run_row(r) for r in conn().execute(
        "SELECT id,kind,trigger,level,started,finished,ok,data_day,steps,error "
        "FROM runs ORDER BY id DESC LIMIT ?", (int(limit),))]


def run_get(rid: int) -> dict | None:
    r = conn().execute(
        "SELECT id,kind,trigger,level,started,finished,ok,data_day,steps,error "
        "FROM runs WHERE id=?", (int(rid),)).fetchone()
    return _run_row(r) if r else None


def runs_page(limit: int = 60, offset: int = 0, kind: str = "", trigger: str = "",
              ok: str = "", q: str = "") -> dict:
    """更新记录分页 + 筛选。runs 是「做过什么、成没成」，保留全部，不做清理。"""
    where, args = [], []
    if kind and kind != "all":
        where.append("kind=?")
        args.append(kind)
    if trigger and trigger != "all":
        where.append("trigger=?")
        args.append(trigger)
    if ok == "1":
        where.append("ok=1")
    elif ok == "0":
        where.append("ok=0")
    elif ok == "run":
        where.append("ok IS NULL")
    if q:
        where.append("(kind LIKE ? OR steps LIKE ? OR error LIKE ?)")
        args += ["%%%s%%" % q] * 3
    tail = (" WHERE " + " AND ".join(where)) if where else ""
    c = conn()
    total = int(c.execute("SELECT COUNT(*) FROM runs" + tail, args).fetchone()[0] or 0)
    rows = c.execute(
        "SELECT id,kind,trigger,level,started,finished,ok,data_day,steps,error "
        "FROM runs" + tail + " ORDER BY id DESC LIMIT ? OFFSET ?",
        args + [max(1, int(limit)), max(0, int(offset))]).fetchall()
    return {"ok": True, "runs": [_run_row(r) for r in rows], "total": total,
            "offset": int(offset), "limit": int(limit)}


# --------------------------------------------------------------------------
# 系统运行日志（syslog）
#
# 和 runs 的分工：
#   runs    = 一次「动作」的结果（谁、什么时候、成功否、几步、多久）—— 给业务看
#   syslog  = 过程中的细节与异常（为什么失败、退避到几点、降级到哪个源、异常栈）
# 用 ref 把两者串起来（syslog.ref 存 runs.id）。
# --------------------------------------------------------------------------

LOG_KEEP_DAYS = 30          # 保留天数
LOG_KEEP_ROWS = 50000       # 保留条数上限
_LOG_OFF = str(os.environ.get("QD_SYSLOG", "")).strip().lower() in ("0", "off", "false", "no")


def log(src: str, msg: str, level: str = "info", act: str = "",
        detail: str = "", ref: str = "") -> None:
    """写一条系统日志。

    **任何异常都吞掉**：写日志不能反过来把主流程弄挂 —— 排查工具自己先崩了最坑。
    debug 级别默认不落库（tick 每 30 秒一次，全存下来只会把有用的淹掉），
    排查时设环境变量 QD_SYSLOG=debug 再开。
    """
    if _LOG_OFF:
        return
    if level == "debug" and os.environ.get("QD_SYSLOG", "").lower() != "debug":
        return
    try:
        c = conn()
        c.execute("INSERT INTO syslog(at,level,src,act,msg,detail,ref) VALUES(?,?,?,?,?,?,?)",
                  (_now(), str(level)[:8], str(src)[:24], str(act)[:60],
                   str(msg)[:500], str(detail)[:4000], str(ref)[:64]))
        c.commit()
    except Exception:
        pass


def log_many(items, src: str = "ui") -> int:
    """批量写日志（前端把若干条右上角通知攒一拍再发过来）。

    一条 INSERT 一个事务，避免 30 条通知变成 30 次 fsync。返回实际写入条数。
    只接受 dict 列表，字段一律截断 —— 前端传来的东西不能信长度。
    """
    if _LOG_OFF or not items:
        return 0
    rows = []
    at = _now()
    for it in list(items)[:40]:
        if not isinstance(it, dict):
            continue
        lv = str(it.get("level") or "info").lower()
        if lv not in ("info", "warn", "error", "debug"):
            lv = "info"
        if lv == "debug" and os.environ.get("QD_SYSLOG", "").lower() != "debug":
            continue
        msg = str(it.get("msg") or "").strip()
        if not msg:
            continue
        rows.append((at, lv[:8], str(src)[:24], str(it.get("act") or "")[:60],
                     msg[:500], str(it.get("detail") or "")[:4000],
                     str(it.get("ref") or "")[:64]))
    if not rows:
        return 0
    try:
        c = conn()
        c.executemany("INSERT INTO syslog(at,level,src,act,msg,detail,ref) "
                      "VALUES(?,?,?,?,?,?,?)", rows)
        c.commit()
        return len(rows)
    except Exception:
        return 0


def _log_row(r) -> dict:
    return {"id": r[0], "at": r[1], "level": r[2], "src": r[3],
            "act": r[4] or "", "msg": r[5] or "", "detail": r[6] or "", "ref": r[7] or ""}


def logs_list(limit: int = 200, offset: int = 0, level: str = "", src: str = "",
              q: str = "") -> dict:
    where, args = [], []
    # URL 里的 "+" 会被按表单规则解成空格（warn+ → "warn "），这里统一还原，
    # 否则「需关注」筛选在直接用 curl / 旧浏览器时静默失效。
    level = str(level or "").strip().replace(" ", "+")
    src = str(src or "").strip()
    q = str(q or "").strip()
    if level and level != "all":
        if level == "warn+":                 # 只看需要关注的
            where.append("level IN ('warn','error')")
        else:
            where.append("level=?")
            args.append(level)
    if src and src != "all":
        where.append("src=?")
        args.append(src)
    if q:
        where.append("(msg LIKE ? OR detail LIKE ? OR act LIKE ? OR src LIKE ?)")
        args += ["%%%s%%" % q] * 4
    tail = (" WHERE " + " AND ".join(where)) if where else ""
    c = conn()
    total = int(c.execute("SELECT COUNT(*) FROM syslog" + tail, args).fetchone()[0] or 0)
    rows = c.execute("SELECT id,at,level,src,act,msg,detail,ref FROM syslog" + tail +
                     " ORDER BY id DESC LIMIT ? OFFSET ?",
                     args + [max(1, min(1000, int(limit))), max(0, int(offset))]).fetchall()
    return {"ok": True, "items": [_log_row(r) for r in rows], "total": total,
            "offset": int(offset), "limit": int(limit)}


def log_facets() -> dict:
    """来源清单 + 各级别计数：页面筛选下拉与徽标用。"""
    c = conn()
    srcs = [r[0] for r in c.execute(
        "SELECT src, COUNT(*) n FROM syslog GROUP BY src ORDER BY n DESC LIMIT 40")]
    cnt = {k: 0 for k in ("info", "warn", "error")}
    for lv, n in c.execute("SELECT level, COUNT(*) FROM syslog GROUP BY level"):
        if lv in cnt:
            cnt[lv] = int(n or 0)
    # 最近 24 小时：判断「现在有没有在出问题」，全量计数会被历史噪声淹没
    since = (dt.datetime.now() - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    recent = {k: 0 for k in ("info", "warn", "error")}
    for lv, n in c.execute(
            "SELECT level, COUNT(*) FROM syslog WHERE at>=? GROUP BY level", (since,)):
        if lv in recent:
            recent[lv] = int(n or 0)
    return {"ok": True, "sources": srcs, "counts": cnt, "recent": recent}


def log_prune(days: int = LOG_KEEP_DAYS, keep: int = LOG_KEEP_ROWS) -> int:
    """按时间和条数清理，返回删了多少条。

    runs 不动：那是业务事实，用户明确要求全部保留。
    """
    c = conn()
    n = 0
    try:
        cut = (dt.datetime.now() - dt.timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
        n += int(c.execute("DELETE FROM syslog WHERE at<?", (cut,)).rowcount or 0)
        # 超过上限时按 id 截断（保留最新）
        row = c.execute("SELECT id FROM syslog ORDER BY id DESC LIMIT 1 OFFSET ?",
                        (max(1000, int(keep)),)).fetchone()
        if row:
            n += int(c.execute("DELETE FROM syslog WHERE id<=?", (int(row[0]),)).rowcount or 0)
        c.commit()
    except Exception:
        pass
    return n


def log_text(limit: int = 2000, level: str = "", src: str = "", q: str = "") -> str:
    """导出成纯文本，直接贴给排查用（也可当附件）。"""
    d = logs_list(limit=limit, level=level, src=src, q=q)
    out = ["# QuantDesk 运行日志  %s  共 %d 条（导出 %d 条）" %
           (_now(), d.get("total") or 0, len(d.get("items") or [])), ""]
    for x in reversed(d.get("items") or []):
        out.append("%s [%s] %s/%s %s%s" % (
            x["at"], x["level"].upper(), x["src"], x["act"] or "-", x["msg"],
            ("\n    " + x["detail"].replace("\n", "\n    ")) if x["detail"] else ""))
    return "\n".join(out)

# --------------------------------------------------------------------------
# 资金流（两融 / 龙虎榜 / 大宗 / 北向）
# --------------------------------------------------------------------------

CAP_COLS = ("lhb_net", "lhb_amt", "blk_amt", "blk_prem",
            "margin_bal", "margin_net", "north_ratio", "north_chg")


def capital_put(rows: list[dict]) -> int:
    """按 (code, date) 覆盖写一行。缺的字段写 NULL 而不是 0——
    「没上榜」和「净买额为 0」是两件事，混起来会让因子产生假信号。"""
    if not rows:
        return 0
    data = []
    for r in rows:
        code = str(r.get("code") or "").zfill(6)
        date = str(r.get("date") or "")[:10]
        if not code or len(date) != 10:
            continue
        vals = []
        for k in CAP_COLS:
            v = r.get(k)
            try:
                vals.append(None if v is None else float(v))
            except Exception:
                vals.append(None)
        data.append((code, date, *vals))
    if not data:
        return 0
    c = conn()
    c.executemany(
        "INSERT OR REPLACE INTO capital(code,date," + ",".join(CAP_COLS) + ") "
        "VALUES(?,?," + ",".join("?" * len(CAP_COLS)) + ")", data)
    c.commit()
    return len(data)


def capital_rows(codes: list[str] | None = None, start: str = "", end: str = "") -> pd.DataFrame:
    sql = ("SELECT code,date," + ",".join(CAP_COLS) + " FROM capital WHERE 1=1")
    args: list = []
    if codes:
        marks = ",".join("?" * len(codes))
        sql += " AND code IN (%s)" % marks
        args += [str(c).zfill(6) for c in codes]
    if start:
        sql += " AND date>=?"
        args.append(str(start)[:10])
    if end:
        sql += " AND date<=?"
        args.append(str(end)[:10])
    sql += " ORDER BY code, date"
    cur = conn().execute(sql, tuple(args))
    names = ["code", "date"] + list(CAP_COLS)
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=names)


def capital_meta() -> dict:
    """每个字段的最新日期与行数 —— 判断哪一路数据停了。"""
    c = conn()
    out = {}
    for k in CAP_COLS:
        r = c.execute("SELECT MAX(date), COUNT(*) FROM capital WHERE %s IS NOT NULL" % k).fetchone()
        out[k] = {"last": (r[0] or "") if r else "", "rows": int(r[1] or 0) if r else 0}
    r = c.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM capital").fetchone()
    out["_all"] = {"rows": int(r[0] or 0), "start": r[1] or "", "end": r[2] or ""}
    return out


def amount_sum(codes: list[str], start: str, end: str) -> dict[str, float]:
    """区间成交额合计（批量、单条 SQL）。资金流因子要用它做分母。"""
    codes = [str(c).zfill(6) for c in (codes or [])]
    if not codes:
        return {}
    out: dict[str, float] = {}
    step = 400                      # 避开 SQLite 变量上限（默认 999）
    for i in range(0, len(codes), step):
        part = codes[i:i + step]
        marks = ",".join("?" * len(part))
        sql = ("SELECT code, SUM(amount) FROM bars WHERE code IN (%s) "
               "AND date>=? AND date<=? GROUP BY code" % marks)
        for code, amt in conn().execute(sql, tuple(part) + (str(start)[:10], str(end)[:10])):
            if amt:
                out[str(code)] = float(amt)
    return out
