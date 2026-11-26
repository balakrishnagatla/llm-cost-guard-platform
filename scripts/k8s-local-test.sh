#!/usr/bin/env bash
# Real-traffic test of the published Helm chart on a local kind cluster.
#
# Installs the chart from ghcr.io (as an outside user would), sends real requests to Groq through
# the gateway, and checks routing, caching, streaming, budgets, persistence, Prometheus scraping,
# the Grafana dashboard and Jaeger traces.
#
#   ./scripts/k8s-local-test.sh          # create the cluster (if needed) and run the test
#   ./scripts/k8s-local-test.sh ui       # port-forward Grafana, Prometheus, Jaeger and the gateway
#   ./scripts/k8s-local-test.sh down     # delete the cluster
#
# Settings (environment variables):
#   ENV_FILE        .env holding OPENAI_API_KEY (your Groq key)  [~/llm-cost-guard/.env]
#   CHART_VERSION   chart version to install from ghcr.io         [version in charts/llm-cost-guard/Chart.yaml]
#   LOCAL_CHART=1   install ./charts/llm-cost-guard instead of the published chart
#   MONITORING=0    skip Prometheus, Grafana and Jaeger
#
# Needs Docker Desktop running. Installs kind, kubectl and helm with Homebrew if they are missing.
# The API key is passed to Kubernetes through stdin and never printed.
set -euo pipefail

CLUSTER=lcg-local
NS=llm-cost-guard
RELEASE=lcg
MON_NS=monitoring
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$HOME/llm-cost-guard/.env}"
CHART_VERSION="${CHART_VERSION:-$(awk '/^version:/ {print $2}' "$ROOT/charts/llm-cost-guard/Chart.yaml")}"
MONITORING="${MONITORING:-1}"
STATE_DIR="$ROOT/.k8s-local"
REPORT="$STATE_DIR/report.txt"
GW="deploy/$RELEASE-llm-cost-guard"

PASS=0
FAIL=0
log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m  %s\n' "$*" | tee -a "$REPORT"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m  %s\n' "$*" | tee -a "$REPORT"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
kc()   { kubectl --context "kind-$CLUSTER" "$@"; }
hm()   { helm --kube-context "kind-$CLUSTER" "$@"; }
gwpy() { kc -n "$NS" exec -i "$GW" -- python - "$@"; }  # run Python inside the gateway pod

need_tools() {
  docker info >/dev/null 2>&1 || die "Docker isn't running. Start Docker Desktop and try again."
  local missing=()
  for t in kind kubectl helm; do command -v "$t" >/dev/null 2>&1 || missing+=("$t"); done
  if [ ${#missing[@]} -gt 0 ]; then
    command -v brew >/dev/null 2>&1 || die "Missing: ${missing[*]}. Install them, then rerun."
    log "Installing ${missing[*]} with Homebrew"
    brew install "${missing[@]}"
  fi
}

create_cluster() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    log "Using existing kind cluster '$CLUSTER'"
  else
    log "Creating kind cluster '$CLUSTER'"
    kind create cluster --name "$CLUSTER" --wait 120s
  fi
}

install_monitoring() {
  [ "$MONITORING" = "1" ] || return 0
  log "Installing Prometheus + Grafana (kube-prometheus-stack) and Jaeger"
  kc create namespace "$MON_NS" --dry-run=client -o yaml | kc apply -f - >/dev/null
  mkdir -p "$STATE_DIR"
  [ -s "$STATE_DIR/grafana-admin-password" ] || openssl rand -hex 12 > "$STATE_DIR/grafana-admin-password"
  hm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  hm repo update prometheus-community >/dev/null
  # Lean install for a laptop: no Alertmanager, node exporter, kube-state-metrics, default alert
  # rules or Kubernetes control-plane scraping. Only the gateway (and the stack itself) is scraped.
  hm upgrade --install kps prometheus-community/kube-prometheus-stack -n "$MON_NS" \
    --set fullnameOverride=kps \
    --set alertmanager.enabled=false \
    --set nodeExporter.enabled=false \
    --set kubeStateMetrics.enabled=false \
    --set defaultRules.create=false \
    --set kubeApiServer.enabled=false \
    --set kubelet.enabled=false \
    --set kubeControllerManager.enabled=false \
    --set kubeScheduler.enabled=false \
    --set kubeProxy.enabled=false \
    --set kubeEtcd.enabled=false \
    --set coreDns.enabled=false \
    --set prometheus.prometheusSpec.retention=6h \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
    --set prometheus.prometheusSpec.scrapeInterval=15s \
    --set grafana.adminPassword="$(cat "$STATE_DIR/grafana-admin-password")" \
    --set grafana.sidecar.dashboards.searchNamespace=ALL \
    --set 'grafana.additionalDataSources[0].name=Jaeger' \
    --set 'grafana.additionalDataSources[0].uid=jaeger' \
    --set 'grafana.additionalDataSources[0].type=jaeger' \
    --set "grafana.additionalDataSources[0].url=http://jaeger.$MON_NS:16686" \
    --wait --timeout 8m >/dev/null || { diagnose "$MON_NS"; die "The monitoring stack didn't become ready."; }

  kc -n "$MON_NS" apply -f - >/dev/null <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: jaeger
  labels: {app: jaeger}
spec:
  replicas: 1
  selector: {matchLabels: {app: jaeger}}
  template:
    metadata: {labels: {app: jaeger}}
    spec:
      containers:
        - name: jaeger
          image: jaegertracing/jaeger:2.11.0
          ports:
            - {name: ui, containerPort: 16686}
            - {name: otlp-http, containerPort: 4318}
          resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {memory: 512Mi}}
---
apiVersion: v1
kind: Service
metadata:
  name: jaeger
spec:
  selector: {app: jaeger}
  ports:
    - {name: ui, port: 16686, targetPort: 16686}
    - {name: otlp-http, port: 4318, targetPort: 4318}
YAML
  kc -n "$MON_NS" rollout status deploy/jaeger --timeout 3m >/dev/null
}

install_gateway() {
  [ -f "$ENV_FILE" ] || die "No .env at $ENV_FILE. Set ENV_FILE to the file that holds OPENAI_API_KEY."
  local key
  key="$(grep -E '^OPENAI_API_KEY=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]")"
  [ -n "$key" ] || die "OPENAI_API_KEY is empty in $ENV_FILE"

  log "Creating namespace and Secret (key read from $ENV_FILE, not printed)"
  kc create namespace "$NS" --dry-run=client -o yaml | kc apply -f - >/dev/null
  # METRICS_TOKEN protects /metrics; the ServiceMonitor sends it to Prometheus as a bearer token.
  mkdir -p "$STATE_DIR"
  [ -s "$STATE_DIR/metrics-token" ] || openssl rand -hex 16 > "$STATE_DIR/metrics-token"
  # No trailing newline: it would end up inside the token (and break the HTTP header).
  printf '%s' "$(tr -d '[:space:]' < "$STATE_DIR/metrics-token")" > "$STATE_DIR/metrics-token"
  printf '%s' "$key" | kc -n "$NS" create secret generic llm-cost-guard-secrets \
    --from-file=OPENAI_API_KEY=/dev/stdin --from-file=METRICS_TOKEN="$STATE_DIR/metrics-token" \
    --dry-run=client -o yaml | kc apply -f - >/dev/null
  unset key

  local chart values=()
  if [ "${LOCAL_CHART:-0}" = "1" ]; then
    chart="$ROOT/charts/llm-cost-guard"
    log "Installing the chart from this checkout"
  else
    chart="oci://ghcr.io/balakrishnagatla/charts/llm-cost-guard"
    log "Installing the published chart $chart --version $CHART_VERSION"
  fi
  if [ "$MONITORING" = "1" ]; then
    values=(-f "$ROOT/deploy/values-local-observability.yaml")
  else
    values=(-f "$ROOT/deploy/values-local.yaml" --set config.budgets="demo:1\,k8s-budget:0.000001")
  fi
  # A failed earlier attempt leaves the Deployment marked "progress deadline exceeded", which
  # Helm then reads as the new upgrade failing. Start clean; the data volume is kept.
  local status
  status="$(hm status "$RELEASE" -n "$NS" -o json 2>/dev/null | grep -o '"status":"[a-z-]*"' | head -1 | cut -d'"' -f4 || true)"
  if [ -n "$status" ] && [ "$status" != "deployed" ]; then
    log "Removing the previous '$status' release (the data volume is kept)"
    hm uninstall "$RELEASE" -n "$NS" --wait >/dev/null
  fi

  # shellcheck disable=SC2046
  if ! hm upgrade --install "$RELEASE" "$chart" -n "$NS" \
      $( [ "${LOCAL_CHART:-0}" = "1" ] || echo --version "$CHART_VERSION" ) \
      "${values[@]}" --wait --timeout 5m; then
    diagnose "$NS"
    die "The gateway didn't become ready. Diagnostics are above."
  fi
  kc -n "$NS" get pods,svc,pvc
}

run_checks() {
  mkdir -p "$STATE_DIR"
  : > "$REPORT"
  log "Checks"

  if hm test "$RELEASE" -n "$NS" >/dev/null 2>&1; then ok "helm test (readiness from inside the cluster)"
  else bad "helm test"; fi

  local ver
  ver="$(kc -n "$NS" get "$GW" -o jsonpath='{.spec.template.spec.containers[0].image}')"
  ok "running image: $ver"

  # Real Groq traffic: routing, cache, low reasoning effort, tracing, streaming.
  local smoke
  if smoke="$(kc -n "$NS" exec "$GW" -- env GATEWAY_URL=http://localhost:8080 python scripts/live_smoke.py 2>&1)"; then
    ok "live smoke against Groq (all checks below passed)"
  else
    bad "live smoke against Groq (see FAIL lines below)"
  fi
  printf '%s\n' "$smoke" | sed 's/^/        /' | tee -a "$REPORT"

  # Some traffic from several teams, so the dashboard has something to show.
  if gwpy <<'PY'
import json, urllib.request
prompts = [("demo", "faq", "What is Kubernetes in one sentence?"),
           ("search", "summarise", "Summarise what a Helm chart is in two sentences."),
           ("support", "reply", "Write a one-line polite reply to 'my order is late'."),
           ("search", "analysis", "Compare caching and routing for LLM cost control, briefly.")]
for team, feature, text in prompts * 2:  # second round hits the cache
    req = urllib.request.Request("http://localhost:8080/openai/v1/chat/completions", method="POST",
        data=json.dumps({"model": "auto", "temperature": 0, "messages": [{"role": "user", "content": text}]}).encode(),
        headers={"Content-Type": "application/json", "X-Team": team, "X-Feature": feature})
    urllib.request.urlopen(req, timeout=120).read()
PY
  then ok "sample traffic from teams demo, search and support"; else bad "sample traffic"; fi

  # Budget: team k8s-budget has a tiny daily budget, so the gateway refuses before calling Groq.
  if gwpy <<'PY'
import json, urllib.request, urllib.error
req = urllib.request.Request("http://localhost:8080/openai/v1/chat/completions", method="POST",
    data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}]}).encode(),
    headers={"Content-Type": "application/json", "X-Team": "k8s-budget"})
try:
    urllib.request.urlopen(req, timeout=30)
    raise SystemExit(1)
except urllib.error.HTTPError as e:
    raise SystemExit(0 if e.code == 429 else 1)
PY
  then ok "budget enforced (429 before reaching the provider)"; else bad "budget not enforced"; fi

  local stats_py='import json,urllib.request; t=json.load(urllib.request.urlopen("http://localhost:8080/stats", timeout=10))["totals"]; print(t["calls"], t["cache_hits"], t["cost_usd"], t["saved_usd"])'
  local before after
  before="$(kc -n "$NS" exec "$GW" -- python -c "$stats_py")"
  ok "stats (calls, cache hits, cost USD, saved USD): $before"

  # Persistence: the SQLite volume must survive the pod being replaced.
  kc -n "$NS" delete pod -l app.kubernetes.io/name=llm-cost-guard --wait >/dev/null
  kc -n "$NS" rollout status "$GW" --timeout 2m >/dev/null
  kc -n "$NS" wait --for=condition=ready pod -l app.kubernetes.io/name=llm-cost-guard --timeout 90s >/dev/null
  after=""
  for _ in $(seq 1 10); do after="$(kc -n "$NS" exec "$GW" -- python -c "$stats_py" 2>/dev/null)" && [ -n "$after" ] && break; sleep 2; done
  if [ "${before%% *}" = "${after%% *}" ]; then ok "data survived a pod restart (${before%% *} calls before and after)"
  else bad "data lost on restart (before: ${before%% *}, after: ${after%% *})"; fi

  if [ "$MONITORING" = "1" ]; then
    if kc -n "$NS" logs "$GW" --tail=5 | grep -q '^{'; then ok "logs are structured JSON"; else bad "logs aren't JSON"; fi
  fi

  [ "$MONITORING" = "1" ] && check_monitoring
  return 0
}

diagnose() {  # $1 = namespace: show why pods aren't ready
  log "Diagnostics for namespace $1"
  kc -n "$1" get pods -o wide || true
  kc -n "$1" get events --sort-by=.lastTimestamp 2>/dev/null | tail -15 || true
  local p
  for p in $(kc -n "$1" get pods -o name 2>/dev/null); do
    kc -n "$1" logs "$p" --all-containers --tail=15 2>/dev/null | sed "s|^|  ${p#pod/}: |" || true
  done
}

prom_query() {  # $1 = PromQL; prints the raw JSON from Prometheus via the API server proxy
  kc get --raw "/api/v1/namespaces/$MON_NS/services/kps-prometheus:9090/proxy/api/v1/query?query=$1"
}

check_monitoring() {
  # Prometheus discovers the ServiceMonitor and scrapes the gateway.
  local up=""
  for _ in $(seq 1 12); do
    up="$(prom_query 'up%7Bnamespace%3D%22llm-cost-guard%22%7D' 2>/dev/null || true)"
    printf '%s' "$up" | grep -q '"value":\[[0-9.]*,"1"\]' && break
    sleep 10
  done
  if printf '%s' "$up" | grep -q '"value":\[[0-9.]*,"1"\]'; then ok "Prometheus scrapes the gateway via the ServiceMonitor"
  else
    bad "Prometheus isn't scraping the gateway (checked for 2 minutes)"
    # Prometheus's own view of the target: health and last scrape error
    kc get --raw "/api/v1/namespaces/$MON_NS/services/kps-prometheus:9090/proxy/api/v1/targets?state=active" 2>/dev/null \
      | grep -o '"scrapeUrl":"[^"]*llm-cost-guard[^"]*"\|"lastError":"[^"]*"\|"health":"[^"]*"' | head -6 | sed 's/^/        /' | tee -a "$REPORT" || true
  fi

  local reqs
  reqs="$(prom_query 'sum(costguard_requests_total)' 2>/dev/null || true)"
  if printf '%s' "$reqs" | grep -q '"value":\[[0-9.]*,"[1-9]'; then
    ok "metric costguard_requests_total = $(printf '%s' "$reqs" | sed -n 's/.*"value":\[[0-9.]*,"\([0-9.]*\)".*/\1/p')"
  else bad "costguard_requests_total not found in Prometheus"; fi

  # Grafana loaded the dashboard from the chart's ConfigMap (asked from inside the cluster).
  local found=0
  for _ in $(seq 1 12); do
    if gwpy "$(cat "$STATE_DIR/grafana-admin-password")" <<'PY'
import base64, json, sys, urllib.request
auth = base64.b64encode(f"admin:{sys.argv[1]}".encode()).decode()
req = urllib.request.Request("http://kps-grafana.monitoring/api/search?query=LLM%20Cost%20Guard",
                             headers={"Authorization": f"Basic {auth}"})
hits = json.load(urllib.request.urlopen(req, timeout=10))
raise SystemExit(0 if any(h.get("uid") == "llm-cost-guard" for h in hits) else 1)
PY
    then found=1; break; fi
    sleep 5
  done
  if [ "$found" = "1" ]; then ok "Grafana dashboard 'LLM Cost Guard' loaded from the chart"
  else bad "Grafana dashboard not found"; fi

  # Jaeger received OpenTelemetry spans from the gateway.
  local svcs=""
  for _ in $(seq 1 12); do
    svcs="$(kc get --raw "/api/v1/namespaces/$MON_NS/services/jaeger:16686/proxy/api/services" 2>/dev/null || true)"
    printf '%s' "$svcs" | grep -q 'llm-cost-guard' && break
    sleep 5
  done
  if printf '%s' "$svcs" | grep -q 'llm-cost-guard'; then ok "Jaeger has traces from service llm-cost-guard"
  else bad "no traces in Jaeger"; fi
}

ui() {
  local pw=""
  [ -s "$STATE_DIR/grafana-admin-password" ] && pw="$(cat "$STATE_DIR/grafana-admin-password")"
  log "Port-forwarding (Ctrl+C to stop)"
  echo "  Gateway dashboard  http://localhost:8080"
  if [ "$MONITORING" = "1" ]; then
    echo "  Grafana            http://localhost:3000   (admin / $pw) -> Dashboards -> LLM Cost Guard"
    echo "  Prometheus         http://localhost:9090"
    echo "  Jaeger             http://localhost:16686  (service: llm-cost-guard)"
  fi
  trap 'kill 0' INT TERM EXIT
  # kubectl port-forward exits when a connection to the pod resets; keep each one running.
  forward() { while true; do kc -n "$1" port-forward "svc/$2" "$3" >/dev/null 2>&1; sleep 1; done; }
  forward "$NS" "$RELEASE-llm-cost-guard" 8080:8080 &
  if [ "$MONITORING" = "1" ]; then
    forward "$MON_NS" kps-grafana 3000:80 &
    forward "$MON_NS" kps-prometheus 9090:9090 &
    forward "$MON_NS" jaeger 16686:16686 &
  fi
  wait
}

summary() {
  log "Result: $PASS passed, $FAIL failed  (report: ${REPORT#"$ROOT"/})"
  echo "  See it live:  ./scripts/k8s-local-test.sh ui"
  echo "  Clean up:     ./scripts/k8s-local-test.sh down"
  [ "$FAIL" -eq 0 ]
}

case "${1:-test}" in
  test)
    need_tools
    create_cluster
    install_monitoring
    install_gateway
    run_checks
    summary
    ;;
  ui)   ui ;;
  down) kind delete cluster --name "$CLUSTER" && rm -rf "$STATE_DIR" ;;
  *)    die "Usage: $0 [test|ui|down]" ;;
esac
