"""CLI entry point for the autonomous SRE bot."""
import os
import sys
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.markdown import Markdown
from langgraph.types import Command

load_dotenv()

console = Console()


def check_env():
    missing = []
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if not os.getenv("LANGSMITH_API_KEY"):
        missing.append("LANGSMITH_API_KEY")
    if missing:
        console.print(f"[red]Missing required env vars: {', '.join(missing)}[/red]")
        console.print("Copy .env.example to .env and fill in your keys.")
        sys.exit(1)


def handle_interrupt(result: dict) -> str | None:
    """Display interrupt details and prompt user for approve/reject/edit decision.
    Returns the decision type or None to cancel."""
    interrupts = result.get("__interrupt__", [])
    if not interrupts:
        return None

    for interrupt in interrupts:
        console.print()
        console.print(Panel(
            str(interrupt),
            title="[yellow bold]⚠ APPROVAL REQUIRED[/yellow bold]",
            border_style="yellow",
        ))

    console.print("[dim]Options: [bold]y[/bold]=approve  [bold]n[/bold]=reject  [bold]e[/bold]=edit  [bold]q[/bold]=quit[/dim]")
    choice = Prompt.ask("Your decision", choices=["y", "n", "e", "q"], default="n")
    return choice


def run_with_hitl(agent, messages: list[dict], config: dict) -> dict:
    """Invoke agent and handle any HITL interrupts in a loop."""
    result = agent.invoke({"messages": messages}, config=config)

    while "__interrupt__" in result and result["__interrupt__"]:
        choice = handle_interrupt(result)

        if choice == "y":
            result = agent.invoke(
                Command(resume={"decisions": [{"type": "approve"}]}),
                config=config,
            )
        elif choice == "n":
            feedback = Prompt.ask("Reason for rejection (optional)", default="")
            result = agent.invoke(
                Command(resume={"decisions": [{"type": "reject", "message": feedback}]}),
                config=config,
            )
        elif choice == "e":
            console.print("[dim]Enter edited arguments as key=value pairs, one per line. Empty line to finish.[/dim]")
            edited_args = {}
            while True:
                line = input("  ").strip()
                if not line:
                    break
                if "=" in line:
                    k, v = line.split("=", 1)
                    edited_args[k.strip()] = v.strip()
            result = agent.invoke(
                Command(resume={"decisions": [{"type": "edit", "args": edited_args}]}),
                config=config,
            )
        else:  # quit
            console.print("[yellow]Cancelled.[/yellow]")
            return result

    return result


def print_response(result: dict):
    """Print the final agent response."""
    messages = result.get("messages", [])
    if not messages:
        return
    last = messages[-1]
    content = last.content if hasattr(last, "content") else str(last)
    if content:
        console.print()
        console.print(Panel(Markdown(content), title="[green bold]SRE Bot[/green bold]", border_style="green"))


def print_todos(result: dict):
    """Print todo list if present."""
    todos = result.get("todos", [])
    if not todos:
        return
    console.print("\n[dim]Task progress:[/dim]")
    icons = {"completed": "✅", "in_progress": "🔄", "pending": "⏳"}
    for todo in todos:
        icon = icons.get(todo.get("status", "pending"), "•")
        console.print(f"  {icon} {todo.get('content', '')}")


def main():
    check_env()

    console.print(Panel(
        "[bold green]Autonomous SRE Bot[/bold green]\n"
        "[dim]Kubernetes cluster monitoring, diagnosis, and remediation[/dim]\n\n"
        "Commands:\n"
        "  [bold]audit[/bold] — full cluster health check\n"
        "  [bold]pods <namespace>[/bold] — inspect pod health\n"
        "  [bold]scaling <namespace>[/bold] — analyze scaling & HPA\n"
        "  [bold]performance <namespace>[/bold] — CPU/memory analysis\n"
        "  [bold]logs <namespace>[/bold] — scan logs for errors\n"
        "  [bold]fix <description>[/bold] — apply a specific change\n"
        "  [bold]quit[/bold] — exit",
        title="SRE Bot",
        border_style="blue",
    ))

    # Import here so env vars are loaded first
    from agent import create_sre_agent

    agent = create_sre_agent()
    config = {"configurable": {"thread_id": "sre-main"}}
    session_messages = []

    console.print(f"\n[dim]LangSmith project: {os.getenv('LANGSMITH_PROJECT', 'default')}[/dim]")
    console.print("[dim]Type your request or 'quit' to exit.[/dim]\n")

    while True:
        try:
            user_input = Prompt.ask("[bold blue]You[/bold blue]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        # Expand shorthand commands
        if user_input.lower() == "audit":
            user_input = (
                "Run a full Kubernetes cluster health audit. Check all namespaces. "
                "Inspect pod health, scaling configuration, resource performance, and logs. "
                "Give me a prioritized list of issues and recommendations."
            )

        session_messages = [{"role": "user", "content": user_input}]

        with console.status("[bold green]Analyzing cluster...[/bold green]", spinner="dots"):
            pass  # Status shown during invoke below

        console.print(f"\n[dim]Running: {user_input[:80]}{'...' if len(user_input) > 80 else ''}[/dim]")

        try:
            result = run_with_hitl(agent, session_messages, config)
            print_todos(result)
            print_response(result)
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")


if __name__ == "__main__":
    main()
