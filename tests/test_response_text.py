from response_text import response_text


class Message:
    def __init__(self, content):
        self.content = content


def test_response_text_preserves_anthropic_string_content():
    assert response_text(Message("Cluster is healthy.")) == "Cluster is healthy."


def test_response_text_extracts_openai_responses_content_blocks():
    content = [
        {"type": "reasoning", "summary": "internal"},
        {"type": "text", "text": "CPU usage is within limits."},
        {"type": "text", "text": "No action required."},
    ]

    assert response_text(Message(content)) == (
        "CPU usage is within limits.\nNo action required."
    )


def test_response_text_ignores_non_text_blocks():
    assert response_text([{"type": "tool_use", "name": "kubectl_get_pods"}]) == ""
