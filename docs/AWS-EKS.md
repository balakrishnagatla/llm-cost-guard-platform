# Deploying on AWS EKS

This guide uses `deploy/values-aws-eks.yaml`: the gateway on a private ALB with TLS, keys in AWS Secrets Manager, metrics in Prometheus and traces through an OpenTelemetry Collector.

## Prerequisites in the cluster

| Component | Why |
|---|---|
| [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/) | Creates the ALB from the Ingress |
| [EBS CSI driver](https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html) with a `gp3` StorageClass | Persistent volume for call history |
| [External Secrets Operator](https://external-secrets.io) | Syncs keys from AWS Secrets Manager into a Kubernetes Secret |
| kube-prometheus-stack (optional) | Prometheus and Grafana, via the ServiceMonitor |
| OpenTelemetry Collector (optional) | Receives traces, forwards to Tempo, X-Ray or Jaeger |

An ACM certificate for the gateway's hostname is also needed.

## 1. Store the keys in Secrets Manager

```bash
aws secretsmanager create-secret --name llm-cost-guard/prod --secret-string '{
  "OPENAI_API_KEY": "...",
  "COST_GUARD_KEYS": "search:k_...,support:k_...",
  "DASHBOARD_PASSWORD": "...",
  "METRICS_TOKEN": "..."
}'
```

## 2. Sync them into the cluster

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: llm-cost-guard-secrets
  namespace: llm-cost-guard
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: aws-secrets-manager
  target:
    name: llm-cost-guard-secrets
  dataFrom:
    - extract:
        key: llm-cost-guard/prod
```

## 3. Install

Copy `deploy/values-aws-eks.yaml`, set your certificate ARN, hostname and budgets, then:

```bash
helm upgrade --install lcg oci://ghcr.io/balakrishnagatla/charts/llm-cost-guard --version 0.6.3 \
  --namespace llm-cost-guard --create-namespace -f values-aws-eks.yaml
helm test lcg -n llm-cost-guard
```

## 4. Let apps call it

The values file only accepts traffic from namespaces labelled `llm-cost-guard/client=true` (plus the load balancer and Prometheus):

```bash
kubectl label namespace my-app llm-cost-guard/client=true
```

Apps use `http://lcg-llm-cost-guard.llm-cost-guard.svc:8080/openai/v1` inside the cluster, or the ALB hostname from within the VPC, with their team's `X-Cost-Guard-Key`.

## Production checklist

- [ ] Keys only in Secrets Manager; `COST_GUARD_KEYS` and `DASHBOARD_PASSWORD` set
- [ ] Internal ALB with TLS (`alb.ingress.kubernetes.io/scheme: internal`)
- [ ] NetworkPolicy on, egress restricted
- [ ] Budgets set for every team
- [ ] ServiceMonitor scraped and Grafana dashboard imported; alert on spend spikes
- [ ] Traces flowing to your tracing backend
- [ ] EBS snapshots (AWS Backup) for the data volume
- [ ] Image pinned to a released version, not `latest`
