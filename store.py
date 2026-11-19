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

import pandas as pd

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
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
    """删除一个批次（as_of + recorded_at 唯一定位），返回删除条数。"""
    c = conn()
    cur = c.execute("DELETE FROM picks WHERE as_of=? AND recorded_at=?",
                    (str(as_of)[:10], str(recorded_at)))
    c.commit()
    return int(cur.rowcount)


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