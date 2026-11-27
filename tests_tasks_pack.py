# -*- coding: utf-8 -*-
"""回归：行情包任务必须聚合进 /api/tasks，右上角状态灯才能画进度环。

背景：导出行情包跑在后端线程里（datapack.STATE），原先前端只用本地任务
sysTaskStart 让灯转圈、没有百分比。把 datapack 接进 tasks_status 后，
状态灯能像行情增量一样显示环形进度。
"""
import app as A
import datapack

FAILED = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def _task(tasks, key):
    return next((t for t in tasks if t.get("key") == key), None)


def test_pack_task_aggregated():
    print("\n[/api/tasks 聚合行情包任务]")
    # 用 Flask test_client，直接注入 datapack.STATE（avoid 真起线程写 340MB）
    saved = dict(datapack.STATE)
    try:
        datapack.STATE.update(kind="export", running=True, phase="写入行情",
                              done=4200000, total=10000000, pct=42,
                              message="合并 bars", result=None)
        with A.app.test_client() as c:
            r = c.get("/api/tasks")
            d = r.get_json()
        check(d.get("ok") is True, "/api/tasks 正常返回")

        tasks = d.get("tasks") or []
        t = _task(tasks, "pack")
        check(t is not None, "running 时应聚合出 key=pack 的任务")
        if t:
            check(t.get("name") == "导出行情包", "导出任务名应为「导出行情包」")
            check(abs((t.get("pct") or 0) - 0.42) < 0.01,
                  "pct 应由 datapack 的 0~100 转成 0~1（实际 %s）" % t.get("pct"))
            check(t.get("done") == 4200000 and t.get("total") == 10000000,
                  "done/total 应原样带出")
            check("写入行情" in (t.get("detail") or "") and "合并 bars" in (t.get("detail") or ""),
                  "detail 应含 phase 与 message")
        check(d.get("running") is True, "running 标志应为 true")
        check(d.get("overall") is not None and 0.4 < d["overall"] < 0.45,
              "只有 pack 一个任务时整体进度应等于它自己（实际 %s）" % d.get("overall"))
    finally:
        datapack.STATE.clear()
        datapack.STATE.update(saved)


def test_import_task_name():
    print("\n[导入任务名区分]")
    saved = dict(datapack.STATE)
    try:
        datapack.STATE.update(kind="import", running=True, phase="写入行情",
                              done=0, total=100, pct=0, message="", result=None)
        with A.app.test_client() as c:
            d = c.get("/api/tasks").get_json()
        t = _task(d.get("tasks") or [], "pack")
        check(t is not None and t.get("name") == "行情包导入",
              "kind=import 时任务名应为「行情包导入」")
    finally:
        datapack.STATE.clear()
        datapack.STATE.update(saved)


def test_idle_no_pack_task():
    print("\n[空闲时不聚合行情包任务]")
    saved = dict(datapack.STATE)
    try:
        datapack.STATE.update(running=False)
        with A.app.test_client() as c:
            d = c.get("/api/tasks").get_json()
        check(_task(d.get("tasks") or [], "pack") is None,
              "没在跑时不应出现 pack 任务")
    finally:
        datapack.STATE.clear()
        datapack.STATE.update(saved)


if __name__ == "__main__":
    test_pack_task_aggregated()
    test_import_task_name()
    test_idle_no_pack_task()
    if FAILED:
        print("\n失败 %d 项：%s" % (len(FAILED), "；".join(FAILED)))
        raise SystemExit(1)
    print("\n全部通过")
