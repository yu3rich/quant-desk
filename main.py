"""
QuantDesk 桌面启动器。

职责边界很窄，只做「让双击 exe 就能用」这件事：
1. 建可写目录（data/、logs/ 等），换机迁移后也能直接跑；
2. 首次运行补默认配置（config.json 及各类 *.example.json）；
3. 预检端口，已有实例在跑就提示并退出，而不是抛一个看不懂的异常；
4. 起后台线程（收盘定时更新、盘中实盘刷新），延迟打开浏览器；
5. 交给 Flask。

开发时依然可以 `python app.py` 直接跑，两者行为一致。
"""

import socket
import sys
import threading
import time
import webbrowser

# 与 app.py 保持一致：Windows 控制台默认 GBK，中文与符号容易乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import paths

BANNER = "=" * 60


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _bootstrap_config():
    """首次运行补齐配置。已存在的一律不覆盖，避免冲掉用户填的账号与费率。"""
    copied = []
    # 示例配置在打包后位于 _internal/，所以统一走 paths.resource（同级优先、包内兜底）
    src = paths.resource("config.example.json")
    dst = paths.APP_HOME / "config.json"
    if src.exists() and not dst.exists():
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        copied.append(dst.name)

    # data 下的示例配置：只补缺，不覆盖用户已填的账号/密钥
    seen = set()
    for res_dir in (paths.DATA, paths.RES / "data"):
        if not res_dir.exists():
            continue
        for s2 in res_dir.glob("*.example.json"):
            if s2.name in seen:
                continue
            seen.add(s2.name)
            d2 = paths.DATA / s2.name.replace(".example", "")
            if d2.exists():
                continue
            d2.parent.mkdir(parents=True, exist_ok=True)
            d2.write_text(s2.read_text(encoding="utf-8"), encoding="utf-8")
            copied.append(str(d2.relative_to(paths.APP_HOME)))
    return copied


def _selftest() -> int:
    """
    依赖自检：只验证「打包后最容易缺东西」的几项，不碰网络、不读账户。
    用法：QuantDesk.exe --selftest
    """
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            print("  [OK]   %-22s %s" % (name, fn()))
        except Exception as e:
            ok = False
            print("  [FAIL] %-22s %r" % (name, e))

    def _py():
        return "%d.%d.%d" % sys.version_info[:3]

    def _lgb():
        import numpy as np
        import lightgbm as lgb
        X = np.random.rand(120, 4)
        y = (X[:, 0] + X[:, 1] > 1).astype(int)
        m = lgb.train({"objective": "binary", "verbose": -1, "num_leaves": 7},
                      lgb.Dataset(X, label=y), num_boost_round=8)
        ver = getattr(lgb, "__version__", "") or getattr(lgb, "VERSION", "") or "?"
        return "v%s 预测=%s" % (ver, round(float(m.predict(X[:3])[0]), 3))

    def _pd():
        import pandas as pd
        df = pd.DataFrame({"a": [1, 2, 3]})
        return "v%s sum=%d" % (pd.__version__, int(df["a"].sum()))

    def _ocr():
        from importlib import resources
        p = resources.files("emtl").joinpath("models", "tiny_captcha_model.b64")
        if not p.is_file():
            raise RuntimeError("验证码模板缺失：" + str(p))
        from emtl._ocr import make_ocr
        make_ocr()
        return "emtl 资源与 OCR 引擎就绪"

    print(BANNER)
    print("  QuantDesk 依赖自检  " + paths.describe())
    print(BANNER)
    check("Python 运行时", _py)
    check("pandas", _pd)
    check("lightgbm 训练/预测", _lgb)
    check("emtl 验证码资源", _ocr)
    check("资源目录 web/index.html", lambda: (paths.WEB / "index.html").is_file())
    print(BANNER)
    print("  结果：" + ("全部通过" if ok else "存在失败项，见上"))
    return 0 if ok else 1


def _bulk_worker(argv: list[str]) -> bool:
    """打包态的并行下载子进程：复用同一个 exe，用参数分派到 bulkdata 的 worker。

    冻结后没有 bulkdata.py 可执行文件，父进程只能拉自己，因此必须在这里分派，
    否则会变成「又启动一个完整服务」——端口被占，子进程立刻退出，下载永远 0 进度。
    """
    if len(argv) >= 6 and argv[1] == "--bulkdata-worker":
        import bulkdata
        bulkdata._worker_main(argv[2:6])
        return True
    return False


def main() -> int:
    if _bulk_worker(sys.argv):
        return 0
    if "--selftest" in sys.argv:
        return _selftest()

    paths.ensure_dirs()
    copied = _bootstrap_config()

    print(BANNER)
    print("  QuantDesk - 本地 A 股量化工作台")
    print("  " + paths.describe())
    if copied:
        print("  已补默认配置: " + ", ".join(copied))
    print(BANNER)

    # app 侧有模块级副作用（调度器注册等），放在 paths 就绪之后再导入
    import app as server

    port = int(server.cfg().get("port", 8765))
    url = "http://127.0.0.1:%d/" % port

    if _port_busy(port):
        print("")
        print("  端口 %d 已被占用，可能已经有一个 QuantDesk 在运行。" % port)
        print("  如果那个窗口还开着，直接用它就行：%s" % url)
        print("  确认没有在运行，可以改 config.json 里的 port 换一个端口。")
        print("")
        try:
            input("  按回车退出…")
        except (EOFError, KeyboardInterrupt):
            pass
        return 1

    print("")
    print("  浏览器地址: %s" % url)
    print("  关闭本窗口即退出程序（数据都在 data/ 里，随时可再打开）")
    print(BANNER)

    # 延迟开浏览器，给 Flask 一点绑定端口的时间
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        server.scheduler.start()     # 收盘后自动更新数据链
        server.emtlink.start_auto()  # 盘中低频繁刷新 + 收盘日结实盘账户
    except Exception as e:           # 后台线程起不来不该拖垮整个程序
        print("  [警告] 后台任务启动失败（不影响手动使用）: %r" % (e,))

    try:
        server.app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("")
        print("  服务异常退出: %r" % (e,))
        try:
            input("  按回车关闭窗口…")
        except (EOFError, KeyboardInterrupt):
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
