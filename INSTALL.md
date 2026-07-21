# Install

Requirements: Python 3, Node.js

## Setup the environment

```
python -m venv .venv
.venv\Scripts\activate.ps1    # Windows
source .venv/bin/activate     # Linux/macOS
pip install -r server/requirements.txt

cd client
npm install
cd ..
```

> **PowerShell note:** If you see an error like *"activate.ps1 cannot be loaded because running scripts is disabled on this system"*, PowerShell's execution policy is blocking `Activate.ps1`. To bypass for the current session only, run this **before** `activate`:
>
> ```powershell
> Set-ExecutionPolicy -Scope Process Bypass
> ```
>
> Alternatively, use `cmd.exe` (where the policy doesn't apply), or activate with the `.bat` form: `.venv\Scripts\activate.bat`.

Callsign lookups additionally need the FCC dataset at `server/datasets/fcc_amateur.sqlite` (see [server/datasets/README.md](server/datasets/README.md)); it is not in the repo, and without it lookups fall back to prefix-level answers only.

## Run development server

Run the server:

```
python server/main.py
```

Run the client dev server (proxies API/WebSocket to the server on port 80):

```
cd client
npm run dev
```

Open http://localhost:5173

## Build and run for production

Build the client, then run the server — it serves the built client from `client/dist`:

```
cd client
npm run build
cd ..

python server/main.py
```

Open http://localhost

## Run the tests

Each smoke test spawns its own server on a scratch port with a scratch data directory, so
nothing needs to be running first.  Run them one at a time, from the repo root:

```
python server/tests/smoke.py          # server core: events, sync, admin
python server/tests/smoke_ws.py       # WebSocket: presence, chat, pokes
python server/tests/smoke_lookup.py   # callsign lookup
```

Each prints its check count and exits non-zero on failure, keeping its scratch directory
for debugging.