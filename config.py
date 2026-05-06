import os
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-6"
SUBAGENT_MODEL = "claude-haiku-4-5-20251001"  # Used for read-only subagents to reduce cost
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")

# In-cluster detection: Kubernetes injects SERVICE_ACCOUNT_TOKEN at this path
IN_CLUSTER = os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")

# For local dev: optionally pin to a specific context / namespace list
K8S_CONTEXT = os.getenv("K8S_CONTEXT", "")
DEFAULT_NAMESPACES = [
    ns.strip() for ns in os.getenv("DEFAULT_NAMESPACES", "").split(",") if ns.strip()
]

API_PORT = int(os.getenv("API_PORT", "8080"))
