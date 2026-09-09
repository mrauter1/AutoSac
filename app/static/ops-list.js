(function () {
  "use strict";
  const key = "autosac.ops.show-internal-summary";
  let show = false;
  try { show = localStorage.getItem(key) === "true"; } catch (_) {}

  function render() {
    document.querySelectorAll("[data-ops-summary-option]").forEach(option => { option.hidden = false; });
    document.querySelectorAll("[data-ops-summary-toggle]").forEach(input => { input.checked = show; });
    document.querySelectorAll(".ops-list-summary").forEach(summary => { summary.hidden = !show; });
  }
  document.addEventListener("change", event => {
    if (!event.target.matches("[data-ops-summary-toggle]")) return;
    show = event.target.checked;
    try { localStorage.setItem(key, String(show)); } catch (_) {}
    render();
  });
  document.addEventListener("htmx:load", render);
  document.addEventListener("htmx:historyRestore", render);
  document.addEventListener("DOMContentLoaded", render);
  window.addEventListener("pageshow", render);
  render();
})();
