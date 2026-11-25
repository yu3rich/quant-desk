# Agent 操作规范 · QuantDesk

> 这份文件是给所有接手本项目的 AI agent 看的。每次会话开始先读它。
> 里面每一条都对应一次**真实踩过的坑**，不是泛泛而谈。

---

## 一、改前端必须验证真实渲染，不能只用静态检查

### 血泪史

我曾用纯 CSS 给 `select option:hover` 加高亮，声称「Chrome/Edge 生效」，
结果用户刷新后发现**完全没变化**。原因是：

> 原生 `<select>` 的下拉面板由**浏览器 UI 层**渲染，
> `option` 的 `background` / `:hover` 在 Chrome/Edge 上基本不生效。
> CSS 改不动弹出层，必须自己画菜单。

当时我的"验证"是：
- jsdom 测试全绿（jsdom 根本不渲染 option 面板）
- grep 确认 CSS 规则存在
- curl 确认服务器返回了新 CSS

**三者都没错，但用户看到的界面没变。**

### 强制规范

改任何前端视觉，必须按这个顺序验证：

1. **语法检查**：`node --check web/app.js`（或对应文件）
2. **jsdom 行为测试**：`npm test`（`tests_ui/*.test.mjs`）
3. **真实浏览器渲染**：用 Edge 无头截图
   ```powershell
   $edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
   $tmp = Join-Path $env:TEMP ("edge_" + [guid]::NewGuid().ToString("N").Substring(0,8))
   $args = @("--headless=old","--disable-gpu","--no-sandbox","--hide-scrollbars",
             "--no-first-run","--disable-extensions","--disable-background-networking",
             "--window-size=1440,900","--screenshot=<输出路径>",
             "--virtual-time-budget=8000","--user-data-dir=$tmp","http://127.0.0.1:8765/")
   $p = Start-Process -FilePath $edge -ArgumentList $args -PassThru -WindowStyle Hidden
   $p | Wait-Process -Timeout 45 -ErrorAction SilentlyContinue
   if (-not $p.HasExited) { $p | Stop-Process -Force }
   Remove-Item $tmp -Recurse -Force
   ```
   **注意**：截图前先 `Get-Process msedge* | Stop-Process -Force`，否则残留进程会让截图挂住。
4. **如果看不了图**：用 Python 解析 PNG 做像素统计（亮度梯度、暗像素占比），
   证明阴影/颜色确实渲染出来了，而不是只证明 CSS 规则存在。

**铁律**：`grep 到 CSS 规则` ≠ `界面变了`。没有任何一种静态检查能替代真实渲染。

---

## 二、浏览器缓存：改完必须确认用户能拿到新文件

### 血泪史

`index.html` 里的 `?v=20261121f` 从 11-21 一直没改，
之后两天的 CSS/JS 改动用户全部看不到 —— 浏览器看到同一个 URL 直接用缓存。
用户反馈"感觉没什么变化"，我一开始还以为是他的操作问题。

### 当前方案（已实现，别破坏）

`app.py` 的 `index()` 会自动把静态资源 `?v=` 替换成文件 mtime + size 的哈希：

```python
def _asset_stamp():  # 取 style.css / app.js / index.html / icon.svg 的 mtime+size 做 md5
```

**规范**：
- 新增静态 JS/CSS 时，必须在 `index.html` 里加 `<script>` / `<link>` 引用
- 不要手工改 `?v=` 的值，服务端会自动注入
- 改动后验证：
  ```powershell
  $html = (Invoke-WebRequest "http://127.0.0.1:8765/").Content
  # 提取 ?v= 的值，改一个静态文件后应变化
  ```
- 用户报"没变化"时，**先怀疑缓存**，再怀疑用户操作

---

## 二·五、改行为前先搜完整链路，别只看调用点就动手

### 血泪史

用户说「打开系统好像默认重新选股票」。我只搜到 `init()` 里的
`await screenRun(true)`，就把首屏改成「手动空态」，还把后验也一起停了。
用户立刻指出改错了：

> 「直接显示上期的不就行了吗？而且重新选股的按钮不是有个判定候选榜单过期了会
> 彩虹环绕提示。你都没看完整个项目」

**我漏看的东西**（全都在 app.js 里，一搜就有）：

- [app.js:2812] `scrIsStale()` / `scrMarkStale()` —— 「重新选股」按钮的彩虹流光，
  判据是「今天是否选过 + 数据日是否超 24h」，本来就在正常工作
- [app.js:2824] `scrLastRunDay()` —— 用 localStorage 记录上次选股日，
  首屏 silent 出数明确 `record=false` 且不算「选过」，就是为了防止彩虹提醒被误压掉
- 落盘数据 `picks` + `picks_meta` 已经存了上期候选与因子 —— 正确做法是直接恢复显示，
  而不是让人手动点，也不是改空态

**这次做对的方向**（用户确认后）：新增 `/api/screen/resume`，
从落盘记录恢复上期候选（190ms vs 重建 6300ms），首屏先恢复，
彩虹提醒照旧判据；完全没有落盘记录时才自动出数一次（新用户不空表）。

### 规范

改任何「用户觉得行为不对」的问题，先按这个顺序把链路走完，**再动手**：

1. **搜同类机制**：不只搜用户点名的那一处，要搜「相关概念」的全部实现。
   例：改首屏 → 必须同时搜 `stale` / `rainbow` / `lastRun` / `record` / `resume`
   / `recover` / `picks` / `mark` —— 看有没有已存在的正确机制被我忽略。
2. **读注释和 docstring**：这个项目在每个关键决策点都写了「为什么这么做」。
   `screenRun` 的 silent 分支注释就明确解释了为什么不写记录、为什么不算选过。
   先读注释能避免推翻本来就正确的设计。
3. **问清目标行为再改**：用户说「好像重新选股」，真实诉求是「显示上期 + 保留过期提醒」，
   不是「别自动跑」。改之前用 `ask_user_question` 确认方向（这次问了才没继续错下去）。
4. **默认复用已有数据，而不是让用户输入更多**：有落盘记录就恢复显示；
   只有确实什么都没有时才fallback到自动计算。让用户点按钮是最后的选择。
5. 用户说「你没看完整个项目」时 —— 停下，把相关模块 grep 一遍，
   带着完整链路重新给方案，不要再顺着上一版错误继续改。

---

## 三、不要为了"像某个设计"就整体重构 UI

### 血泪史

用户让我参考一个"46 维度分层"网页重设计后台 UI。
我实现了四级海拔令牌、z-index 分区、按钮悬停升空……结果用户说"变丑了，改回去"。

最后只保留了一个下拉选择改造。

### 规范

- 用户说"参考 X 重设计"时，先用 `ask_user_question` 确认范围：
  - 是整体视觉语言，还是只改某一类控件？
  - 接受多大改动面？
- **默认最小改动**：能只改一个控件就不改全局令牌
- 大范围视觉改动前，先给一个截图/样例让用户确认方向，再全面铺开
- 用户说"改回去"时，**精确撤销**本次改动，不要顺手留下"觉得更好"的部分

---

## 四、原生 HTML 控件改造：先判断能不能纯 CSS 解决

| 控件 | 哪些能纯 CSS 改 | 哪些不能 |
|---|---|---|
| `input` / `textarea` | 边框、圆角、字体、focus 环 | — |
| `select` **触发器** | 边框、圆角、chevron 背景图 | — |
| `select` **弹出菜单** | ❌ 基本不行（浏览器 UI 层渲染） | 必须自定义 DOM |
| `input[type=date]` 日历弹窗 | ❌ 浏览器原生 | 要改只能自己写日历 |
| checkbox / radio | accent-color 可改主色 | 形状细节要自定义 |

判断不了时，先用一个最小探针页 + 真实浏览器截图验证，不要直接下结论。

---

## 五、包装 DOM 前，先盘依赖

`select-enhance.js` 包装 `<select>` 前，我先 grep 了 JS 里对 select 的操作：

```
.value / .innerHTML / .addEventListener("change")  ← 全部安全
.parentNode / .parentElement / nextElementSibling  ← 有就危险
```

**规范**：任何会改变 DOM 结构的增强，先搜这些依赖：
- `parentNode` / `parentElement`
- `nextElementSibling` / `previousElementSibling`
- `children` / `firstChild`
- 基于 `>` 的 CSS 选择器

包装后必须保证：
- 原生元素的 `.value` / `.checked` 照常读写
- 事件照常派发（点自定义项时显式 `dispatchEvent(new Event('change'))`）
- 动态内容（如 `innerHTML +=`）通过 `MutationObserver` 同步

---

## 六、测试写错时的处理

有两次测试"失败"其实是**断言写错**：

1. 我把漏斗摘要移出 `#scrSampleBox`，测试还断言"摘要必须在里面" ——
   测试正确抓到了设计变更，我更新断言为"必须在同一张卡内，且排在结论条之后"
2. 我统计 `backdrop-filter` 行数，把 `-webkit-` 前缀算成独立浮层，误报 5 处 ——
   改成按选择器块计数

**规范**：测试失败时先问「是产品错了，还是断言错了」。
更新断言时，要断言**设计意图**（"在同一张卡内"），不要断言**实现细节**（"在某个 div 里"）。
实现会变，意图不变。

---

## 七、用过的验证脚本

| 脚本 | 用途 |
|---|---|
| `npm test` | jsdom 行为测试（`tests_ui/*.test.mjs`） |
| `node --check web/app.js` | JS 语法 |
| Edge 无头截图 | 真实渲染 |
| `tests_ui/_probe*.mjs` | 一次性探针，验证完即删 |

新脚本用完就删，不要留在 `tests_ui/` 里当正式测试。正式测试文件名必须 `*.test.mjs`。

---

## 八、当前环境速查

- 项目根目录：`C:\Users\Administrator\WorkBuddy\html\quant-desk`
- Python：`.venv\Scripts\python.exe`
- 服务：`http://127.0.0.1:8765/`（Flask，`python app.py`）
- Node：`C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe`
- Edge：`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`
- 测试：`node --test --test-force-exit "tests_ui/*.test.mjs"`
  （`--test-force-exit` 必需：app.js 里有 setInterval，不加强制退出进程挂住）
