/**
 * 手机端（PWA / 添加到主屏幕）配置：manifest 字段、head 标签、图标文件。
 *
 * 这几项错一个，手机上就只是普通书签：没有图标、没有独立窗口、状态栏配色也不对，
 * 而且浏览器不会报错，只能靠肉眼发现 —— 所以用测试钉住。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

test("manifest：名称、启动地址、独立窗口、图标齐全", () => {
  const m = JSON.parse(read("web/manifest.webmanifest"));
  assert.equal(m.start_url, "/");
  assert.equal(m.display, "standalone", "独立窗口（否则打开还是带地址栏的浏览器）");
  assert.match(m.name, /QuantDesk/);
  assert.ok(m.short_name && m.short_name.length <= 12, "short_name 要能放在图标下");
  assert.equal(m.theme_color, "#1c5fd8");
  const sizes = (m.icons || []).map((x) => x.sizes);
  assert.ok(sizes.includes("192x192"), "Android 主屏图标至少 192");
  assert.ok(sizes.includes("512x512"), "启动画面用 512");
  assert.ok((m.icons || []).some((x) => x.purpose === "maskable"), "自适应图标要 maskable");
});

test("manifest 里的图标文件真实存在且是 PNG", () => {
  const m = JSON.parse(read("web/manifest.webmanifest"));
  for (const ic of m.icons) {
    const p = path.join(ROOT, "web", path.basename(ic.src));
    assert.ok(fs.existsSync(p), ic.src + " 应存在");
    const buf = fs.readFileSync(p);
    assert.deepEqual([...buf.subarray(0, 4)], [0x89, 0x50, 0x4e, 0x47], ic.src + " 应是 PNG");
    // PNG 的 IHDR 紧跟在 8 字节签名之后：宽高各 4 字节大端
    const w = buf.readUInt32BE(16), h = buf.readUInt32BE(20);
    assert.equal(w + "x" + h, ic.sizes, ic.src + " 实际尺寸要与声明一致");
  }
});

test("index.html：manifest / theme-color / iOS 主屏标签", () => {
  const html = read("web/index.html");
  assert.match(html, /<link rel="manifest" href="\/static\/manifest\.webmanifest">/);
  assert.match(html, /<meta name="theme-color" content="#1c5fd8">/);
  assert.match(html, /apple-mobile-web-app-capable" content="yes"/);
  assert.match(html, /apple-touch-icon" href="\/static\/icon-192\.png"/);
  assert.match(html, /<meta name="viewport"[^>]*width=device-width/);
});

test("后端：默认只监听本机，--lan 才放开", () => {
  const py = read("app.py");
  assert.match(py, /def bind_host\(/, "应集中处理监听地址");
  assert.match(py, /return "0\.0\.0\.0"/, "--lan 时放开");
  assert.match(py, /cfg\(\)\.get\("host"\) or "127\.0\.0\.1"/, "默认回落本机");
  assert.match(py, /add_type\("application\/manifest\+json", "\.webmanifest"\)/,
    ".webmanifest 要显式登记 MIME，否则 Chrome 忽略 manifest");
  // 打包已下线（exe 不再维护），入口只剩 app.py，--lan 由它自己解析
  assert.match(py, /bind_host\("--lan" in sys\.argv\)/, "启动参数 --lan 要能放开监听");
});
