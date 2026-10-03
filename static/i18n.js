/* Thai or English, per browser. See app/i18n.py for the whole scheme.
 *
 * The Thai text in the code is the key. In Thai tr() hands it straight back;
 * in English it looks it up in window.I18N, which base.html loads from
 * static/i18n/en.js only for a viewer who chose English. Changing language
 * reloads the page: the server renders the static text, so there is nothing
 * to keep in step and no per-label binding for the browser to run.
 *
 *   tr('ออนไลน์ {on} จาก {n}', { on: 9, n: 10 })
 *   tr('เปิดอยู่##contact')      the part after ## only picks the English
 */
(function () {
  'use strict';
  var lang = document.documentElement.lang === 'en' ? 'en' : 'th';
  var table = (lang === 'en' && window.I18N) || {};

  window.LANG = lang;
  /** What to hand toLocaleString and friends. */
  window.LOCALE = lang === 'en' ? 'en-GB' : 'th-TH';

  window.tr = function (text, values) {
    var out = table[text];
    if (out === undefined) out = text.split('##')[0];
    if (values) {
      out = out.replace(/\{(\w+)\}/g, function (whole, name) {
        return name in values ? values[name] : whole;
      });
    }
    return out;
  };

  window.setLang = function (next) {
    if (next === lang) return;
    // A cookie, not localStorage: the server has to see it to render the page.
    document.cookie = 'lang=' + (next === 'en' ? 'en' : 'th')
      + '; path=/; max-age=31536000; SameSite=Lax'
      + (location.protocol === 'https:' ? '; Secure' : '');
    location.reload();
  };
})();
