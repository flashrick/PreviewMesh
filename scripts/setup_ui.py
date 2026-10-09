"""Small, optional terminal UI layer shared by setup and the wizard.

Rich and Questionary are deliberately optional.  The installer must still work
on a clean Ubuntu host, in CI, and when its output is redirected, so this
module owns both the polished terminal path and the plain-text fallback.
"""
from contextlib import contextmanager
import os
import shutil
import sys

try:
    import questionary
except ImportError:  # pragma: no cover - depends on the host environment.
    questionary = None

try:
    from rich.box import SIMPLE
    from rich.console import Console
    from rich.progress import SpinnerColumn, Progress, TextColumn
    from rich.table import Table
    from rich.text import Text
    from rich.panel import Panel
    from rich.theme import Theme
except ImportError:  # pragma: no cover - depends on the host environment.
    SIMPLE = Console = SpinnerColumn = Progress = TextColumn = Table = Text = Panel = Theme = None


_MANUAL = object()


# A small semantic palette keeps the terminal UI coherent and makes a color
# change a one-place decision. Text labels and symbols remain the primary cue.
_STYLE_TOKENS = {
    "heading": "bold #7aa2f7",
    "muted": "dim #8b949e",
    "info": "#56b6c2",
    "success": "#98c379",
    "warning": "#e5c07b",
    "error": "#e06c75",
    "skipped": "dim #8b949e",
    "reused": "#c678dd",
    "running": "bold #56b6c2",
    "pending": "dim #6b7280",
    "current": "bold #61afef",
}

_STATUS_META = {
    "success": ("OK", "通过", "✓", "success"),
    "warning": ("WARN", "警告", "!", "warning"),
    "error": ("ERROR", "失败", "×", "error"),
    "skipped": ("SKIP", "跳过", "–", "skipped"),
    "reused": ("REUSE", "复用", "↺", "reused"),
    "running": ("RUN", "执行", "›", "running"),
    "info": ("INFO", "信息", "·", "info"),
}

_STAGE_META = {
    "done": ("DONE", "完成", "✓", "success"),
    "current": ("CURRENT", "当前", "›", "current"),
    "reused": ("REUSE", "复用", "↺", "reused"),
    "rerun": ("RERUN", "重新执行", "↻", "warning"),
    "failed": ("FAILED", "失败", "×", "error"),
    "cancelled": ("CANCELLED", "已取消", "!", "warning"),
    "skipped": ("SKIP", "跳过", "–", "skipped"),
    "pending": ("PENDING", "待处理", "○", "pending"),
}


def _is_tty(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def prompt_capable():
    """Return whether it is safe to ask a user for input."""
    return _is_tty(sys.stdin) and (not os.environ.get("CI") or os.environ.get("PREVIEWMESH_INTERACTIVE") == "1")


def interactive_capable():
    """Return whether arrow-key terminal controls can be used."""
    return (
        prompt_capable()
        and _is_tty(sys.stdout)
        and os.environ.get("TERM") != "dumb"
        and os.environ.get("PREVIEWMESH_UI", "").strip().lower()
        not in {"plain", "off", "false", "0"}
    )


def _no_color():
    return bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"


class TerminalUI:
    """Render concise status and prompt messages without owning installer logic."""

    def __init__(self, *, force_plain=False):
        self.force_plain = force_plain
        self._live_console = None

    @property
    def interactive(self):
        return not self.force_plain and interactive_capable()

    @property
    def rich_enabled(self):
        return not self.force_plain and Console is not None and self.interactive

    @property
    def questionary_enabled(self):
        return not self.force_plain and questionary is not None and self.interactive

    @staticmethod
    def _style(name):
        if _no_color():
            return ""
        return f"preview.{name}" if name in _STYLE_TOKENS else ""

    def _console(self):
        if self._live_console is not None:
            return self._live_console
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        theme = Theme({f"preview.{name}": value for name, value in _STYLE_TOKENS.items()}) if Theme else None
        return Console(
            file=sys.stdout,
            force_terminal=True,
            no_color=_no_color(),
            width=max(20, width),
            soft_wrap=True,
            theme=theme,
        )

    def _write(self, value=""):
        print(value, file=sys.stdout, flush=True)

    def message(self, value=""):
        """Print text without allowing config values to be interpreted as markup."""
        if self.rich_enabled:
            self._console().print(Text(str(value)), markup=False)
        else:
            self._write(value)

    def section(self, number, total, title, body=""):
        """Render one wizard section with the same hierarchy in both modes."""
        if self.rich_enabled:
            heading = Text(f"{number}/{total}  {title}", style=self._style("heading"))
            content = Text(str(body))
            self._console().print(Panel(content, title=heading, border_style=self._style("heading"), padding=(0, 1)))
            return
        self._write(f"\n[{number}/{total}] {title}")
        if body:
            self._write(body)

    def status(self, kind, value, *, chinese=False):
        """Print a status line with a text label that remains clear without colour."""
        english, chinese_label, symbol, style = _STATUS_META.get(
            kind, (kind.upper(), kind, "·", "info"))
        label = chinese_label if chinese else english
        if self.rich_enabled:
            text = Text.assemble(
                (f"{symbol} ", self._style(style)),
                (f"{label:<8} ", self._style(style)),
                (str(value), ""),
            )
            self._console().print(text)
        else:
            self._write(f"{label:<6} {value}")

    def table(self, title, rows):
        """Render key/value data compactly and wrap values in narrow terminals."""
        if self.rich_enabled:
            table = Table(
                title=Text(str(title), style=self._style("heading")),
                box=SIMPLE,
                show_header=False,
                expand=True,
                padding=(0, 1),
            )
            table.add_column(style=self._style("muted"), no_wrap=False)
            table.add_column(overflow="fold")
            for label, value in rows:
                table.add_row(
                    label if isinstance(label, Text) else Text(str(label)),
                    value if isinstance(value, Text) else Text(str(value)),
                )
            self._console().print(table)
            return
        self._write(str(title))
        for label, value in rows:
            self._write(f"  {label}: {value}")

    def installation_plan(self, stages, *, completed=(), current=None, results=None, chinese=False):
        """Show all stages and their current status."""
        completed = set(completed)
        results = results or {}
        rows = []
        for number, title in stages:
            result = results.get(number, ("pending", ""))[0].lower()
            if number == current:
                stage_state = "current"
            elif result == "reused":
                stage_state = "reused"
            elif result in ("failed", "cancelled", "skipped"):
                stage_state = result
            elif number in completed:
                stage_state = "done"
            elif result == "rerun":
                stage_state = "rerun"
            else:
                stage_state = "pending"
            english, chinese_label, symbol, style = _STAGE_META[stage_state]
            state = chinese_label if chinese else english
            if self.rich_enabled:
                value = Text.assemble(
                    (f"{symbol} ", self._style(style)),
                    (f"{state:<9} ", self._style(style)),
                    (str(title), ""),
                )
                rows.append((Text(f"{number:>2}", style=self._style("muted")), value))
            else:
                rows.append((f"{number:>2}", f"{state:<9} {title}"))
        self.table("安装阶段" if chinese else "Installation stages / 安装阶段", rows)

    def stage(self, number, total, title, detail="", *, label=None):
        """Render the active installer stage and its purpose."""
        if self.rich_enabled:
            heading = Text(label or f"Stage {number} of {total}  {title}", style=self._style("current"))
            self._console().print(Panel(Text(str(detail)), title=heading,
                                         border_style=self._style("current"), padding=(0, 1)))
        else:
            self._write(f"\n[{number}/{total}] {title}")
            if detail:
                self._write(detail)

    def progress_line(self, total, completed, current=None, *, chinese=False):
        completed = sorted(set(completed))
        remaining = [str(number) for number in range(1, total + 1) if number not in completed]
        current_text = ((f"当前 {current}；" if chinese else f"current {current}; ") if current else "")
        done_text = ", ".join(map(str, completed)) or "none"
        remaining_text = ", ".join(remaining) or "none"
        if self.rich_enabled:
            prefix = "进度：" if chinese else "Progress: "
            complete = "完成；" if chinese else " complete; "
            done_label = "已完成 " if chinese else "completed "
            remaining_label = "剩余 " if chinese else "remaining "
            text = Text.assemble(
                (prefix, self._style("muted")),
                (f"{len(completed)}/{total}", self._style("success" if len(completed) == total else "current")),
                (complete, self._style("muted")),
                (current_text, self._style("current")),
                (done_label, self._style("muted")),
                (done_text, self._style("success")),
                ("；" if chinese else "; ", self._style("muted")),
                (remaining_label, self._style("muted")),
                (remaining_text, self._style("pending")),
                ("。" if chinese else ".", self._style("muted")),
            )
            self._console().print(text)
            return
        if chinese:
            self._write(f"进度：{len(completed)}/{total} 完成；{current_text}已完成 {done_text}；剩余 {remaining_text}。")
        else:
            self._write(f"Progress: {len(completed)}/{total} complete; {current_text}completed {done_text}; remaining {remaining_text}.")

    def summary(self, title, rows, *, success=False):
        if self.rich_enabled:
            rows = list(rows)
            body = Text()
            for index, (label, value) in enumerate(rows):
                body.append(str(label), self._style("muted"))
                body.append(": ")
                body.append(str(value))
                if index < len(rows) - 1:
                    body.append("\n")
            style = self._style("success" if success else "heading")
            self._console().print(Panel(body, title=Text(str(title), style=style),
                                         border_style=style, padding=(0, 1)))
        else:
            self.table(title, rows)

    def line(self, prompt, default=""):
        """Read a line, using Questionary text input only in a real terminal."""
        if self.questionary_enabled:
            try:
                answer = questionary.text(str(prompt), default=str(default),
                                          style=self._questionary_style()).ask()
            except (EOFError, KeyboardInterrupt):
                raise EOFError()
            if answer is None:
                raise EOFError()
            return str(answer).strip()
        suffix = f" [{default}]" if default else ""
        return input(f"{prompt}{suffix}: ").strip()

    def select(self, prompt, choices, *, default=None, instruction=None, manual_label=None):
        """Select a value with arrows when possible, while retaining a text path."""
        values = [str(value) for value in choices]
        if self.questionary_enabled and values:
            question_choices = list(values)
            if manual_label:
                question_choices.append(questionary.Choice(str(manual_label), value=_MANUAL))
            kwargs = {
                "choices": question_choices,
                "default": str(default) if default is not None and str(default) in values else None,
                "instruction": instruction or "Use ↑/↓ and Enter / 使用 ↑/↓ 后按 Enter",
                "style": self._questionary_style(),
                "use_shortcuts": True,
            }
            try:
                answer = questionary.select(str(prompt), **kwargs).ask()
            except TypeError:
                # Older Questionary releases do not expose use_shortcuts.
                kwargs.pop("use_shortcuts", None)
                answer = questionary.select(str(prompt), **kwargs).ask()
            except (EOFError, KeyboardInterrupt):
                raise EOFError()
            if answer is None:
                raise EOFError()
            return None if answer is _MANUAL else str(answer)

        for number, value in enumerate(values, 1):
            self._write(f"  {number}. {value}")
        if manual_label:
            self._write(f"  {len(values) + 1}. {manual_label}")
        answer = self.line(prompt, str(default) if default is not None else "")
        if not answer and default is not None:
            return str(default)
        if answer.isdigit():
            number = int(answer)
            if 1 <= number <= len(values):
                return values[number - 1]
            if manual_label and number == len(values) + 1:
                return None
        return answer

    def confirm(self, prompt, *, default=False):
        """Ask a yes/no question with a safe, explicit default."""
        if self.questionary_enabled:
            try:
                answer = questionary.confirm(str(prompt), default=default,
                                              style=self._questionary_style()).ask()
            except (EOFError, KeyboardInterrupt):
                raise EOFError()
            if answer is None:
                raise EOFError()
            return bool(answer)
        suffix = " (yes/no)"
        while True:
            answer = input(f"{prompt}{suffix} [{'yes' if default else 'no'}]: ").strip().lower()
            if answer in ("q", "quit"):
                raise EOFError()
            if not answer:
                return default
            if answer in ("yes", "y", "是"):
                return True
            if answer in ("no", "n", "否"):
                return False
            self.status("warning", "Enter yes or no / 请输入 yes 或 no。")

    @staticmethod
    def _questionary_style():
        if questionary is None:
            return None
        if _no_color():
            return questionary.Style([
                ("qmark", ""), ("question", ""), ("answer", ""),
                ("pointer", ""), ("highlighted", ""), ("selected", ""),
                ("instruction", ""), ("text", ""), ("description", ""),
            ])
        return questionary.Style([
            ("qmark", "fg:#7aa2f7 bold"),
            ("question", "bold"),
            ("answer", "fg:#98c379"),
            ("pointer", "fg:#7aa2f7 bold"),
            ("highlighted", "fg:#61afef bold"),
            ("selected", "fg:#98c379"),
            ("instruction", "fg:#8b949e"),
            ("description", "fg:#8b949e"),
        ])

    @contextmanager
    def activity(self, label):
        """Show a restrained spinner only while a long-running stage is active."""
        if self.rich_enabled and Progress is not None:
            console = self._console()
            progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}", style=self._style("info")),
                console=console,
                transient=True,
            )
            self._live_console = console
            try:
                with progress:
                    progress.add_task(str(label), total=None)
                    yield
            finally:
                self._live_console = None
            return
        if self.interactive:
            self._write(f"... {label}")
        yield

