# Kubernetes guide

## What the chart creates

| Resource | When | Purpose |
|---|---|---|
| Deployment | always | The gateway (one replica; see [scaling](#scaling)) |
| Service | always | `ClusterIP` on port 8080 |
| ConfigMap | always | `routing.json` and `pricing.json`, mounted read-only at `/srv/config` |
| PersistentVolumeClaim | `persistence.enabled` (default) | Call history, traces and cache. Kept on `helm uninstall` |
| Secret | `secrets.create` (dev only) | API keys. In production use `secrets.existingSecret` |
| ServiceAccount | `serviceAccount.create` (default) | No token mounted; add an IRSA annotation if needed |
| Ingress | `ingress.enabled` | Expose the gateway (ALB, NGINX, ...) |
| ServiceMonitor | `metrics.serviceMonitor.enabled` | Prometheus Operator scraping of `/metrics` |
| Grafana dashboard | `metrics.grafanaDashboard.enabled` | ConfigMap picked up by the Grafana sidecar (kube-prometheus-stack) |
| NetworkPolicy | `networkPolicy.enabled` | Limit who can call the gateway, and its egress |
| PodDisruptionBudget | `podDisruptionBudget.enabled` | Voluntary disruption protection |
| Mock provider | `mockProvider.enabled` | Simulated LLM API for demos and CI. Never in production |

Changing `routing`, `pricing` or chart-managed secrets rolls the pod automatically (checksum annotations).

## Key settings

| Value | Default | Meaning |
|---|---|---|
| `image.repository` / `image.tag` | GHCR image / chart `appVersion` | Container image |
| `provider.openaiBaseUrl` | Groq | Any OpenAI-compatible API |
| `secrets.existingSecret` | | Secret with `OPENAI_API_KEY`, `COST_GUARD_KEYS`, `DASHBOARD_PASSWORD`, `METRICS_TOKEN`, ... |
| `config.budgets` | | Daily USD budget per team, e.g. `search:5,support:2.50` |
| `config.logFormat` | `json` | `json` for log pipelines, `text` for humans |
| `routing` / `pricing` | bundled files | Override the routing rules or price list as YAML |
| `otel.endpoint` | | OTLP/HTTP collector, e.g. `http://otel-collector.observability:4318` |
| `persistence.size` / `storageClass` | `1Gi` / default | Volume for the SQLite database |
| `ingress.*` | disabled | Hosts, TLS, class and annotations |
| `networkPolicy.ingressFrom` | any pod | `NetworkPolicyPeer` list allowed to reach the gateway |
| `networkPolicy.restrictEgress` | `false` | Allow only DNS and the ports in `egressPorts` |

Every value is checked against `values.schema.json`, so typos and invalid settings fail at install time.

## Probes

| Probe | Path | Meaning |
|---|---|---|
| Startup | `/health/live` | Gives the app up to 90 s to start |
| Liveness | `/health/live` | The process is up; restart if not |
| Readiness | `/health/ready` | Config loads and the database answers; stop sending traffic if not |

## Observability

- **Metrics:** with the Prometheus Operator, set `metrics.serviceMonitor.enabled=true`; otherwise the pod carries `prometheus.io/scrape` annotations.
- **Dashboard:** `metrics.grafanaDashboard.enabled=true` ships the Grafana dashboard as a ConfigMap labelled `grafana_dashboard: "1"`, which the kube-prometheus-stack Grafana sidecar loads. It uses the Prometheus data source with uid `prometheus` (that stack's default). Set `metrics.grafanaDashboard.namespace` if the sidecar only watches its own namespace.
- **Traces:** set `otel.endpoint` to your OpenTelemetry Collector. Spans follow the GenAI semantic conventions.
- **Logs:** one JSON line per call with `request_id`, team, feature, model, upstream, status, tokens, cost and latency. Prompts and answers are never logged.

## Real-traffic test on a laptop

`scripts/k8s-local-test.sh` tests the chart the way a user would run it: published chart from GHCR, real Groq calls, full monitoring.

| Step | What happens |
|---|---|
| Cluster | Creates kind cluster `lcg-local` (reused if it exists) |
| Monitoring | kube-prometheus-stack (Prometheus Operator, Prometheus, Grafana; no Alertmanager or node exporter) and Jaeger, in namespace `monitoring` |
| Secret | Reads `OPENAI_API_KEY` from `ENV_FILE` (default `~/llm-cost-guard/.env`) and passes it to Kubernetes through stdin. It is never printed or put on a command line |
| Install | `oci://ghcr.io/balakrishnagatla/charts/llm-cost-guard` at the chart version in this checkout, with [`deploy/values-local-observability.yaml`](../deploy/values-local-observability.yaml) |
| Checks | `helm test`; real calls through the gateway (cheap vs strong routing, cache hit, low reasoning effort, trace steps, streaming); traffic from three teams; budget 429; pod restart keeps the data; JSON logs; Prometheus target up and `costguard_requests_total`; Grafana dashboard present; traces in Jaeger |
| Report | PASS/FAIL per check, saved to `.k8s-local/report.txt` |

Options: `LOCAL_CHART=1` installs the chart from your checkout instead of GHCR, `MONITORING=0` skips the monitoring stack, `CHART_VERSION=x.y.z` picks a published version. `ui` port-forwards the gateway (8080), Grafana (3000), Prometheus (9090) and Jaeger (16686); `down` deletes the cluster.

A run makes about 15 small calls, well inside Groq's free tier. The first run downloads the kind node and monitoring images, so allow 5 to 10 minutes; later runs take about 2.

## Scaling

The gateway stores history and cache in SQLite on a `ReadWriteOnce` volume, so the chart runs **one replica** and uses the `Recreate` strategy. It refuses `replicaCount > 1`. Shared storage (Postgres and Redis) for multiple replicas and autoscaling is next on the [roadmap](Roadmap.md).

One replica comfortably handles typical gateway traffic, since the work is waiting on the provider. Size `resources` for your peak concurrent calls.

## Upgrades and rollback

```bash
helm upgrade lcg charts/llm-cost-guard -f my-values.yaml
helm rollback lcg   # previous release
```

The database schema upgrades itself on start, and the volume is kept across upgrades and uninstalls.
