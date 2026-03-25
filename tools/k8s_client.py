"""Shared Kubernetes client initialization — in-cluster or local."""
from kubernetes import client, config as k8s_config
from config import IN_CLUSTER, K8S_CONTEXT

_initialized = False


def init_client():
    global _initialized
    if _initialized:
        return
    if IN_CLUSTER:
        k8s_config.load_incluster_config()
    else:
        k8s_config.load_kube_config(context=K8S_CONTEXT or None)
    _initialized = True


def core_v1() -> client.CoreV1Api:
    init_client()
    return client.CoreV1Api()


def apps_v1() -> client.AppsV1Api:
    init_client()
    return client.AppsV1Api()


def autoscaling_v2() -> client.AutoscalingV2Api:
    init_client()
    return client.AutoscalingV2Api()


def networking_v1() -> client.NetworkingV1Api:
    init_client()
    return client.NetworkingV1Api()


def custom_objects() -> client.CustomObjectsApi:
    init_client()
    return client.CustomObjectsApi()


def apiextensions_v1() -> client.ApiextensionsV1Api:
    init_client()
    return client.ApiextensionsV1Api()


def rbac_v1() -> client.RbacAuthorizationV1Api:
    init_client()
    return client.RbacAuthorizationV1Api()


def batch_v1() -> client.BatchV1Api:
    init_client()
    return client.BatchV1Api()


def policy_v1() -> client.PolicyV1Api:
    init_client()
    return client.PolicyV1Api()
