.PHONY: setup test eval eval-live ui demo chaos sim web mcp
setup:   ## create venv + install deps
	python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && cp -n .env.example .env || true
ui:      ## demo dashboard on http://localhost:8080
	python demo_server.py
test:    ## 43 tests: engine, guards, MCP server, full ADK pipeline, eval gate (mock LLM, no key)
	python -m pytest -q
eval:    ## groundedness / truthfulness / hallucination evals, mock LLM (CI gate, exit 1 on regression)
	python -m evals.run_evals
eval-live: ## same evals on live Gemini + LLM-as-judge (spends tokens on your key)
	python -m evals.run_evals --live --judge
demo:    ## real ADK runtime, 3 scenarios (mock LLM if no key)
	python run_agent.py
chaos:   ## same, with 30% of carrier + model API calls failing
	SHIPMIND_CHAOS_RATE=0.3 python run_agent.py
sim:     ## 1000 concurrent orders, 35% chaos
	python simulate.py --orders 1000 --workers 64 --chaos 0.35
web:     ## ADK dev UI on http://localhost:8000 (pick logistics_agent)
	adk web --port 8000 .
mcp:     ## inspect the MCP server
	npx @modelcontextprotocol/inspector python mcp_server/server.py
