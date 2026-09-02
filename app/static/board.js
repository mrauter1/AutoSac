(function () {
  "use strict";

  const JSON_ACCEPT = "application/json";

  class BoardController {
    constructor(root) {
      this.root = root;
      this.draggedCard = null;
      this.pointerStartedOnControl = false;
      this.submitting = false;
      this.announcer = root.querySelector("[data-board-announcer]");
      this.feedback = root.querySelector("[data-board-feedback]");
      this.feedbackMessage = root.querySelector("[data-board-feedback-message]");
      this.refreshLink = root.querySelector("[data-board-refresh]");
      this.onSubmit = this.onSubmit.bind(this);
      this.onPointerDown = this.onPointerDown.bind(this);
      this.onDragStart = this.onDragStart.bind(this);
      this.onDragEnd = this.onDragEnd.bind(this);
      this.onDragOver = this.onDragOver.bind(this);
      this.onDragLeave = this.onDragLeave.bind(this);
      this.onDrop = this.onDrop.bind(this);
      this.onAfterSwap = this.onAfterSwap.bind(this);
      this.onKeyDown = this.onKeyDown.bind(this);
    }

    start() {
      this.root.addEventListener("submit", this.onSubmit);
      this.root.addEventListener("pointerdown", this.onPointerDown);
      this.root.addEventListener("dragstart", this.onDragStart);
      this.root.addEventListener("dragend", this.onDragEnd);
      this.root.addEventListener("dragover", this.onDragOver);
      this.root.addEventListener("dragleave", this.onDragLeave);
      this.root.addEventListener("drop", this.onDrop);
      document.body.addEventListener("htmx:afterSwap", this.onAfterSwap);
      this.root.addEventListener("keydown", this.onKeyDown);
      this.enableDraggableCards();
    }

    enableDraggableCards() {
      this.root.querySelectorAll("[data-board-card]").forEach((card) => {
        this.updateCardDraggability(card);
      });
    }

    isCardLocked(card) {
      return Boolean(card && card.getAttribute("aria-busy") === "true");
    }

    updateCardDraggability(card) {
      const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
      const draggable = finePointer && !this.isCardLocked(card);
      card.draggable = draggable;
      card.classList.toggle("board-card--draggable", draggable);
    }

    lockCard(card) {
      if (!card) {
        return;
      }
      card.setAttribute("aria-busy", "true");
      card.classList.add("board-card--submitting");
      card.querySelectorAll("[data-board-move-form] button:not(:disabled)").forEach((button) => {
        button.disabled = true;
        button.dataset.disabledByBoardLock = "true";
      });
      this.updateCardDraggability(card);
    }

    unlockCard(card) {
      if (!card) {
        return;
      }
      card.removeAttribute("aria-busy");
      card.classList.remove("board-card--submitting");
      card.querySelectorAll("[data-disabled-by-board-lock]").forEach((button) => {
        button.disabled = false;
        delete button.dataset.disabledByBoardLock;
      });
      this.updateCardDraggability(card);
      const summary = card.querySelector(".board-move-menu > summary");
      if (summary) {
        summary.focus();
      }
    }

    onAfterSwap(event) {
      if (event.target && event.target.id === "ops-workspace-results") {
        this.enableDraggableCards();
      }
    }

    onKeyDown(event) {
      if (event.key !== "Escape") {
        return;
      }
      const details = event.target.closest("details[open]");
      if (details) {
        details.open = false;
        const summary = details.querySelector(":scope > summary");
        if (summary) {
          summary.focus();
        }
      }
    }

    onPointerDown(event) {
      this.pointerStartedOnControl = Boolean(event.target.closest("a, button, input, summary, select, textarea"));
    }

    onDragStart(event) {
      const card = event.target.closest("[data-board-card]");
      if (!card || this.isCardLocked(card) || this.pointerStartedOnControl || this.submitting) {
        event.preventDefault();
        return;
      }
      this.draggedCard = card;
      card.classList.add("board-card--dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.dataset.reference || "");
      this.root.querySelectorAll("[data-board-column]").forEach((column) => {
        column.classList.toggle("ops-column--eligible", column.dataset.status !== card.dataset.status);
      });
    }

    onDragEnd() {
      this.clearDragState();
    }

    onDragOver(event) {
      const column = event.target.closest("[data-board-column]");
      if (!column || !this.draggedCard || column.dataset.status === this.draggedCard.dataset.status) {
        return;
      }
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      column.classList.add("ops-column--drop-target");
    }

    onDragLeave(event) {
      const column = event.target.closest("[data-board-column]");
      if (column && !column.contains(event.relatedTarget)) {
        column.classList.remove("ops-column--drop-target");
      }
    }

    onDrop(event) {
      const column = event.target.closest("[data-board-column]");
      const card = this.draggedCard;
      if (!column || !card || this.isCardLocked(card) || column.dataset.status === card.dataset.status) {
        return;
      }
      event.preventDefault();
      const form = Array.from(card.querySelectorAll("[data-board-move-form]")).find(
        (candidate) => candidate.elements.next_status.value === column.dataset.status
      );
      this.clearDragState();
      if (form) {
        this.submitMove(form, card.dataset.reference);
      }
    }

    clearDragState() {
      this.root.querySelectorAll("[data-board-column]").forEach((column) => {
        column.classList.remove("ops-column--eligible", "ops-column--drop-target");
      });
      if (this.draggedCard) {
        this.draggedCard.classList.remove("board-card--dragging");
      }
      this.draggedCard = null;
      this.pointerStartedOnControl = false;
    }

    onSubmit(event) {
      const form = event.target.closest("[data-board-move-form]");
      if (!form) {
        return;
      }
      event.preventDefault();
      const card = form.closest("[data-board-card]");
      this.submitMove(form, card ? card.dataset.reference : null);
    }

    async submitMove(form, reference) {
      const card = form.closest("[data-board-card]");
      if (this.submitting || this.isCardLocked(card)) {
        return;
      }
      const confirmation = form.dataset.confirm;
      if (confirmation && !window.confirm(confirmation)) {
        return;
      }

      let requestBody;
      try {
        requestBody = new FormData(form);
      } catch (_error) {
        this.showFeedback(this.root.dataset.boardErrorLabel);
        return;
      }

      this.submitting = true;
      this.hideFeedback();
      this.lockCard(card);

      try {
        let response;
        try {
          response = await window.fetch(form.action, {
            method: "POST",
            credentials: "same-origin",
            headers: { Accept: JSON_ACCEPT },
            body: requestBody,
          });
        } catch (_error) {
          await this.reconcileBoard(reference);
          return;
        }

        const payload = await response.json().catch(() => null);
        const mutationConfirmed = response.ok && payload && payload.ok === true;
        const mutationRejected = response.status >= 400 && response.status < 500;

        if (!mutationConfirmed) {
          if (mutationRejected) {
            this.showFeedback(payload && payload.error ? payload.error : this.root.dataset.boardErrorLabel);
            this.unlockCard(card);
          } else {
            await this.reconcileBoard(reference);
          }
          return;
        }

        this.announce(`${this.root.dataset.boardSuccessLabel}: ${payload.reference}`);
        try {
          await this.refreshBoard(reference);
        } catch (_error) {
          this.showFeedback(this.root.dataset.boardRefreshErrorLabel, true);
        }
      } finally {
        this.submitting = false;
      }
    }

    async reconcileBoard(reference) {
      try {
        await this.refreshBoard(reference);
        this.hideFeedback();
        this.announce(this.root.dataset.boardReconciledLabel);
      } catch (_error) {
        this.showFeedback(this.root.dataset.boardReconcileErrorLabel, true);
      }
    }

    async refreshBoard(reference) {
      const response = await window.fetch(window.location.pathname + window.location.search, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-cache",
        headers: { "HX-Request": "true" },
      });
      if (!response.ok) {
        throw new Error(this.root.dataset.boardErrorLabel);
      }
      const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
      const replacement = documentFragment.querySelector("#ops-workspace-results");
      const current = this.root.querySelector("#ops-workspace-results");
      if (!replacement || !current) {
        throw new Error(this.root.dataset.boardErrorLabel);
      }
      current.replaceWith(replacement);
      this.enableDraggableCards();
      const escapedReference = window.CSS && window.CSS.escape ? window.CSS.escape(reference || "") : reference;
      const updatedCard = escapedReference
        ? this.root.querySelector(`[data-board-card][data-reference="${escapedReference}"]`)
        : null;
      const focusTarget = (updatedCard && updatedCard.querySelector(".board-move-menu > summary, .board-card__open"))
        || this.root.querySelector(".ops-filter-search input");
      if (focusTarget) {
        focusTarget.focus();
      }
    }

    announce(message) {
      if (this.announcer) {
        this.announcer.textContent = "";
        window.requestAnimationFrame(() => { this.announcer.textContent = message; });
      }
    }

    showFeedback(message, offerRefresh = false) {
      if (this.feedback) {
        if (this.feedbackMessage) {
          this.feedbackMessage.textContent = message;
        }
        if (this.refreshLink) {
          if (offerRefresh) {
            this.refreshLink.href = window.location.pathname + window.location.search;
          }
          this.refreshLink.hidden = !offerRefresh;
        }
        this.feedback.hidden = false;
      }
      this.announce(message);
    }

    hideFeedback() {
      if (this.feedback) {
        this.feedback.hidden = true;
        if (this.feedbackMessage) {
          this.feedbackMessage.textContent = "";
        }
        if (this.refreshLink) {
          this.refreshLink.hidden = true;
        }
      }
    }
  }

  function initialize() {
    const root = document.querySelector("[data-board-controller]");
    if (!root || root.boardController) {
      return;
    }
    root.boardController = new BoardController(root);
    root.boardController.start();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
