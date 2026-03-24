# Auto SAC: Automated Support Triage Engine

Auto SAC is a production-ready, open-source helpdesk orchestration engine. It moves beyond "chat-wrapper" architectures by implementing strict state management, cryptographic input validation, and agentic workflows to automate ticket routing and resolution.

---

### Key Technical Differentiators

#### 1. Deterministic State Hashing (Idempotency)
Instead of re-running the agent on every webhook update, Auto SAC computes a SHA-256 fingerprint of the ticket’s public state (messages, attachments, and status).
* **Cost Efficiency:** The agent only executes when the ticket fingerprint changes.
* **Race Condition Protection:** If a ticket status or message changes while an agent is mid-execution, the system detects the fingerprint mismatch upon completion. It automatically invalidates the stale output and queues a fresh run, preventing the agent from replying to outdated context.

#### 2. Pydantic-Backed Output Guardrails
The agent is constrained by a strict Pydantic model (`TriageResult`).
* **Schema Enforcement:** The agent cannot produce arbitrary text; it must yield valid, structured JSON.
* **Business Logic Validation:** We use `model_validator(mode="after")` to enforce cross-field dependencies. For example, if the agent selects `ask_clarification` but fails to provide a question, the application layer catches the logical contradiction before it reaches the end-user, triggering a `TriageValidationError` and routing the ticket to a human queue instead.

#### 3. Separation of Concerns (Dual-Channel Output)
The agent is architected to produce two distinct output channels:
* **Public Channel:** Drafts or automatic responses optimized for the end-user.
* **Internal Channel:** A structured diagnostic summary, relevant file paths from the repository, and internal reasoning.
This ensures the agent never leaks private diagnostics or internal notes to the requester.

#### 4. Human-in-the-Loop Fallback
The system uses tiered confidence thresholds (configurable via `Settings`).
* **High Confidence:** Triggers `auto_public_reply` for immediate resolution.
* **Low Confidence / Ambiguity:** Automatically moves the ticket to a `pending_approval` state for human review.
* **Drafting Engine:** Humans act as the final judge, using an "Approve/Reject" interface for AI-generated drafts.

#### 5. Sandboxed Context Loading
The worker node uses strict boundary enforcement. It maps local mounts for `app/` and `manuals/` into the agent's context.
* **Read-only Constraints:** The agent is given a read-only view of the codebase, preventing unauthorized code modification.
* **Artifact Attribution:** Every AI-generated message is cryptographically linked to the `AIRun` ID that produced it, ensuring full auditability of the agent's decision-making process.

---

### Tech Stack
* **Runtime:** Python 3.11+, FastAPI (Async/Await)
* **Storage:** PostgreSQL (JSONB support for state metadata)
* **ORM:** SQLAlchemy 2.0 (Constraint-based enum validation)
* **Orchestration:** Decoupled background workers with dedicated heartbeat monitoring
* **Frontend:** Server-side rendered Jinja2 + HTMX (zero-JS overhead)

---

### Getting Started

1. **Configure environment variables:**
   Copy `.env.example` to `.env` and fill in required values for your local setup.
   ```bash
   cp .env.example .env
   ```

2. **Bootstrap Workspace:**
   Maps your repo and manuals for the agent.
   ```bash
   python scripts/bootstrap_workspace.py
   ```

3. **Run Services:**
   ```bash
   # Start the web interface
   python scripts/run_web.py

   # Start the worker (processes the pending queue)
   python scripts/run_worker.py
   ```

4. **Validate local wiring before starting services (optional but recommended):**
   ```bash
   python scripts/run_web.py --check
   python scripts/run_worker.py --check
   ```

5. **Verify runtime health:**
   Run `GET /readyz` to confirm database connectivity, workspace availability, and agent contract compliance.

6. **Run tests:**
   ```bash
   pytest
   ```
