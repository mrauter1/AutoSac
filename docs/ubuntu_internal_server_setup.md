# Ubuntu Internal Server Setup

This guide installs AutoSac on a fresh Ubuntu 22.04 or 24.04 machine for internal use.

Assumptions:

- the web app and worker run on the same Ubuntu server
- PostgreSQL runs on the same server
- runtime data lives under your user home directory, not `/opt/triage`
- systemd starts AutoSac automatically on boot

This guide does not configure a reverse proxy, HTTPS, or public internet exposure.

## 1. Install Ubuntu packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl ca-certificates postgresql postgresql-contrib
sudo systemctl enable --now postgresql
```

## 2. Install Node.js and Codex CLI

AutoSac requires a working `codex` binary. The Codex CLI package requires Node.js 16 or newer. The example below installs Node.js 22 from NodeSource so the `codex` binary is available system-wide for both manual use and systemd services.

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version
npm --version
sudo npm install -g @openai/codex
codex --help
```

Authenticate Codex as the same non-root user that will run the AutoSac services:

```bash
codex login
```

If you do not want to use `codex login`, leave that step out and set `CODEX_API_KEY` in `.env` later.

## 3. Allow Codex sandboxing on Ubuntu 24.04

Ubuntu 24.04 may block Codex local read-only probing with an AppArmor error similar to:

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

If that happens, add an AppArmor profile for `bwrap`. Most Ubuntu installs use `/usr/bin/bwrap`. If `which bwrap` prints a different path, adjust both the profile filename and the path inside the profile.

```bash
which bwrap
sudo mkdir -p /etc/apparmor.d/local

sudo tee /etc/apparmor.d/usr.bin.bwrap >/dev/null <<'EOF'
abi <abi/4.0>,

include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,

  include if exists <local/usr.bin.bwrap>
}
EOF

sudo touch /etc/apparmor.d/local/usr.bin.bwrap
sudo apparmor_parser -r /etc/apparmor.d/usr.bin.bwrap
sudo systemctl daemon-reload
sudo systemctl reload apparmor
codex sandbox linux -- bash -lc 'pwd'
```

Expected result:

- the sandbox test prints a working directory instead of failing with the `bwrap` / `RTM_NEWADDR` error

## 4. Clone the repository and create the Python environment

```bash
cd ~
git clone <YOUR_AUTOSAC_REPO_URL> AutoSac
cd AutoSac
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 5. Create the runtime directories

Create the directories that AutoSac expects for uploads and the read-only worker workspace:

```bash
mkdir -p \
  "$HOME/autosac-data/uploads" \
  "$HOME/autosac-data/triage_workspace/app" \
  "$HOME/autosac-data/triage_workspace/manuals"
```

Notes:

- `app/` and `manuals/` only need to exist for bootstrap and readiness checks to pass.
- If you want the worker to inspect internal application files or manuals, populate those directories before running production traffic.

## 6. Create `.env`

Start from the example file:

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
command -v codex
```

Edit `.env` and set absolute paths for your server. Replace `YOUR_USER`, `YOUR_SERVER_IP`, and the sample password values.

```dotenv
APP_BASE_URL=http://YOUR_SERVER_IP:8000
APP_SECRET_KEY=PASTE_A_LONG_RANDOM_SECRET_HERE
DATABASE_URL=postgresql+psycopg://triage:CHOOSE_A_DB_PASSWORD@localhost:5432/triage

CODEX_BIN=codex
CODEX_API_KEY=

UPLOADS_DIR=/home/YOUR_USER/autosac-data/uploads
TRIAGE_WORKSPACE_DIR=/home/YOUR_USER/autosac-data/triage_workspace
REPO_MOUNT_DIR=/home/YOUR_USER/autosac-data/triage_workspace/app
MANUALS_MOUNT_DIR=/home/YOUR_USER/autosac-data/triage_workspace/manuals
```

Important:

- Use absolute paths in `.env`. Do not use `$HOME` in these values.
- If `command -v codex` returns a path outside the normal system PATH, set `CODEX_BIN` to that full path instead of `codex`.
- Leave `CODEX_API_KEY` empty if you already ran `codex login` as the service user.

## 7. Prepare PostgreSQL, the workspace, and the schema

Activate the virtual environment if you are not already in it:

```bash
cd ~/AutoSac
source .venv/bin/activate
```

Run the local preflight/bootstrap sequence:

```bash
python scripts/preflight_setup.py --ensure-workspace-dirs --setup-postgres-local
alembic upgrade head
python scripts/backfill_ai_run_steps.py
python scripts/bootstrap_workspace.py
```

What this does:

- ensures the upload and workspace directories exist
- provisions the local PostgreSQL role and database from `DATABASE_URL`
- applies the Alembic schema
- backfills historical AI run rows into the current step-based structure
- writes the worker workspace contract and initializes the workspace git repository

## 8. Create the first admin user

```bash
python scripts/create_admin.py \
  --email admin@example.com \
  --display-name "Admin" \
  --password "change-me-now"
```

You can add more local users later with `python scripts/create_user.py`.

## 9. Run readiness checks

Before enabling systemd services, make sure both checks pass:

```bash
python scripts/run_web.py --check
python scripts/run_worker.py --check
```

Both commands should exit successfully.

## 10. Optional manual first start

If you want to verify the processes manually before installing the services, start them in two terminals:

Terminal 1:

```bash
cd ~/AutoSac
source .venv/bin/activate
python scripts/run_worker.py
```

Terminal 2:

```bash
cd ~/AutoSac
source .venv/bin/activate
python scripts/run_web.py
```

Then open `http://YOUR_SERVER_IP:8000/login`.

Stop both processes after you confirm the app is reachable.

## 11. Install the systemd services

Use the repo-provided installer:

```bash
cd ~/AutoSac
sudo bash scripts/setup_systemd_services.sh
```

The installer writes:

- `autosac-web.service`
- `autosac-worker.service`

By default it uses:

- service user: the non-root user who invoked `sudo`
- repo dir: the current repository root
- venv dir: `REPO/.venv`
- env file: `REPO/.env`

If you need to override those defaults:

```bash
sudo bash scripts/setup_systemd_services.sh \
  --service-user YOUR_USER \
  --repo-dir /home/YOUR_USER/AutoSac \
  --venv-dir /home/YOUR_USER/AutoSac/.venv \
  --env-file /home/YOUR_USER/AutoSac/.env
```

## 12. Verify the services

```bash
systemctl status autosac-web autosac-worker --no-pager
journalctl -u autosac-web -u autosac-worker -n 100 --no-pager
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

Expected results:

- both systemd units are `active (running)`
- `/healthz` returns `{"status":"ok"}`
- `/readyz` returns `{"status":"ready"}`

## 13. Reboot test

Confirm the services return after a reboot:

```bash
sudo reboot
```

After the server is back:

```bash
systemctl status autosac-web autosac-worker --no-pager
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```
