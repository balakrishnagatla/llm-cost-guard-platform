# Roadmap

## Done (v0.6)

- ✅ Helm chart with security defaults, probes, persistence, Ingress, ServiceMonitor, NetworkPolicy, PDB, `helm test` and a values schema
- ✅ Readiness and liveness endpoints, JSON logs, graceful shutdown
- ✅ End-to-end tests on a real Kubernetes (kind) cluster in CI
- ✅ Release pipeline: image and chart to GitHub Container Registry, with SBOM, provenance and a vulnerability scan
- ✅ AWS EKS reference values and guide

## Next: high availability

- **Postgres** for call history and traces (Amazon RDS / Aurora), selected with `DATABASE_URL`; SQLite stays the default for single-node setups
- **Redis** for the response cache (ElastiCache)
- Then: multiple replicas, `HorizontalPodAutoscaler`, rolling updates, `topologySpreadConstraints` across zones

## Then: infrastructure as code and GitOps

- **Terraform** module for AWS: VPC, EKS, RDS, ElastiCache, Secrets Manager, IAM roles for service accounts, ACM
- **Argo CD** application manifests for dev, staging and prod, promoted by pull request
- Signed images (cosign) and admission policy (Kyverno) to only run signed images

## Later

- Other clouds: GKE (Workload Identity, Cloud SQL, Memorystore) and AKS
- Grafana dashboard and Prometheus alerts packaged with the chart
