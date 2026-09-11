# Monitoring Stack (Grafana + Prometheus + Loki)

A minimal monitoring stack deployed with Helm into the `monitoring` namespace of
the existing Kubernetes cluster (k3s via Rancher Desktop, Traefik ingress).

| Component  | Chart                                    | Purpose                        |
|------------|------------------------------------------|--------------------------------|
| Grafana    | `grafana/grafana`                        | Dashboards (POC)               |
| Prometheus | `prometheus-community/prometheus`        | Metrics scraping + storage     |
| Loki       | `grafana/loki` (single binary)           | Log aggregation (auth disabled)|

## Prerequisites

- A running Kubernetes cluster (the local k3s from Rancher Desktop works; any
  cluster with Traefik as the default ingress class is fine)
- `kubectl` configured against that cluster (`kubectl get nodes` succeeds)
- Helm v3 (`helm version`)
- Outbound network access to fetch Helm charts and container images
- The Traefik ingress controller must be installed (it is by default in k3s;
  the values files set `ingressClassName: traefik`)
- External hosts use `nip.io` names derived from the Traefik LoadBalancer IP
  (`192.168.5.15` here). If your LB IP differs, or you have real DNS names,
  update the `hosts:` entries in `prometheus-values.yaml` / `loki-values.yaml`
  and the URLs in `values.yaml`.

No other configuration is required — no secrets, no storage classes, no TLS.

## Files

| File                     | Purpose                                            |
|--------------------------|----------------------------------------------------|
| `monitoring.sh`          | Start/stop/status script for the whole stack       |
| `values.yaml`            | Grafana values (datasources, no persistence/auth)  |
| `prometheus-values.yaml` | Prometheus values (default components + ingress)   |
| `loki-values.yaml`       | Loki values (single binary, no auth + ingress)     |
| `startup.txt`            | The underlying Helm commands, for reference        |

## Quick start

```bash
cd dashboard
./monitoring.sh up       # install/upgrade all three releases, wait for rollouts
./monitoring.sh status   # releases, pods, services, ingresses
./monitoring.sh down     # uninstall everything and delete the namespace
```

`up` is idempotent — rerunning it upgrades in place. `down` removes all data
(no persistence is configured, so treat it as disposable).

## Accessing Grafana

```bash
kubectl port-forward --namespace monitoring svc/grafana 3000:80
# then open http://localhost:3000
kubectl get secret --namespace monitoring grafana \
  -o jsonpath="{.data.admin-password}" | base64 --decode   # password, user: admin
```

In the UI: **Connections → Data sources** — Prometheus (default) and Loki
should both be listed and pass "Save & test". They are pre-provisioned by the
chart from `values.yaml` (Prometheus points at
`prometheus-server.monitoring.svc.cluster.local:80`, Loki at
`loki-gateway.monitoring.svc.cluster.local:80`).

## External access (outside apps)

Prometheus and Loki are exposed through Traefik using nip.io hostnames:

- Prometheus: `http://prometheus.192.168.5.15.nip.io/` (e.g. `/-/healthy`)
- Loki: `http://loki.192.168.5.15.nip.io/loki/api/v1/...` (e.g. `/loki/api/v1/labels`)

Notes:

- Loki runs with `auth_enabled: false` — outside apps push and query without
  an `X-Scope-OrgID` header.
- The Loki gateway only proxies `/loki/api/*`-style paths; hitting `/ready` on
  the external host returns 404 — expected, not an outage.
- From the Rancher Desktop host itself, the LoadBalancer IP is not directly
  reachable (Lima networking). Use the forwarded port 80 with a Host header:
  ```bash
  curl -H "Host: prometheus.192.168.5.15.nip.io" http://127.0.0.1/-/healthy
  ```
- Other machines on the LAN can use the nip.io URLs directly if they can route
  to `192.168.5.15`.

## Sending logs to Loki (from inside the cluster)

```bash
curl -H "Content-Type: application/json" -XPOST \
  "http://loki-gateway.monitoring.svc.cluster.local/loki/api/v1/push" \
  --data-raw '{"streams":[{"stream":{"job":"test"},"values":[["'"$(date +%s)"'000000000","hello from loki"]]}]}'
```

## Manual rollout (same steps as `monitoring.sh up`)

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update grafana prometheus-community

helm upgrade --install grafana grafana/grafana \
  --namespace monitoring --create-namespace --values values.yaml
helm upgrade --install prometheus prometheus-community/prometheus \
  --namespace monitoring --create-namespace --values prometheus-values.yaml
helm upgrade --install loki grafana/loki \
  --namespace monitoring --create-namespace --values loki-values.yaml
```

## Troubleshooting

- **Datasource "Save & test" fails** — check the Services exist and the URLs in
  `values.yaml` match actual Service names (`kubectl get svc -n monitoring`),
  then rerun `./monitoring.sh up`.
- **Pod stuck Pending with `Insufficient memory`** — the Loki memcached caches
  are disabled in `loki-values.yaml` for this reason; keep them off on
  memory-constrained single-node clusters.
- **A leftover ClusterRole from a previous install blocks Loki** — the chart
  creates cluster-scoped resources; if an old release left
  `loki-clusterrole(-binding)` behind, delete them and rerun `up`.
- **Everything is non-persistent** — restarts lose Grafana state, Prometheus
  history, and Loki logs. Fine for a POC; add persistence values before
  treating it as long-lived.
