"""Exercise the browser renderer with Node when it is available."""

import ast
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def _ui_html() -> str:
    source = ast.parse((Path(__file__).resolve().parents[1] / "api.py").read_text())
    assignment = next(
        node for node in source.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_UI_HTML" for target in node.targets)
    )
    return ast.literal_eval(assignment.value)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_inline_italics_and_adjacent_tables():
    html = _ui_html()
    start = html.index("function escapeHtml(text)")
    end = html.index("function appendMsg(text", start)
    bundle = Path(__file__).resolve().parents[1] / "static/vendor/markdown-it/markdown-it.umd.min.js"
    script = f"const window = {{markdownit: require({json.dumps(str(bundle))})}};\n" + html[start:end] + "\n" + """
process.stdout.write(JSON.stringify({
  italic: renderInline('*italic*'),
  bold: renderInline('**bold**'),
  tables: renderMarkdown('| First | Value |\\n| --- | --- |\\n| a | 1 |\\n| Second | Value |\\n| --- | --- |\\n| b | 2 |')
}));
"""
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, check=True)
    rendered = json.loads(result.stdout)

    assert rendered["italic"] == "<em>italic</em>"
    assert rendered["bold"] == "<strong>bold</strong>"
    assert rendered["tables"].count("<table>") == 2
    assert "<th>Second</th>" in rendered["tables"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
@pytest.mark.parametrize("source, expected, forbidden", [
    ("`some_pod_name`", "<code>some_pod_name</code>", "<em>"),
    ("`*literal*`", "<code>*literal*</code>", "<em>"),
    ("``a `backtick` here``", "<code>a `backtick` here</code>", None),
    ("```\n*literal*\nsome_pod_name\n```", "*literal*\nsome_pod_name", "<em>"),
    ("*italic* and **bold**", "<em>italic</em> and <strong>bold</strong>", None),
    ("[docs](https://example.com)", 'rel="noopener noreferrer"', None),
    ("[bad](javascript:alert(1))", "javascript:", "<a "),
    ('<img src=x onerror="alert(1)">', "&lt;img", "<img"),
    ("![image](https://example.com/image.png)", "image", "<img"),
    ("| A | B |\n| --- | --- |\n| a\\|b | `c_d_e` |", "<code>c_d_e</code>", "<em>"),
    ("- parent\n  - child", "<li>child</li>", None),
])
def test_markdown_preserves_code_and_escapes_unsafe_content(source, expected, forbidden):
    html = _ui_html()
    start = html.index("function escapeHtml(text)")
    end = html.index("function appendMsg(text", start)
    bundle = Path(__file__).resolve().parents[1] / "static/vendor/markdown-it/markdown-it.umd.min.js"
    script = (
        f"const window = {{markdownit: require({json.dumps(str(bundle))})}};\n"
        + html[start:end]
        + f"\nprocess.stdout.write(renderMarkdown({json.dumps(source)}));"
    )
    rendered = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, check=True).stdout
    assert expected in rendered
    if forbidden:
        assert forbidden not in rendered
