# ShipMind: 60-Minute Team Build Plan (3 people)

> All commands below run inside the `shipmind/` folder: `cd shipmind` first.

The repo already runs end to end (`pytest -q` → 10 passed, `python run_agent.py` → 3/3 dispatched).
The hour is for **owning, going live on Gemini, extending, and demoing**, not starting from zero.

## 0. Before minute 0 (everyone, 2 min)
- [ ] Python 3.10+ installed, Git installed
- [ ] Google AI Studio key: <https://aistudio.google.com/apikey> (one key **per person**, so rate limits don't collide)
- [ ] Clone the repo, open it in Antigravity / VS Code

## 1. Roles

| Who | Owns | Files | Done when |
|---|---|---|---|
| **A: Lead / Agents** | ADK agent graph, prompts, live Gemini run, model routing | `logistics_agent/*`, `run_agent.py` | All 3 demo scenarios dispatch on **live** Gemini |
| **B: Integrations** | MCP server, carrier APIs, self-healing, chaos | `core/*`, `mcp_server/*` | New carrier/disruption added, chaos demo stable at 30% |
| **C: Demo / QA / Story** | Tests, simulator, docs, `adk web` demo, pitch | `tests/*`, `simulate.py`, `README.md`, `docs/*` | Demo script rehearsed, numbers + screenshots captured |

## 2. Minute-by-minute

| Min | A: Agents | B: Integrations | C: Demo / QA |
|---|---|---|---|
| **00–05** | **All:** `python -m venv .venv`, activate, `pip install -r requirements.txt`, `cp .env.example .env`, `python -m pytest -q`, `python run_agent.py` | ← | ← |
| **05–20** | Add key to `.env`, run `SHIPMIND_MOCK_LLM=false python run_agent.py`. Tune `prompts.py` until all 3 scenarios dispatch. If a model ID 404s, override it in `.env`. | Add one carrier in `core/data.py` (e.g. `OnTrac`) **or** a new disruption (e.g. `ATL2` hurricane). Run `SHIPMIND_CHAOS_RATE=0.3 python run_agent.py`, confirm healing. | `adk web --port 8000 .` → pick `logistics_agent`, walk through one order in the UI. Draft the demo script (section 6). |
| **20–35** | Stretch: cross-vendor validator (`SHIPMIND_VALIDATOR_MODEL=openai/gpt-4o-mini`, `pip install litellm`) **or** add a concierge capability (e.g. "cancel order"). | Stretch: **inventory reservation** at allocation time (removes the concurrency race that shows up as `ESCALATED_NO_PROGRESS` in big simulations). Add a test for it. | `python simulate.py --orders 1000 --workers 64 --chaos 0.35`; record outcomes / heal counts / p50-p95. Update README §7 with your numbers. Screenshot the flowchart + `adk web`. |
| **35** | **CODE FREEZE: merge all PRs to `main`** | ← | ← |
| **35–45** | Pull `main`, run `python -m pytest -q` + all demos on **each** laptop. Fix blockers only. | ← | ← |
| **45–55** | Rehearse the demo twice: once mock, once chaos. Decide who speaks which part. | ← | ← |
| **55–60** | Buffer → submit repo link. | ← | ← |

## 3. Git workflow
```bash
git checkout -b feat/agents          # A   (B: feat/integrations, C: feat/demo)
# ... work ...
python -m pytest -q && git add -A && git commit -m "feat: ..." && git push -u origin HEAD
gh pr create --fill                  # or open the PR on github.com
```
- Small commits, push often. Merge into `main` by **minute 35**.
- Conflict hotspots: `README.md` (only C edits it; others post notes in chat), `core/data.py` (only B).

## 4. Interface contract (don't change without telling the team)

**Session state keys** (written by tools in `logistics_agent/tools.py`):

| Key | Written by | Read by |
|---|---|---|
| `order`, `complexity` | `save_order` | planner, router, reporter |
| `auctions` | `run_carrier_auction` | `submit_plan`, `validate_plan`, `dispatch_shipment` |
| `plan`, `plan_valid` | `submit_plan`, `validate_plan` | `dispatch_shipment` |
| `dispatch` | `dispatch_shipment` | reporter, `run_agent.py` |
| `heal_log`, `heal_summary` | every tool + `heal_tracker` | reporter, `run_agent.py` |
| `order_summary`, `inventory_report`, `risk_report`, `plan_summary`, `feedback`, `final_report` | agent `output_key`s | next agents' instructions |

**Core engine functions** (`core/logistics.py`), shared by MCP, ADK tools and the simulator:
`allocate_inventory`, `carrier_auction`, `build_plan`, `validate_plan`, `dispatch`, `normalize_order`.

## 5. Risk register

| Risk | Signal | Fallback (≤1 min) |
|---|---|---|
| Quota / 429 / invalid key | errors on live run | `SHIPMIND_MOCK_LLM=true`; demo still runs the real ADK runtime |
| Model ID not enabled for your key | 404 `model not found` | Set `SHIPMIND_*_MODEL` in `.env` to a model listed in AI Studio |
| MCP subprocess issues (Windows paths, etc.) | MCP error in trace | `SHIPMIND_USE_MCP=false` (same tools in-process) |
| Live LLM goes off-script | wrong tool args | Tools return `valid_options`; tighten the prompt in `prompts.py` |
| Laptop dies | – | Every member can run the full demo from `main` |

## 6. Demo script (3 minutes), all in the dashboard
Start it before you present: `python demo_server.py` → <http://localhost:8080> (it warms up for ~10 s).
1. **Problem (30 s):** Amazon-scale order handling is a brittle chain of rules engines and human exception queues.
2. **Healing (60 s):** dispatch **1 Kindle → Boston**. The cheapest carrier misses the promise, the loop turns amber, and the planner fixes it on pass 2. Then dispatch **3 laptops → Austin**: the Dallas storm forces a reroute to Chicago.
3. **Multi-model (15 s):** dispatch **8 TVs → Chicago**. The label shows the planner switched to the Pro model for a high-complexity split shipment.
4. **Chaos (30 s):** set API failures to 30% and dispatch again. The label lists retries, carrier switches and model fallbacks, and the order still ships.
5. **Scale (30 s):** Scale test, 1,000 orders at 35% failures: ~96% shipped at ~140 orders/s, and the rest are correctly escalated.
6. **Architecture (15 s):** README flowchart and the path to production.

## 7. Definition of done
- [ ] `python -m pytest -q` green on `main`
- [ ] Live Gemini run: 3/3 dispatched (or mock fallback documented)
- [ ] Chaos run: 3/3 dispatched
- [ ] README numbers updated, flowchart renders on GitHub
- [ ] Repo public, link submitted
