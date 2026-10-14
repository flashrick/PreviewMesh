# PreviewMesh

[English](README.md)

PreviewMesh 为符合条件的拉取请求提供一个位于私有开发网络中的临时应用 URL。它构建准确的源代码提交，发布不可变镜像，将镜像部署到 K3s，验证 `/health` 和应用返回的提交 SHA，将结果回写到拉取请求，并在拉取请求关闭后删除预览。

> [!WARNING]
> 这是公开的 PreviewMesh 代码仓库。由它创建的部署管理仓库必须保持私有。不要在这里提交 GitHub Token、kubeconfig、真实仓库注册信息、私有 URL 或运行时证据。

## 快速导航

- [你将获得什么](#你将获得什么)
- [工作原理](#工作原理)
- [前置条件](#前置条件)
- [安装](#安装)
- [使用预览](#使用预览)
- [运维与排错](#运维与排错)
- [高级参考](#高级参考)

## 你将获得什么

| 能力 | 行为 |
| --- | --- |
| 准确版本 | 构建拉取请求的完整提交 SHA，并使用不可变镜像摘要部署。 |
| 隔离预览 | 为每个已注册仓库和拉取请求创建可预测、带所有权标记的 Kubernetes Namespace 和 Release。 |
| 可信准入 | 校验已注册的仓库 ID 和名称、当前拉取请求状态、同一仓库的 head/base，以及作者的写权限。Fork 拉取请求会被拒绝。 |
| 运行时验证 | 等待工作负载和 Ingress 就绪，然后要求 `/health` 返回 HTTP 200、`status: "ok"` 和预期的 `commit_sha`。 |
| 拉取请求反馈 | 写入 Commit Status；在权限允许时写入结果评论；验证成功后提供 **Open preview** URL。 |
| 清理 | 拉取请求关闭或合并后删除所属预览，并提供检查和有限重试路径用于恢复。 |

## 工作原理

PreviewMesh 使用三类相互分离的仓库角色：

| 仓库角色 | 职责 | 可见性 |
| --- | --- | --- |
| PreviewMesh 代码仓库 | 可复用 Go 代码、Helm Chart、工作流、安装脚本和公开文档。 | 公开 |
| 部署管理仓库 | 可信工作流、应用注册信息、Actions Secrets、Runner 配置和私有控制平面。 | 私有 |
| 应用仓库 | 应用代码、根目录的 `Dockerfile`、`/health` 和通知工作流。 | 公开或私有 |

公开仓库是创建或更新私有部署管理仓库的代码来源，不是控制平面。不要把应用仓库填写为部署管理仓库。

~~~mermaid
flowchart LR
    A[Open or update PR in application repository] --> B[Notification workflow]
    B -->|Repository ID, name, PR number| C[Private deployment-management workflow]
    C --> D[Validate registration, PR and permissions]
    D --> E[Checkout exact source SHA]
    E --> F[Build and push immutable GHCR digest]
    F --> G[Deploy to K3s on restricted runner]
    G --> H[Verify readiness, /health and served SHA]
    H --> I[Commit status, PR comment and preview URL]
    A2[Close or merge PR] --> J[Remove owned namespace]
~~~

重要边界：

- 应用仓库的通知工作流只转发元数据，不会检出或执行拉取请求代码。
- 只有私有部署管理仓库中注册的应用才会被接受。
- 私有仓库设置 `PREVIEWMESH_ENABLED=true` 之前，预览工作流保持禁用。
- 自托管 Runner 必须在专用开发集群中使用受限 kubeconfig。不要给它 cluster-admin kubeconfig。
- 预览 URL 只能从私有网络访问，不是公开互联网托管。

## 前置条件

引导式安装器支持 **Ubuntu 22.04 或 24.04 x64**，包括使用 **WSL2 systemd** 的这些发行版。请使用有 `sudo` 权限的普通 Linux 用户、专用开发 K3s 集群和稳定的私有局域网 IPv4 地址。WSL 安装还需要 Windows 管理员权限来配置局域网入口。

你需要：

- 能访问安装涉及的 GitHub 仓库；
- 按安装器或接入流程的要求，使用已认证的 `gh` CLI；
- 为公开代码、私有部署管理 checkout 和每个应用 checkout 准备互不重叠的本地目录；
- 将 Token 文件放在 Git checkout 外，并限制为文件所有者可读。

### 应用契约

每个已注册应用都必须提供：

| 要求 | 契约 |
| --- | --- |
| 镜像 | 根目录中的 `Dockerfile`，并能构建 `linux/amd64` 镜像。 |
| 网络 | 应用在注册的 HTTP 端口上监听 `0.0.0.0`。 |
| 运行时 | 镜像兼容 UID/GID `65532`，不依赖额外的 Linux capability。 |
| 健康接口 | `GET /health` 返回 HTTP 200，并在 JSON 中包含 `status: "ok"` 和运行时的 `PREVIEW_COMMIT_SHA`。 |

例如：

~~~json
{"status":"ok","commit_sha":"the value of PREVIEW_COMMIT_SHA"}
~~~

预检可以检查 Dockerfile，也可以选择运行临时本地容器。这些检查不能证明集群部署、Ingress 可达性或应用级测试通过。第一个真实拉取请求才会验证完整链路。

## 安装

在公开 PreviewMesh checkout 中运行下面的命令。公开 checkout、私有部署管理 checkout 和应用 checkout 不能互相包含或重叠。

### 1. 创建配置

向导默认把活动配置保存到 `~/.config/previewmesh/setup.ini`。它保存 Token 文件路径，不保存 Token 内容。

~~~bash
bash scripts/setup.sh init
~~~

如需使用其他路径，请显式指定：

~~~bash
bash scripts/setup.sh init --config /path/to/setup.ini
~~~

向导会询问私有部署管理仓库、其本地 checkout、公开 PreviewMesh 仓库、应用仓库和端口、Token 文件路径、私有局域网地址、允许网段、预览域名后缀和输出语言。请保持所有仓库角色和本地目录分离。

如果只需要带注释的模板而不启动交互流程：

~~~bash
bash scripts/setup.sh init --template --config /path/to/setup.ini
~~~

不要把填写后的配置或 Token 内容放入本仓库。

### 2. 安装控制平面

~~~bash
bash scripts/setup.sh install --config /path/to/setup.ini
~~~

`install` 会准备工具、私有部署管理仓库、GitHub Secrets、K3s 权限、受限 Runner、预览网络入口、应用通知集成和就绪检查。安装支持断点续跑；某个阶段失败时，修复提示的问题后重新运行同一条命令。

安装器负责准备基础设施，不会改写应用代码，也不能证明应用已经能够提供预览。[手动安装参考](ops/install/manual.md) 解释了不适合使用引导式流程时的各个基础设施步骤。

### 3. 通过审核 PR 接入应用

先预览工作流变更，不写入应用仓库：

~~~bash
bash scripts/setup.sh onboard-source --config /path/to/setup.ini --source OWNER/REPO
~~~

审核完整 diff 后，添加 `--create-pr` 创建专用的接入拉取请求：

~~~bash
bash scripts/setup.sh onboard-source --config /path/to/setup.ini --source OWNER/REPO --create-pr
~~~

该命令只准备 `.github/workflows/previewmesh-notify.yml`，不会合并 PR，也不会修改应用代码。请在创建测试预览前审核并合并应用默认分支上的这个 PR。

### 4. 创建第一个预览

1. 在已注册的应用仓库中从分支创建普通拉取请求，目标为应用默认分支。不要使用 fork。
2. 等待应用通知工作流和私有部署管理工作流完成。
3. 确认构建、部署、就绪、健康检查和提交版本校验均成功。
4. 在能够访问配置的私有网络的机器上打开生成的 URL。请阅读[预览访问说明](ops/install/access.md)了解 DNS、hosts 文件、WSL、`18080` 端口和 `/health` 检查。
5. 关闭或合并拉取请求，等待清理工作流删除对应的预览 Namespace。

## 使用预览

正常的开发循环是：

~~~text
推送分支 -> 创建同仓库 PR -> 等待部署 -> 查看私有 URL -> 关闭或合并 -> 确认清理
~~~

在私有部署管理 checkout 中，可以构建只读的状态查询命令：

~~~bash
go build -o /tmp/previewmesh ./cmd/previewmesh
gh auth status
/tmp/previewmesh status --repo OWNER/REPO --pr 123
~~~

自动化场景使用结构化输出：

~~~bash
/tmp/previewmesh status --repo OWNER/REPO --pr 123 --json
~~~

`status` 通过已认证的 `gh` CLI 读取当前拉取请求状态、`PreviewMesh` Commit Status 和结构化工作流反馈。它不会探测预览 URL，不会查询 Kubernetes，也不会执行实时健康检查。`ready` 表示当前提交有完整且较新的证据；拉取请求关闭后，`removed` 表示已有清理证据。

HTTP 200 并不等于验证成功。应用验证还必须要求 `status: ok`，并且 `commit_sha` 与本次请求的准确源代码版本一致。

## 运维与排错

### 检查和恢复

下面的 setup 命令只执行读取检查：

~~~bash
# Read configuration and inspect prerequisites.
bash scripts/setup.sh check --config /path/to/setup.ini

# Inspect installed services, credentials, DNS and GitHub setup.
bash scripts/setup.sh doctor --config /path/to/setup.ini
~~~

`--config` 是 setup 脚本的选项，用来选择配置文件；它不会创建独立的安装状态。`doctor` 报告基础设施就绪情况和待接入工作，但 `check` 和 `doctor` 都不能证明应用预览成功。

如果必须重置旧安装，请使用可选择的清理菜单，并在确认前逐项审核所有破坏性类别：

~~~bash
bash scripts/setup.sh cleanup --config /path/to/setup.ini
~~~

### 配置与凭据边界

| 位置 | 用途 | 处理方式 |
| --- | --- | --- |
| 公开 PreviewMesh checkout | 可复用代码、Chart、工作流、模板和安全示例。 | 可公开；不得加入真实注册信息、凭据或运行时证据。 |
| 私有部署管理 checkout | 可信工作流和 `config/repositories.json` 注册信息。 | 必须保持私有，只在这里提交注册信息。 |
| `~/.config/previewmesh/setup.ini` | 每个用户的仓库、目录、网络、端口和 Token 路径设置。 | 放在 Git 外并限制所有者访问；只保存引用，不保存 Token 内容。 |
| `~/.local/share/previewmesh/install-state.json` | 每个账号的安装断点和所有权状态。 | 放在 Git 外并限制所有者访问；不要为了绕过控制仓库不匹配而删除。 |
| Token 文件 | 应用、派发和 GHCR 凭据。 | 放在 Git 外，并设置为仅所有者可读。 |

应用工作流使用 `PREVIEWMESH_DISPATCH_TOKEN` 通知私有部署管理仓库。私有仓库存放应用仓库 Secrets 和 `GHCR_READ_TOKEN`。安装器会说明所需权限和目标位置；不要把这些值复制到公开 checkout。

### 故障排查

| 现象 | 首先检查 |
| --- | --- |
| 没有应用通知 | 确认工作流已在应用默认分支、Actions 已启用、接入 PR 已合并。Fork PR 会被有意跳过。 |
| 部署任务被跳过 | 在私有部署管理仓库中将 `PREVIEWMESH_ENABLED=true`，并在 `main` 上派发工作流。 |
| `local` 任务一直排队 | 确认私有仓库的自托管 Runner 在线，并具有 `self-hosted`、`Linux`、`X64` 和 `previewmesh` 标签。 |
| 注册或授权失败 | 对照私有注册信息中的仓库 ID 和 owner/name。PR 必须属于该仓库，作者必须具有写权限。 |
| 镜像推送或拉取失败 | 检查构建工作流的 GitHub package 写权限、`GHCR_READ_TOKEN` 和预览 Namespace 使用的 `ghcr-pull` Secret。 |
| 就绪或 HTTP 验证失败 | 检查 Deployment、Pod、Service、Traefik、配置的 hostname 和 `/health` 响应。请使用[预览访问说明](ops/install/access.md)。 |
| `status` 没有可用 URL | 证据可能仍在等待、已经失败、已过期，或属于旧提交。检查最新工作流并重新运行预览。`status` 不会探测 URL。 |
| PR 关闭后预览仍存在 | 等待 closed-PR 通知和控制工作流。如果派发失败，修复后手动重试清理路径。 |
| WSL 访问中断 | 重新运行 `init` 确认当前 Windows 主机局域网地址，然后重新运行 `install`。查看[手动 WSL 和访问说明](ops/install/access.md)。 |

Runner 权限请看 [Kubernetes 访问说明](ops/kubernetes/README.md)。完整手动安装请看[手动安装参考](ops/install/manual.md)。生命周期和恢复细节请看[预览生命周期验证](ops/verification/preview-lifecycle.md)。

## 高级参考

<details>
<summary>直接 CLI 生命周期</summary>

推荐使用私有部署管理工作流，因为它会重新检查拉取请求状态，并处理过时版本和清理。受信任的控制环境也可以使用较低层级的 CLI 命令：

| 命令 | 用途 |
| --- | --- |
| `preflight` | 检查应用 checkout，也可以运行临时本地容器检查。 |
| `build` | 校验准确 checkout，并为控制工作流发布镜像。 |
| `deploy` | 将不可变镜像摘要部署到所属预览 Namespace。 |
| `verify` | 等待就绪，并验证 `/health` 和应用返回的版本。 |
| `cleanup` | 拉取请求关闭后删除所属预览。 |
| `cleanup-inspect` | 在不删除资源的情况下收集所有权和清理状态。 |
| `cleanup-retry` | 使用原始 Namespace 身份重试清理。 |

在私有控制 checkout 中执行 `previewmesh <command> --help` 查看准确参数。不要在公开文档或 shell 历史中传递私有凭据。

</details>

<details>
<summary>更新私有部署管理 checkout</summary>

请在私有 checkout 中运行更新，而不是在公开源代码目录中运行。先审核或暂存本地改动：

~~~bash
git switch main
git pull --ff-only origin main
git status --short
scripts/update-upstream.sh --remote upstream --ref main
~~~

推送更新分支或创建 PR 前，先审核变更并运行本地检查。更新器会保留私有仓库的应用注册信息，不会自动更新已经发布到应用仓库的通知工作流。合并更新后，如果本机管理文件需要刷新，请重新运行 `install`。

</details>

<details>
<summary>本地检查</summary>

在受信任的控制 checkout 中运行：

~~~bash
scripts/verify-local.sh
~~~

它会检查 Go 代码、工作流和 setup fixtures、Helm Chart 以及 Shell/Python 校验。它不会部署应用，也不能证明外部 GitHub、GHCR、K3s、Runner、网络或应用凭据可用。

</details>

## 相关文档

- [预览访问](ops/install/access.md) — DNS、hosts 文件、WSL/LAN 访问、`18080` 端口和 `/health` 验证。
- [手动安装](ops/install/manual.md) — 不适合使用引导式安装时的基础设施流程。
- [Kubernetes Runner 访问](ops/kubernetes/README.md) — 受限 kubeconfig 和 RBAC 边界。
- [预览生命周期验证](ops/verification/preview-lifecycle.md) — 运行生命周期和清理检查。
