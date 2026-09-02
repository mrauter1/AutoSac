(function () {
  "use strict";

  const composers = document.querySelectorAll("[data-ticket-composer]");
  let hasUnsavedDraft = false;
  let submitting = false;

  function controlHasDraft(control) {
    if (control instanceof HTMLInputElement && control.type === "file") {
      return control.files && control.files.length > 0;
    }
    return typeof control.value === "string" && control.value.trim().length > 0;
  }

  function updateUnsavedDraftState() {
    hasUnsavedDraft = Array.from(
      document.querySelectorAll("#ticket-composer-region textarea, #ticket-composer-region input[type='file']")
    ).some(controlHasDraft);
  }

  function activateComposerMode(composer, mode, focusPanel) {
    composer.classList.add("ticket-composer--enhanced");
    composer.querySelectorAll("[data-composer-mode-button]").forEach((button) => {
      const active = button.dataset.composerModeButton === mode;
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    composer.querySelectorAll("[data-composer-mode-panel]").forEach((panel) => {
      const active = panel.dataset.composerModePanel === mode;
      panel.hidden = !active;
      if (active && focusPanel) {
        const field = panel.querySelector("textarea");
        if (field) {
          field.focus();
        }
      }
    });
  }

  composers.forEach((composer) => {
    const initialMode = composer.dataset.initialMode || "public";
    activateComposerMode(composer, initialMode, false);
    composer.querySelectorAll("[data-composer-mode-button]").forEach((button) => {
      button.addEventListener("click", () => activateComposerMode(composer, button.dataset.composerModeButton, true));
    });
    composer.querySelectorAll("textarea, input[type='file']").forEach((control) => {
      control.addEventListener(control instanceof HTMLInputElement ? "change" : "input", updateUnsavedDraftState);
    });
  });
  updateUnsavedDraftState();

  window.addEventListener("beforeunload", (event) => {
    if (!hasUnsavedDraft || submitting) {
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }

    let confirmation = form.dataset.confirm || "";
    if (form.matches("[data-status-form]")) {
      const statusField = form.elements.next_status;
      if (statusField && statusField.value === "ai_triage") {
        confirmation = form.dataset.confirmAi || "";
      } else if (statusField && statusField.value === "resolved") {
        confirmation = form.dataset.confirmResolved || "";
      }
    }
    if (confirmation && !window.confirm(confirmation)) {
      event.preventDefault();
      return;
    }

    const composer = form.closest("#ticket-composer-region");
    const otherDraftExists = composer && Array.from(composer.querySelectorAll("textarea, input[type='file']"))
      .some((control) => control.form !== form && controlHasDraft(control));
    submitting = Boolean(composer) && !otherDraftExists;

    const submitter = event.submitter || form.querySelector("button[type='submit']");
    if (submitting && submitter && submitter.dataset.submittingLabel) {
      submitter.textContent = submitter.dataset.submittingLabel;
      submitter.disabled = true;
      form.setAttribute("aria-busy", "true");
    }
  });
})();
