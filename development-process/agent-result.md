Deliverable 1: values.yaml
Already written to dashboard/values.yaml and template-validated. Key points:
- Prometheus datasource → http://prometheus-server.monitoring.svc.cluster.local:80, isDefault: true
- Loki datasource → http://loki.monitoring.svc.cluster.local:3100
- Both URLs use Kubernetes Service DNS names and are editable in the file
- No persistence, no dashboards, no auth providers, no ingress
- Admin: admin / admin (POC only)
Adjust the two url: values once your Prometheus/Loki Services exist — the <service>.<namespace>.svc.cluster.local:<port> pattern is the only thing to change.
Deliverable 2: Helm commands
# Repo is already added, but update it to get latest chart index
helm repo update grafana

# Create namespace
kubectl create namespace monitoring

# Install/upgrade Grafana (run from the dashboard/ directory containing values.yaml)
helm upgrade --install grafana grafana/grafana \
  --namespace monitoring \
  --values values.yaml

# Verify the deployment
kubectl rollout status deployment/grafana --namespace monitoring
kubectl get all --namespace monitoring -l app.kubernetes.io/name=grafana
Deliverable 3: Access & datasource verification
Grafana Service is ClusterIP, so for the POC use port-forwarding (no ingress needed):
kubectl port-forward --namespace monitoring svc/grafana 3000:80
Then open http://localhost:3000 and log in with admin / admin.
Verify datasources:
1. Go to Connections → Data sources — you should see Prometheus (default) and Loki.
2. Click Prometheus → Save & test — it should report "Data source is working" once Prometheus is reachable at the URL in values.yaml.
3. Click Loki → Save & test — same for Loki.
If a datasource test fails with a connection error, update its url: in values.yaml to match the actual Service DNS name of your deployed Prometheus/Loki, then run helm upgrade --install grafana grafana/grafana --namespace monitoring --values values.yaml again.
Note: datasource tests will fail until Prometheus and Loki Services actually exist in the cluster — that's expected since those are out of scope here. The Grafana provisioning and datasource configuration itself is complete.