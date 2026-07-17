# Install

Requirements: Python 3, Node.js

## Setup the environment

```
python -m venv .venv
.venv\Scripts\activate        # Windows
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