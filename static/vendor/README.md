# Vendored front-end libraries

Served from here rather than a CDN: any script on the dashboard can switch
every device in the house, so no third party may be able to change one. The
CSP (`app/main.py`) allows scripts from this origin only.

| File | Package | Source |
|---|---|---|
| `alpinejs-3.14.1.min.js` | alpinejs 3.14.1 (MIT) | `dist/cdn.min.js` from `npm pack alpinejs@3.14.1` |

To update one: `npm pack <package>@<version>`, copy the same file out of the
tarball under a new versioned name, and change the `<script>` in
`templates/base.html`. npm checks the tarball against the registry's integrity
hash, which is the check a CDN `<script>` never got.

Chart.js was here until the history chart became plain SVG drawn by
`static/dashboard.js`: 200 KB less for every browser to fetch from the Pi.
