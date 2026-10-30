# 清理安装

[返回 README](../../README_CN.md#更新与清理) · [English](cleanup.md)

## 清理级别

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

## `init` 期间的清理

如果 `init` 检测到已有 PreviewMesh 安装记录或系统服务，会先显示痕迹并让你直接选择三个累积清理级别。选择的清理会立即执行，完成后才会启动新的配置向导。级别 1 只重置本地安装器设置和状态；级别 2 还会删除本机 PreviewMesh 服务和 Runner 文件，但保留 K3s 和远端 Runner 登记；级别 3 还会在通过路径和 origin 安全检查并明确确认删除仓库后，直接删除配置中的本地 control/source checkout，包括其中未提交、未跟踪和被忽略的文件。上述清理完成后，`init` 会检查 `~/.config/previewmesh/secrets/*.token`，并询问是否删除这些 Token 文件。K3s、GitHub 仓库、远端资源和新配置都会保留。如果没有选择级别 2 或 3，属于另一个 control repository 的旧 Runner 会继续运行，新安装使用独立的本地 Runner 目录。
