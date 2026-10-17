# AGENTS.md: instructions for AI coding agents (Antigravity, Claude Code, Copilot, etc.)

## Project
ShipMind is a Google ADK (v2.12) multi-agent logistics system. Read `README.md` §3 and `docs/ARCHITECTURE.md` first.

## Commands
- Install: `pip install -r requirements.txt`
- Test (run after **every** change): `python -m pytest -q` (expects 10+ passed, no API key needed)
- Demo: `python run_agent.py` · chaos: `SHIPMIND_CHAOS_RATE=0.3 python run_agent.py`
- Scale: `python simulate.py --orders 1000 --workers 64 --chaos 0.35`
- UI: `adk web --port 8000 .` then choose `logistics_agent`

## Architecture rules
1. **Business logic lives only in `core/`.** It must not import `google.adk`. The MCP server, the ADK tools and the simulator all call `core.logistics`; never duplicate logic in them.
2. ADK tools in `logistics_agent/tools.py` are thin wrappers: read/write session state, call `core`, append to `heal_log`.
3. When a tool mutates a nested state value (dict/list), **reassign** it (`state["auctions"] = auctions`) so ADK records the change.
4. Tool docstrings and type hints are the LLM's API docs: keep `Args:` sections accurate.
5. Tools return `{"error": ..., "valid_options": ...}` instead of raising, so the LLM can self-correct.
6. `mcp_server/server.py` must never `print()` to stdout (stdout carries the MCP protocol).
7. Model IDs come from env vars via `core/config.py`. Never hard-code model names elsewhere.
8. Every new external call needs: `retry(...)` + `BREAKER` + a `log_event(...)` on failure.
9. If you add an agent or tool, update `logistics_agent/mock_llm.py` so mock mode keeps working, and add a test.

## Don't
- Commit `.env` or any API key.
- Change the state keys listed in `TEAM_PLAN.md` §4 without updating all readers.
- Add blocking sleeps on the hot path (mock latency is controlled by `SHIPMIND_MOCK_LATENCY_MS`).
