from .pod_inspector import pod_inspector_subagent
from .scaling_analyzer import scaling_analyzer_subagent
from .performance_analyzer import performance_analyzer_subagent
from .log_analyzer import log_analyzer_subagent
from .change_executor import change_executor_subagent
from .security_auditor import security_auditor_subagent
from .reliability_auditor import reliability_auditor_subagent
from .job_inspector import job_inspector_subagent
from .config_auditor import config_auditor_subagent

ALL_SUBAGENTS = [
    pod_inspector_subagent,
    scaling_analyzer_subagent,
    performance_analyzer_subagent,
    log_analyzer_subagent,
    change_executor_subagent,
    security_auditor_subagent,
    reliability_auditor_subagent,
    job_inspector_subagent,
    config_auditor_subagent,
]
