"""Check generic secret routing and failure handling with a fake gh command."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


script = Path(__file__).with_name("configure-github-secrets.sh").resolve()
registry = [
    {
        "repository_id": "12",
        "source_repository": "owner/app-one",
        "port": 8080,
        "source_secret": "SOURCE_APP_ONE",
    },
    {
        "repository_id": "34",
        "source_repository": "team/app-two",
        "port": 3000,
        "source_secret": "SOURCE_APP_TWO",
    },
]
control_repository = "owner/previewmesh-control"
expected = [
    ("SOURCE_APP_ONE", control_repository, "source-token-1"),
    ("SOURCE_APP_TWO", control_repository, "source-token-2"),
    ("PREVIEWMESH_DISPATCH_TOKEN", "owner/app-one", "dispatch-token"),
    ("PREVIEWMESH_DISPATCH_TOKEN", "team/app-two", "dispatch-token"),
    ("GHCR_READ_TOKEN", control_repository, "ghcr-token"),
]

with tempfile.TemporaryDirectory(prefix="previewmesh-secrets-check-") as directory:
    directory = Path(directory)
    registry_path = directory / "repositories.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    gh = directory / "gh"
    gh.write_text(
        """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ['auth', 'status']:
    sys.exit(0)
args = sys.argv[1:]
assert args[:2] == ['secret', 'set']
assert args[3] == '--repo' and args[5:] == ['--app', 'actions']
with open(os.environ['MOCK_GH_LOG'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps([args[2], args[4], sys.stdin.read()]) + '\\n')
if args[2] == os.environ.get('MOCK_FAIL_SECRET'):
    sys.exit(1)
""",
        encoding="utf-8",
    )
    gh.chmod(0o700)
    for case, data, fail, count, code in [
        (
            "success",
            "\n".join(["source-token-1", "source-token-2", "dispatch-token", "ghcr-token"])
            + "\n",
            "",
            5,
            0,
        ),
        ("empty", "\n", "", 0, 1),
        ("upload_failure", "source-token-1\nsource-token-2\n", "SOURCE_APP_TWO", 2, 1),
    ]:
        log = directory / (case + ".jsonl")
        env = dict(
            os.environ,
            PATH=str(directory) + os.pathsep + os.environ["PATH"],
            MOCK_GH_LOG=str(log),
            MOCK_FAIL_SECRET=fail,
        )
        result = subprocess.run(
            ["bash", str(script), control_repository, str(registry_path)],
            input=data,
            text=True,
            capture_output=True,
            env=env,
        )
        assert result.returncode == code, (case, result.stderr)
        rows = [tuple(json.loads(line)) for line in log.read_text().splitlines()] if log.exists() else []
        assert rows == expected[:count], (case, rows)
        assert "source-token-" not in result.stdout + result.stderr, case
        assert "dispatch-token" not in result.stdout + result.stderr, case
        assert "ghcr-token" not in result.stdout + result.stderr, case

    duplicate_registry = directory / "duplicate.json"
    duplicate_registry.write_text(
        json.dumps(
            [
                {
                    "repository_id": "12",
                    "source_repository": "owner/app-one",
                    "port": 8080,
                    "source_secret": "SOURCE_SHARED",
                },
                {
                    "repository_id": "34",
                    "source_repository": "team/app-two",
                    "port": 3000,
                    "source_secret": "SOURCE_SHARED",
                },
            ]
        ),
        encoding="utf-8",
    )
    duplicate = subprocess.run(
        ["bash", str(script), control_repository, str(duplicate_registry)],
        input="",
        text=True,
        capture_output=True,
        env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"], MOCK_GH_LOG=str(directory / "duplicate.jsonl")),
    )
    assert duplicate.returncode == 1
    assert not (directory / "duplicate.jsonl").exists()

print("PASS: generic secret routing, duplicate protection, hidden values, empty input and upload failure.")
