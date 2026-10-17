# ShipMind Architecture

## 1. Layers

| Layer | Components | Responsibility |
|---|---|---|
| **Conversation** | `shipmind_concierge` | Chat, answers tracking/inventory questions via MCP, hands new orders to the pipeline |
| **Orchestration** | `fulfillment_pipeline` (Sequential) → `parallel_recon` (Parallel) → `plan_validate_heal_loop` (Loop) → reporter | Deterministic control flow; LLMs decide *within* steps, not *which* steps run |
| **Reasoning** | 7 LlmAgents across 3 model tiers + validator | Extraction, planning, critique, reporting |
| **Tools** | ADK function tools + MCP toolset | Bridge between agents and systems; keep state |
| **Engine** | `core/logistics.py` | Allocation, carrier auction, scoring, validation, dispatch. Framework-free and unit-tested |
| **Integration** | MCP server (stdio), carrier REST APIs | Warehouse/inventory/tracking systems and carriers |
| **Resilience** | `core/healing.py`, `FallbackModel`, LoopAgent | Retry, circuit breaker, fail-over, re-planning, model fallback |

Design choice: **workflow agents for control, LLM agents for judgment.** Sequential, parallel and loop structure is fixed in code, so latency is predictable and the system can't wander. LLMs only fill in the parts that need language or reasoning.

## 2. Agent graph

| Agent | Type | Model tier | Tools | Writes state |
|---|---|---|---|---|
| shipmind_concierge | LlmAgent | balanced | MCP: track_shipment, check_inventory, get_disruptions, list_warehouses | – |
| intake_agent | LlmAgent | fast | save_order | order, complexity, order_summary |
| inventory_agent | LlmAgent (parallel) | fast | MCP: allocate_inventory | inventory_report |
| risk_agent | LlmAgent (parallel) | fast | MCP: get_disruptions | risk_report |
| planner_agent | LlmAgent (loop) | **routed** by complexity | allocate_inventory, run_carrier_auction, submit_plan | auctions, plan, plan_summary |
| validator_agent | LlmAgent (loop) | validator | validate_plan, dispatch_shipment, exit_loop | plan_valid, dispatch, feedback |
| reporter_agent | LlmAgent | fast | notify_customer | final_report |

## 3. Control loop

```
iteration k:
  planner:   (reallocate if told) → carrier auction per origin (parallel APIs) → submit_plan
  validator: validate_plan
             ├─ valid            → dispatch (retry → breaker → fail-over) → exit_loop ✓
             ├─ healable issues  → "FEEDBACK: [leg X] code: msg -> suggestion" → iteration k+1
             └─ not healable     → exit_loop("ESCALATED: ...")  (first pass, no wasted iterations)
max_iterations = SHIPMIND_MAX_HEAL_ITERATIONS (default 3)
```

Issue codes: `sla_breach` (swap carrier), `sla_breach_origin` (reallocate away from a disrupted site), `carrier_unavailable`, `stock`, `quantity_mismatch` (all healable); `insufficient_stock` and `sla_infeasible` (escalate).

## 4. Complexity score → model route

| Signal | Points |
|---|---|
| express SLA | +1 |
| split shipment | +2 |
| insufficient network stock | +2 |
| disrupted origin | +1 |
| heavy freight (>30 kg) | +1 |

`≥3 = high` → deep model chain; otherwise balanced chain. It's computed deterministically in `save_order`, so routing costs zero LLM calls. A `before_model_callback` sets a context variable that `ComplexityRoutedModel` reads to pick its delegate.

## 5. Carrier scoring
For each available quote, with weights per priority (express favours ETA, economy favours price):

`score = w_price·norm(price) + w_eta·norm(eta) + w_risk·(1−reliability)/0.1 + (SLA miss ? 1 : 0)`

`best` is the lowest-scoring SLA-compliant quote; `cheapest` is kept for comparison. Loads over a carrier's parcel limit are quoted as multi-piece (up to 4 parcels).

## 6. Latency budget (live Gemini, typical order)

| Step | LLM calls | Notes |
|---|---|---|
| concierge → transfer | 1 | |
| intake | 2 | tool call + summary |
| parallel recon | 2 × 2 | inventory ∥ risk, wall time of one |
| planner | 2–3 | auctions run as parallel function calls in one turn |
| validator | 3–4 | validate → dispatch → exit → text |
| reporter | 2 | |

Roughly 8–12 sequential model calls on flash-tier models. Carrier I/O adds ~40–120 ms per auction because all carriers are called concurrently. The MCP subprocess starts once (~1–2 s) and is then reused.

**If you need more speed:** set `SHIPMIND_USE_MCP=false` to skip IPC; drop the risk agent for non-express orders; or run intake on the fast model with `include_contents="none"`.

## 7. Scaling to Amazon volume
- Stateless agent workers behind a Pub/Sub (or Kafka) order queue; one ADK session per order.
- `DatabaseSessionService` (or Vertex AI sessions) instead of in-memory.
- MCP server over streamable HTTP as a shared service; carrier clients with connection pooling.
- Reserve inventory atomically at allocation (fixes the race visible as `ESCALATED_NO_PROGRESS` in `simulate.py`).
- Route ~90% of orders (low complexity) through a rules fast-path and send only exceptions to the full agent loop. The simulator shows the engine alone handles 100+ orders/s per process.
