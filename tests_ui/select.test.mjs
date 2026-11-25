/**
 * Element Plus 风格下拉的回归测试。
 * 关键：包装 DOM 后，所有 $("#id").value / change 事件必须照常工作。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { JSDOM } from "jsdom";

const selectJs = fs.readFileSync('web/select-enhance.js', 'utf8');
const html = `<!DOCTYPE html><html><body>
<div class="field">
  <label>范围</label>
  <select id="scope">
    <option value="cached">已有行情缓存</option>
    <option value="pool">股票池</option>
    <option value="all">全 A</option>
  </select>
</div>
<div class="inline">
  <select class="mini-sel" id="mini">
    <option value="5">5 日</option>
    <option value="10" selected>10 日</option>
    <option value="20" disabled>20 日（禁用）</option>
  </select>
</div>
</body></html>`;

function boot() {
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost/' });
  const { window } = dom;
  window.eval(selectJs);
  window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  return { dom, window, doc: window.document };
}

test('所有 select 都被包装，原生 select 仍保留', () => {
  const { doc } = boot();
  assert.equal(doc.querySelectorAll('.ep-sel').length, 2, '应包装 2 个 select');
  assert.equal(doc.querySelectorAll('select').length, 2, '原生 select 必须保留');
  // select 自身的 class 继承到包装层（.field 属于父容器，不该被继承）
  const scopeBox = doc.querySelector('#scope').closest('.ep-sel');
  assert.ok(scopeBox, '#scope 应有包装层');
  assert.ok(!scopeBox.classList.contains('field'), '.field 是父容器，不应复制到包装层');
  // 关键：包装层不能再继承 mini-sel —— 那会让包装层和 trigger 各画一层边框，
  // 表现为「套了一次容器」。改用专用 ep-sel--mini 只保留宽度。
  const miniBox = doc.querySelector('#mini').closest('.ep-sel');
  assert.ok(miniBox.classList.contains('ep-sel--mini'), '应用专用宽度标记');
  assert.ok(!miniBox.classList.contains('mini-sel'), '包装层不应带 mini-sel（会重复画边框）');
  assert.ok(!doc.querySelector('#mini').classList.contains('mini-sel'),
    '原生 select 应移除外观类，避免隐藏元素也参与控件样式');
});

test('触发器显示当前选项文本', () => {
  const { doc } = boot();
  const scope = doc.querySelector('#scope');
  const box = scope.closest('.ep-sel');
  assert.equal(box.querySelector('.ep-sel-val').textContent, '已有行情缓存');

  const mini = doc.querySelector('#mini');
  assert.equal(mini.closest('.ep-sel').querySelector('.ep-sel-val').textContent, '10 日');
});

test('点击触发器弹出菜单，含全部选项与勾选态', () => {
  const { window, doc } = boot();
  const trigger = doc.querySelector('#scope').closest('.ep-sel').querySelector('.ep-sel-trigger');
  trigger.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  const box = trigger.closest('.ep-sel');
  assert.ok(box.querySelector('.ep-sel-menu').classList.contains('on'), '菜单应打开');
  const opts = box.querySelectorAll('.ep-sel-opt');
  assert.equal(opts.length, 3, '应有 3 个选项');
  assert.ok(opts[0].classList.contains('is-on'), '第一项应标记为选中');
});

test('点击菜单项同步原生 select.value 并派发 change', () => {
  const { window, doc } = boot();
  const sel = doc.querySelector('#scope');
  let changed = 0;
  sel.addEventListener('change', () => changed++);

  const box = sel.closest('.ep-sel');
  box.querySelector('.ep-sel-trigger').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const second = box.querySelectorAll('.ep-sel-opt')[1];
  second.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  assert.equal(sel.value, 'pool', '原生 select.value 应同步');
  assert.equal(changed, 1, '应派发一次 change 事件');
  assert.equal(box.querySelector('.ep-sel-val').textContent, '股票池', '触发器文本应更新');
  assert.ok(!box.querySelector('.ep-sel-menu').classList.contains('on'), '菜单应关闭');
});

test('禁用选项不可点击', () => {
  const { window, doc } = boot();
  const sel = doc.querySelector('#mini');
  const box = sel.closest('.ep-sel');
  box.querySelector('.ep-sel-trigger').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  const disabled = Array.from(box.querySelectorAll('.ep-sel-opt'))
    .find(o => o.classList.contains('is-disabled'));
  assert.ok(disabled, '应有禁用项');
  const before = sel.value;
  disabled.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  assert.equal(sel.value, before, '禁用项点击不应改变值');
});

test('同时只开一个菜单，点外部及 Esc 关闭', () => {
  const { window, doc } = boot();
  const t1 = doc.querySelector('#scope').closest('.ep-sel').querySelector('.ep-sel-trigger');
  const t2 = doc.querySelector('#mini').closest('.ep-sel').querySelector('.ep-sel-trigger');
  t1.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  t2.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  const menus = doc.querySelectorAll('.ep-sel-menu.on');
  assert.equal(menus.length, 1, '同时只能开一个菜单');

  doc.body.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  assert.equal(doc.querySelectorAll('.ep-sel-menu.on').length, 0, '点外部应关闭');

  t1.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(doc.querySelectorAll('.ep-sel-menu.on').length, 0, 'Esc 应关闭');
});

test('动态填充 options 后菜单同步', () => {
  const { window, doc } = boot();
  const sel = doc.querySelector('#scope');
  const box = sel.closest('.ep-sel');
  sel.innerHTML += '<option value="new">新增项</option>';
  // MutationObserver 在 jsdom 中是微任务，用定时器等它
  return new Promise((resolve) => {
    setTimeout(() => {
      const opts = box.querySelectorAll('.ep-sel-opt');
      assert.equal(opts.length, 4, '动态新增的 option 应同步到菜单');
      assert.ok(Array.from(opts).some(o => o.textContent.includes('新增项')));
      resolve();
    }, 20);
  });
});
