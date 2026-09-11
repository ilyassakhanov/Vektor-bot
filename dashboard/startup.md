# Monitoring stack rollout — Grafana + Prometheus + Loki (idempotent).
# Run from dashboard/. Cluster: k3s (Rancher Desktop) with Traefik.
# External hosts use nip.io against the Traefik LoadBalancer IP (192.168.5.15);
# replace with real DNS names in the *-values.yaml files if you have them.

# 1. Add/update Helm repositories
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# 2. Grafana (datasources point at Prometheus server + Loki gateway Services)
helm upgrade --install grafana grafana/grafana \
  --namespace monitoring \
  --create-namespace \
  --values values.yaml

# 3. Prometheus (default chart components; server exposed via Traefik ingress)
helm upgrade --install prometheus prometheus-community/prometheus \
  --namespace monitoring \
  --create-namespace \
  --values prometheus-values.yaml

# 4. Loki (single-binary, auth disabled, gateway exposed via Traefik ingress)
helm upgrade --install loki grafana/loki \
  --namespace monitoring \
  --create-namespace \
  --values loki-values.yaml

# 5. Verify everything
kubectl rollout status deployment/grafana --namespace monitoring
kubectl rollout status deployment/prometheus-server --namespace monitoring
kubectl rollout status statefulset/loki --namespace monitoring
kubectl get svc,ingress --namespace monitoring

# Datasource provisioning check (both must be listed, Prometheus default)
kubectl exec --namespace monitoring deployment/grafana -- cat /etc/grafana/provisioning/datasources/datasources.yaml

# 6. External access (outside apps)
#   Prometheus: http://prometheus.192.168.5.15.nip.io/-/healthy
#   Loki:       http://loki.192.168.5.15.nip.io/loki/api/v1/labels (auth disabled,
#               no X-Scope-OrgID needed; only /loki/api/* and a few paths are
#               proxied by the gateway, so /ready returns 404 — expected)
# From the Rancher Desktop host itself the LB IP is not reachable; use
# 127.0.0.1:80 (forwarded to Traefik) with the Host header:
#   curl -H "Host: prometheus.192.168.5.15.nip.io" http://127.0.0.1/-/healthy

# 7. Grafana UI access (ClusterIP, port-forward for the POC)
kubectl get secret --namespace monitoring grafana -o jsonpath="{.data.admin-password}" | base64 --decode
kubectl port-forward --namespace monitoring svc/grafana 3000:80
# Open http://localhost:3000, log in as admin with the password above.
# Connections -> Data sources: Prometheus (default) and Loki must be listed
# and "Save & test" should pass (both verified working).
