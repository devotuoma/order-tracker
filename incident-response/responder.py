"""Incident responder: receives Grafana alerts and starts a headless coding agent.

POST /alerts  Grafana webhook payload. Each firing alert becomes an incident folder
              in incident-response/incidents/ with the alert, metrics, logs, traces,
              the agent's answer, and an independent verification.
GET  /healthz Liveness check.
"""

import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
INCIDENTS = HERE / "incidents"

PORT = int(os.getenv("RESPONDER_PORT", "8001"))
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
AGENT_TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "1800"))
LOOKBACK_SECONDS = 15 * 60

# What the agent may do without asking. Anything else is denied in headless mode.
ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Edit", "Write",
    "Bash(uv run --frozen pytest:*)",
    "Bash(docker compose up --build -d --wait app)",
    "Bash(docker compose logs:*)",
    "Bash(docker compose ps:*)",
    "Bash(curl:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
]

PROMPT = """You are the on-call engineer for Order Tracker, started automatically by an alert.
Incident folder: {incident_dir}
Read {incident_dir}/evidence.md first; the raw alert, logs, and traces are next to it.

Policy:
- If the alert is a test notification (label test="true") or the evidence shows no real
  failure, do not change any files or run any commands. Just say what you received.
- Otherwise find the root cause in app/. You may edit app/ and tests/ only. Add a
  regression test, run `uv run --frozen pytest -q`, then redeploy with
  `docker compose up --build -d --wait app` and confirm the failing request from the
  evidence now succeeds with curl against {app_url}.
- Never commit, push, or edit observability/ or incident-response/.
- If you cannot fix and verify it safely, stop and escalate to the developers.

Reply with a short incident report: what happened, the root cause, what you changed, and
how you verified it. End with exactly one final line in this format:
STATUS: <no-action|fixed|escalated> - <one sentence>
"""

jobs = queue.Queue()


def log(message):
    print(f"[responder {datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def get_json(url, params=None, timeout=10):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def safe(name, func):
    try:
        return func()
    except Exception as error:  # Evidence is best effort; the agent still runs.
        return {"error": f"{name} unavailable: {error}"}


def gather_evidence(alert):
    labels = alert.get("labels", {})
    route = labels.get("http_route") or alert.get("annotations", {}).get("endpoint", "")
    end = time.time()
    start = end - LOOKBACK_SECONDS

    metrics = safe("prometheus", lambda: get_json(f"{PROMETHEUS_URL}/api/v1/query", {
        "query": 'sum by (http_route, http_response_status_code) '
                 '(increase(order_tracker_requests_total[15m]))',
    }))
    logs = safe("loki", lambda: get_json(f"{LOKI_URL}/loki/api/v1/query_range", {
        "query": '{service_name="order-tracker"} | severity_text=~"ERROR|WARN.*"',
        "start": int(start * 1e9), "end": int(end * 1e9), "limit": 30,
    }))
    search = safe("tempo", lambda: get_json(f"{TEMPO_URL}/api/search", {
        "q": '{resource.service.name="order-tracker" && status=error} | select(span.url.path)',
        "start": int(start), "end": int(end), "limit": 5,
    }))
    traces = {}
    for found in (search.get("traces") or [])[:2]:
        trace_id = found["traceID"]
        traces[trace_id] = safe("tempo", lambda: get_json(f"{TEMPO_URL}/api/traces/{trace_id}"))
    return route, metrics, logs, search, traces


def failing_paths(search):
    paths = set()
    for found in search.get("traces") or []:
        for span_set in found.get("spanSets") or [found.get("spanSet") or {}]:
            for span in span_set.get("spans") or []:
                for attribute in span.get("attributes") or []:
                    if attribute.get("key") == "url.path":
                        paths.add(attribute["value"].get("stringValue"))
    return sorted(p for p in paths if p)


def log_lines(logs):
    lines = []
    for stream in (logs.get("data") or {}).get("result") or []:
        meta = stream.get("stream", {})
        for _ts, line in stream.get("values", []):
            lines.append(f"[{meta.get('severity_text', '?')}] trace_id={meta.get('trace_id', '-')} {line}")
    return lines


def write_evidence(incident_dir, payload, alert):
    route, metrics, logs, search, traces = gather_evidence(alert)
    paths = failing_paths(search)
    (incident_dir / "alert.json").write_text(json.dumps(payload, indent=2))
    (incident_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    (incident_dir / "logs.json").write_text(json.dumps(logs, indent=2))
    (incident_dir / "traces-search.json").write_text(json.dumps(search, indent=2))
    for trace_id, trace in traces.items():
        (incident_dir / f"trace-{trace_id}.json").write_text(json.dumps(trace, indent=2))

    metric_rows = [
        f"- {r['metric'].get('http_route')} {r['metric'].get('http_response_status_code')}: "
        f"{float(r['value'][1]):.0f}"
        for r in (metrics.get("data") or {}).get("result") or []
    ]
    lines = log_lines(logs)
    summary = f"""# Incident evidence

- Alert: {alert.get('labels', {}).get('alertname')} ({alert.get('status')})
- Labels: {json.dumps(alert.get('labels', {}))}
- Annotations: {json.dumps(alert.get('annotations', {}))}
- Affected endpoint: {route or 'unknown'}
- Failing request paths from traces: {', '.join(paths) or 'none found'}
- Error trace IDs: {', '.join(traces) or 'none found'} (full traces in trace-*.json)
- Collected at: {datetime.now(timezone.utc).isoformat()} (lookback 15m)

## Requests in the last 15 minutes (route, status: count)
{chr(10).join(metric_rows) or metrics.get('error', 'no data')}

## Error and warning logs (newest first)
```
{chr(10).join(lines[:30]) or logs.get('error', 'no logs')}
```
"""
    (incident_dir / "evidence.md").write_text(summary)
    return paths


def verify(incident_dir, paths):
    results = {}
    for path in paths:
        try:
            with urllib.request.urlopen(f"{APP_URL}{path}", timeout=10) as response:
                results[path] = response.status
        except urllib.error.HTTPError as error:
            results[path] = error.code
        except Exception as error:
            results[path] = str(error)
    ok = bool(results) and all(isinstance(c, int) and c < 500 for c in results.values())
    (incident_dir / "verification.json").write_text(
        json.dumps({"requests": results, "passed": ok}, indent=2))
    return ok, results


def run_agent(incident_dir, paths, is_test):
    prompt = PROMPT.format(incident_dir=incident_dir.relative_to(REPO), app_url=APP_URL)
    command = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
    ]
    (incident_dir / "agent-command.txt").write_text(shlex.join(command))
    log(f"starting agent for {incident_dir.name}")
    try:
        result = subprocess.run(command, cwd=REPO, capture_output=True, text=True,
                                timeout=AGENT_TIMEOUT)
        output, stderr = result.stdout, result.stderr
    except Exception as error:
        output, stderr = "", str(error)
    (incident_dir / "agent-output.json").write_text(output or "")
    if stderr:
        (incident_dir / "agent-stderr.txt").write_text(stderr)
    try:
        answer = json.loads(output).get("result", "")
    except (json.JSONDecodeError, AttributeError):
        answer = output or f"Agent failed: {stderr}"
    (incident_dir / "response.md").write_text(answer + "\n")

    status = re.findall(r"^STATUS:.*$", answer, re.MULTILINE)
    final = status[-1] if status else "STATUS: escalated - agent gave no status"
    if not is_test and paths:
        ok, results = verify(incident_dir, paths)
        log(f"verification {'passed' if ok else 'FAILED'}: {results}")
        if not ok:
            final = "STATUS: escalated - responder verification failed, page a developer"
    (incident_dir / "status.txt").write_text(final + "\n")
    log(f"agent finished for {incident_dir.name}\n{answer}")


def worker():
    while True:
        incident_dir, paths, is_test = jobs.get()
        try:
            run_agent(incident_dir, paths, is_test)
        except Exception as error:
            log(f"agent run failed: {error}")
        jobs.task_done()


def handle_alerts(payload):
    started = []
    for alert in payload.get("alerts", []):
        if alert.get("status", "firing") != "firing":
            continue
        name = re.sub(r"[^A-Za-z0-9_-]", "-", alert.get("labels", {}).get("alertname", "alert"))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        incident_dir = INCIDENTS / f"{stamp}-{name}"
        incident_dir.mkdir(parents=True)
        paths = write_evidence(incident_dir, payload, alert)
        is_test = alert.get("labels", {}).get("test") == "true"
        jobs.put((incident_dir, paths, is_test))
        started.append(incident_dir.name)
        log(f"incident {incident_dir.name} recorded; failing paths: {paths or 'none'}")
    return started


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"status": "ok", "queued": jobs.qsize()})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/alerts":
            return self.reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.reply(400, {"error": "invalid JSON"})
        self.reply(202, {"incidents": handle_alerts(payload)})

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    log(f"listening on 0.0.0.0:{PORT}, agent: {CLAUDE_BIN}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
