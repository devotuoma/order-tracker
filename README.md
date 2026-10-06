# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Observability (Homework 4)

The app is instrumented with OpenTelemetry ([app/telemetry.py](app/telemetry.py)). Every
request (except `/healthz`) produces:

- a metric `order_tracker.requests` (Prometheus: `order_tracker_requests_total`) with
  `http_route`, `http_request_method`, and `http_response_status_code`, plus a duration histogram;
- a server span (and an `order.lookup` child span for order lookups) with the same attributes;
- a log record correlated with the trace.

Set `OTEL_CONSOLE_EXPORT=true` to also print every signal to `docker compose logs app`.

`docker compose up --build -d --wait` starts the whole pipeline:

| Service | URL | Config |
| --- | --- | --- |
| OpenTelemetry Collector | (internal, OTLP 4317/4318) | [observability/otel-collector.yaml](observability/otel-collector.yaml) |
| Prometheus (metrics) | http://localhost:9090 | [observability/prometheus.yaml](observability/prometheus.yaml) |
| Loki (logs) | http://localhost:3100 | built-in local config, OTLP ingest |
| Tempo (traces) | http://localhost:3200 | [observability/tempo.yaml](observability/tempo.yaml) |
| Grafana | http://localhost:3000 (admin / admin) | [observability/grafana/](observability/grafana/) |

Grafana is provisioned with the three data sources (logs ↔ traces linked), the
**Order Tracker** dashboard (request counts, 5xx errors, error logs), and the
`OrderTracker5xx` alert. The alert evaluates every 10s on
`increase(order_tracker_requests_total{http_response_status_code=~"5.."}[5m])` per
`http_route`, fires when it is above 0, treats "no data" (no 5xx at all) as Normal, and
carries the endpoint, the 5m window, and the dashboard link. Its contact point is a
webhook to the incident responder (`RESPONDER_WEBHOOK_URL`, default
`http://host.docker.internal:8001/alerts`).

## Incident responder

[incident-response/responder.py](incident-response/responder.py) runs on the host (it
needs the repo, Docker, and the Claude Code CLI):

```bash
CLAUDE_BIN=claude python3 incident-response/responder.py   # listens on :8001
```

On `POST /alerts` it creates `incident-response/incidents/<time>-<alertname>/` with the
alert, Prometheus metrics, Loki error logs, Tempo error traces, and an `evidence.md`
summary, then starts Claude Code headless (`claude -p`) with a restricted tool allowlist.
The agent may only edit `app/` and `tests/`, run the tests, and redeploy the app
container; it never commits or pushes. After a real incident the responder re-requests the
failing paths itself and records `verification.json`; if they still fail, the status is
escalated. The agent's report is saved in `response.md` and `status.txt`.
