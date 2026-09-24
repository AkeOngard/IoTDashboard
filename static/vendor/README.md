# Vendored front-end libraries

Served from here rather than a CDN: any script on the dashboard can switch
every device in the house, so no third party may be able to change one. The
CSP (`app/main.py`) allows scripts from this origin only.

| File | Package | Source |
|---|---|---|
| `alpinejs-3.14.1.min.js` | alpinejs 3.14.1 (MIT) | `dist/cdn.min.js` from `npm pack alpinejs@3.14.1` |
| `chart.js-4.4.4.umd.min.js` | chart.js 4.4.4 (MIT, `LICENSE.chart.js`) | `dist/chart.umd.js` from `npm pack chart.js@4.4.4` |

To update one: `npm pack <package>@<version>`, copy the same file out of the
tarball under a new versioned name, and change the `<script>` in
`templates/base.html`. npm checks the tarball against the registry's integrity
hash, which is the check a CDN `<script>` never got.
