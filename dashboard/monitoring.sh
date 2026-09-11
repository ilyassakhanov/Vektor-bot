#!/usr/bin/env bash
# Bring the monitoring stack (Grafana + Prometheus + Loki) up or down.
# Usage: ./monitoring.sh {up|down|status}
# Teardown removes the monitoring namespace and all data (no persistence).
set -euo pipefail

cd "$(dirname "$0")"
NS="monitoring"
RELEASES=(grafana prometheus loki)

check_cluster() {
  kubectl get namespace "$NS" >/dev/null 2>&1 || true
  kubectl get --raw=/healthz >/dev/null || {
    echo "error: Kubernetes cluster not reachable" >&2
    exit 1
  }
}

repos() {
  helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update grafana prometheus-community >/dev/null
}

up() {
  check_cluster
  repos
  helm upgrade --install grafana grafana/grafana \
    --namespace "$NS" --create-namespace --values values.yaml
  helm upgrade --install prometheus prometheus-community/prometheus \
    --namespace "$NS" --create-namespace --values prometheus-values.yaml
  helm upgrade --install loki grafana/loki \
    --namespace "$NS" --create-namespace --values loki-values.yaml
  echo "Waiting for rollouts..."
  kubectl rollout status deployment/grafana --namespace "$NS" --timeout=300s
  kubectl rollout status deployment/prometheus-server --namespace "$NS" --timeout=300s
  kubectl rollout status statefulset/loki --namespace "$NS" --timeout=300s
  echo
  echo "Monitoring stack is up."
  echo "External: http://prometheus.192.168.5.15.nip.io/  http://loki.192.168.5.15.nip.io/loki/api/v1/labels"
  echo "Grafana UI: kubectl port-forward --namespace $NS svc/grafana 3000:80 -> http://localhost:3000"
  echo "Password:   kubectl get secret --namespace $NS grafana -o jsonpath=\"{.data.admin-password}\" | base64 --decode"
}

down() {
  check_cluster
  for release in "${RELEASES[@]}"; do
    helm uninstall "$release" --namespace "$NS" || true
  done
  kubectl delete namespace "$NS" --ignore-not-found
  echo "Monitoring stack is down."
}

status() {
  check_cluster
  helm list --namespace "$NS"
  kubectl get pods,svc,ingress --namespace "$NS" || true
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) echo "usage: $0 {up|down|status}" >&2; exit 1 ;;
esac
