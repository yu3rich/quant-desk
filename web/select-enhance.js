/**
 * Element Plus 风格下拉选择（渐进增强，零 API 侵入）
 *
 * 为什么要包装 DOM：原生 <select> 的下拉面板由浏览器 UI 层渲染，
 * CSS 的 option:hover/background 在 Chrome/Edge 上基本不生效 ——
 * 纯 CSS 改不动弹出层，必须自己画菜单。
 *
 * 兼容性保证：
 *   - 原生 <select> 保留在 DOM（视觉隐藏但仍可访问），所有 $("#id").value 照旧
 *   - 动态 .innerHTML 填 options → MutationObserver 同步到自定义菜单
 *   - 点菜单项时给原生 select dispatch change + input，现有监听照常触发
 *   - 原生 select 的 class（field / mini-sel 等）复制到包装层，布局不跑偏
 */
(function () {
  'use strict';

  var ARROW = '<svg viewBox="0 0 12 12" aria-hidden="true">' +
    '<path d="M3 4.5 6 7.5l3-3" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var current = null;   // 当前打开的包装层，保证互斥

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function opts(sel) {
    return Array.prototype.slice.call(sel.options || []);
  }

  function wrap(sel) {
    if (!sel || sel.dataset.epWrapped) return;
    sel.dataset.epWrapped = '1';

    var box = document.createElement('div');
    box.className = 'ep-sel';
    // 不继承原 select 的 class：mini-sel / field 等会带边框、padding、背景，
    // 包装层和 .ep-sel-trigger 各画一遍 → 出现「套了两层容器」的双边框。
    // 只把「宽度类」转成专用标记，外观完全由 .ep-sel-trigger 承担。
    (sel.className || '').split(/\s+/).forEach(function (c) {
      if (c === 'mini-sel') box.classList.add('ep-sel--mini');
    });
    sel.parentNode.insertBefore(box, sel);
    box.appendChild(sel);
    // 从原生 select 上移除外观类，避免它（即使隐藏）参与任何控件样式
    sel.classList.remove('mini-sel');
    sel.classList.add('ep-native');

    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'ep-sel-trigger';
    trigger.innerHTML = '<span class="ep-sel-val"></span>' +
      '<i class="ep-sel-arrow">' + ARROW + '</i>';
    box.appendChild(trigger);

    var menu = document.createElement('div');
    menu.className = 'ep-sel-menu';
    menu.setAttribute('role', 'listbox');
    box.appendChild(menu);

    function render() {
      var list = opts(sel);
      var i = Math.max(0, sel.selectedIndex || 0);
      var cur = list[i];
      var val = trigger.querySelector('.ep-sel-val');
      if (val) val.textContent = cur ? cur.text : '';
      // 触发器本身已显示选中项，再挂原生 title 只会和统一气泡叠成两层
      trigger.removeAttribute('title');
      trigger.setAttribute('aria-label', cur ? cur.text : '请选择');
      trigger.disabled = !!sel.disabled;

      menu.innerHTML = list.length
        ? list.map(function (o, idx) {
            var cls = 'ep-sel-opt' +
              (idx === i ? ' is-on' : '') +
              (o.disabled ? ' is-disabled' : '');
            return '<div class="' + cls + '" data-i="' + idx + '" role="option"' +
              (idx === i ? ' aria-selected="true"' : '') +
              (o.disabled ? ' aria-disabled="true"' : '') + '>' +
              '<span>' + esc(o.text) + '</span>' +
              (idx === i ? '<i class="ep-sel-ok">✓</i>' : '') + '</div>';
          }).join('')
        : '<div class="ep-sel-empty">无选项</div>';
    }

    function open() {
      if (current && current !== box) closeAll();
      render();
      menu.classList.add('on');
      trigger.classList.add('on');
      trigger.setAttribute('aria-expanded', 'true');
      current = box;
    }
    function close() {
      menu.classList.remove('on');
      trigger.classList.remove('on');
      trigger.setAttribute('aria-expanded', 'false');
      if (current === box) current = null;
    }

    trigger.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (sel.disabled) return;
      menu.classList.contains('on') ? close() : open();
    });

    menu.addEventListener('click', function (e) {
      var item = e.target.closest('.ep-sel-opt');
      if (!item || item.classList.contains('is-disabled')) return;
      var idx = Number(item.getAttribute('data-i'));
      if (isNaN(idx)) return;
      sel.selectedIndex = idx;
      // 显式派发，让既有 change 监听器照常工作
      try {
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        sel.dispatchEvent(new Event('input', { bubbles: true }));
      } catch (err) { /* 老浏览器忽略 */ }
      render();
      close();
    });

    sel.addEventListener('change', render);

    // 动态填充 options（股票池预热等）后同步菜单
    if (window.MutationObserver) {
      new MutationObserver(function () {
        if (!menu.classList.contains('on')) render();
      }).observe(sel, { childList: true, subtree: true, characterData: true });
    }

    render();
    box.__epRender = render;
  }

  function closeAll() {
    if (!current) return;
    current.querySelector('.ep-sel-menu').classList.remove('on');
    var t = current.querySelector('.ep-sel-trigger');
    if (t) { t.classList.remove('on'); t.setAttribute('aria-expanded', 'false'); }
    current = null;
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.ep-sel')) closeAll();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeAll();
  });

  window.EpSelect = {
    init: function (root) {
      (root || document).querySelectorAll('select:not([data-ep-wrapped])').forEach(wrap);
    },
    refresh: function (el) {
      var box = el && el.closest ? el.closest('.ep-sel') : null;
      if (box && box.__epRender) box.__epRender();
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { window.EpSelect.init(); });
  } else {
    window.EpSelect.init();
  }
})();
