(function () {
  "use strict";

  document.querySelectorAll("form[data-ticket-attachments]").forEach(form => {
    const input = form.querySelector('input[type="file"][name="attachments"]');
    const description = form.querySelector('textarea[name="description"]');
    const selection = form.querySelector("[data-attachment-selection]");
    const list = form.querySelector("[data-attachment-list]");
    const status = form.querySelector("[data-attachment-status]");
    if (!input || !description || !selection || !list || !status) return;
    // Preserve the native picker if assigning FileList is unsupported.
    try {
      const probe = document.createElement("input");
      probe.type = "file";
      probe.files = new DataTransfer().files;
    } catch (_) { return; }

    let files = Array.from(input.files);
    let dragDepth = 0;

    function render(error = false) {
      list.replaceChildren();
      files.forEach((file, index) => {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = file.name;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "button button--ghost";
        remove.textContent = form.dataset.attachmentRemove;
        remove.setAttribute("aria-label", `${form.dataset.attachmentRemove}: ${file.name}`);
        remove.addEventListener("click", () => {
          update(files.filter((_, position) => position !== index));
          // Removing a focused control must not strand keyboard focus.
          const buttons = list.querySelectorAll("button");
          (buttons[Math.min(index, buttons.length - 1)] || input).focus();
        });
        item.append(name, remove);
        list.append(item);
      });
      list.hidden = files.length === 0;
      status.textContent = error ? form.dataset.attachmentError : `${form.dataset.attachmentCount} ${files.length}`;
    }

    function update(next) {
      try {
        const transfer = new DataTransfer();
        next.forEach(file => transfer.items.add(file));
        input.files = transfer.files;
      } catch (_) {
        // Always show what the native multipart form will actually send.
        files = Array.from(input.files);
        render(true);
        return false;
      }
      files = Array.from(input.files);
      render();
      return true;
    }

    input.addEventListener("change", () => update([...files, ...input.files]));
    const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes("Files");
    form.addEventListener("dragenter", event => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      dragDepth += 1;
      form.classList.add("ticket-create__form--dragging");
    });
    form.addEventListener("dragover", event => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    });
    form.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) form.classList.remove("ticket-create__form--dragging");
    });
    form.addEventListener("drop", event => {
      dragDepth = 0;
      form.classList.remove("ticket-create__form--dragging");
      if (!hasFiles(event)) return;
      event.preventDefault();
      const items = Array.from(event.dataTransfer.items || []);
      if (items.some(item => item.webkitGetAsEntry?.()?.isDirectory)) {
        render(true);
        return;
      }
      update([...files, ...event.dataTransfer.files]);
    });
    description.addEventListener("paste", event => {
      const incoming = Array.from(event.clipboardData?.files || []);
      if (!incoming.length) return;
      if (update([...files, ...incoming]) && !event.clipboardData.getData("text/plain")) {
        event.preventDefault();
      }
    });
    form.addEventListener("reset", () => {
      // Native reset clears the input after the reset event has been dispatched.
      setTimeout(() => {
        files = Array.from(input.files);
        dragDepth = 0;
        form.classList.remove("ticket-create__form--dragging");
        render();
      }, 0);
    });
    selection.hidden = false;
    render();
  });
})();
