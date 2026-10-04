"""Review and publish a single notification workflow without touching a checkout."""
import base64
import difflib
import json
import re
from urllib.parse import quote, urlencode

from setup_config import SetupError

WORKFLOW = ".github/workflows/previewmesh-notify.yml"
BRANCH = "previewmesh/onboard-source"


def notification(root, control):
    owner, repository = control.split("/")
    return (root / "templates/source-notify.yml").read_text().replace(
        "YOUR_GITHUB_OWNER", owner).replace("YOUR_CONTROL_REPOSITORY", repository)


def onboard(installer, root, repository, create_pr=False):
    if repository not in {source.repository for source in installer.c.sources}:
        raise SetupError("Choose a configured --source OWNER/REPO / 请用 --source 指定配置中的源仓库。")
    if not installer.tool("gh"):
        raise SetupError("Install GitHub CLI, then run gh auth login --hostname github.com --scopes repo,workflow / 请安装 gh 并登录后重试。")

    # API errors and verbose responses may contain existing file contents; never echo them.
    def api(path, method="GET", data=None, missing=False):
        endpoint = f"repos/{repository}" + ("/" + path if path else "")
        args = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        if data is not None:
            args += ["--input", "-"]
        verbose = installer.verbose
        installer.verbose = False
        try:
            result = installer.run(args, input=json.dumps(data) if data is not None else None, check=False)
        finally:
            installer.verbose = verbose
        if result.returncode:
            if missing and "HTTP 404" in result.stderr:
                return None
            status = re.search(r"HTTP \d{3}\b", result.stderr)
            reason = status.group() if status else "transport/authentication error"
            raise SetupError(f"GitHub {method} request failed ({reason}). Check network, gh auth status and repository access (classic: repo/workflow; fine-grained: Contents, Workflows and Pull requests write), then rerun the same command. An existing branch is preserved and checked before reuse. / GitHub 请求失败，请检查网络、登录、仓库访问权限及上述写权限后重试；已有分支会保留并在复用前检查。")
        return json.loads(result.stdout)

    def contents(commit):
        tree = commit["tree"]["sha"]
        for index, part in enumerate(WORKFLOW.split("/")):
            entries = api(f"git/trees/{tree}")
            if entries.get("truncated"):
                raise SetupError("Repository tree is truncated; inspect the workflow manually / 仓库树不完整，请手动检查工作流。")
            entry = next((item for item in entries["tree"] if item["path"] == part), None)
            if entry is None:
                return ""
            if index < 2:
                if entry["type"] != "tree":
                    raise SetupError("Workflow parent is not a directory; resolve it manually / 工作流父路径不是目录，请手动处理。")
                tree = entry["sha"]
            else:
                if entry["type"] != "blob" or entry["mode"] != "100644":
                    raise SetupError("Workflow is not a regular non-executable file; review it manually / 工作流不是普通非可执行文件，请手动检查。")
                blob = api(f"git/blobs/{entry['sha']}")
                return base64.b64decode(blob["content"], validate=False).decode("utf-8")

    info = api("")
    default = info["default_branch"]
    base_ref = "git/ref/heads/" + quote(default, safe="")
    base = api(base_ref)["object"]["sha"]
    commit = api(f"git/commits/{base}")
    old, expected = contents(commit), notification(root, installer.c.control)
    if old == expected:
        installer.say("Notification is already merged on the default branch.", "通知工作流已合并到默认分支。")
        installer.source_verification()
        return

    query = urlencode({"state": "open", "head": repository.split("/")[0] + ":" + BRANCH, "base": default})
    prs = api("pulls?" + query)
    if prs:
        installer.say("Existing onboarding PR; review, edit and merge it:", "已有接入 PR，请审核、修改并合并：")
        print(installer.redact(prs[0]["html_url"]))
        installer.source_verification()
        return

    installer.say("Proposed complete diff (only the notification workflow):", "待提交的完整 diff（仅通知工作流）：")
    # Keep line endings visible, including a missing newline in an existing file.
    for line in difflib.unified_diff(old.splitlines(keepends=True), expected.splitlines(keepends=True),
                                     fromfile="a/" + WORKFLOW if old else "/dev/null", tofile="b/" + WORKFLOW):
        print(installer.redact(line), end="" if line.endswith("\n") else "\n\\ No newline at end of file\n")
    if not create_pr:
        installer.say("No changes made. Rerun with --create-pr to review this diff and confirm PR creation.",
                      "未做任何修改。添加 --create-pr 重跑，审阅 diff 并确认创建 PR。")
        return

    branch_ref = "git/ref/heads/" + quote(BRANCH, safe="")
    branch = api(branch_ref, missing=True)
    head = None
    if branch:
        head = branch["object"]["sha"]
        existing = api(f"git/commits/{head}")
        comparison = api(f"compare/{base}...{head}")
        # Never overwrite a branch, even if it contains user edits or an old base.
        if ([parent["sha"] for parent in existing["parents"]] != [base]
                or contents(existing) != expected
                or [item["filename"] for item in comparison.get("files", [])] != [WORKFLOW]
                or comparison.get("status") != "ahead"):
            raise SetupError(f"Existing {BRANCH} differs from this proposal. Review that branch and open its PR manually, or rename it before retrying. It was not changed. / 已有接入分支与提案不同，请审核并手动创建 PR，或重命名分支后重试；未修改该分支。")
    installer.say("Type create to submit ONLY this workflow and open a PR; anything else cancels:",
                  "输入 create，仅提交此工作流并创建 PR；其他输入取消：")
    if input().strip() != "create":
        installer.say("Cancelled; no changes made.", "已取消，未做任何修改。")
        return
    if api(base_ref)["object"]["sha"] != base:
        raise SetupError("Default branch changed; rerun to review a fresh diff / 默认分支已变化，请重跑并审核最新 diff。")
    if head is None:
        tree = api("git/trees", "POST", {"base_tree": commit["tree"]["sha"], "tree": [
            {"path": WORKFLOW, "mode": "100644", "type": "blob", "content": expected}]})
        head = api("git/commits", "POST", {"message": "Add PreviewMesh source notification", "tree": tree["sha"], "parents": [base]})["sha"]
        api("git/refs", "POST", {"ref": "refs/heads/" + BRANCH, "sha": head})
    elif api(branch_ref)["object"]["sha"] != head:
        raise SetupError("Onboarding branch changed; review it and rerun / 接入分支已变化，请审核后重试。")
    pr = api("pulls", "POST", {"title": "Add PreviewMesh source notification", "head": BRANCH, "base": default,
        "body": "Adds only the PreviewMesh notification workflow. Review and edit this PR before merging. After merging, run setup doctor and open a test application PR to verify notification, deployment, SHA, preview access and cleanup."})
    installer.say("PR created. Review, edit and merge it:", "PR 已创建，请审核、修改并合并：")
    print(installer.redact(pr["html_url"]))
    installer.source_verification()
