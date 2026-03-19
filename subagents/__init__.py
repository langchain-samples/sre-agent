from .pod_inspector import pod_inspector_subagent
from .scaling_analyzer import scaling_analyzer_subagent
from .performance_analyzer import performance_analyzer_subagent
from .log_analyzer import log_analyzer_subagent
from .change_executor import change_executor_subagent

ALL_SUBAGENTS = [
    pod_inspector_subagent,
    scaling_analyzer_subagent,
    performance_analyzer_subagent,
    log_analyzer_subagent,
    change_executor_subagent,
]
