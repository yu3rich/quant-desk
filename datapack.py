# -*- coding: utf-8 -*-
"""datapack.py — 行情数据包导出 / 导入。

只做「行情 K 线」这一件事：`bars` + `bar_meta` 是唯一需要别人花一两个小时
从数据源重拉的东西，名单/行业/估值都是分钟级，没必要传。个人数据（账本、日记、
选股记录、LLM 辩护）一律不打包 —— `quant.db` 是单文件装所有表，整库拷会连
这些东西一起带走，导入方拿到别人的持仓是事故，不是功能。

包格式（zip）：
    manifest.json     版本、行数、日期范围、sha256、表结构签名
    bars.csv.gz       981 万行约 340MB
    bar_meta.csv.gz   5642 行

为什么用 CSV 而不是直接给 .db：表结构升级后，旧库文件会变成「能打开但字段对不上」
的静默错误；CSV 带表头，导入前可以逐列校验，对不上就明确拒绝，不会写坏别人的库。
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

from paths import BASE, DATA
PACK_DIR = BASE / "data" / "packs"
DB_PATH = BASE / "data" / "quant.db"

# 导出/导入的列顺序：必须和 quant.db 的实际 schema 一致。导入端逐列比对表头，
# 顺序错了会把 open 写进 high 这类静默错误。顺序取自 store 建表语句。
BAR_COLS = ("code", "date", "open", "high", "low", "close", "volume", "amount",
            "pct", "turnover")
META_COLS = ("code", "basis", "source", "updated", "last_date", "start", "rows")

# 后台任务状态：前端轮询这个结构画进度（导出/导入都不阻塞请求）
STATE: dict = {"kind": "", "running": False, "phase": "", "done": 0, "total": 0,
               "pct": 0, "message": "", "file": "", "size": 0, "result": None,
               "started": 0.0, "finished": 0.0}
_lock = threading.Lock()


def _set(**kw) -> None:
    with _lock:
        STATE.update(kw)
        total = STATE.get("total") or 0
        done = STATE.get("done") or 0
        STATE["pct"] = int(min(100, done * 100 / total)) if total else 0


def status() -> dict:
    with _lock:
        return dict(STATE)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=60000")
    return c


def _schema() -> list:
    """当前库里的列名与顺序，导入时逐列比对。"""
    c = _conn()
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(bars)")]
    finally:
        c.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_gz_csv(cur, out_path: Path, cols: tuple, since: str | None,
                  base_done: int, total: int, phase: str) -> tuple[int, str, str, int]:
    """流式写 csv.gz，返回 (行数, 最早日, 最晚日, 最后 done)。"""
    n = 0
    first = last = ""
    done = base_done
    # gzip 的 mtime 固定，同样的数据打出来的包字节一致，便于对方核对 sha256
    with open(out_path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
            text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
            w = csv.writer(text)
            w.writerow(cols)
            # 按主键 (code,date) 顺序导出：导入端插入顺序与 WITHOUT ROWID 的 B-tree
            # 完全一致，是顺序追加而不是随机落页，981 万行能快一倍以上。
            sql = "select %s from bars%s order by code, date" % (
                ",".join(cols), " where date>=?" if since else "")
            args = (since,) if since else ()
            for row in cur.execute(sql, args):
                w.writerow(["" if v is None else v for v in row])
                n += 1
                d = str(row[1] or "")
                if d:
                    if not first or d < first:
                        first = d
                    if not last or d > last:
                        last = d
                if n % 50000 == 0:
                    done = base_done + n
                    _set(done=done, total=total, phase=phase)
            text.flush()
    return n, first, last, done


def export_bars(since: str | None = None, progress: bool = True) -> dict:
    """导出行情包。since='2023-11-26' 只导近三年；None 为全量。"""
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    tag = ("bars_%s" % stamp) if not since else ("bars_%s_since%s" % (stamp, since.replace("-", "")))
    out = PACK_DIR / (tag + ".zip")
    tmp = PACK_DIR / ("_tmp_" + tag)
    tmp.mkdir(parents=True, exist_ok=True)
    c = _conn()
    try:
        total = c.execute("select count(*) from bars%s" % (" where date>=?" if since else ""),
                          ((since,) if since else ())).fetchone()[0]
        meta_n = c.execute("select count(*) from bar_meta").fetchone()[0]
        _set(kind="export", running=True, phase="准备", done=0,
             total=total + meta_n, pct=0, message="统计行数 %d" % total,
             started=time.time(), result=None, file="", size=0)
        bars_gz = tmp / "bars.csv.gz"
        meta_gz = tmp / "bar_meta.csv.gz"
        n, first, last, done = _write_gz_csv(
            c.cursor(), bars_gz, BAR_COLS, since, 0, total + meta_n, "写行情")
        _set(phase="写元数据", done=done)
        # 只导一段历史时，bar_meta 里的 start/rows 必须按区间重算：
        # 原样带过去会变成「meta 说 2018 起、实际只有 2023 起」，增量更新
        # 与股票池的「已缓存」判定都会跟着错。
        stat = {}
        if since:
            stat = {r[0]: (r[1], r[2]) for r in c.execute(
                "select code, min(date), count(*) from bars where date>=? group by code", (since,))}
        with open(meta_gz, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
                text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
                w = csv.writer(text)
                w.writerow(META_COLS)
                kept = 0
                for row in c.execute("select %s from bar_meta order by code" % ",".join(META_COLS)):
                    row = list(row)
                    if since:
                        hit = stat.get(str(row[0]))
                        if not hit:
                            continue          # 区间内没有任何行情，不该出现在包里
                        row[5], row[6] = hit[0], hit[1]
                    w.writerow(["" if v is None else v for v in row])
                    kept += 1
                text.flush()
        _set(phase="校验", done=total + meta_n)
        manifest = {
            "kind": PACK_KIND, "format": PACK_FORMAT,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "since": since or "", "columns": {"bars": list(BAR_COLS), "bar_meta": list(META_COLS)},
            "schema": _schema(),
            "bars": {"rows": n, "first": first, "last": last,
                     "sha256": _sha256(bars_gz), "bytes": bars_gz.stat().st_size},
            "bar_meta": {"rows": kept, "sha256": _sha256(meta_gz),
                         "bytes": meta_gz.stat().st_size},
        }
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        _set(phase="打包", message="压缩 %d 行" % n)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as z:
            # 内部已是 gz，再压缩一次没有收益，反而多花一倍 CPU
            for name in ("manifest.json", "bars.csv.gz", "bar_meta.csv.gz"):
                z.write(tmp / name, name)
        size = out.stat().st_size
        _prune_packs()
        _set(running=False, phase="完成", pct=100,
             message="导出 %d 根 · %.0f MB" % (n, size / 1e6),
             file=str(out), size=size, finished=time.time(),
             result={"rows": n, "first": first, "last": last, "file": str(out), "size": size})
        return {"ok": True, "file": str(out), "rows": n, "size": size,
                "first": first, "last": last}
    except Exception as e:                                  # noqa: BLE001
        _set(running=False, phase="失败", message=str(e)[:160], finished=time.time())
        return {"ok": False, "error": str(e)[:200]}
    finally:
        c.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _prune_packs(keep: int = 2) -> None:
    try:
        # 只留 2 份：一份全量就近 350MB，留多了 C 盘很快见底；
        # 用户已经拿到自己选的文件夹里那份，服务端这份只是给局域网下载用。
        files = sorted(PACK_DIR.glob("bars_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            f.unlink(missing_ok=True)
    except Exception:
        pass


def inspect_pack(path: Path) -> dict:
    """只读 manifest 并校验与当前库是否兼容，不落库。"""
    if not path.exists():
        return {"ok": False, "error": "文件不存在"}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "manifest.json" not in names:
                return {"ok": False, "error": "不是行情包：缺 manifest.json"}
            if z.getinfo("manifest.json").file_size > 1 << 20:
                return {"ok": False, "error": "manifest.json 异常过大"}
            m = json.loads(z.read("manifest.json").decode("utf-8"))
    except zipfile.BadZipFile:
        return {"ok": False, "error": "压缩包损坏，无法读取"}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": "读取失败：%s" % str(e)[:120]}
    if m.get("kind") != PACK_KIND:
        return {"ok": False, "error": "不是 QuantDesk 行情包（kind=%s）" % m.get("kind")}
    if int(m.get("format") or 0) > PACK_FORMAT:
        return {"ok": False, "error": "包格式版本 %s 高于当前程序支持的 %s，请先升级"
                % (m.get("format"), PACK_FORMAT)}
    cols = (m.get("columns") or {}).get("bars") or []
    mine = _schema()
    if list(cols) != list(mine):
        return {"ok": False, "error": "表结构不一致：包内 %s，本机 %s。"
                                      "跨版本导入会写坏数据，已拒绝。" % (cols, mine)}
    return {"ok": True, "manifest": m}


def import_bars(path: Path) -> dict:
    """导入入口：与流水线共用进程锁，避免导入期间逐只行情写入撞锁。

    981 万行要写一分钟以上，期间 bulkdata 的逐只 bars_write 会不断撞上写锁，
    最终被记成「更新完成但失败几百只」，很难归因。
    """
    try:
        import pipeline
    except Exception:
        return _import_bars_impl(path)
    if not pipeline.PROC_LOCK.acquire():
        return {"ok": False, "error": "有流水线/行情任务在跑，请等它结束后再导入"}
    try:
        return _import_bars_impl(path)
    finally:
        try:
            pipeline.PROC_LOCK.release()
        except Exception:
            pass


def _import_bars_impl(path: Path) -> dict:
    """把行情包合并进本地库（INSERT OR REPLACE，不动其它表）。"""
    info = inspect_pack(path)
    if not info.get("ok"):
        return info
    m = info["manifest"]
    bars_info = m.get("bars") or {}
    meta_info = m.get("bar_meta") or {}
    total = int(bars_info.get("rows") or 0) + int(meta_info.get("rows") or 0)
    _set(kind="import", running=True, phase="校验", done=0, total=max(1, total), pct=0,
         message="校验压缩包", started=time.time(), result=None, file="", size=0)
    t0 = time.time()
    c = _conn()
    try:
        # 导入是「要么重来、要么成功」的批量作业，不需要每条 commit 都落盘：
        # 关掉同步后 981 万行从 8 分钟降到 1 分多钟。失败了大不了重新导入。
        c.execute("PRAGMA synchronous=OFF")
        c.execute("PRAGMA cache_size=-200000")
        with zipfile.ZipFile(path) as z:
            if z.getinfo("bars.csv.gz").file_size > 3 << 30:
                return {"ok": False, "error": "bars.csv.gz 解压后超过 3GB，拒绝处理"}
            # 先校验 sha256 再落库：坏包写到一半才发现，库就变成半新半旧
            _set(phase="校验哈希", message="校验完整性")
            for key, member in (("bars", "bars.csv.gz"), ("bar_meta", "bar_meta.csv.gz")):
                want = (m.get(key) or {}).get("sha256")
                if not want:
                    continue
                h = hashlib.sha256()
                with z.open(member) as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != want:
                    return {"ok": False, "error": "%s 校验失败（sha256 不一致），文件可能在传输中损坏" % member}
            _set(phase="写入行情", message="合并 bars")
            n = 0
            batch = []
            sql = ("insert or replace into bars(%s) values(%s)"
                   % (",".join(BAR_COLS), ",".join("?" * len(BAR_COLS))))
            with z.open("bars.csv.gz") as f:
                gz = gzip.GzipFile(fileobj=f)
                text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
                rd = csv.reader(text)
                head = next(rd, None)
                if head and list(head) != list(BAR_COLS):
                    return {"ok": False, "error": "bars.csv.gz 表头与 manifest 不符"}
                for row in rd:
                    if len(row) < len(BAR_COLS):
                        continue
                    vals = []
                    for i, v in enumerate(row[:len(BAR_COLS)]):
                        if i in (0, 1):
                            vals.append(v)
                        elif v == "":
                            vals.append(None)
                        else:
                            try:
                                vals.append(float(v))
                            except ValueError:
                                vals.append(None)
                    batch.append(vals)
                    n += 1
                    if len(batch) >= 100000:
                        c.executemany(sql, batch)
                        c.commit()          # 分批提交：981 万行一次性事务会把 WAL 撑到 1GB
                        batch.clear()
                        _set(done=n, phase="写入行情")
                if batch:
                    c.executemany(sql, batch)
                    c.commit()
                    batch.clear()
            _set(done=n, phase="写入元数据")
            mrows = 0
            msql = ("insert or replace into bar_meta(%s) values(%s)"
                    % (",".join(META_COLS), ",".join("?" * len(META_COLS))))
            with z.open("bar_meta.csv.gz") as f:
                gz = gzip.GzipFile(fileobj=f)
                text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
                rd = csv.reader(text)
                next(rd, None)
                mb = []
                for row in rd:
                    if len(row) < len(META_COLS):
                        continue
                    vals = []
                    for i, v in enumerate(row[:len(META_COLS)]):
                        if i == 6:
                            try:
                                vals.append(int(float(v)) if v else None)
                            except ValueError:
                                vals.append(None)
                        else:
                            vals.append(v or None)
                    mb.append(vals)
                    mrows += 1
                if mb:
                    c.executemany(msql, mb)
                    c.commit()
        _set(phase="清理缓存", done=total, message="重建缓存")
        # 数据日变了：图表的 TTL 缓存与横截面快照都不能再用，
        # 否则用户导完发现「数据没变」——快照还是旧的那张
        try:
            import chartdata
            chartdata.invalidate()
        except Exception:
            pass
        for name in ("snapshot.csv", "snapshot.json"):
            try:
                (BASE / "data" / "screen" / name).unlink(missing_ok=True)
            except Exception:
                pass
        secs = time.time() - t0
        _set(running=False, phase="完成", pct=100,
             message="导入 %d 根行情 · %d 只元数据 · 用时 %.0f 秒" % (n, mrows, secs),
             finished=time.time(),
             result={"bars": n, "meta": mrows, "seconds": round(secs, 1)})
        return {"ok": True, "bars": n, "meta": mrows, "seconds": round(secs, 1)}
    except Exception as e:                                  # noqa: BLE001
        msg = str(e)[:200]
        if "CRC" in msg or "BadZip" in msg:
            msg = "压缩包已损坏（CRC 校验失败），请重新下载后再导入"
        _set(running=False, phase="失败", message=msg[:160], finished=time.time())
        return {"ok": False, "error": msg}
    finally:
        c.close()
        # 上传的临时包用完即删：342MB 留在 data/packs 里，下次导出又要多占一份
        try:
            if Path(path).name.startswith("_upload_"):
                Path(path).unlink(missing_ok=True)
        except Exception:
            pass


def run_export_async(since: str | None = None) -> dict:
    if STATE.get("running"):
        return {"ok": False, "error": "已有导出/导入任务在进行"}
    threading.Thread(target=export_bars, kwargs={"since": since},
                     name="pack-export", daemon=True).start()
    return {"ok": True, "started": True}


def run_import_async(path: Path) -> dict:
    if STATE.get("running"):
        return {"ok": False, "error": "已有导出/导入任务在进行"}
    threading.Thread(target=import_bars, kwargs={"path": Path(path)},
                     name="pack-import", daemon=True).start()
    return {"ok": True, "started": True}
