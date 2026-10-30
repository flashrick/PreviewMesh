# 更新私有部署管理 checkout

[返回 README](../../README_CN.md#更新与清理) · [English](upgrade.md)

引导式 `init`/`install` 流程会创建或恢复私有部署管理 checkout。后续升级请继续使用这个 checkout，并与公开源代码 checkout 分开。每次更新请选择下面的审核分支路径或直接同步路径之一；不要针对同一次更新同时执行两条路径。

审核分支路径：

1. 进入私有部署管理 checkout，准备 `main`，并确认工作树干净。按顺序运行：

   ~~~bash
   # 进入已有的私有部署管理 checkout。
   # 准备本地 main 分支。
   git switch main
   # 拉取前这里必须没有输出。
   git status --short
   git pull --ff-only origin main
   # 继续前这里必须没有输出。
   git status --short
   ~~~

   如果任一条 `git status --short` 有输出，请先停止，审核、提交或暂存这些改动。更新器要求工作树干净。

2. 运行默认更新器，创建审核分支：

   ~~~bash
   # 保持在私有部署管理 checkout 中。
   scripts/update-upstream.sh --remote upstream --ref main
   ~~~

   默认更新器会获取公开 `main` 分支并创建生成的审核分支，不会提交或推送到 `main`。如果缺少 `upstream` remote，脚本会自动添加公开 PreviewMesh 仓库。如果脚本报告仓库已经是最新状态，就没有需要验证或发布的分支。

3. 更新器报告生成分支后，获取分支名，并在该分支中运行控制仓库检查：

   ~~~bash
   # 保持在生成的审核分支中。
   git branch --show-current
   # 查看生成的变更。
   git diff --stat main...HEAD
   # 运行控制仓库要求的检查。
   git diff --check main...HEAD
   go test ./...
   go vet ./...
   python3 scripts/check-workflow.py
   python3 scripts/check-github-secrets.py
   python3 scripts/check-onboard-source.py
   ~~~

4. 全部检查通过后，按以下顺序执行 push 和 `gh pr create` 命令：

   ~~~bash
   # 验证通过后保持在生成的审核分支中。
   git push --set-upstream origin "$(git branch --show-current)"
   gh pr create \
     --base main \
     --head "$(git branch --show-current)" \
     --title "chore: update PreviewMesh from upstream" \
     --body "请在合并前审核来自上游的 PreviewMesh 更新。"
   ~~~

   如果任何验证命令失败，不要执行 push 或 `gh pr create` 这两个命令中的任何一个。保留本地分支，修复或审核失败原因，并重新运行全部检查后再发布。

   正常情况下，`gh pr create` 会通过此 checkout 的 GitHub `origin` 自动识别当前私有 control repository。如果报告 `no default remote repository has been set`，请先检查 `origin` 是否缺失或指向其他仓库：

   ~~~bash
   git remote get-url origin
   git remote -v
   ~~~

   请确认 `origin` 指向私有 control repository 目标，`upstream` 指向公开 PreviewMesh 来源。如果 `origin` 缺失或不正确，请先修复 remote，再次运行 `git remote get-url origin`。

   第一次从这个 control checkout 创建 PR 时的单次恢复（仅在出现上述错误后执行）：只有当 `origin` 已指向正确的私有 control repository 且 `gh pr create` 仍报告同一错误时，才运行下面的命令一次，然后重试第 4 步中的 `gh pr create` 命令：

   ~~~bash
   gh repo set-default origin
   ~~~

   ~~~bash
   # 设置默认 remote 后执行或重试此命令。
   gh pr create \
     --base main \
     --head "$(git branch --show-current)" \
     --title "chore: update PreviewMesh from upstream" \
     --body "请在合并前审核来自上游的 PreviewMesh 更新。"
   ~~~

   后续更新不要重复这个恢复命令。只有在排查问题或有意跨仓库操作时才使用 `--repo` 显式指定目标仓库；它不属于正常流程。

   `gh pr create` 只创建 PR，不会合并。PR 审核和检查完成前，请保持当前生成的 `update/preview...` 审核分支处于 checkout 状态；不要切换到 `main`，也不要在本地合并。

   PR 在 GitHub 上合并后，回到私有 control checkout，同步本地 `main` 分支：

   ~~~bash
   git switch main
   git pull --ff-only origin main
   ~~~

   删除已合并的审核分支（本地或远程）是可选操作。

更新器会保留私有仓库中 `config/repositories.json` 的应用注册信息；GitHub secrets 位于 Git 之外，不会被修改。它不会自动更新已经发布到应用仓库的通知工作流。合并更新后，如果本机管理文件需要刷新，请重新运行 `install`。

直接同步替代路径：

如果运维人员希望直接更新并推送 `main`，可以使用这条一键路径代替审核分支路径。不要在审核分支路径之后运行它，也不要针对同一次更新组合执行两条路径。它要求当前分支为 `main` 且工作树干净，会先将 `main` 快进到 `origin`，再获取 `upstream/main`，保留 `config/repositories.json`，运行控制仓库检查，提交更新，并且只在验证通过后推送 `main`。它不会创建 PR。

如果私有 checkout 中已经有这个辅助脚本，请运行：

~~~bash
# 请在私有部署管理 checkout 中运行。
scripts/sync-previewmesh.sh
~~~

如果私有 checkout 中还没有这个辅助脚本，可以从公开 checkout 直接对该私有 checkout 调用公开版本。这条命令本身就是直接同步路径，不是额外的准备步骤：

~~~bash
# 请在公开 PreviewMesh checkout 中运行。
scripts/sync-previewmesh.sh --repo-dir /path/to/previewmesh-control
~~~
