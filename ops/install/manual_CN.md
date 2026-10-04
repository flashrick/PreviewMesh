# 手工安装参考

[自动安装入口](../../README_CN.md#安装)

本文是手工流程。自动 `install` 命令可能在本地准备 source 通知文件，但不会提交、推送、创建或合并 source 改动。需要使用引导式接入流程时，执行 `bash scripts/setup.sh onboard-source --config PATH --source OWNER/REPO`；默认模式只显示完整 diff，`--create-pr` 才会显式进入可审核 PR 流程。下面的手工步骤仍适用于希望自行复制和编辑工作流的情况。

## 前提条件

本地检查需要 Git、Go 1.25 或更高版本、Python 3、Bash 和 GitHub CLI。真实预览还需要 Linux 或 WSL2 机器上的 K3s、Traefik、Helm 3 和 kubectl。运行 `onboard-source` 时，`gh auth login` 使用的 classic PAT 需要 `repo` 和 `workflow` scope；fine-grained PAT 需要对目标仓库有访问权，并授予 **Contents: Read and write**、**Workflows: Read and write**、**Pull requests: Read and write** 权限，参见 GitHub 的[细粒度个人访问令牌权限说明](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)。

### 一次设置项目变量

下面的路径、仓库名称和默认分支只需在当前终端设置一次。把示例值改成你自己的；`SOURCE_DEFAULT_BRANCH` 是应用仓库的默认分支。后续步骤直接复用这些值。设置变量不会创建目录、文件或仓库。

```bash
export PUBLIC_REPOSITORY=flashrick/PreviewMesh
export CONTROL_REPOSITORY=YOUR_GITHUB_OWNER/previewmesh-control
export SOURCE_REPOSITORY=YOUR_GITHUB_OWNER/your-application
export SOURCE_DEFAULT_BRANCH=main
export TEMPLATE_DIR="$HOME/workspace/PreviewMesh"
export CONTROL_DIR="$HOME/workspace/previewmesh-control"
export SOURCE_DIR="$HOME/workspace/your-application"
export PREVIEWMESH_RUNNER_CONFIG="$CONTROL_DIR/config/previewmesh-runner.yaml"
```

新开终端或换机器后，先重新执行这段设置，并调整本机路径；已经完成配置时，再执行第 3 步的仓库 ID 查询、第 5 步的 `KUBECONFIG` 选择，以及第 8 步的 PR 变量设置即可恢复操作环境，无需重跑仓库创建或部署。切换 source 时，也要更新它的路径、默认分支和仓库 ID；切换 PR 时，重新计算它的 Namespace 和域名。

### 下载公开模板

执行任何 `scripts/...` 命令之前，先下载公开模板。`TEMPLATE_DIR` 保存安装脚本；`CONTROL_DIR` 是第 1 步创建的另一个 private 仓库目录。两者必须使用不同目录。如果尚未安装 Git，请先安装。

```bash
# 创建公开模板目录的父目录。
mkdir -p "$(dirname "$TEMPLATE_DIR")"
# 下载公开模板及其中的安装脚本。
git clone "https://github.com/$PUBLIC_REPOSITORY.git" "$TEMPLATE_DIR"
# 进入模板目录，才能运行其中的脚本。
cd "$TEMPLATE_DIR"
```

如果已经 clone 过这个公开模板，将 `TEMPLATE_DIR` 设置为已有目录，只执行 `cd "$TEMPLATE_DIR"`。继续前确认该目录中存在 `scripts/create-control-repository.sh`。后续继续使用这个终端，以保留已设置的项目变量。

### 检查本地工具

以下脚本在已下载的模板或 control checkout 根目录执行；它们不会安装软件、修改 GitHub 或部署应用。缺少工具时会报错停止。

在 Bash 中运行以下命令，查看缺少的命令和已安装版本。Go 必须为 1.25 或更高版本，且 `gh auth status` 应显示已登录账号：

```bash
scripts/check-environment.sh local
```

### 检查真实预览环境

在 K3s 运维机器上运行以下命令，检查 Helm 3、kubectl、集群连接和 Traefik IngressClass。这里的只读检查使用运维人员的 kubeconfig；Runner 仍应使用首次配置步骤 5 中的受限 kubeconfig。Helm 版本输出应以 `v3` 开头。

```bash
scripts/check-environment.sh cluster
```

Self-hosted Runner 必须有 self-hosted、Linux、X64、previewmesh 四个标签，并能在 PATH 中找到 Go、Python、Helm 和 kubectl。下面的检查在创建 control checkout 并完成第 5 步 Runner 配置后执行。Runner 服务的 PATH 可能不同于交互式 Shell，因此请在与 Runner 服务相同的账号和环境中检查：

```bash
"$CONTROL_DIR/scripts/check-environment.sh" runner
```

正常镜像构建运行在 GitHub-hosted Runner；本地 Docker 只用于可选的手动构建。

## 首次配置

请按顺序完成。会改变 GitHub 或 K3s 的命令都属于运维操作，执行前请确认目标仓库和集群。

### 1. 创建 private control 仓库

在公开 PreviewMesh 模板 checkout 中运行下面的脚本。脚本会检查 GitHub 登录状态，将公开模板 clone 到 `CONTROL_DIR`，创建一个 **Private** 目标仓库，把公开模板添加为 `upstream` remote，并将 `main` 推送到新的 control 仓库。如果 GitHub 仓库已经存在，或本地目标目录非空，脚本会停止；它不会删除已有目录。

```bash
# 进入已下载的公开模板目录。
cd "$TEMPLATE_DIR"
# 运行已下载的安装脚本，创建 private control 仓库。
scripts/create-control-repository.sh \
  --source "$PUBLIC_REPOSITORY" \
  --repository "$CONTROL_REPOSITORY" \
  --directory "$CONTROL_DIR"
# 脚本成功后，进入新建的 private 仓库，继续后面的步骤。
cd "$CONTROL_DIR"
```

`gh` 使用的账号必须有权创建 private 仓库并推送 workflow 文件。先执行 `gh auth login --hostname github.com`。如果登录权限不足以推送 workflow，运行 `gh auth refresh --scopes workflow`。详见 [GitHub CLI 登录文档](https://cli.github.com/manual/gh_auth_login)。

脚本完成后，Control 仓库默认分支必须是 `main`，Actions 必须启用，并且不要把 self-hosted Runner 注册到公开模板仓库。

### 2. 启用 private 工作流

公开副本默认不会运行部署。在 private control 仓库创建 Actions repository variable：

```bash
# 启用 private Control 仓库中的预览生命周期工作流。
gh variable set PREVIEWMESH_ENABLED --repo "$CONTROL_REPOSITORY" --body true
```

这条命令在 `CONTROL_REPOSITORY` 指定的仓库中保存 Actions 变量 `PREVIEWMESH_ENABLED=true`，作为预览工作流的启用开关。构建、本地部署/清理和结果汇总 Job 都会检查它。值必须严格为 `true`；未设置或使用其他值时，这些 Job 会被跳过。设置变量不会立即启动预览，后续仍需 PR 通知或手动触发工作流。不要在公开模板仓库创建这个变量。

### 3. 登记 source 仓库

编辑登记文件前，先选定 source 仓库并从 GitHub 查询数字 ID；不要从仓库 URL 推测 ID：

```bash
REPOSITORY_ID=$(gh api "repos/$SOURCE_REPOSITORY" --jq '.id')
export REPOSITORY_ID
printf '%s\n' "$REPOSITORY_ID"
```

当前跟踪的 config/repositories.json 有意为空。复制通用示例，并在 private control checkout 中编辑，填入上面查到的 ID 和仓库名称。以下示例使用 `nano`；如果机器上没有安装它，可以换成已安装的编辑器：

```bash
cp config/repositories.example.json config/repositories.json
nano config/repositories.json
```

每一项包括不可变的仓库 ID、精确的 GitHub owner/name、应用 HTTP 端口，以及一个 source-access Secret 的名称。`port` 应与应用实际监听的端口一致。PreviewMesh 将它用于容器和 ClusterIP Service，不会占用 K3s 节点端口。每个预览运行在独立的 namespace 中，因此多个预览可以共用 8080，不会争用节点端口：

```json
[
  {
    "repository_id": "123456789",
    "source_repository": "YOUR_GITHUB_OWNER/your-application",
    "port": 8080,
    "source_secret": "SOURCE_APP"
  }
]
```

为每个 source 仓库填写唯一的 `source_secret` 名称，例如 `SOURCE_APP_ONE`。首次配置步骤 6 中的辅助脚本会提示输入该仓库的 Token，并在 private control 仓库中以此名称保存为 Actions Secret。登记文件只填写 Secret 名称，绝不能写入 Token 值。

编译并验证登记：

```bash
# 创建保存编译后命令行工具的目录。
mkdir -p bin
# 编译登记、PR 检查和 GitHub 状态回写工具。
go build -o bin/control ./cmd/control
# 编译镜像、部署、验证和清理工具。
go build -o bin/previewmesh ./cmd/previewmesh
# 不访问 GitHub，只检查 source 登记是否匹配。
./bin/control resolve \
  --repository-id "$REPOSITORY_ID" \
  --source-repository "$SOURCE_REPOSITORY" \
  --pr 1
# 暂存 private source 登记配置，准备提交。
git add config/repositories.json
# 将 source 登记配置记录为一次 Git 提交。
git commit -m "Configure preview source repository"
# 将 private control 配置推送到 main 分支。
git push origin main
```

示例中的 --pr 1 只检查编号格式，不要求 PR #1 已存在。

如果 `resolve` 报错 `repository not registered with this exact identity`，请检查 `config/repositories.json` 中是否恰好有一项同时匹配相同的 GitHub 数字仓库 ID 和完整 `OWNER/REPO` 名称。公开仓库中的登记文件有意留空；运行此命令前，必须先在 private control checkout 中填写登记信息。

### 4. 准备 source 应用

Source 仓库根目录必须有能构建 linux/amd64 镜像的 Dockerfile。应用必须：

- 在登记的端口监听 0.0.0.0；
- 能以 UID/GID 65532 运行，且不需要额外 capabilities；
- 对 GET /health 返回 HTTP 200，格式如下：

```json
{"status":"ok","commit_sha":"0123456789abcdef0123456789abcdef01234567"}
```

返回的 `commit_sha` 必须来自 `PREVIEW_COMMIT_SHA` 环境变量。PreviewMesh 部署时会注入请求的 source SHA，不要硬编码示例值。PreviewMesh 不会修改 source 应用；如果应用原本不返回这种健康检查响应，就必须先适配应用，才能通过版本验证。

版本验证必须精确匹配：如果 checkout 的 `HEAD` 与 Pull Request SHA 不同，构建会失败；部署结果会记录 `requested_sha` 和 `served_sha`。只有 `/health` 返回请求的 SHA，且工作流确认两者一致时，预览才会被报告为 ready。

### 5. 配置 K3s 和 Runner

在 K3s 服务器上，以将来运行 GitHub Runner 的 Linux 用户执行本步骤。该用户需要通过 `sudo` 管理 K3s，并且已安装 Python 3 和 `kubectl`。本例将 Runner 与 K3s 放在同一台机器上，使用前面设置的 `CONTROL_DIR` 和 `PREVIEWMESH_RUNNER_CONFIG`。

使用已设置的配置路径，将凭据排除在 Git 之外，然后应用受限的 Runner 权限：

```bash
# 进入 private control checkout。
cd "$CONTROL_DIR"
# 创建保存被 Git 忽略的 Runner kubeconfig 的目录。
mkdir -p "$(dirname "$PREVIEWMESH_RUNNER_CONFIG")"
# 确保凭据和临时文件不会进入 Git，包括旧 checkout 中的文件。
grep -qxF '/config/previewmesh-runner.yaml' .gitignore || printf '\n/config/previewmesh-runner.yaml\n' >> .gitignore
grep -qxF '/config/.runner-*' .gitignore || printf '\n/config/.runner-*\n' >> .gitignore
# 使用管理员 kubeconfig 确认 K3s 节点可以访问。
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml get nodes
# 安装或更新 Runner 所需的受限 Kubernetes 权限。
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml apply -f ops/kubernetes/runner-rbac.yaml
```

生成受限 kubeconfig，并检查创建/删除 Namespace、创建 Secret 的权限，以及创建 ClusterRole 必须被拒绝。脚本只保存集群地址、CA 和 Runner Token，不复制管理员凭据，也不打印 Token。以 Runner 的普通用户运行：

```bash
# 生成或续期受限 Runner kubeconfig，并检查它的权限。
python3 scripts/configure-runner.py
# 让当前 Shell 中的 kubectl 使用刚生成的 kubeconfig。
export KUBECONFIG="$PREVIEWMESH_RUNNER_CONFIG"
```

只有看到 `Created runner kubeconfig` 才继续。脚本以 `600` 权限写入临时文件，权限检查全部通过后才替换配置；失败时保留原配置。输出路径必须是 control checkout 中未被 Git 跟踪且已被忽略的绝对路径，临时文件 `.runner-*` 也必须被忽略；默认路径已满足这些要求。

请求的 Token 有效期为 24 小时，实际时长由 API Server 决定。到期前重新运行 `python3 scripts/configure-runner.py` 续期；脚本不会自动续期。不要把 K3s 管理员 kubeconfig 交给 Runner。如果 Runner 位于另一台机器，集群地址必须可达且包含在 API Server 证书中。参见 [K3s 集群访问说明](https://docs.k3s.io/cluster-access) 和 [`kubectl create token` 文档](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_create/kubectl_create_token/)。

在 GitHub 打开 `$CONTROL_REPOSITORY` 指定的仓库，进入 **Settings → Actions → Runners → New self-hosted runner**。按页面上的 Linux x64 说明，在 K3s 机器的独立目录中安装 Runner，并在提示时添加 `previewmesh` 标签。Runner 必须注册到这个 control 仓库，因为 `local` Job 会在这里排队；注册到另一个 private 仓库的 Runner 无法接收这个 Job。确认 Runner 用户可以使用 Go、Python、Helm 和 `kubectl`。注册完成后，将 Runner 安装为服务，并明确设置受限 kubeconfig。以下命令在 Runner 目录中执行，当前终端应保留前面的项目变量。如果服务已经安装，跳过 `svc.sh install`，并在再次启动前执行 `sudo ./svc.sh stop`。

```bash
# 将 GitHub Runner 安装为系统服务。
sudo ./svc.sh install "$(id -un)"
# 读取 Runner 安装器创建的 systemd 服务名称。
RUNNER_SERVICE=$(cat .service)
# 创建保存 Runner 配置覆盖项的 systemd drop-in 目录。
sudo mkdir -p "/etc/systemd/system/${RUNNER_SERVICE}.d"
# 指定 Runner 服务使用受限 kubeconfig。
printf '[Service]\nEnvironment="KUBECONFIG=%s"\n' "$PREVIEWMESH_RUNNER_CONFIG" \
  | sudo tee "/etc/systemd/system/${RUNNER_SERVICE}.d/previewmesh.conf" >/dev/null
# 添加 Runner 环境覆盖项后重新加载 systemd。
sudo systemctl daemon-reload
# 启动 GitHub Runner 服务。
sudo ./svc.sh start
```

回到刚才在 private control 仓库中设置项目变量的那个终端，在那里执行下面的命令，检查 Runner 是否上线并带有所需标签。如果那个终端已经关闭，先按[项目变量设置](#一次设置项目变量)重新设置变量；仅切换目录不会恢复这些变量。

```bash
# 在刚才的 Control 仓库终端中，确认目标仓库名称。
printf 'CONTROL_REPOSITORY=%s\n' "$CONTROL_REPOSITORY"
# 列出 private control 仓库中的 Runner 及其标签。
gh api "repos/$CONTROL_REPOSITORY/actions/runners" \
  --jq '.runners[] | {name, status, labels: [.labels[].name]}'
```

输出中应能看到 Runner 的 `status: online`，以及 `self-hosted`、`Linux`、`X64`、`previewmesh` 四个标签。上面的 `export` 只影响当前终端及其子进程：前台运行时，从这个终端启动 `./run.sh`；作为服务运行时，在 Runner 服务环境中将 `KUBECONFIG` 设为提示中输出的文件绝对路径，然后重启服务。服务用户必须能够读取该文件并访问它的各级父目录。

新终端中的恢复方法见[项目变量设置](#一次设置项目变量)。详见 [Kubernetes Runner 说明](../kubernetes/README.md)。

### 6. 配置 GitHub Secrets

配置脚本负责上传 Token，不会自动生成。如果只登记了一个 source 仓库，请先按下面的步骤创建三个 Token，再运行脚本。每增加一个 source 仓库，需要多创建一个对应的 source Token。记下到期时间，到期前更新对应的 Secret。

| Secret 名称 | Token 可以访问的资源 | 所需权限 | Secret 保存位置 |
| --- | --- | --- | --- |
| 登记文件中的 `source_secret`，例如 `SOURCE_APP` | 该条登记对应的 source 仓库 | Contents: Read-only；Metadata: Read-only；Pull requests: Read and write；Commit statuses: Read and write | Control 仓库 |
| `PREVIEWMESH_DISPATCH_TOKEN` | Control 仓库 | Actions: Read and write；Metadata: Read-only | 每个已登记的 source 仓库 |
| `GHCR_READ_TOKEN` | 存放预览镜像的 Package | 使用 **classic** PAT，勾选 `read:packages`；Token 所属用户需要有这些 Package 的读取权限 | Control 仓库 |

`SOURCE_APP` 只是 Secret 名称，不需要因此创建 GitHub App。每个 source 仓库分别创建一个 source Token。Dispatch Token 用来调用 `workflow_dispatch`，需要 [Actions 写权限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。GHCR 的这种登录方式需要 [classic PAT](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)，不能用 fine-grained Token 替代。如果组织要求审批 Token 或完成 SSO 授权，先完成这些操作。

#### 创建 source Token

用途：让 Control 工作流读取 Source 仓库的代码和 PR 信息，并向 Source PR 回写状态和预览链接。保存在 Control 仓库，Secret 名称使用登记的名称，例如 `SOURCE_APP`。

打开 [fine-grained Token 创建页面](https://github.com/settings/personal-access-tokens/new)，也可以从个人账号的 **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token** 进入。

1. **Token name** 填写方便识别的名称，例如 `previewmesh-source`。这个名称只是给你自己看的，不必与 Actions Secret 名称一致。
2. 在 **Expiration** 中选择有效期。
3. **Resource owner** 选择 source 仓库的所有者。
4. **Repository access** 选择 **Only select repositories**，然后选中 `SOURCE_REPOSITORY` 指定的 **Source 仓库（存放应用代码的仓库）**。这里不要选择 Control 仓库。
5. 在 **Repository permissions** 中设置 **Contents: Read-only**、**Pull requests: Read and write** 和 **Commit statuses: Read and write**。保留 **Metadata: Read-only**，GitHub 通常会自动添加它。
6. 点击 **Generate token**，将生成的值复制到密码管理器中，稍后粘贴给脚本。不要把它写进 `config/repositories.json`。

如果登记文件中填写的是 `source_secret: SOURCE_APP`，就在脚本提示 `Paste SOURCE_APP for OWNER/APPLICATION` 时粘贴这个值。如果使用了其他 Secret 名称，提示也会使用对应名称。每个已登记的 source 仓库分别创建一个 Token。参见 [GitHub Token 创建指南](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)。

#### 创建 dispatch Token

用途：让 Source 仓库的通知工作流在 PR 变化时触发 Control 工作流。保存在每个 Source 仓库，Secret 名称为 `PREVIEWMESH_DISPATCH_TOKEN`。

再次打开 [fine-grained Token 创建页面](https://github.com/settings/personal-access-tokens/new)。

1. **Token name** 填写 `previewmesh-dispatch`，并选择 **Expiration** 有效期。
2. **Resource owner** 选择 control 仓库的所有者。
3. 在 **Repository access → Only select repositories** 中，选中 `previewmesh-control`；如果你的 control 仓库用了其他名称，就选那个仓库。
4. 在 **Repository permissions** 中设置 **Actions: Read and write**，并保留 **Metadata: Read-only**。
5. 点击 **Generate token**，将生成的值保存到密码管理器中。

在脚本提示 `Paste PREVIEWMESH_DISPATCH_TOKEN (control Actions write only)` 时粘贴这个值。脚本会把同一个 dispatch Token 保存到每个已登记的 source 仓库，让它们能够触发 control 工作流。

#### 创建 GHCR 读取 Token

用途：让 K3s 从 GHCR 下载私有预览镜像来运行应用。保存在 Control 仓库，Secret 名称为 `GHCR_READ_TOKEN`。

打开 [classic Token 创建页面](https://github.com/settings/tokens/new)，也可以从 **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)** 进入。

1. **Note** 填写 `previewmesh-ghcr-read`，并选择 **Expiration** 有效期。
2. 在权限范围中勾选 **`read:packages`**。这个 Token 只用于拉取镜像，不需要勾选 `write:packages` 或 `delete:packages`。
3. 点击 **Generate token**，将生成的值保存到密码管理器中。

在脚本提示 `Paste GHCR_READ_TOKEN (classic PAT, read:packages only)` 时粘贴这个值。创建 Token 的用户必须有预览镜像 Package 的读取权限；勾选权限范围不会自动授予其他用户私有 Package 的访问权。如果组织要求 SSO 授权，也需要完成。参见 [GHCR 认证说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。

#### 上传并检查 Secrets

回到 control 项目目录，复用前面设置的仓库变量。`SOURCE_REPOSITORY` 应与第 3 步的登记一致。当前 `gh` 登录账号需要有两个仓库的 Actions Secret 管理权限：它负责上传 Secret，你稍后粘贴的 Token 则供工作流运行时使用。

```bash
# 进入 private control checkout。
cd "$CONTROL_DIR"
# 确认 GitHub CLI 已登录。
gh auth status
# 上传 source、dispatch 和 GHCR Actions Secret。
bash scripts/configure-github-secrets.sh "$CONTROL_REPOSITORY" config/repositories.json
```

脚本会依次询问每个 source Token、dispatch Token 和 GHCR Token。粘贴时终端不会显示内容，粘贴后按 Enter 即可。脚本将它们保存为仓库级 Actions Secret；如果上传失败，会停止执行，已经保存的值会保留。重新运行脚本会覆盖这些值。

查看保存的名称，不会显示 Token 内容：

```bash
# 列出 private control 仓库中的 Secret 名称。
gh secret list --repo "$CONTROL_REPOSITORY" --app actions
# 列出 source 仓库中的 dispatch Secret 名称。
gh secret list --repo "$SOURCE_REPOSITORY" --app actions
```

Control 仓库应包含所有登记的 `source_secret` 和 `GHCR_READ_TOKEN`；每个 source 仓库应包含 `PREVIEWMESH_DISPATCH_TOKEN`。如果登记了多个 source，分别执行第二条命令检查。这一步只能确认保存成功，实际访问权限还需要通过首次预览验证。也可以在 **Repository Settings → Secrets and variables → Actions → Repository secrets** 中管理它们。

#### 镜像包归属

每个 source 的镜像发布为 `ghcr.io/OWNER/previewmesh-cCONTROL_REPOSITORY_ID-rSOURCE_REPOSITORY_ID`。工作流从 GitHub 获取当前 control 仓库 ID，从已验证的注册信息获取 source 仓库 ID，使用自身的 `GITHUB_TOKEN` 创建并发布属于当前 control 仓库的私有镜像包。`GHCR_READ_TOKEN` 用于让 K3s 拉取该镜像，无需手动创建 Package。

### 7. 配置 Ingress 和 source 通知

预览域名由 source 仓库 ID 和 PR 编号组成，例如 `pm-r123456789-pr12.preview.test`。Runner 和浏览器都需要通过这个域名访问 Traefik。`.test` 域名不会自动解析；第 8 步拿到 PR 编号后，再添加对应的 hosts 记录。

#### 配置 HTTP 入口

PreviewMesh 的预览链接和健康检查统一使用主机端口 `18080`。代理连接 Traefik 时仍使用集群内部的 `80` 端口；`config/repositories.json` 中的应用端口是另一项配置。Hosts 记录只填写域名，不要加端口。

安装代理前，在 WSL 中执行 `ss -ltn 'sport = :18080'`，在 Windows PowerShell 中执行 `Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue`。两边都不应显示监听记录。如果端口已被占用，先处理冲突再继续。

如果使用普通 Linux 服务器，且 Traefik 已经可以通过服务器的 18080 端口访问，后续使用该服务器可访问的 IP 配置 DNS 或 hosts 即可，可以直接继续下面的通知配置。

如果 K3s 运行在 WSL 中，浏览器在 Windows 上，可以使用项目提供的 socket 代理，将 WSL 回环地址的 18080 端口转发到 Traefik。这要求 WSL 已启用 systemd，存在 `/usr/lib/systemd/systemd-socket-proxyd`，本地 18080 端口空闲，并且 [Windows 可以通过 localhost 访问 WSL](https://learn.microsoft.com/en-us/windows/wsl/networking)。如果下面的可执行文件检查失败，请先安装发行版中提供 `systemd-socket-proxyd` 的软件包。

在 WSL 的 control 项目目录中执行。这里显式使用管理员 kubeconfig，因为受限的 Runner 无权修改 Traefik：

```bash
cd "$CONTROL_DIR"
test -x /usr/lib/systemd/systemd-socket-proxyd
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml apply -f ops/kubernetes/traefik-helmchartconfig.yaml
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n kube-system get service traefik -w
```

这个 HelmChartConfig 会让 Traefik 的 Service 保持为集群内部服务，关闭 `publishedService` 地址同步，并在 Ingress 状态中发布 `127.0.0.1`。ClusterIP Service 没有可同步的外部地址；如果保留 `publishedService`，Ingress 状态会一直为空，导致就绪检查无法通过。PreviewMesh 用这个状态判断路由是否就绪；实际请求仍通过 socket 代理的 `18080` 端口到达 Traefik。等到 `TYPE` 显示为 `ClusterIP` 且 `CLUSTER-IP` 出现地址后，按 Ctrl+C 结束观察。然后确认 Traefik 已应用 chart 配置：

```bash
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n kube-system rollout status deployment/traefik --timeout=120s
```

再次读取地址，并创建本地代理配置文件：

```bash
sudo install -d -m 0755 /etc/previewmesh
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n kube-system get service traefik \
  -o jsonpath='{.spec.clusterIP}{"\n"}'
sudoedit /etc/previewmesh/ingress.env
```

在编辑器中写入下面这一行，将 `TRAEFIK_CLUSTER_IP` 替换为刚才输出的地址，保存后退出：

```text
PREVIEWMESH_TRAEFIK_ENDPOINT=TRAEFIK_CLUSTER_IP:80
```

安装并启动 socket，然后检查转发是否正常：

```bash
sudo install -m 0644 ops/wsl/previewmesh-ingress.socket ops/wsl/previewmesh-ingress.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now previewmesh-ingress.socket
curl --noproxy '*' --silent --show-error --max-time 5 -o /dev/null -w '%{http_code}\n' \
  -H 'Host: previewmesh-ingress-check.invalid' http://127.0.0.1:18080
```

预期输出是 `404`：请求已经到达 Traefik，但这个检查域名没有对应的预览。在 Windows PowerShell 中执行 `curl.exe --noproxy "*" -I http://127.0.0.1:18080`，确认 Windows 也能访问 Traefik。如果 Linux 检查失败，查看 `sudo journalctl -u previewmesh-ingress.service -n 30 --no-pager`；如果只有 Windows 失败，先检查 WSL 网络设置。`/etc/previewmesh/ingress.env` 只保存在本机。如果 Traefik 的 ClusterIP 改变，修改该文件，再执行 `sudo systemctl restart previewmesh-ingress.service`。

#### 添加 source 通知工作流

下面复用前面设置的两个项目路径，将模板复制到存放应用代码的 **source 仓库**：

```bash
cd "$SOURCE_DIR"
git remote -v
mkdir -p .github/workflows
cp -i "$CONTROL_DIR/templates/source-notify.yml" .github/workflows/previewmesh-notify.yml
```

确认 `git remote -v` 显示的是目标 source 仓库。如果已有同名工作流，先检查内容，再决定是否覆盖。用编辑器打开 `.github/workflows/previewmesh-notify.yml`，修改 `jobs.notify.env` 下的两个值：

```yaml
PREVIEWMESH_CONTROL_OWNER: YOUR_GITHUB_OWNER
PREVIEWMESH_CONTROL_REPOSITORY: previewmesh-control
```

Owner 是 `CONTROL_REPOSITORY` 中 `/` 前面的部分，repository 是后面的部分。按照你的正常代码审核流程，将文件提交并合并或推送到 source 仓库的默认分支。在该仓库启用 Actions，并确认第 6 步已经保存 `PREVIEWMESH_DISPATCH_TOKEN`，然后再创建测试 PR。每个已登记的 source 仓库都需要配置一次。

PR 打开、重新打开、收到新提交（`synchronize`）或关闭时，通知工作流都会发送请求。它会跳过 Fork PR，只传递元数据；不要在这个 `pull_request_target` Job 中加入检出或执行 PR 代码的步骤。Control 工作流会在部署前后检查当前 PR head，如果已经有更新的提交，就不会将旧版本报告为 ready。

### 8. 运行预览

在 source checkout 中，从 source 仓库的默认分支创建一个新分支。这里使用开头已设置的 `SOURCE_DEFAULT_BRANCH`。如果你已经有一个推送到 GitHub、并包含目标应用改动的分支，可以直接使用，跳过下面这段。

```bash
cd "$SOURCE_DIR"
git status --short
git fetch origin "$SOURCE_DEFAULT_BRANCH"
git switch -c preview/demo "origin/$SOURCE_DEFAULT_BRANCH"
```

如果 `git status --short` 列出了尚未提交的改动，请先提交或暂存，再切换分支。然后修改你希望 PreviewMesh 构建的应用文件，再提交并推送。将 `path/to/changed-file` 替换为相对于 source 项目目录的实际文件路径：

```bash
git add -- path/to/changed-file
git commit -m "Add preview demo change"
git push -u origin preview/demo
```

在 GitHub 选择 **Pull requests → New pull request**。**base** 选 source 默认分支（本例为 `main`），**compare** 选 `preview/demo`。确认左右两侧显示的是同一个 source 仓库，再创建 PR。创建者需要有仓库写权限。PR 会告诉 PreviewMesh 要构建哪个应用版本。第 7 步的通知工作流已进入 source 默认分支且 Actions 已启用后，打开 PR 就会自动发送预览请求。

回到 control 项目目录，复用前面设置的仓库变量，把 `123` 换成实际 PR 编号。即使工作流自动启动，后面的检查也需要这些变量：

```bash
cd "$CONTROL_DIR"
export PR_NUMBER=123
export PREVIEW_NAMESPACE="pm-r${REPOSITORY_ID}-pr${PR_NUMBER}"
export PREVIEW_HOST="${PREVIEW_NAMESPACE}.preview.test"
go run ./cmd/control resolve \
  --repository-id "$REPOSITORY_ID" \
  --source-repository "$SOURCE_REPOSITORY" \
  --pr "$PR_NUMBER"
gh api "repos/$SOURCE_REPOSITORY/pulls/$PR_NUMBER" \
  --jq '{state: .state, base: .base.repo.full_name, head: .head.repo.full_name, sha: .head.sha}'
printf 'Preview hostname: %s\n' "$PREVIEW_HOST"
```

确认 `resolve` 接受该登记，PR 状态为 `open`，且 `base` 和 `head` 都是你的 source 仓库。让 Runner 和浏览器所在的机器都能解析输出的域名。如果采用 WSL localhost 方案，按下面的格式填写 hosts，域名使用你刚才得到的实际值：

```text
127.0.0.1 pm-r123456789-pr12.preview.test
```

在 WSL 中用 `sudoedit /etc/hosts` 编辑；在 Windows 中以管理员身份打开编辑器，修改 `C:\Windows\System32\drivers\etc\hosts`。如果使用远程 Linux 服务器，将 `127.0.0.1` 换成可访问 Traefik 的服务器 IP。每个预览需要一条 hosts 记录，hosts 文件不支持通配域名。

先在 source 仓库的 Actions 页面查看 **Notify PreviewMesh**，再到 control 仓库查看 **PreviewMesh lifecycle**。通知成功只代表请求已经发出，要等 control 工作流完成后再检查应用。

如果安装通知工作流时 PR 已经存在，或者首次运行因网络尚未配置好而失败，可以手动启动一次。先等该 PR 已有的运行结束，再执行：

```bash
gh workflow run preview.yml --repo "$CONTROL_REPOSITORY" --ref main \
  -f repository_id="$REPOSITORY_ID" \
  -f source_repository="$SOURCE_REPOSITORY" \
  -f pr_number="$PR_NUMBER"
gh run list --repo "$CONTROL_REPOSITORY" --workflow preview.yml --limit 5
```

从列表中复制本次运行的 ID，替换下面的示例。如果新运行还没出现，再执行一次列表命令。核对运行页面上的 source 和 PR 编号；每次跟踪新的运行时，都更新 `RUN_ID`：

```bash
export RUN_ID=123456789
gh run view "$RUN_ID" --repo "$CONTROL_REPOSITORY" --web
gh run watch "$RUN_ID" --repo "$CONTROL_REPOSITORY" --exit-status
```

Control 工作流成功后，获取 PR 当前 head 并核对线上响应。执行检查的机器需要能够解析和访问预览域名。每次推送新提交后，都重新执行下面的检查，以刷新 `EXPECTED_SHA`：

```bash
EXPECTED_SHA=$(gh api "repos/$SOURCE_REPOSITORY/pulls/$PR_NUMBER" --jq '.head.sha')
export EXPECTED_SHA
curl --noproxy '*' --fail --silent --show-error --max-time 10 "http://${PREVIEW_HOST}:18080/health" \
  | python3 -c '
import json, os, sys
result = json.load(sys.stdin)
if result.get("status") != "ok" or result.get("commit_sha") != os.environ["EXPECTED_SHA"]:
    raise SystemExit(f"Preview does not match the PR head: {result}")
print(json.dumps(result))
'
```

检查通过会输出 `status: ok` 和预期的 `commit_sha`。如果应用有页面，可以打开 `http://${PREVIEW_HOST}:18080`。测试结束后关闭或合并 PR，等待清理运行完成；下面给出清理确认方法。

## 端到端演示

完成首次配置后，可以复用第 8 步的 PR 和 hosts 记录，展示创建、更新和清理。选择你有权关闭的 PR；base 和 head 都必须属于已登记的 source 仓库，作者需要有写权限。演示期间不要为同一 PR 并行启动另一次部署。

### 准备演示

复用已有的项目变量和 PR 变量。如果换了 PR，回到[第 8 步](#8-运行预览)更新 PR 编号、Namespace 和域名，并检查登记与 PR 状态。如果新开了终端，先按[项目变量设置](#一次设置项目变量)恢复环境。确认 Runner 和浏览器都能解析预览域名。

### 启动并跟踪工作流

创建 PR 或向已有 PR 推送新提交，会触发 source 仓库中的 **Notify PreviewMesh**，再触发 control 工作流。如果要复用已有提交，执行第 8 步的手动触发命令；它会跳过 source 通知，运行相同的预览流程。使用第 8 步的跟踪命令，把 `RUN_ID` 更新为本次运行的 ID，再等待完成。

Actions 页面中，`build` 验证登记和 PR 并发布镜像，`local` 在 K3s 部署并检查响应，`report` 汇总结果。`combined-*` artifact 包含摘要和 CSV 运行记录。如果 source Token 具有所需写权限，PR 还会显示 PreviewMesh 状态和指向本次运行的结果评论。

### 核对展示的代码版本

重新执行[第 8 步](#8-运行预览)的健康检查，获取最新 `EXPECTED_SHA` 并核对响应。HTTP 200 还不够，响应中的 `status` 必须为 `ok`，`commit_sha` 必须与预期一致。检查通过后，可以打开 `http://${PREVIEW_HOST}:18080` 展示应用页面。

要演示版本更新，先等当前运行结束，再向同一个 PR 推送新提交，等待 `synchronize` 通知和 control 工作流完成，然后重新检查。URL 和 Namespace 保持不变。

### 展示自动清理

关闭或合并 demo PR，然后跟踪 `closed` 通知启动的 control 工作流。等清理运行完成后再检查，不要手动删除 Namespace，否则无法确认自动清理是否正常。在 K3s 机器上选择首次配置步骤 5 中的受限 runner kubeconfig，然后确认 Namespace 已不存在：

```bash
kubectl --kubeconfig "$PREVIEWMESH_RUNNER_CONFIG" get namespace "$PREVIEW_NAMESPACE" --ignore-not-found
```

命令成功退出且没有输出，才表示 Namespace 已删除。认证失败或连接错误不能作为清理成功的依据。如果使用了不同的 kubeconfig 路径，这里也要修改；如果新开了终端，还需要从第 8 步恢复 `PREVIEW_NAMESPACE`。要演示清理的幂等性，可以对已关闭的 PR 再手动派发一次相同工作流；报告应确认 Namespace 已经不存在。
