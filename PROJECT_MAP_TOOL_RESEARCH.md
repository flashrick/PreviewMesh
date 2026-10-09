# 混合语言仓库项目地图工具调研

> 研究日期：2026-10-09
>
> 资料范围：只采用工具作者、维护组织或语言官方文档/源码；未安装任何依赖。仓库范围判断只依据已跟踪的公开路径，未读取或列出被忽略的内容。

## 结论先行

没有一个成熟的开源 CLI 能同时对 Go、Python、Shell、PowerShell、普通 YAML 和 Helm 模板完成“目录职责 + 语义关系 + 可读图形”的端到端生成。最实际的是分层组合：

1. 用仓库文件清单建立目录/文件职责基线。
2. 用语言或领域专用分析器提取关系：Go 使用 `go list`/`gopls`，Python 使用 `pyreverse` 或 AST，Shell 使用 Tree-sitter Bash，PowerShell 使用官方 PowerShell AST，GitHub Actions 使用 `actionlint` 的解析能力，Helm 先用 `helm template` 得到渲染后的资源关系。
3. 用 Graphviz 统一输出 DOT、SVG 和 JSON；需要文档内嵌时再转 Mermaid 或直接嵌入 SVG。

对本仓库最合适的路线是：先采用已经存在的 `go`、`gopls`、`helm`、`actionlint` 和 `dot` 生成低成本的 Go/工作流/Helm 基线；若必须覆盖所有脚本语义，再增加 Tree-sitter 与 PowerShell AST 提取器。Sourcegraph SCIP 和 CodeQL 更适合作为代码导航/查询平台，不适合作为唯一的项目地图生成器。

## 本仓库的输入形态

已跟踪的公开路径显示：

- Go：`cmd/`、`internal/`、`go.mod`，包含两个命令入口和内部包。
- Python：`scripts/` 下的工具和测试脚本。
- Shell：`scripts/`、`ops/wsl/` 下的 `.sh`。
- PowerShell：`ops/wsl/` 下的 `.ps1`。
- YAML：GitHub Actions 工作流、Kubernetes/运维 YAML。
- Helm：`charts/preview/`，包含 `Chart.yaml`、`values.yaml`、`templates/` 和 values schema。

这意味着地图至少应区分以下边：目录包含关系、Go 包导入、Python import、脚本 source/调用、工作流 `needs`/`uses`、Helm values 到模板以及模板到渲染资源；单纯的目录树或符号索引都不够。

## 候选工具比较

| 候选 | 覆盖范围及关系能力 | 输出形式 | 维护/部署成本 | 局限与本仓库适配度 |
|---|---|---|---|---|
| **Go `go list` + `gopls` + Graphviz** | `go list -json -deps` 能给出包、源文件、Imports 和递归 Deps；`gopls` 可提供 definition、references、implementation 和 call hierarchy。只覆盖 Go，但对 `cmd`/`internal` 的语义关系最可靠。 | `go list` JSON；`gopls` CLI/LSP 响应；Graphviz 可生成 DOT、SVG、PDF、JSON 等。 | **低**：本机已有 `go`、`gopls`、`dot`，只需一个聚合脚本。 | `gopls` 的调用层级不包含动态调用，且结果取决于当前 build configuration；需要遍历符号并自行聚合成全仓库图。适合作为 Go 子图，不是混合语言方案。官方：[go list](https://pkg.go.dev/cmd/go#hdr-List_packages_or_modules)、[Go module graph](https://go.dev/ref/mod#go-mod-graph)、[gopls navigation](https://go.dev/gopls/features/navigation)、[Graphviz 输出](https://graphviz.org/doc/info/output.html)。 |
| **Tree-sitter + 多语言 grammar + 自定义提取器 + Graphviz** | 官方 Tree-sitter 提供增量解析和语法树；上游 grammar 覆盖 Go、Python、Bash，社区维护的 YAML grammar 可解析 YAML。可统一提取文件、函数、import/source、调用或配置键。 | Tree-sitter syntax tree；自定义 JSON/DOT；Graphviz SVG/JSON；也可生成 Mermaid 文本。 | **中**：运行时轻量，但需要维护 grammar 版本、查询规则和名称解析逻辑。 | Tree-sitter 主要给语法结构，不自动给出编译器级的跨文件语义。PowerShell 官方 tree-sitter grammar 已归档且 README 明列未实现语法，不应作为生产主解析器；Helm 原始文件是 Go template + YAML，需额外处理。适合做全仓库统一骨架。官方：[Tree-sitter](https://tree-sitter.github.io/tree-sitter/)、[Bash grammar](https://github.com/tree-sitter/tree-sitter-bash)、[Python grammar](https://github.com/tree-sitter/tree-sitter-python)、[YAML grammar](https://github.com/tree-sitter-grammars/tree-sitter-yaml)、[已归档的 PowerShell grammar](https://github.com/PowerShell/tree-sitter-PowerShell)。 |
| **PowerShell 官方 AST + `actionlint` API + Helm CLI + Graphviz** | PowerShell `Parser.ParseFile` 返回 `ScriptBlockAst`、tokens 和 parse errors；`ScriptBlockAst` 支持遍历 AST。`actionlint` 解析 GitHub Actions YAML、表达式、`needs`、action metadata，并可作为 Go API 使用。`helm template` 将 chart 本地渲染为 Kubernetes YAML。 | PowerShell AST/自定义 JSON；actionlint 诊断或 API AST；Helm 渲染 YAML；Graphviz 图。 | **中**：PowerShell 解析需要 PowerShell/.NET 运行时；Helm 和 actionlint 已有；需要跨领域适配器。 | 这些工具本身不是项目地图生成器；PowerShell AST 需要自己定义调用/source 边，`helm template` 的本地值和集群能力是模拟的，且官方文档明确不做服务器端有效性测试。对本仓库的脚本、工作流、Helm 覆盖最准确。官方：[Parser.ParseFile](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.parser.parsefile?view=powershellsdk-7.4.0)、[ScriptBlockAst](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.scriptblockast?view=powershellsdk-7.4.0)、[actionlint README](https://github.com/rhysd/actionlint)、[actionlint API](https://github.com/rhysd/actionlint/blob/main/docs/api.md)、[helm template](https://helm.sh/docs/helm/helm_template/)、[Helm chart 结构与依赖](https://helm.sh/docs/topics/charts/)。 |
| **Sourcegraph SCIP（Go/Python indexers）** | SCIP 是语言无关的索引协议，能表达 symbols、occurrences、definition/reference/implementation；Sourcegraph 当前将 Go 和 Python 精确代码导航列为 generally available。 | `.scip` protobuf 索引，上传后在 Sourcegraph UI 中导航；要生成静态项目图仍需自定义 SCIP-to-DOT/JSON 转换。 | **高**：每种语言要有 indexer；完整体验通常需要 Sourcegraph 实例或上传流程。 | 当前精确 indexer 列表不覆盖 Shell、PowerShell、普通 YAML 或 Helm；它解决代码导航，不直接产生目录职责说明或架构图。适合作为 Go/Python 的深层关系后端，不适合作为全仓库唯一工具。官方：[SCIP 仓库](https://github.com/scip-code/scip)、[精确代码导航支持](https://sourcegraph.com/docs/code-navigation/precise-code-navigation)、[编写 indexer](https://sourcegraph.com/docs/code-navigation/writing-an-indexer)。 |
| **CodeQL + 自定义 QL + Graphviz** | 官方支持 Go、Python 和 GitHub Actions workflows；可将代码建成可查询数据库，编写关系/路径查询。 | CodeQL database、BQRS 原始结果、SARIF；需额外导出为 DOT/JSON/图形。 | **高**：需要 CodeQL CLI/bundle、数据库构建和 QL 查询维护；Go 等编译语言还要满足构建/提取环境。 | CodeQL 面向安全、正确性和可维护性查询，不是目录职责地图；不覆盖本仓库的 Shell、PowerShell、Helm，普通 YAML 也不是完整通用覆盖。适合作为安全关系或特定影响分析补充。官方：[支持语言](https://codeql.github.com/docs/codeql-overview/supported-languages-and-frameworks/)、[查询](https://codeql.github.com/docs/writing-codeql-queries/about-codeql-queries/)、[系统要求](https://codeql.github.com/docs/codeql-overview/system-requirements/)。 |
| **Pylint `pyreverse` / `pydeps` + 其他语言专用工具** | `pyreverse` 专注 Python 包依赖、类层次和关系；`pydeps` 专注 Python module import graph。配合 Go、Shell、PowerShell、Helm 专用提取器可形成组合。 | `pyreverse` 原生输出 DOT/GV、PlantUML、Mermaid，也能调用 Graphviz 输出 PNG/SVG/PDF；`pydeps` 可输出 DOT、SVG/PNG 和依赖中间数据。 | **中**：Python 部分易用，但引入 Python 包和 Graphviz；多语言组合后仍需统一 schema。 | 只覆盖 Python。`pydeps` 依赖 Python import machinery/bytecode，未被 import 的文件和动态 import 可能不会进入图；`pyreverse` 也不能解释 Shell、PowerShell、YAML 或 Helm。适合快速补足 Python 子图，不宜单独承担全仓库地图。官方：[pyreverse](https://pylint.readthedocs.io/en/latest/additional_tools/pyreverse/index.html)、[pydeps 官方仓库](https://github.com/thebjorn/pydeps)。 |
| **Universal Ctags + Graphviz** | 适合做跨语言符号/文件清单；官方定义是生成语言对象的 tag index，并支持若干语言和 JSON 输出。通过 scope、kind、路径可推导“文件包含哪些符号”。 | tags、xref、JSON Lines。Graphviz 仍需自行生成边。 | **低到中**：单二进制、部署低；但需要按语言校准 parser/kinds，并编写关系推导。 | 它是索引器，不是语义关系分析器；默认不会告诉你 import、调用、工作流依赖或 Helm values 流向。可作为目录职责基线，不应作为唯一工具。官方：[Universal Ctags 仓库](https://github.com/universal-ctags/ctags)、[命令手册](https://github.com/universal-ctags/ctags/blob/master/docs/man/ctags.1.rst)、[JSON 输出](https://docs.ctags.io/en/stable/man/ctags-json-output.5.html)。 |
| **Repomix + 人工/脚本后处理** | 可打包仓库内容，生成目录树和适合分析的文件边界；不负责解析跨文件语义关系。 | XML、Markdown、JSON、plain；可只输出元数据/目录结构或压缩代码结构。 | **低**：单个 Node CLI；本机未安装，且不应为本次调研安装。 | 它是代码库快照/分析输入，不是自动项目关系图。适合生成“职责说明”的上下文材料，不能替代 Go/Python/脚本/Helm 专用提取器。官方：[Repomix README](https://github.com/yamadashy/repomix)、[官方仓库中的输出实现](https://github.com/yamadashy/repomix/blob/main/src/core/output/outputGenerate.ts)。 |

## 语言与领域覆盖速查

| 方案 | Go | Python | Shell | PowerShell | YAML / GitHub Actions | Helm |
|---|---:|---:|---:|---:|---:|---:|
| `go list` + `gopls` | 强 | — | — | — | — | — |
| Tree-sitter 组合 | 语法 | 语法 | Bash 语法 | 不建议使用已归档 grammar | YAML 语法 | 需先处理 Go template |
| PowerShell AST + actionlint + Helm | — | — | — | 强 | GitHub Actions 强、普通 YAML 部分 | 渲染/依赖强，源码引用需自定义 |
| SCIP | 强 | 强 | — | — | — | — |
| CodeQL | 强 | 强 | — | — | GitHub Actions | — |
| `pyreverse` / `pydeps` | — | 强 | — | — | — | — |
| Ctags / Repomix | 文件/符号层 | 文件/符号层 | 取决于 parser | 取决于 parser | 文件/文本层 | 文件/文本层 |

“强”表示工具直接理解该领域的关系；“语法”表示主要得到 AST/CST，跨文件语义仍需自定义；“文件/符号层”不等于依赖或调用图。

## 推荐落地组合

### 低成本第一阶段（不新增依赖）

- 用已安装的 `go list -json -deps` 生成 Go 包/文件依赖 JSON。
- 用已安装的 `gopls` 对入口函数或关键符号补充 references/call hierarchy；在图中标注它是 build-configuration-specific 的静态结果。
- 用已安装的 `actionlint` 解析和校验 `.github/workflows`，把 job、`needs`、`uses` 和本地 action 文件作为工作流边。
- 用已安装的 `helm template` 将 `charts/preview` 渲染为资源清单，同时把 `values.yaml`、schema、模板文件和输出资源作为不同节点类型。
- 用已安装的 `dot` 统一渲染 DOT 为 SVG/JSON；SVG 适合文档，JSON/DOT 适合后续 diff。

这一步能快速得到 Go、CI、Helm 的可靠骨架，但 Python、Shell、PowerShell 的语义边需要额外提取。

### 完整覆盖阶段

采用统一中间模型，例如：

```text
Node: path | directory | package | symbol | workflow-job | chart | manifest
Edge: contains | imports | calls | sources | needs | uses | values-into | renders
Evidence: file + line + extractor + confidence
```

然后按语言接入 Go 专用数据、Python AST/`pyreverse`、Tree-sitter Bash/YAML、PowerShell 官方 AST，以及 Helm 的模板/渲染结果，最后全部输出到 Graphviz。对动态 Shell、PowerShell 命令、模板条件分支和 Helm 运行时值应保留“未知/推断”标记，而不是伪装成确定关系。

## 已检查的本机 CLI

版本检查未安装任何东西。当前可用：

```text
go       go1.25.1
gopls    v0.22.0
helm     v3.21.4+g813176c
actionlint v1.7.12	dot (Graphviz) 2.43.0
python3  3.12.3	bash
jq       1.7
```

未找到的相关 CLI 包括 `pwsh`/`powershell`、`tree-sitter`、`repomix`、`scip`、`codeql`、`pyreverse`、`pydeps`、`shellcheck`、`shfmt`、`yq`、`ctags` 和 `mermaid-cli`。这只是环境盘点，不代表推荐安装；本次没有安装依赖。

`ShellCheck` 可作为 Shell 质量门禁，但其官方定位是 Shell 静态分析/诊断，并非关系图生成器；同理，`actionlint` 是 GitHub Actions 静态检查器，`helm lint` 是 chart 形状检查器。它们适合作为提取前的验证器，而不是地图本身。官方：[ShellCheck](https://github.com/koalaman/shellcheck)、[actionlint 检查范围](https://github.com/rhysd/actionlint/blob/main/docs/checks.md)、[helm lint](https://helm.sh/docs/helm/helm_lint/)。

## 最终判断

- **只要一张可维护的全仓库地图**：选 Tree-sitter/PowerShell AST/actionlint/Helm 与 Go/Python 专用提取器的组合，再用 Graphviz 输出；接受维护一个小型适配层。
- **只要 Go/Python 的精确代码导航**：选 SCIP/Sourcegraph；不要期待它覆盖 Shell、PowerShell、YAML、Helm。
- **只要快速目录快照或供人/模型阅读**：选 Repomix 或 Ctags；明确标注它们不提供语义关系。
- **不建议**把 CodeQL、pyreverse、ShellCheck、actionlint 或 Helm 单独当作全仓库地图工具；它们各自只解决关系查询、Python 图、Shell 诊断、工作流校验或 chart 渲染中的一部分。

