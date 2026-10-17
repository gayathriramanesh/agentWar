.PHONY: setup test ui demo chaos sim web mcp
setup:   ## create venv + install deps
	python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && cp -n .env.example .env || true
ui:      ## demo dashboard on http://localhost:8080
	python demo_server.py
test:    ## 10 tests: engine, MCP server, full ADK pipeline (mock LLM)
	python -m pytest -q
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
