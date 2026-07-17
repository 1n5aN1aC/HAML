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