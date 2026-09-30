/* Theme and glass level, applied before the first paint.
 *
 * Loaded synchronously in <head> (a file, not an inline script, so the CSP
 * can keep refusing inline code): setting these this early is what stops a
 * light-theme viewer from seeing a dark flash on every load.
 *
 *   data-theme  dark | light      the viewer's choice, dark by default
 *   --glass     0 … 1             how see-through the panels are
 *   data-look   glass | solid     solid whenever --glass is 0, so a fully
 *                                 opaque page carries no blur at all
 *
 * Full glass is the default only where it is cheap to honour: the browser
 * must support backdrop-filter, and the viewer must not have asked the system
 * to reduce transparency. A level the viewer picked is respected, except that
 * glass without backdrop-filter would just be see-through, so it stays solid.
 */
(function () {
  'use strict';
  var root = document.documentElement;
  var saved = {};
  try {
    saved.theme = localStorage.getItem('iot.theme');
    saved.glass = localStorage.getItem('iot.glass');
    saved.look = localStorage.getItem('iot.look');   // the earlier on/off switch
  } catch (e) { /* private mode: defaults it is */ }

  var canBlur = !!(window.CSS && CSS.supports &&
    (CSS.supports('backdrop-filter', 'blur(1px)') || CSS.supports('-webkit-backdrop-filter', 'blur(1px)')));
  var calm = !!(window.matchMedia && matchMedia('(prefers-reduced-transparency: reduce)').matches);

  var level = parseInt(saved.glass, 10);
  if (isNaN(level)) level = saved.look === 'solid' || calm ? 0 : 100;
  level = canBlur ? Math.max(0, Math.min(100, level)) : 0;

  root.setAttribute('data-theme', saved.theme === 'light' ? 'light' : 'dark');
  root.setAttribute('data-look', level > 0 ? 'glass' : 'solid');
  root.setAttribute('data-glass', String(level));
  root.setAttribute('data-can-blur', canBlur ? 'yes' : 'no');
  root.style.setProperty('--glass', String(level / 100));
})();
