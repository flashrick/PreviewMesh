#!/usr/bin/env python3
"""Render the Assessment 2 D2 sources to document-ready SVG and PNG figures."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIGURES = ROOT / "docs/assessment2/figures"
SOURCES = (
    "figure-1-data-flow",
    "figure-2-architecture",
    "figure-3-cleanup-state-machine",
    "figure-4-lifecycle-workflow",
)


def render(d2: str, source: Path, output: Path) -> None:
    subprocess.run(
        [
            d2,
            "--layout",
            "tala",
            "--pad",
            "8",
            "--center",
            "--scale",
            "1",
            str(source),
            str(output),
        ],
        check=True,
        cwd=ROOT,
    )


def main() -> None:
    d2 = shutil.which("d2")
    if not d2:
        raise SystemExit(
            "D2 is required. Install it globally, then ensure d2 is available on PATH."
        )

    for stem in SOURCES:
        source = FIGURES / f"{stem}.d2"
        if not source.is_file():
            raise FileNotFoundError(source)
        render(d2, source, FIGURES / f"{stem}.svg")
        render(d2, source, FIGURES / f"{stem}.png")


if __name__ == "__main__":
    main()
