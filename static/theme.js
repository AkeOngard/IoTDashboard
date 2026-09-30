/* Theme and surface style, applied before the first paint.
 *
 * Loaded synchronously in <head> (a file, not an inline script, so the CSP
 * can keep refusing inline code): setting the attributes this early is what
 * stops a light-theme viewer from seeing a dark flash on every load.
 *
 *   data-theme  dark | light     the viewer's choice, dark by default
 *   data-look   glass | solid    frosted panels or opaque ones
 *
 * Glass is the default only where it is cheap to honour: the browser must
 * support backdrop-filter, and the viewer must not have asked the system to
 * reduce transparency. An explicit choice is respected either way, except
 * that glass without backdrop-filter would just be see-through, so it falls
 * back to solid.
 */
(function () {
  'use strict';
  var root = document.documentElement;
  var saved = {};
  try {
    saved.theme = localStorage.getItem('iot.theme');
    saved.look = localStorage.getItem('iot.look');
  } catch (e) { /* private mode: defaults it is */ }

  var canBlur = !!(window.CSS && CSS.supports &&
    (CSS.supports('backdrop-filter', 'blur(1px)') || CSS.supports('-webkit-backdrop-filter', 'blur(1px)')));
  var calm = !!(window.matchMedia && matchMedia('(prefers-reduced-transparency: reduce)').matches);

  root.setAttribute('data-theme', saved.theme === 'light' ? 'light' : 'dark');
  var look = saved.look === 'solid' || saved.look === 'glass' ? saved.look : (calm ? 'solid' : 'glass');
  root.setAttribute('data-look', look === 'glass' && canBlur ? 'glass' : 'solid');
  root.setAttribute('data-can-blur', canBlur ? 'yes' : 'no');
})();
