(function () {
  "use strict";
  if (!window.htmx) return;

  const selector = "form[data-ticket-filters]";
  const states = new WeakMap();
  const requests = new WeakMap();

  function state(form) {
    if (!states.has(form)) states.set(form, { revision: 0, timer: null });
    return states.get(form);
  }

  function feedback(form, loading, error = false) {
    form.classList.toggle("ticket-filters--error", error);
    form.setAttribute("aria-busy", String(loading));
    const workspace = form.closest(form.getAttribute("hx-target"));
    const results = workspace?.querySelector("[data-ticket-filter-results]");
    if (results) {
      results.setAttribute("aria-busy", String(loading));
      results.querySelector("[data-filter-loading]").hidden = !loading;
    }
    const region = form.nextElementSibling;
    if (region && region.matches(".ticket-filter-feedback")) {
      region.hidden = !error;
      region.querySelector("[data-filter-error]").hidden = !error;
    }
  }

  function initialize() {
    document.querySelectorAll(selector).forEach((form) => {
      form.classList.add("ticket-filters--automatic");
    });
  }

  function changed(event) {
    const input = event.target;
    const form = input.closest(selector);
    if (!form || !input.matches("input, select")) return;
    if (event.type === "input" && input.name !== "q") return;
    if (event.type === "change" && input.name === "q") return;
    const current = state(form);
    current.revision += 1;
    clearTimeout(current.timer);
    // Invalidate immediately, including during the search debounce window.
    window.htmx.trigger(form, "htmx:abort");
    if (input.name === "state") {
      const exact = form.querySelector('[name="status"]');
      if (exact) exact.value = "";
    }
    feedback(form, true);
    if (event.isComposing) return;
    const submit = () => {
      if (form.isConnected) window.htmx.trigger(form, "ticket-filter-submit");
    };
    if (input.name === "q") current.timer = setTimeout(submit, 300);
    else submit();
  }

  document.addEventListener("input", changed);
  document.addEventListener("change", changed);
  document.addEventListener("compositionend", changed);
  document.addEventListener("htmx:configRequest", (event) => {
    const form = event.detail.elt;
    if (form.matches(selector)) {
      event.detail.headers["X-Ticket-Auto-Filter"] = "true";
    }
  });
  document.addEventListener("htmx:beforeRequest", (event) => {
    const form = event.detail.elt;
    if (!form.matches(selector)) return;
    const current = state(form);
    clearTimeout(current.timer);
    requests.set(event.detail.xhr, { form, revision: current.revision });
    feedback(form, true);
  });
  document.addEventListener("htmx:beforeOnLoad", (event) => {
    const request = requests.get(event.detail.xhr);
    if (request && (!request.form.isConnected ||
        state(request.form).revision !== request.revision)) {
      // Stop processing before HTMX applies history/redirect headers or swaps.
      event.preventDefault();
    }
  });
  document.addEventListener("htmx:beforeSwap", (event) => {
    const request = requests.get(event.detail.xhr);
    const focused = document.activeElement;
    if (request && request.form.contains(focused) && focused.name === "q") {
      request.focus = { input: focused, start: focused.selectionStart, end: focused.selectionEnd };
    }
  });
  document.addEventListener("htmx:afterSwap", (event) => {
    const focus = requests.get(event.detail.xhr)?.focus;
    if (focus && focus.input.isConnected) {
      focus.input.focus({ preventScroll: true });
      focus.input.setSelectionRange(focus.start, focus.end);
    }
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    const request = requests.get(event.detail.xhr);
    if (!request || !request.form.isConnected ||
        state(request.form).revision !== request.revision) return;
    feedback(request.form, false, !event.detail.successful);
  });
  document.addEventListener("htmx:load", initialize);
  document.addEventListener("htmx:historyRestore", initialize);
  document.addEventListener("DOMContentLoaded", initialize);
  initialize();
})();
