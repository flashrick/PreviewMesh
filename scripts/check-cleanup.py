#!/usr/bin/env python3
"""Focused regression checks for the selectable cleanup command."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup.sh"


def run(home: Path, *args: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ, HOME=str(home))
    return subprocess.run(
        ["bash", str(SCRIPT), "cleanup", "--language", "en", *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


def run_install(home: Path, config: Path, path: Path) -> subprocess.CompletedProcess:
    environment = dict(os.environ, HOME=str(home), PATH=f"{path}:/usr/bin:/bin")
    return subprocess.run(
        ["bash", str(SCRIPT), "install", "--language", "en", "--config", str(config)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


def fixture(home: Path) -> dict[str, Path]:
    config_dir = home / ".config" / "previewmesh"
    state_dir = home / ".local" / "share" / "previewmesh"
    control_dir = home / "control"
    config_dir.mkdir(parents=True)
    (state_dir / "runner").mkdir(parents=True)
    (state_dir / "tools").mkdir(parents=True)
    (state_dir / "bin").mkdir(parents=True)
    (control_dir / "config").mkdir(parents=True)
    token = config_dir / "secrets" / "keep.token"
    token.parent.mkdir()
    (config_dir / "setup.ini").write_text(
        "[project]\ncontrol_repository = new-owner/new-control\ncontrol_directory = "
        + str(control_dir)
        + "\n\n[credentials]\ndispatch_token_file = "
        + str(token)
        + "\n",
        encoding="utf-8",
    )
    for path in (
        config_dir / "setup.ini.draft",
        state_dir / "install-state.json",
        state_dir / "install-state.json.bak",
        state_dir / "setup.log",
        state_dir / "install.lock",
        state_dir / "runner" / ".runner",
        state_dir / "tools" / "downloaded-tool",
        state_dir / "bin" / "gh",
        control_dir / "config" / "previewmesh-runner.yaml",
        control_dir / "config" / ".runner-check",
    ):
        path.touch()
    (state_dir / "install-state.json").write_text(
        json.dumps({"control": "old-owner/old-control"}), encoding="utf-8"
    )
    (state_dir / "keep-me").touch()
    token.touch()
    return {
        "config": config_dir / "setup.ini",
        "draft": config_dir / "setup.ini.draft",
        "state": state_dir / "install-state.json",
        "runner": state_dir / "runner",
        "tools": state_dir / "tools",
        "binary": state_dir / "bin" / "gh",
        "credential": control_dir / "config" / "previewmesh-runner.yaml",
        "keep": state_dir / "keep-me",
        "token": token,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="previewmesh-cleanup-") as directory:
        home = Path(directory)
        paths = fixture(home)
        result = run(home, "--select", "config,state", "--yes")
        assert result.returncode == 0, result.stderr
        assert "ownership mismatch" in result.stdout
        for key in ("config", "draft", "state", "runner", "tools", "binary", "credential"):
            assert not paths[key].exists(), key
        assert paths["keep"].exists(), "unknown local state was removed"
        assert paths["token"].exists(), "token file was removed"

    with tempfile.TemporaryDirectory(prefix="previewmesh-cleanup-dry-") as directory:
        home = Path(directory)
        paths = fixture(home)
        result = run(home, "--select", "config,state", "--dry-run", "--yes")
        assert result.returncode == 0, result.stderr
        assert paths["config"].exists() and paths["state"].exists()

    with tempfile.TemporaryDirectory(prefix="previewmesh-cleanup-credentials-") as directory:
        home = Path(directory)
        paths = fixture(home)
        result = run(home, "--select", "credentials", "--yes")
        assert result.returncode == 0, result.stderr
        assert not paths["token"].exists(), "selected token file was kept"
        assert paths["config"].exists(), "unselected config was removed"

    with tempfile.TemporaryDirectory(prefix="previewmesh-cleanup-noninteractive-") as directory:
        result = run(Path(directory))
        assert result.returncode == 2, result.stdout + result.stderr

    with tempfile.TemporaryDirectory(prefix="previewmesh-cleanup-install-") as directory:
        home = Path(directory)
        config = home / ".config" / "previewmesh" / "setup.ini"
        config.parent.mkdir(parents=True)
        config.write_text(
            "[project]\n"
            "control_repository = new-owner/new-control\n"
            f"control_directory = {home / 'control'}\n"
            "public_repository = flashrick/PreviewMesh\n"
            "language = en\n\n"
            "[network]\nlan_ip = 192.168.1.10\nallowed_subnet = auto\n"
            "domain_suffix = auto\n\n"
            "[credentials]\n"
            f"dispatch_token_file = {home / 'tokens' / 'dispatch.token'}\n"
            f"ghcr_token_file = {home / 'tokens' / 'ghcr.token'}\n\n"
            "[source:app]\nrepository = new-owner/application\n"
            f"directory = {home / 'application'}\nport = 8080\n"
            f"token_file = {home / 'tokens' / 'application.token'}\n",
            encoding="utf-8",
        )
        state = home / ".local" / "share" / "previewmesh" / "install-state.json"
        state.parent.mkdir(parents=True)
        state.write_text(
            json.dumps({"control": "old-owner/old-control", "control_id": "123"}),
            encoding="utf-8",
        )
        fake_bin = home / "fake-bin"
        fake_bin.mkdir()
        fake_sudo = fake_bin / "sudo"
        fake_sudo.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        fake_sudo.chmod(0o755)

        before = run_install(home, config, fake_bin)
        before_output = before.stdout + before.stderr
        assert "another deployment management repository" in before_output, before_output
        cleaned = run(home, "--select", "state", "--yes")
        assert cleaned.returncode == 0, cleaned.stderr
        after = run_install(home, config, fake_bin)
        after_output = after.stdout + after.stderr
        assert "another deployment management repository" not in after_output
        assert "systemd is required" in after_output or "sudo -v" in after_output or "Supported:" in after_output, after_output

    print("Cleanup checks passed: selective removal, token handling, install resume boundary, dry-run and non-interactive guard.")


if __name__ == "__main__":
    main()
