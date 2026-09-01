(function () {
  "use strict";

  const ACTIVE_POLL_MS = 3000;
  const IDLE_POLL_MS = 15000;
  const MAX_BACKOFF_MS = 60000;
  const NEAR_LATEST_PX = 160;
  const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

  class TicketLiveController {
    constructor(root) {
      this.root = root;
      this.stateUrl = root.dataset.liveStateUrl;
      this.detailUrl = root.dataset.detailUrl;
      this.contentVersion = root.dataset.contentVersion;
      this.active = root.dataset.active === "true";
      this.startedAt = this.parseDate(root.dataset.startedAt);
      this.etag = null;
      this.failureCount = 0;
      this.timer = null;
      this.elapsedTimer = null;
      this.stopped = false;
      this.polling = false;
      this.refreshing = false;
      this.pendingContentVersion = null;
      this.jumpAfterRefresh = false;
      this.lastPollAt = 0;
      this.statusRegion = root.querySelector("#ticket-live-region");
      this.label = root.querySelector("[data-ticket-live-label]");
      this.elapsed = root.querySelector("[data-ticket-live-elapsed]");
      this.error = root.querySelector("[data-ticket-live-error]");
      this.jump = root.querySelector("[data-ticket-live-jump]");
      this.onVisibilityChange = this.onVisibilityChange.bind(this);
      this.onFocus = this.onFocus.bind(this);
      this.onJump = this.onJump.bind(this);
    }

    start() {
      document.addEventListener("visibilitychange", this.onVisibilityChange);
      window.addEventListener("focus", this.onFocus);
      if (this.jump) {
        this.jump.addEventListener("click", this.onJump);
      }
      this.updateElapsedTimer();
      this.schedule(0);
    }

    parseDate(value) {
      if (!value) {
        return null;
      }
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    onVisibilityChange() {
      if (document.hidden) {
        this.clearScheduledPoll();
        return;
      }
      this.schedule(0);
    }

    onFocus() {
      if (!document.hidden && Date.now() - this.lastPollAt > 1000) {
        this.schedule(0);
      }
    }

    async onJump() {
      if (this.pendingContentVersion) {
        this.jumpAfterRefresh = true;
        const requestedVersion = this.pendingContentVersion;
        const refreshWasHealthy = await this.refreshFragments(requestedVersion);
        if (refreshWasHealthy === true && this.contentVersion === requestedVersion) {
          this.markHealthy();
        }
        return;
      }
      const messages = document.querySelectorAll("#ticket-ledger-region [id^='ticket-message-']");
      const latest = messages[messages.length - 1];
      if (latest) {
        latest.scrollIntoView({ block: "start", behavior: "smooth" });
      }
      if (this.jump) {
        this.jump.hidden = true;
      }
    }

    clearScheduledPoll() {
      if (this.timer !== null) {
        window.clearTimeout(this.timer);
        this.timer = null;
      }
    }

    schedule(delay) {
      if (this.stopped || document.hidden) {
        return;
      }
      this.clearScheduledPoll();
      const jitter = delay > 0 ? Math.floor(Math.random() * Math.min(500, delay * 0.1)) : 0;
      this.timer = window.setTimeout(() => this.poll(), delay + jitter);
    }

    nextDelay() {
      if (this.failureCount > 0) {
        return Math.min(ACTIVE_POLL_MS * Math.pow(2, this.failureCount - 1), MAX_BACKOFF_MS);
      }
      return this.active ? ACTIVE_POLL_MS : IDLE_POLL_MS;
    }

    async poll() {
      if (this.stopped || this.polling || document.hidden) {
        return;
      }
      this.polling = true;
      this.lastPollAt = Date.now();
      try {
        const headers = { Accept: "application/json" };
        if (this.etag) {
          headers["If-None-Match"] = this.etag;
        }
        const response = await window.fetch(this.stateUrl, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-cache",
          redirect: "manual",
          headers: headers,
        });
        if (response.type === "opaqueredirect" || REDIRECT_STATUSES.has(response.status)) {
          this.stopLiveUpdates();
          return;
        }
        if (response.status === 304) {
          let refreshWasHealthy = true;
          if (this.pendingContentVersion && this.pendingContentVersion !== this.contentVersion) {
            refreshWasHealthy = await this.refreshFragments(this.pendingContentVersion);
          }
          if (refreshWasHealthy === true) {
            this.markHealthy();
          }
          return;
        }
        if (response.status === 401 || response.status === 403 || response.status === 404) {
          this.stopLiveUpdates();
          return;
        }
        if (!response.ok) {
          throw new Error("Ticket live-state request failed with status " + response.status);
        }
        const payload = await response.json();
        if (
          typeof payload.active !== "boolean" ||
          typeof payload.phase !== "string" ||
          typeof payload.label !== "string" ||
          typeof payload.content_version !== "string"
        ) {
          throw new Error("Ticket live-state response was invalid");
        }
        this.etag = response.headers.get("ETag");
        this.applyState(payload);
        let refreshWasHealthy = true;
        if (payload.content_version !== this.contentVersion) {
          this.pendingContentVersion = payload.content_version;
          refreshWasHealthy = await this.refreshFragments(payload.content_version);
        }
        if (refreshWasHealthy === true) {
          this.markHealthy();
        }
      } catch (_error) {
        this.failureCount += 1;
        if (this.failureCount >= 2) {
          this.showConnectionError();
        }
      } finally {
        this.polling = false;
        if (!this.stopped) {
          this.schedule(this.nextDelay());
        }
      }
    }

    applyState(payload) {
      this.active = payload.active;
      this.startedAt = this.parseDate(payload.started_at);
      this.root.dataset.active = payload.active ? "true" : "false";
      if (this.label) {
        this.label.textContent = payload.label;
      }
      if (this.statusRegion) {
        this.statusRegion.hidden = !payload.active;
        this.statusRegion.classList.toggle("ticket-live__status--delayed", Boolean(payload.delayed));
      }
      this.updateElapsedTimer();
    }

    markHealthy() {
      this.failureCount = 0;
      if (this.error) {
        this.error.hidden = true;
      }
    }

    showConnectionError(stopped) {
      if (this.error) {
        if (stopped && this.root.dataset.stoppedLabel) {
          this.error.textContent = this.root.dataset.stoppedLabel;
        }
        this.error.hidden = false;
      }
    }

    stopLiveUpdates() {
      this.stopped = true;
      this.active = false;
      this.startedAt = null;
      this.pendingContentVersion = null;
      this.jumpAfterRefresh = false;
      this.root.dataset.active = "false";
      this.clearScheduledPoll();
      if (this.statusRegion) {
        this.statusRegion.hidden = true;
      }
      if (this.jump) {
        this.jump.hidden = true;
      }
      this.updateElapsedTimer();
      this.showConnectionError(true);
    }

    updateElapsedTimer() {
      if (this.elapsedTimer !== null) {
        window.clearInterval(this.elapsedTimer);
        this.elapsedTimer = null;
      }
      this.renderElapsed();
      if (this.active && this.startedAt) {
        this.elapsedTimer = window.setInterval(() => this.renderElapsed(), 1000);
      }
    }

    renderElapsed() {
      if (!this.elapsed) {
        return;
      }
      if (!this.active || !this.startedAt) {
        this.elapsed.textContent = "";
        return;
      }
      const totalSeconds = Math.max(0, Math.floor((Date.now() - this.startedAt.getTime()) / 1000));
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds % 60;
      this.elapsed.textContent = minutes > 0 ? `· ${minutes}m ${seconds}s` : `· ${seconds}s`;
    }

    refreshIsSafe() {
      const activeElement = document.activeElement;
      if (!activeElement || activeElement === document.body) {
        return true;
      }
      return !activeElement.closest(
        "#ticket-status-region, #ticket-ledger-region, #ticket-ai-analysis-region, #ticket-pending-draft-region"
      );
    }

    disclosureState() {
      return Array.from(document.querySelectorAll("[data-live-disclosure][open]"))
        .map((element) => element.dataset.liveDisclosure)
        .filter(Boolean);
    }

    restoreDisclosures(openDisclosures) {
      document.querySelectorAll("[data-live-disclosure]").forEach((element) => {
        element.open = openDisclosures.includes(element.dataset.liveDisclosure);
      });
    }

    latestMessageId() {
      const messages = document.querySelectorAll("#ticket-ledger-region [id^='ticket-message-']");
      return messages.length > 0 ? messages[messages.length - 1].id : null;
    }

    showUpdateAvailable(pendingVersion) {
      if (!this.jump) {
        return;
      }
      if (pendingVersion) {
        this.pendingContentVersion = pendingVersion;
      }
      const updateLabel = this.root.dataset.updateLabel || "New ticket update";
      const jumpLabel = this.root.dataset.jumpLabel || "Jump to latest";
      this.jump.textContent = `${updateLabel} — ${jumpLabel}`;
      this.jump.hidden = false;
    }

    requestFragmentSwap() {
      // HTMX resolves ajax() for completed HTTP errors; afterRequest carries the actual success signal.
      return new Promise((resolve, reject) => {
        let settled = false;
        const cleanup = () => {
          this.root.removeEventListener("htmx:afterRequest", onAfterRequest);
        };
        const settle = (callback, value) => {
          if (settled) {
            return;
          }
          settled = true;
          cleanup();
          callback(value);
        };
        const fail = (error) => {
          settle(reject, error instanceof Error ? error : new Error("Ticket fragment request failed"));
        };
        const onAfterRequest = (event) => {
          if (event.target !== this.root) {
            return;
          }
          if (event.detail && event.detail.successful === true) {
            settle(resolve);
            return;
          }
          const responseStatus = event.detail && event.detail.xhr ? event.detail.xhr.status : "unknown";
          fail(new Error("Ticket fragment request failed with status " + responseStatus));
        };

        this.root.addEventListener("htmx:afterRequest", onAfterRequest);
        let request;
        try {
          request = window.htmx.ajax("GET", this.detailUrl, {
            source: this.root,
            target: "#ticket-ledger-region",
            swap: "outerHTML",
            headers: { "X-AutoSac-Live-Refresh": "true" },
          });
        } catch (error) {
          fail(error);
          return;
        }
        Promise.resolve(request).then(
          () => {
            if (!settled) {
              fail(new Error("Ticket fragment request completed without an HTMX completion event"));
            }
          },
          fail
        );
      });
    }

    async refreshFragments(expectedVersion) {
      if (!expectedVersion || expectedVersion === this.contentVersion) {
        if (this.pendingContentVersion === expectedVersion) {
          this.pendingContentVersion = null;
        }
        return true;
      }
      this.pendingContentVersion = expectedVersion;
      if (this.refreshing) {
        return null;
      }
      if (!this.refreshIsSafe()) {
        this.showUpdateAvailable(expectedVersion);
        return true;
      }
      const ledger = document.querySelector("#ticket-ledger-region");
      if (!ledger || !window.htmx) {
        this.showUpdateAvailable(expectedVersion);
        return true;
      }

      this.refreshing = true;
      const previousLatestId = this.latestMessageId();
      const previousScrollY = window.scrollY;
      const documentHeight = document.documentElement.scrollHeight;
      const nearLatest = window.innerHeight + previousScrollY >= documentHeight - NEAR_LATEST_PX;
      const openDisclosures = this.disclosureState();
      try {
        await this.requestFragmentSwap();
        this.contentVersion = expectedVersion;
        if (this.pendingContentVersion === expectedVersion) {
          this.pendingContentVersion = null;
        }
        this.root.dataset.contentVersion = expectedVersion;
        this.restoreDisclosures(openDisclosures);
        window.requestAnimationFrame(() => {
          const currentLatestId = this.latestMessageId();
          if (nearLatest || this.jumpAfterRefresh) {
            const latest = currentLatestId ? document.getElementById(currentLatestId) : null;
            if (latest) {
              latest.scrollIntoView({ block: "start", behavior: this.jumpAfterRefresh ? "smooth" : "auto" });
            }
            this.jumpAfterRefresh = false;
            if (this.jump) {
              this.jump.hidden = true;
            }
          } else {
            window.scrollTo({ top: previousScrollY, behavior: "auto" });
            if (currentLatestId && currentLatestId !== previousLatestId) {
              this.showUpdateAvailable(null);
            }
          }
        });
        return true;
      } catch (_error) {
        this.failureCount += 1;
        this.showConnectionError();
        return false;
      } finally {
        this.refreshing = false;
      }
    }
  }

  function initialize() {
    const root = document.querySelector("[data-ticket-live-controller]");
    if (!root || root.ticketLiveController) {
      return;
    }
    const controller = new TicketLiveController(root);
    root.ticketLiveController = controller;
    controller.start();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
