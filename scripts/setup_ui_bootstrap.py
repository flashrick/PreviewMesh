#!/usr/bin/env python3
"""Install the optional terminal UI into a user-owned virtual environment."""

import argparse
from pathlib import Path
import subprocess
import sys


_IMPORT_CHECK = "import rich, questionary"


def _run(command, *, timeout):
    """Run dependency preparation without exposing package-manager output."""
    try:
        return subprocess.run(
            [str(item) for item in command],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def dependencies_available(python):
    return _run([python, "-c", _IMPORT_CHECK], timeout=10)


def ensure_ui_python(root, base_python, venv):
    """Return a Python executable with Rich and Questionary, or ``None``."""
    base_python = Path(base_python)
    if dependencies_available(base_python):
        return str(base_python)

    venv = Path(venv).expanduser().absolute()
    venv_python = venv / "bin" / "python"
    try:
        venv.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    if not dependencies_available(venv_python):
        if not _run([base_python, "-m", "venv", venv], timeout=120):
            return None
        requirements = Path(root) / "scripts" / "ui-requirements.txt"
        if not _run(
            [
                venv_python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--quiet",
                "--requirement",
                requirements,
            ],
            timeout=300,
        ):
            return None

    return str(venv_python) if dependencies_available(venv_python) else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    args = parser.parse_args(argv)
    selected = ensure_ui_python(args.root, sys.executable, args.venv)
    if selected is None:
        return 1
    print(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
