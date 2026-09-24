// Tailwind build for the dashboard. The output, static/tailwind.css, is
// committed so the image needs no Node: rebuild it with `make css` after
// changing classes in templates/ or static/.
//
// It replaced the play CDN (cdn.tailwindcss.com), which compiled the classes
// in the browser from a script served by a third party -- a script that could
// drive every device in the house, and that Tailwind itself says is not for
// production.
module.exports = {
  // Relative to this file, so the build works from any directory.
  content: {
    relative: true,
    files: ["../templates/**/*.html", "../static/*.js"],
  },
  theme: { extend: {} },
  plugins: [],
};
