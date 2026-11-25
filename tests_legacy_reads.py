# -*- coding: utf-8 -*-
"""回归：迁移完成后不得再读旧文件（ledger JSON/CSV、data/cache）。"""
import tempfile
from pathlib import Path

import bulkdata
import emtlink
import store

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


class Boom:
    """任何触碰旧路径的行为都会立即暴露。"""

    def exists(self):
        raise AssertionError("迁移完成后不应再检查旧文件是否存在")

    def glob(self, *a, **k):
        raise AssertionError("迁移完成后不应再扫描旧目录")

    def open(self, *a, **k):
        raise AssertionError("迁移完成后不应再打开旧文件")


def test_ledger_skip_after_migration():
    print("\n[迁移已完成] 不再扫描旧账本")
    orig = (emtlink._LEDGER_DB_READY, store.migration_done, emtlink.LEDGER,
            emtlink.FILLS_CSV, emtlink.ORDERS_CSV, emtlink.FLOWS_CSV,
            store.migrate_ledger)
    try:
        emtlink._LEDGER_DB_READY = False
        store.migration_done = lambda k: k == "ledger_migrated"
        emtlink.LEDGER = Boom()
        emtlink.FILLS_CSV = emtlink.ORDERS_CSV = emtlink.FLOWS_CSV = Boom()
        called = []
        store.migrate_ledger = lambda days, kv: called.append((days, kv))
        emtlink._ensure_ledger_db()
        check(not called, "没有再次调用 migrate_ledger")
        check(emtlink._LEDGER_DB_READY, "进程内标记已就绪")
    finally:
        (emtlink._LEDGER_DB_READY, store.migration_done, emtlink.LEDGER,
         emtlink.FILLS_CSV, emtlink.ORDERS_CSV, emtlink.FLOWS_CSV,
         store.migrate_ledger) = orig


def test_ledger_first_run_still_migrates():
    print("\n[首次迁移] 仍能读旧账本")
    orig = (emtlink._LEDGER_DB_READY, store.migration_done, emtlink.LEDGER,
            emtlink.FILLS_CSV, emtlink.ORDERS_CSV, emtlink.FLOWS_CSV,
            store.migrate_ledger)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = root / "ledger"
            ledger.mkdir()
            (ledger / "2026-01-01.json").write_text('{"overview":{}}', "utf-8")
            paths = [root / (n + ".csv") for n in ("fills", "orders", "flows")]
            for p in paths:
                p.write_text("date\n", "utf-8")
            emtlink.LEDGER, (emtlink.FILLS_CSV, emtlink.ORDERS_CSV,
                             emtlink.FLOWS_CSV) = ledger, paths
            store.migration_done = lambda k: False
            called = []
            store.migrate_ledger = lambda days, kv: called.append((days, kv))
            emtlink._LEDGER_DB_READY = False
            emtlink._ensure_ledger_db()
            check(len(called) == 1 and len(called[0][0]) == 1
                  and called[0][0][0][0] == "2026-01-01",
                  "未迁移时仍会读出旧日账本并交给 migrate_ledger")
            check(emtlink._LEDGER_DB_READY, "迁移后标记已就绪")
    finally:
        (emtlink._LEDGER_DB_READY, store.migration_done, emtlink.LEDGER,
         emtlink.FILLS_CSV, emtlink.ORDERS_CSV, emtlink.FLOWS_CSV,
         store.migrate_ledger) = orig


def test_real_db_and_dead_constant():
    print("\n[真实库] 账本可用 & 死常量已清")
    check(bool(emtlink.ledger_days()), "ledger_days() 仍能从 SQLite 读到数据")
    check(not hasattr(bulkdata, "CACHE"), "bulkdata 不再保留指向已删 data/cache 的常量")
    check(store.migration_done("ledger_migrated"), "ledger_migrated 标志已落库")


if __name__ == "__main__":
    test_ledger_skip_after_migration()
    test_ledger_first_run_still_migrates()
    test_real_db_and_dead_constant()
    print("\n%s" % ("全部通过" if not FAILED else "失败 %d 项：%s" % (len(FAILED), FAILED)))
    raise SystemExit(1 if FAILED else 0)
