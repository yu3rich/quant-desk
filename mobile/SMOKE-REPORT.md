# 冒烟验证报告（无 Android SDK，纯 Python 层）

日期：2026-11-25　环境：Linux x86-64, Python 3.12.3（venv `/tmp/qd-smoke`，无图形、无 Android SDK）

## 方法

```bash
python3 -m venv /tmp/qd-smoke
/tmp/qd-smoke/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    baostock rsa requests Pillow pandas flask cryptography
/tmp/qd-smoke/bin/pip install --no-deps /tmp/emtl/emtl-0.3.0-py3-none-any.whl
/tmp/qd-smoke/bin/python mobile/smoke_test.py
```

脚本：`mobile/smoke_test.py`（验证 import emtl / baostock 拉 sh.600000 日线 / import flask）。

## 原始输出

```
== smoke test, python: 3.12.3 ==
[OK] import emtl (version 0.3.0)
[OK] emtl.client importable
login success!
[..] login: 0 success
[OK] baostock daily rows: 259 (error_code=0)
    ['2026-11-12', 'sh.600000', '10.2100', '50081746']
    ['2026-11-13', 'sh.600000', '10.4000', '76157313']
    ['2026-11-16', 'sh.600000', '10.7800', '96415701']
    ['2026-11-17', 'sh.600000', '10.8600', '92524741']
    ['2026-11-18', 'sh.600000', '10.7200', '65527092']
    ['2026-11-19', 'sh.600000', '10.7000', '67763411']
    ['2026-11-20', 'sh.600000', '10.8700', '100866741']
    ['2026-11-23', 'sh.600000', '10.7500', '78526337']
    ['2026-11-24', 'sh.600000', '10.6900', '72450363']
    ['2026-11-25', 'sh.600000', '10.6700', '71758061']
logout success!
[OK] import flask 3.1.3
```

（DeprecationWarning: flask `__version__` 属 Python 3.12 + Flask 3.1 环境提示，非错误，已省略。）

## 结论

- ✅ emtl 0.3.0 纯 Python wheel 安装并 `import emtl` 成功（验证码 OCR 走内置轻量模型，无原生库依赖生效）
- ✅ baostock 登录 + 拉 sh.600000 日线成功（最近 10 根收盘价/成交量如上，259 根全量）
- ✅ Flask 3.1.3 可导入（手机版 WebView 后端服务层）
- ✅ `mobile/sync_core.py` 同步 25 个白名单文件成功，`--check` 校验通过

## 不能在本机验证的（需 Android SDK / 真机）

Chaquopy 构建链、pandas/Pillow 的 Android wheel 行为、Android 下 socket 连通性、
WebView 渲染、app.py 白名单外 import 问题（见 README 未验证清单 #2，v1 阻塞项）。
