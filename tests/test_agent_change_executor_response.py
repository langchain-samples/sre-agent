import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, ToolMessage


class ChangeExecutorResponseTests(unittest.TestCase):
    def _agent_module(self):
        with patch.dict("sys.modules", {
            "tools": __import__("types").SimpleNamespace(READ_TOOLS=[]),
            "subagents": __import__("types").SimpleNamespace(ALL_SUBAGENTS=[]),
        }):
            import importlib
            import agent
            return importlib.reload(agent)

    def test_preserves_existing_final_text(self):
        agent = self._agent_module()
        result = {"messages": [AIMessage(content="Already done.")]}

        updated = agent._ensure_change_executor_response(result)

        self.assertIs(updated, result)
        self.assertEqual(updated["messages"][-1].content, "Already done.")

    def test_appends_task_result_when_final_message_has_no_text(self):
        agent = self._agent_module()
        result = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "task",
                        "args": {
                            "subagent_type": "change-executor",
                            "description": "scale default/api to zero",
                        },
                        "id": "call-1",
                    }],
                ),
                ToolMessage(content="Scaled default/api from 3 replicas to 0.", tool_call_id="call-1"),
                AIMessage(content=[]),
            ]
        }

        updated = agent._ensure_change_executor_response(result)

        self.assertIsNot(updated, result)
        self.assertEqual(updated["messages"][-1].content, "Scaled default/api from 3 replicas to 0.")

    def test_appends_fallback_when_task_result_is_empty(self):
        agent = self._agent_module()
        result = {
            "messages": [
                AIMessage(
                    content=[{
                        "type": "tool_use",
                        "name": "task",
                        "id": "call-2",
                        "input": {
                            "subagent_type": "change-executor",
                            "description": "delete the test fleet",
                        },
                    }]
                ),
                ToolMessage(content=[], tool_call_id="call-2"),
            ]
        }

        updated = agent._ensure_change_executor_response(result)

        self.assertIn("completed", updated["messages"][-1].content)
        self.assertIn("delete the test fleet", updated["messages"][-1].content)


if __name__ == "__main__":
    unittest.main()
