"""Tests for read-only Kubernetes tools — cluster-wide sentinel dispatch."""
from __future__ import annotations
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from tools import kubernetes_read


def _hpa(name: str, namespace: str):
    h = MagicMock()
    h.metadata.name = name
    h.metadata.namespace = namespace
    h.metadata.creation_timestamp = datetime.now(timezone.utc)
    h.spec.scale_target_ref.name = "app"
    h.spec.min_replicas = 1
    h.spec.max_replicas = 5
    h.status.current_replicas = 2
    h.status.current_metrics = None
    return h


def _pvc(name: str, namespace: str):
    p = MagicMock()
    p.metadata.name = name
    p.metadata.namespace = namespace
    p.metadata.creation_timestamp = datetime.now(timezone.utc)
    p.status.phase = "Bound"
    p.status.capacity = {"storage": "1Gi"}
    p.spec.access_modes = ["ReadWriteOnce"]
    p.spec.storage_class_name = "standard"
    return p


# ---------- HPA ----------

def test_hpa_all_namespaces_sentinel_dispatches_cluster_wide():
    api = MagicMock()
    resp = MagicMock()
    resp.items = [_hpa("h1", "prod"), _hpa("h2", "staging")]
    api.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = resp
    with patch.object(kubernetes_read, "autoscaling_v2", return_value=api):
        out = kubernetes_read.kubectl_get_hpa.invoke({"namespace": "--all-namespaces"})
    api.list_horizontal_pod_autoscaler_for_all_namespaces.assert_called_once_with()
    api.list_namespaced_horizontal_pod_autoscaler.assert_not_called()
    assert "prod" in out and "staging" in out
    assert "h1" in out and "h2" in out


def test_hpa_empty_string_sentinel_dispatches_cluster_wide():
    api = MagicMock()
    resp = MagicMock()
    resp.items = [_hpa("h1", "a"), _hpa("h2", "b")]
    api.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = resp
    with patch.object(kubernetes_read, "autoscaling_v2", return_value=api):
        out = kubernetes_read.kubectl_get_hpa.invoke({"namespace": ""})
    api.list_horizontal_pod_autoscaler_for_all_namespaces.assert_called_once_with()
    api.list_namespaced_horizontal_pod_autoscaler.assert_not_called()
    assert "a" in out and "b" in out


def test_hpa_single_namespace_still_calls_namespaced_api():
    api = MagicMock()
    resp = MagicMock()
    resp.items = [_hpa("h1", "prod")]
    api.list_namespaced_horizontal_pod_autoscaler.return_value = resp
    with patch.object(kubernetes_read, "autoscaling_v2", return_value=api):
        out = kubernetes_read.kubectl_get_hpa.invoke({"namespace": "prod"})
    api.list_namespaced_horizontal_pod_autoscaler.assert_called_once_with("prod")
    api.list_horizontal_pod_autoscaler_for_all_namespaces.assert_not_called()
    assert "h1" in out


def test_hpa_all_namespaces_empty_result_string():
    api = MagicMock()
    resp = MagicMock()
    resp.items = []
    api.list_horizontal_pod_autoscaler_for_all_namespaces.return_value = resp
    with patch.object(kubernetes_read, "autoscaling_v2", return_value=api):
        out = kubernetes_read.kubectl_get_hpa.invoke({"namespace": "--all-namespaces"})
    assert "all namespaces" in out


# ---------- PVC ----------

def test_pvc_all_namespaces_sentinel_dispatches_cluster_wide():
    api = MagicMock()
    resp = MagicMock()
    resp.items = [_pvc("v1", "prod"), _pvc("v2", "staging")]
    api.list_persistent_volume_claim_for_all_namespaces.return_value = resp
    with patch.object(kubernetes_read, "core_v1", return_value=api):
        out = kubernetes_read.kubectl_get_pvc.invoke({"namespace": "--all-namespaces"})
    api.list_persistent_volume_claim_for_all_namespaces.assert_called_once_with()
    api.list_namespaced_persistent_volume_claim.assert_not_called()
    assert "prod" in out and "staging" in out
    assert "v1" in out and "v2" in out


def test_pvc_empty_string_sentinel_dispatches_cluster_wide():
    api = MagicMock()
    resp = MagicMock()
    resp.items = [_pvc("v1", "a"), _pvc("v2", "b")]
    api.list_persistent_volume_claim_for_all_namespaces.return_value = resp
    with patch.object(kubernetes_read, "core_v1", return_value=api):
        out = kubernetes_read.kubectl_get_pvc.invoke({"namespace": ""})
    api.list_persistent_volume_claim_for_all_namespaces.assert_called_once_with()
    api.list_namespaced_persistent_volume_claim.assert_not_called()
    assert "a" in out and "b" in out


def test_pvc_single_namespace_still_calls_namespaced_api():
    api = MagicMock()
    resp = MagicMock()
    resp.items = [_pvc("v1", "prod")]
    api.list_namespaced_persistent_volume_claim.return_value = resp
    with patch.object(kubernetes_read, "core_v1", return_value=api):
        out = kubernetes_read.kubectl_get_pvc.invoke({"namespace": "prod"})
    api.list_namespaced_persistent_volume_claim.assert_called_once_with("prod")
    api.list_persistent_volume_claim_for_all_namespaces.assert_not_called()
    assert "v1" in out


def test_pvc_all_namespaces_empty_result_string():
    api = MagicMock()
    resp = MagicMock()
    resp.items = []
    api.list_persistent_volume_claim_for_all_namespaces.return_value = resp
    with patch.object(kubernetes_read, "core_v1", return_value=api):
        out = kubernetes_read.kubectl_get_pvc.invoke({"namespace": "--all-namespaces"})
    assert "all namespaces" in out
