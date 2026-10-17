#!/usr/bin/env python3
"""ShipMind demo dashboard.  python demo_server.py  ->  open http://localhost:8080

Streams the real ADK multi-agent run (mock LLM, or live Gemini when GOOGLE_API_KEY is in .env)
to the browser with Server-Sent Events. Standard library only, besides ADK itself.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import queue
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)
logging.getLogger("google_adk").setLevel(logging.ERROR)

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from core import config, data, store  # noqa: E402
from core.carrier_apis import Chaos  # noqa: E402
from core.healing import BREAKER  # noqa: E402
from logistics_agent.agent import root_agent  # noqa: E402

import simulate  # noqa: E402

PORT = int(os.getenv("SHIPMIND_DEMO_PORT", "8080"))
PAGE = Path(__file__).resolve().parent / "demo" / "index.html"
INVENTORY_SNAPSHOT = copy.deepcopy(data.INVENTORY)

# One event loop + one runner for the whole server: the MCP subprocess starts once and stays warm.
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
RUNNER = InMemoryRunner(agent=root_agent, app_name="shipmind")
RUN_LOCK = threading.Lock()  # one demo run at a time keeps the board readable


def _trim(obj, n=260):
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _reset_world(chaos: float) -> None:
    data.INVENTORY.clear()
    data.INVENTORY.update(copy.deepcopy(INVENTORY_SNAPSHOT))
    BREAKER.reset()
    Chaos.configure(chaos, config.CHAOS_SEED)


async def _run(prompt: str, out: queue.Queue) -> None:
    session = await RUNNER.session_service.create_session(app_name="shipmind", user_id="demo")
    t0 = time.perf_counter()
    async for ev in RUNNER.run_async(user_id="demo", session_id=session.id,
                                     new_message=types.Content(role="user", parts=[types.Part(text=prompt)])):
        ms = round((time.perf_counter() - t0) * 1000)
        for p in (ev.content.parts if ev.content and ev.content.parts else []):
            if p.function_call:
                out.put({"type": "call", "agent": ev.author, "ms": ms, "name": p.function_call.name,
                         "detail": _trim(p.function_call.args or {}, 160)})
            elif p.function_response:
                out.put({"type": "response", "agent": ev.author, "ms": ms, "name": p.function_response.name,
                         "detail": _trim(p.function_response.response)})
            elif p.text and not getattr(p, "thought", False):
                out.put({"type": "text", "agent": ev.author, "ms": ms, "detail": _trim(p.text.strip(), 600)})
    state = (await RUNNER.session_service.get_session(
        app_name="shipmind", user_id="demo", session_id=session.id)).state
    heals = list(state.get("heal_log") or [])
    if config.MOCK_LLM:
        from logistics_agent.mock_llm import MODEL_HEAL_EVENTS

        heals += [{"kind": "model_fallback", "detail": m} for m in MODEL_HEAL_EVENTS]
        MODEL_HEAL_EVENTS.clear()
    out.put({"type": "result", "ms": round((time.perf_counter() - t0) * 1000),
             "order": state.get("order"), "dispatch": state.get("dispatch"), "plan": state.get("plan"),
             "route": state.get("planner_model_route"), "exit": state.get("loop_exit_reason"),
             "notification": state.get("customer_notification"), "heals": heals})


def run_stream(prompt: str, chaos: float):
    out: queue.Queue = queue.Queue()
    with RUN_LOCK:
        _reset_world(chaos)
        fut = asyncio.run_coroutine_threadsafe(_run(prompt, out), LOOP)
        while True:
            try:
                yield out.get(timeout=0.1)
            except queue.Empty:
                if fut.done():
                    break
        while not out.empty():
            yield out.get()
        if fut.exception():
            yield {"type": "error", "detail": f"{type(fut.exception()).__name__}: {fut.exception()}"}
        Chaos.configure(config.CHAOS_RATE, config.CHAOS_SEED)
        yield {"type": "done"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/config":
            self._json({"mock": config.MOCK_LLM, "mcp": config.USE_MCP, "models": config.MODELS,
                        "max_iterations": config.MAX_HEAL_ITERATIONS})
        elif url.path == "/api/run":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                for ev in run_stream(q.get("prompt", ""), float(q.get("chaos", 0))):
                    self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif url.path == "/api/simulate":
            with RUN_LOCK:
                _reset_world(0)
                r = simulate.run_simulation(orders=min(int(q.get("orders", 500)), 5000), workers=64,
                                            chaos=float(q.get("chaos", 0.3)))
            self._json(r)
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    store.reset()
    print("Warming up agents and MCP server…", flush=True)
    for _ in run_stream("Ship 1 Echo to Portland, standard, ORD-0000.", 0):
        pass
    store.reset()
    mode = "MOCK data + scripted LLM" if config.MOCK_LLM else "LIVE Gemini"
    print(f"ShipMind demo ({mode}) → http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
