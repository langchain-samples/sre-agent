# SRE Bot

An autonomous Kubernetes SRE agent. It monitors cluster health, diagnoses issues, and applies fixes — with human approval required before any write operation.

## Features

- **Autonomous health audits** — pods, scaling, resources, logs, security, reliability, config hygiene, and batch jobs analyzed in parallel by specialized subagents
- **Human-in-the-loop (HITL)** — every write operation (restart, scale, patch, delete) pauses for explicit approval. Approving or rejecting in Slack confirms the decision inline (who decided, and the outcome) and removes the buttons, so an action can't be double-triggered
- **Slack integration** — alerts, health reports, and HITL approve/reject buttons via Socket Mode (no public ingress needed). Mention the bot in a channel and it replies in-thread
- **Fast interactive health checks** — a "run a health check" mention is served by the same bounded path as the scheduler (direct cluster reads + a single structured-output call), so it returns in seconds and can never hit the agent's recursion limit — unlike routing it through the full orchestrator
- **Custom resource support** — the change-executor can create, update, and delete CRD instances, so it can remove an operator's top-level custom resource (e.g. an `lgps.apps.langchain.ai`) instead of fighting the operator's reconciliation loop
- **Model gateway support** — route Claude calls through a LangChain/LangSmith model gateway by setting `ANTHROPIC_BASE_URL`; unset, it calls Anthropic directly
- **Scheduled monitoring** — periodic cluster health checks on a configurable interval. The scheduler collects cluster state directly via the Kubernetes client (no LLM tokens), then makes a single structured-output call to summarize findings
- **Structured findings** — health analysis returns a typed `HealthReport` (see `schemas.py`) rather than free text, so Slack rendering reads typed fields instead of parsing markdown
- **Two interfaces** — CLI for interactive use, FastAPI + web UI for in-cluster deployment
- **LangSmith tracing** — full observability of every agent run, with an eval dataset and online evaluators

## Example Output

Slack health report showing a cluster audit with critical and warning findings:

![Slack health report](docs/slack-health-report.png)

![Slack health report 2](docs/slack_health_2.png)

HITL approval in Slack — after you approve or reject, the decision is confirmed inline (with who decided) and the buttons are removed so the action can't be re-triggered:

![Slack HITL approval confirmation](docs/confirmation.png)

## Architecture

```text
main.py / api.py
    └── SRE orchestrator (agent.py)
            ├── pod-inspector        (read-only) — pod health, crashes, logs
            ├── scaling-analyzer     (read-only) — HPA, replicas, node capacity
            ├── performance-analyzer (read-only) — CPU/memory right-sizing
            ├── log-analyzer         (read-only) — error detection in logs
            ├── security-auditor     (read-only) — RBAC, privileged pods, NetworkPolicies, image tags
            ├── reliability-auditor  (read-only) — PDBs, probes, endpoints, single-replica SPOFs
            ├── job-inspector        (read-only) — Jobs, CronJobs, failures, missed schedules
            ├── config-auditor       (read-only) — resource limits, PV hygiene, selector mismatches
            └── change-executor      (write ops — all require HITL approval)
```

The main agent only has read tools. All writes are delegated to `change-executor`, which is configured to interrupt before every write tool call.

## Quick Start

### Prerequisites

- Python 3.12+
- `kubectl` configured and pointing at your cluster (for local dev)
- Anthropic API key
- LangSmith API key (for tracing)
- Slack app with Bot and App-level tokens (optional, for Slack notifications)

### Local dev

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your keys in .env

python main.py        # CLI mode
python api.py         # API + web UI at http://localhost:8080
```

### Environment variables

| Variable | Required | Description |
| -------- | -------- | ----------- |
| `ANTHROPIC_API_KEY` | Yes | Claude API key. When routing through a gateway (see `ANTHROPIC_BASE_URL`), set this to your gateway key |
| `ANTHROPIC_BASE_URL` | No | Route Claude calls through a model gateway, e.g. `https://gateway.smith.langchain.com/anthropic`; unset = call Anthropic directly |
| `LANGSMITH_API_KEY` | Yes | LangSmith tracing key |
| `LANGSMITH_TRACING` | Yes | Set to `true` to enable tracing |
| `LANGSMITH_PROJECT` | No | Project name (default: `sre-agent`) |
| `LANGSMITH_WORKSPACE_ID` | No | Route traces to a specific LangSmith workspace |
| `SLACK_BOT_TOKEN` | No | `xoxb-...` bot token |
| `SLACK_APP_TOKEN` | No | `xapp-...` Socket Mode token |
| `SLACK_CHANNEL` | No | Channel for alerts (default: `#sre-alerts`) |
| `MONITOR_INTERVAL_MINUTES` | No | Health check frequency (default: `30`) |
| `MONITORING_ENABLED` | No | Set to `false` to stop scheduled checks (default: `true`) |
| `MONITOR_DIGEST_EVERY_N_CHECKS` | No | Post a report every N checks even when nothing changed; `0` disables (default: `12`) |
| `MONITOR_NOTIFY_ON_RESOLVED` | No | Announce findings that cleared (default: `true`) |
| `MONITOR_ACK_HOURS` | No | How long the Slack **Ack** button mutes a finding (default: `24`) |
| `PVC_USAGE_ALERT_PERCENT` | No | PVCs at or above this fill level are listed individually in the health snapshot (default: `70`) |
| `POD_FAILURE_RECENCY_MINUTES` | No | A failed container termination older than this is history, not a live fault (default: `60`) |
| `POD_STARTUP_GRACE_MINUTES` | No | Grace before Pending or not-ready counts as a fault (default: `10`) |
| `POD_RESTART_NOTABLE` | No | Lifetime restart counts at or above this are reported as context, never a fault (default: `10`) |
| `EVENT_MAX_AGE_MINUTES` | No | Warning events older than this are dropped (default: `60`) |
| `DATABASE_URL` | No | Postgres DSN for durable state. Unset = in-memory, and pending approvals do not survive a restart |
| `SLACK_APPROVER_IDS` | No | Comma-separated Slack user IDs allowed to approve changes. **Empty means anyone who can see the message may approve** |
| `DEFAULT_NAMESPACES` | No | Comma-separated namespaces to watch (default: auto-discover) |
| `PROMETHEUS_URL` | No | Prometheus endpoint for richer metrics |
| `API_PORT` | No | Port for API server (default: `8080`) |
| `CORS_ALLOW_ORIGINS` | No | Comma-separated browser origins allowed to call `/api/*`. Empty (default) means none; the bundled UI is same-origin and needs no grant |

## Deploy to Kubernetes

The included `deploy.sh` handles build, ECR push, and EKS apply in one step:

```bash
./deploy.sh           # tags as :latest
./deploy.sh v1.2.0    # optional: tag with a version
```

Or manually:

```bash
# 1. Authenticate with ECR (tokens expire every 12 hours)
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com

# 2. Build and push (--push avoids a separate docker push step)
docker buildx build --platform linux/amd64 \
  -t your-registry/sre-agent:latest --push .
# Update image in k8s/deployment.yaml

# 3. Create the secrets file (never commit this)
#   Values under data: must be base64-encoded; stringData: accepts plain text
echo -n "sk-ant-..." | base64   # ANTHROPIC_API_KEY
echo -n "lsv2_..."  | base64   # LANGSMITH_API_KEY
echo -n "xoxb-..."  | base64   # SLACK_BOT_TOKEN
echo -n "xapp-..."  | base64   # SLACK_APP_TOKEN
# POSTGRES_PASSWORD must be alphanumeric. deployment.yaml interpolates it into
# DATABASE_URL, and a /, + or @ would corrupt the DSN.
LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 | base64
# Paste values into k8s/secret.yaml

# 4. Apply (includes the Postgres StatefulSet backing durable state)
kubectl apply -k k8s/
kubectl rollout status statefulset/sre-agent-postgres -n sre-agent

# 5. Access the UI
kubectl port-forward svc/sre-agent 8080:80 -n sre-agent
# Open http://localhost:8080

# 6. Confirm state is durable (not silently degraded to in-memory)
curl -s localhost:8080/health | jq '{state_backend, durable_state}'
# => {"state_backend": "postgres", "durable_state": true}
```

### RBAC

The included manifests grant:

- **Read** on all resources cluster-wide (`ClusterRole: sre-agent-reader`)
- **Write** (patch/update/delete) on all namespaces cluster-wide (`ClusterRole: sre-agent-writer`)

All write operations are still gated by HITL regardless of RBAC.

## Stopping the bot

| Mode | How to stop |
| ---- | ----------- |
| CLI (`main.py`) | `Ctrl+C` |
| API (`api.py`) | `Ctrl+C` or `kill <pid>` |
| In-cluster | `kubectl scale deployment sre-agent -n sre-agent --replicas=0` |
| Delete everything | `kubectl delete -k k8s/` |

## Project structure

```text
agent.py              Main SRE orchestrator
api.py                FastAPI server (SSE streaming, HITL endpoints, web UI)
main.py               CLI entry point
config.py             Env-based configuration
schemas.py            Pydantic models (Finding, HealthReport) — structured-output contract
scheduler.py          Periodic health check scheduler (structured HealthReport via tool-use),
                      diffed against stored state so only changes are posted
persistence.py        Postgres checkpointer/store, sessions, HITL audit, finding state,
                      with in-memory fallback when DATABASE_URL is unset or unreachable
monitor_state.py      Stable finding fingerprints and run-to-run diffing (pure functions)
slack_notifier.py     Slack Block Kit messages and HITL action handling
                      (send_structured_report renders typed findings directly, and
                      labels them NEW / ESCALATED / ongoing / RESOLVED from a diff)
deploy.sh             Build, push to ECR, and deploy to EKS
tools/
  kubernetes_read.py        Read-only kubectl tools
  kubernetes_write.py       Write tools (all require HITL approval), including
                            kubectl_scale_bulk and kubectl_delete_resources_bulk
                            for batching multiple resources into a single approval,
                            and kubectl_delete_custom_resource for deleting CRD
                            instances (operator-owned resources)
  kubernetes_security.py    RBAC, pod security, NetworkPolicy, image tag tools
  kubernetes_reliability.py PDB, probe, endpoint, single-replica tools
  kubernetes_hygiene.py     Resource limits, PV, selector mismatch tools
  kubernetes_batch.py       Job and CronJob tools
  helm.py                   Helm release inspection and upgrade/rollback tools
  k8s_client.py             In-cluster vs local kubectl detection
  slack.py                  Slack notification tool for the agent
subagents/
  pod_inspector.py
  scaling_analyzer.py
  performance_analyzer.py
  log_analyzer.py
  security_auditor.py
  reliability_auditor.py
  job_inspector.py
  config_auditor.py
  change_executor.py      Only subagent with write tools
k8s/                  Kustomize manifests for cluster deployment
                      (postgres.yaml = StatefulSet + Service + NetworkPolicy)
tests/
  test_monitor_state.py   Fingerprint stability and diff semantics
  test_slack_render.py    Block Kit rendering for every diff shape
  test_persistence.py     Postgres integration (skipped without TEST_DATABASE_URL)
evals/
  snapshot_fixture.py       Capture/replay real cluster states as eval fixtures
  capture_snapshot.py       CLI to record a live cluster state (redacts by default)
  fixtures/                 Recorded cluster states + hand-written expectations
  create_dataset.py         Script to upload eval examples to LangSmith
  sre-agent-k8s-eval.jsonl  Pre-built JSONL dataset (upload directly via LangSmith UI)
  evaluators.py             Online evaluators
  upload_online_evals.py    Script to register online evaluators
```

## Durable state

Checkpoints, sessions, the HITL audit log, and monitoring finding-state live in
Postgres (`k8s/postgres.yaml` deploys a StatefulSet into the `sre-agent`
namespace). Without it, a pod restart stranded every pending approval. The Slack
Approve button stayed live but the session behind it was gone, so the click
dead-ended and the proposed change could neither be applied nor rejected.

`DATABASE_URL` is assembled in `k8s/deployment.yaml` from `POSTGRES_PASSWORD`
via `$(VAR)` interpolation, so the credential lives in exactly one place. Use an
alphanumeric password, because a `/`, `+`, or `@` would corrupt the DSN it is
interpolated into.

If Postgres is unreachable the process logs loudly and **degrades to in-memory
state rather than refusing to boot**, so a cluster problem cannot also remove
your ability to ask the bot about it. `/health` exposes `durable_state`. Alert
on it, because that degradation is otherwise invisible.

Note the trade-off. The bot's durability now depends on a database inside the
cluster it monitors. A cluster-wide outage takes the audit trail with it.

### Audit trail

`GET /api/audit?limit=50` returns recent HITL decisions, showing who approved
or rejected which tool call, with the actual arguments. One row per tool call, so a
batched change records each resource separately. Denied attempts are recorded too.

Two known gaps are worth calling out.

- `POST /api/approve` is unauthenticated, so audit rows from the web UI can only
  attribute to `api-user`. Slack clicks carry a real identity.
- `SLACK_APPROVER_IDS` defaults to empty, which means **any** workspace member
  who can see `#sre-alerts` can approve a cluster mutation. Set it to your
  on-call rotation.

## Monitoring behaviour

Scheduled checks are stateful. Each run is diffed against the previous one and
Slack is only notified when something is **new**, **escalated**, or **newly
resolved**. Otherwise the run is logged and stays quiet. A digest posts every
`MONITOR_DIGEST_EVERY_N_CHECKS` runs regardless, so a silent channel still
proves the bot is alive.

Findings are identified by `namespace/kind/name:reason`, not by the model's
free-text title (which it rewords between runs) and not by raw pod name (which
changes on every restart). See `monitor_state.fingerprint`. That is what lets
one ongoing incident report as "ongoing 6h · seen 12×" instead of as a fresh
alert every interval.

The **Ack** button on a report mutes its findings for `MONITOR_ACK_HOURS`. Acked
findings stay tracked, so history remains correct when the ack lapses, and they
resolve silently. Acking is not gated by `SLACK_APPROVER_IDS`, because it
changes no cluster state.

Interactive health checks (`@sre-bot health check`) read this history to annotate
age and counts but do not advance it; only scheduled runs do, so ad-hoc requests
cannot inflate the counters.

## Tests

```bash
python -m pytest tests/ -q          # unit tests; Postgres tests skip

# With Postgres for the integration tests:
docker run --rm -d --name pg -p 55433:5432 \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=sre_agent \
  -e POSTGRES_DB=sre_agent postgres:16-alpine
TEST_DATABASE_URL=postgresql://sre_agent:testpw@127.0.0.1:55433/sre_agent \
  python -m pytest tests/ -q
```

## Security notes

- `k8s/secret.yaml` is in `.gitignore` — never commit it
- The container runs as a non-root user (`uid 1000`)
- Postgres traffic is unencrypted cluster-internal traffic; a NetworkPolicy in
  `k8s/postgres.yaml` restricts port 5432 to the `sre-agent` pod
- If you suspect keys were exposed, rotate them immediately via the respective provider dashboards
