# PreviewMesh

<div align="center">
  <p><strong>面向拉取请求的私有预览：精确提交验证、K3s 隔离和自动清理。</strong></p>
  <p>
    <a href="README.md">English</a> ·
    <a href="#快速开始">快速开始</a> ·
    <a href="#工作原理">工作原理</a> ·
    <a href="#验证改动">验证改动</a>
  </p>
  <p>
    <a href="go.mod"><img src="https://img.shields.io/badge/Go-1.25%2B-00ADD8?logo=go&logoColor=white" alt="Go 1.25 或更高版本"></a>
    <a href="charts/preview/Chart.yaml"><img src="https://img.shields.io/badge/Helm-3-0F1689?logo=helm&logoColor=white" alt="Helm 3"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT 许可证"></a>
  </p>
  <img src="assets/previewmesh-flow.svg" alt="PreviewMesh 工作流：符合条件的拉取请求经过精确提交、不可变镜像、隔离的 K3s 预览、健康响应验证，最后完成环境清理。" width="100%">
</div>

PreviewMesh 让每个 Pull Request 都拥有一个可访问的本地预览环境：每次 PR 更新都会自动构建对应版本，并在本地开发集群中生成一个隔离、仅限内网访问的专属预览链接，让产品、测试和业务同学无需等待人工搭建测试环境即可提前体验和验证功能；验证完成后环境自动清理，减少维护成本和资源浪费，让研发反馈更快、更安全。

PreviewMesh 为符合条件的拉取请求提供一个位于私有开发网络中的临时应用 URL。它构建准确的源代码提交，发布不可变镜像，将镜像部署到 K3s，验证 `/health` 和应用返回的提交 SHA，将结果回写到拉取请求，并在拉取请求关闭后删除预览。

> [!WARNING]
> 这是公开的 PreviewMesh 代码仓库。由它创建的部署管理仓库必须保持私有。不要在这里提交 GitHub Token、kubeconfig、真实仓库注册信息、私有 URL 或运行时证据。

## 快速导航

- [快速开始](#快速开始)
- [你将获得什么](#你将获得什么)
- [工作原理](#工作原理)
- [项目结构](#项目结构)
- [前置条件](#前置条件)
- [安装](#安装)
- [使用预览](#使用预览)
- [运维与排错](#运维与排错)
- [验证改动](#验证改动)
- [高级参考](#高级参考)
- [相关文档](#相关文档)

## 快速开始

使用引导式安装器创建或更新私有部署管理 checkout。在公开 PreviewMesh checkout 中运行：

~~~bash
bash scripts/setup.sh init
bash scripts/setup.sh install --config /path/to/setup.ini
~~~

公开 checkout、私有部署管理 checkout 和应用 checkout 必须放在互相分离的目录中。如果只是评估代码改动而不是安装基础设施，请直接运行[验证改动](#验证改动)。

## 你将获得什么

| 能力 | 行为 |
| --- | --- |
| 准确版本 | 构建拉取请求的完整提交 SHA，并使用不可变镜像摘要部署。 |
| 隔离预览 | 为每个已注册仓库和拉取请求创建可预测、带所有权标记的 Kubernetes Namespace 和 Release。 |
| 可信准入 | 校验已注册的仓库 ID 和名称、当前拉取请求状态、同一仓库的 head/base，以及作者的写权限。Fork 拉取请求会被拒绝。 |
| 运行时验证 | 等待工作负载和 Ingress 就绪，然后要求 `/health` 返回 HTTP 200、`status: "ok"` 和预期的 `commit_sha`。 |
| 拉取请求反馈 | 立即写入 pending 检查；PR 关闭时，在派发清理工作流前写入一条幂等的“清理进行中”评论；完成后再写入终态结果和验证成功时的 **Open preview** URL。 |
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

## 项目结构

| 路径 | 职责 |
| --- | --- |
| [`cmd/previewmesh/`](cmd/previewmesh/) | 负责预检、构建、部署、验证、状态和清理的生命周期 CLI。 |
| [`cmd/control/`](cmd/control/) | 负责受信任的仓库注册、拉取请求检查和 GitHub 回写。 |
| [`.github/workflows/preview.yml`](.github/workflows/preview.yml) | 构建、部署、验证并回写预览结果的控制平面工作流。 |
| [`templates/source-notify.yml`](templates/source-notify.yml) | 只转发元数据、通知私有控制仓库的应用工作流。 |
| [`charts/preview/`](charts/preview/) | 预览 Deployment、Service、Ingress 和 Namespace 级身份的 Helm Chart。 |
| [`scripts/`](scripts/) | 引导式安装、接入、维护、证据和本地验证工具。 |
| [`ops/kubernetes/`](ops/kubernetes/) | 开发集群的受限 Runner RBAC 和 Traefik 配置。 |

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

如果 `init` 检测到已有 PreviewMesh 安装记录或系统服务，会先显示痕迹并让你直接选择三个累积清理级别。选择的清理会立即执行，完成后才会启动新的配置向导。级别 1 只重置本地安装器设置和状态；级别 2 还会删除本机 PreviewMesh 服务和 Runner 文件，但保留 K3s 和远端 Runner 登记；级别 3 还会在通过路径和 origin 安全检查并明确确认删除仓库后，直接删除配置中的本地 control/source checkout，包括其中未提交、未跟踪和被忽略的文件。上述清理完成后，`init` 会检查 `~/.config/previewmesh/secrets/*.token`，并询问是否删除这些 Token 文件。K3s、GitHub 仓库、远端资源和新配置都会保留。如果没有选择级别 2 或 3，属于另一个 control repository 的旧 Runner 会继续运行，新安装使用独立的本地 Runner 目录。

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

在准备应用通知工作流的阶段，`install` 会处理应用本地的工作流文件，但不会发布应用改动：

- 如果 `.github/workflows/previewmesh-notify.yml` 不存在，就创建该文件。
- 如果文件已经是预期的当前模板，就保持不变。如果文件与需要更新配置值的精确当前或历史 PreviewMesh 模板（或此前由安装器管理的版本）匹配，就自动升级，并在旁边保存旧内容为 `previewmesh-notify.yml.previewmesh.bak`。
- 如果发现的是自定义工作流，`install` 会保留原文件，并在旁边生成完整的当前工作流 `previewmesh-notify.yml.previewmesh-proposed` 和 `previewmesh-notify.yml.previewmesh.patch`。自定义工作流需要审核，但不会使安装阶段失败。

用户不必逐行复制 patch。对于自定义工作流，请先审核完整的 proposed 文件和 patch，保留你有意继续使用的逻辑并写回受 Git 管理的工作流，然后再发布审核后的工作流。`install` 不会提交、推送或合并应用改动。

`.previewmesh.bak`、`.previewmesh-proposed` 和 `.previewmesh.patch` 是本地审核产物；除非你有意保留，否则不要将它们提交到应用仓库。

审核工作流后，在应用 checkout 中执行下面这组简短命令。对于新创建的文件，请先查看完整文件，再将它加入暂存区：

~~~bash
cd /path/to/application-checkout
git status --short
git diff -- .github/workflows/previewmesh-notify.yml
git add -- .github/workflows/previewmesh-notify.yml
git diff --cached
git diff --cached --check
git commit -m "Update PreviewMesh source notification"
git push origin HEAD
~~~

`git add` 只指定工作流文件，因此不会暂存本地审核产物。如果当前分支就是应用仓库的默认分支，`git push origin HEAD` 可以直接发布；否则请遵循该仓库的策略，自行创建或更新拉取请求。

`install` 还会在 `<control checkout>/bin/previewmesh` 构建面向用户的 CLI。若 `CONTROL_DIR` 指向 private control checkout，请使用 `"$CONTROL_DIR/bin/previewmesh"`；该二进制文件只属于这个 checkout 及其配置，不会全局安装，也不会把不带路径的 `previewmesh` 命令加入 `PATH`。

安装器负责准备基础设施，不会改写应用代码，也不能证明应用已经能够提供预览。[手动安装参考](ops/install/manual.md) 解释了不适合使用引导式流程时的各个基础设施步骤。

### 3. 可选：使用 `onboard-source` 准备接入 PR

上面的本地审核、提交和推送流程是主流程。如果希望由 GitHub 自动准备一个只包含通知工作流的 PR，可以选择使用 `onboard-source`。默认模式只输出完整的待提交变更：

~~~bash
bash scripts/setup.sh onboard-source --config /path/to/setup.ini --source OWNER/REPO
~~~

审核输出后，添加 `--create-pr` 创建专用的接入拉取请求：

~~~bash
bash scripts/setup.sh onboard-source --config /path/to/setup.ini --source OWNER/REPO --create-pr
~~~

该命令只提交 `.github/workflows/previewmesh-notify.yml`，不会自动合并 PR，也不会修改其他应用代码。请按需审核或修改这个 PR，保留有意使用的自定义逻辑，然后在创建测试预览前将它合并到应用默认分支。

### 4. 创建第一个预览

1. 在已注册的应用仓库中从分支创建普通拉取请求，目标为应用默认分支。不要使用 fork。
2. 应用通知工作流成功后，`PreviewMesh` 检查会立即显示请求正在等待部署管理工作流启动；请继续等待该工作流完成。
3. 确认构建、部署、就绪、健康检查和提交版本校验均成功。
4. 在能够访问配置的私有网络的机器上打开生成的 URL。请阅读[预览访问说明](ops/install/access.md)了解 DNS、hosts 文件、WSL、`18080` 端口和 `/health` 检查。
5. 关闭或合并拉取请求。PR 会立即显示清理正在进行；清理工作流确认 Namespace 删除前，预览 URL 可能短暂保持可访问，确认后才停止路由。

## 使用预览

正常的开发循环是：

~~~text
推送分支 -> 创建同仓库 PR -> 等待部署 -> 查看私有 URL -> 关闭或合并 -> 确认清理
~~~

在私有部署管理 checkout 中，使用 `install` 构建的本地 CLI 查询状态：

~~~bash
CONTROL_DIR=/path/to/previewmesh-control
gh auth status
"$CONTROL_DIR/bin/previewmesh" status --repo OWNER/REPO --pr 123
~~~

自动化场景使用结构化输出：

~~~bash
"$CONTROL_DIR/bin/previewmesh" status --repo OWNER/REPO --pr 123 --json
~~~

`status` 通过已认证的 `gh` CLI 读取当前拉取请求状态、`PreviewMesh` Commit Status 和结构化工作流反馈。它不会探测预览 URL，不会查询 Kubernetes，也不会执行实时健康检查。`ready` 表示当前提交有完整且较新的证据；拉取请求关闭后，`removed` 表示已有清理证据。

HTTP 200 并不等于验证成功。应用验证还必须要求 `status: ok`，并且 `commit_sha` 与本次请求的准确源代码版本一致。

### 用户预览命令

日常操作可以使用 private control checkout 中的 CLI，不需要记住 Kubernetes 查询细节。将 `CONTROL_DIR` 设置为该 checkout，并使用它的明确路径；安装器不会在 `PATH` 中提供不带路径的 `previewmesh` 命令：

~~~bash
CONTROL_DIR=/path/to/previewmesh-control
"$CONTROL_DIR/bin/previewmesh" help
"$CONTROL_DIR/bin/previewmesh" ps
"$CONTROL_DIR/bin/previewmesh" inspect 123
"$CONTROL_DIR/bin/previewmesh" health 123
"$CONTROL_DIR/bin/previewmesh" rm 123
~~~

工具会自动读取本机的 PreviewMesh 配置和当前 control repository 注册表。`ps` 显示 Kubernetes 的实时运行状态和 URL；`inspect` 合并运行时详情与 GitHub 生命周期证据；`health` 会实时请求 `/health`。脚本可以使用 `--json`，`--all` 可查看不属于当前 control repository 的资源。

`rm` 只删除当前 control repository 所拥有的预览 Namespace。它默认要求确认，开放的拉取请求需要额外使用 `--force`，并且不会修改拉取请求本身。现有的 `preflight`、`build`、`deploy`、`verify`、`cleanup`、`cleanup-inspect`、`cleanup-retry` 和 `status` 命令继续供 CI 与高级操作使用；可以使用这个 control-local 二进制文件，或 CI 中单独构建的二进制文件来调用它们。

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

如果必须清理旧安装，建议使用累积的三档清理级别，并在执行前先用 `--dry-run` 审核计划：

~~~bash
# 1：只清理 setup.ini、草稿和安装器状态；保留服务、Runner 文件、仓库和远端资源
bash scripts/setup.sh cleanup --level 1 --config /path/to/setup.ini --dry-run

# 2：级别 1 + 本机 Runner 文件和 PreviewMesh systemd/入口服务；保留 K3s 和远端 Runner 登记
bash scripts/setup.sh cleanup --level 2 --config /path/to/setup.ini --dry-run

# 3：级别 2 + 配置中的本地 control/source Git checkout
bash scripts/setup.sh cleanup --level 3 --config /path/to/setup.ini --dry-run
~~~

确认无误后去掉 `--dry-run`。级别 2 不会卸载 K3s，也不会删除 GitHub Runner 远端登记；如果确实要删除远端 Runner，必须额外使用独立的 `--remote-runner` 选项。非交互模式下还必须指定 `--confirm-remote-runner`。在 WSL 中，级别 2 还会清除 PreviewMesh 创建的 Windows 防火墙、端口转发、计划任务和 `ProgramData\PreviewMesh` 配置。级别 3 只删除配置中指定的本地 control/source checkout，不删除 GitHub 远端仓库、Secrets、工作流或其他远端资源；含未提交、未跟踪或被忽略文件的 checkout 需要显式使用 `--allow-dirty-repositories`。公开 PreviewMesh checkout 永远不会被级别 3 删除。

Token 文件和 PreviewMesh Kubernetes Namespace/RBAC 不属于三档默认内容，分别使用 `--include-credentials` 和 `--cluster` 单独选择；`--cluster` 也不会卸载 K3s。`--select` 仍可用于高级的独立项目选择。删除本机 Runner 文件而保留远端登记会让远端 Runner 变为离线，通常只适合迁移或明确的旧安装退役流程。

请一次选择最终需要的最高级别。如果先执行级别 1 或 2，再执行级别 3，`setup.ini` 已被删除后，清理器可能无法再找到旧的 source 本地路径。

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
| PR 关闭后预览仍存在 | PR 中的“清理进行中”评论会说明这段短暂过渡；等待 closed-PR 通知和控制工作流。如果派发失败，修复后手动重试清理路径。 |
| WSL 访问中断 | 重新运行 `init` 确认当前 Windows 主机局域网地址，然后重新运行 `install`。查看[手动 WSL 和访问说明](ops/install/access.md)。 |

Runner 权限请看 [Kubernetes 访问说明](ops/kubernetes/README.md)。完整手动安装请看[手动安装参考](ops/install/manual.md)。生命周期和恢复细节请看[预览生命周期验证](ops/verification/preview-lifecycle.md)。

## 验证改动

在公开 checkout 中运行项目的标准本地验证：

~~~bash
bash scripts/verify-local.sh
~~~

它会检查 Go 代码、工作流和 setup fixtures、Python 校验、Bash 语法、Helm Chart 以及 GitHub Actions 工作流语法。它不会部署应用，也不需要访问 GitHub、GHCR、K3s、私有网络或应用凭据。

如需执行更小范围的检查，可以运行：

~~~bash
go test ./...
go vet ./...
helm lint charts/preview
~~~

## 高级参考

<details>
<summary>直接 CLI 生命周期</summary>

推荐使用私有部署管理工作流，因为它会重新检查拉取请求状态，并处理过时版本和清理。受信任的控制环境也可以使用较低层级的 CLI 命令：

使用下面的命令，将两个受信任的 CLI 构建到私有 control checkout，并使用明确的路径：

~~~bash
CONTROL_DIR=/path/to/previewmesh-control
go build -o "$CONTROL_DIR/bin/control" ./cmd/control
go build -o "$CONTROL_DIR/bin/previewmesh" ./cmd/previewmesh
~~~

配套的 `control` CLI 在向 GitHub 回写之前，负责验证受信任的注册信息和拉取请求状态：

| 命令 | 用途 |
| --- | --- |
| `control resolve` | 解析一个准确的仓库注册信息。 |
| `control inspect` | 验证拉取请求、源仓库、Fork 状态和作者权限。 |
| `control status` | 发布 Commit Status 和结构化的拉取请求反馈。 |

`previewmesh` CLI 负责预览部署生命周期：

| 命令 | 用途 |
| --- | --- |
| `preflight` | 检查应用 checkout，也可以运行临时本地容器检查。 |
| `build` | 校验准确 checkout，并为控制工作流发布镜像。 |
| `deploy` | 将不可变镜像摘要部署到所属预览 Namespace。 |
| `verify` | 等待就绪，并验证 `/health` 和应用返回的版本。 |
| `cleanup` | 拉取请求关闭后删除所属预览。 |
| `cleanup-inspect` | 在不删除资源的情况下收集所有权和清理状态。 |
| `cleanup-retry` | 使用原始 Namespace 身份重试清理。 |

在 private control checkout 中使用 `"$CONTROL_DIR/bin/control"` 和 `"$CONTROL_DIR/bin/previewmesh"` 查看准确参数。`previewmesh` 必须只存在于 control checkout 中，不会全局安装。不要在公开文档或 shell 历史中传递私有凭据。

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

## 相关文档

- [预览访问](ops/install/access.md) — DNS、hosts 文件、WSL/LAN 访问、`18080` 端口和 `/health` 验证。
- [手动安装](ops/install/manual.md) — 不适合使用引导式安装时的基础设施流程。
- [Kubernetes Runner 访问](ops/kubernetes/README.md) — 受限 kubeconfig 和 RBAC 边界。
- [预览生命周期验证](ops/verification/preview-lifecycle.md) — 运行生命周期和清理检查。
