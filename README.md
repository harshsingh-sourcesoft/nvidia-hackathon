# PlanLift

Turn a single floor-plan image into a furnished, explorable 3D scene — then open it straight in **NVIDIA Isaac Sim**, where a **Nemotron-powered AI agent** keeps editing the scene for you in plain English.

**Live demo:** _`https://hackathon.greymatterz.com/`_
**Track:** Best Apps and Agents Track

---

## What it does

1. **Upload a floor plan** (photo, scan, or clean drawing) — a trained Mask R-CNN model detects walls, doors, and windows automatically.
2. **Edit** the detected layout in a browser-based 2D editor (snap walls, resize openings, add stairs, fix anything the model got wrong).
3. **Label rooms** (kitchen, bedroom, reception, …) so the rest of the app — and the AI — knows what's where.
4. **AI Furnishing**: type something like *"a sofa, coffee table, and plant"* for a labeled room, and an NVIDIA Nemotron model turns that into a structured furnishing plan, which gets resolved into real downloadable Sketchfab 3D models and placed geometrically into the room.
5. **View in 3D** right in the browser — walls get real PBR textures (PolyHaven), windows get real glass materials.
6. **Open in Isaac Sim** with one click: the scene is converted to USD and injected into a live Isaac Sim session, complete with correct glass/furniture materials.
7. **Talk to the scene inside Isaac Sim**: a native panel lets you type things like *"add a light in the kitchen"*, *"furnish the reception as a waiting area"*, or *"add the camera in current view"* — another Nemotron model parses the request and the agent executes it live against the open stage (no relaunch needed per prompt).

## How NVIDIA Nemotron + Nebius Token Factory power this

This project calls Nemotron through **Nebius Token Factory** at two separate points in the pipeline:

| Where | Model | What it does | Code |
|---|---|---|---|
| **AI Furnishing** (web app) | `nvidia/nemotron-3-super-120b-a12b` | Turns a free-text furnishing request + room dimensions into a structured JSON plan (`[{item, qty, zone}, ...]`), which a separate geometric solver places collision-free in the room | [`api_server_mrcnn.py`](api_server_mrcnn.py) — `_ai_furnish_via_nebius()`, route `POST /ai-furnish` |
| **Isaac Sim AI Agent** (in-sim panel) | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Parses natural-language scene-editing commands into a structured intent (`add`/`furnish`, entity kind, target room or "all rooms", attributes) that the Kit extension then executes against the live USD stage | [`frontend/src/app/api/agent/route.ts`](frontend/src/app/api/agent/route.ts) → [`kit-app/exts/planlift.ui_setup/planlift_ui_setup/extension.py`](kit-app/exts/planlift.ui_setup/planlift_ui_setup/extension.py) |

Both endpoints are plain OpenAI-compatible calls to `https://api.tokenfactory.nebius.com/v1/chat/completions`. The AI Furnishing endpoint also supports Claude or a local Ollama model as fallback providers (useful for development without burning Nebius credits), but **Nebius Token Factory + Nemotron is the default, primary path** for both features.

## Architecture

```
planlift/
├── api_server_mrcnn.py / api_server.py   ← Python backend: floor-plan detection (Mask R-CNN),
│                                            Sketchfab/PolyHaven proxying, AI Furnishing (Nemotron),
│                                            Isaac Sim launch + USD injection
├── frontend/                              ← Next.js web app: the 2D/3D editor UI (editor.html),
│   └── src/app/api/agent/route.ts            and the Isaac Sim AI Agent's Nemotron call
└── kit-app/                               ← NVIDIA Isaac Sim Kit app + extensions
    └── exts/planlift.ui_setup/                the in-sim AI Agent panel, scene injection,
                                                 glass/material handling, camera-view placement
```

## Running it locally

**1. Backend**
```bat
cd planlift
pip install -r requirements.txt
```
Create a `.env` file in this folder with:
```
SKETCHFAB_TOKEN=...          # sketchfab.com account token, for furniture downloads
NEBIUS_API_KEY=...           # from https://tokenfactory.nebius.com/
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1/
ISAAC_SIM_PATH=...           # path to your Isaac Sim install, for the "Open in Isaac Sim" feature
KIT_APP_PATH=...             # path to this repo's kit-app/ folder
ANTHROPIC_API_KEY=...        # optional — enables Claude as an AI Furnishing fallback provider
```
Then:
```bat
run_mrcnn_server.bat
```
Serves on `http://localhost:8001`.

> **Model weights are not included in this repo** (too large for git). Place your trained Mask R-CNN weights under `weights/` — see the detection stage for the expected filenames. The live demo link above is the fastest way to see full detection working end-to-end without training/sourcing weights yourself.

**2. Frontend**
```bat
cd frontend
npm install
npm run dev
```
Opens on `http://localhost:3000`. Reads `NEBIUS_API_KEY` from the same `PlanLift/.env` file above (not a separate frontend `.env`).

**3. Isaac Sim**
No separate setup — the backend launches Isaac Sim with `kit-app/` as the extension folder and `planlift.ui_setup` enabled automatically when you click "Open in Isaac Sim" in the editor.

## License

MIT — see [`LICENSE`](LICENSE).

## Feedback on Nebius Token Factory / NVIDIA tools

Integrating Nebius Token Factory was genuinely fast — the endpoint is OpenAI-chat-completions-compatible, so swapping Nemotron into both the AI Furnishing feature and the Isaac Sim AI Agent was a matter of pointing an existing `/chat/completions` call at a new base URL and model name, not rewriting any request/response handling. That made it easy to run Nemotron side-by-side with our existing providers and compare them directly.

What stood out most was inference speed. Both features depend on the model responding quickly enough to feel interactive — typing a furnishing request or a scene-editing command and actually seeing it happen in a few seconds, not tens of seconds. The Nemotron models on Token Factory were noticeably faster to respond than other models we'd used for the same job before, which made it realistic to call the model directly in the request path instead of needing to design around a slow, queued response.
