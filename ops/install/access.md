# PreviewMesh preview access / PreviewMesh 预览访问

Use this page from the guided installer, the manual reference, and the normal
PR workflow. It is the single place for the address format, DNS/hosts choice,
port meaning, and final verification.

配置向导、手工安装说明和日常 PR 流程都从这里进入。本文统一说明地址格式、DNS
或 hosts 方案、端口含义和最终验证。

## From installation to a URL / 从安装完成到 URL

The installer checks the configured preview entry, but it does not create an
application preview. After the source notification workflow is merged, create
an ordinary PR in that source repository and wait for the control workflow.
Run the read-only status query and open the URL it reports:

安装器会检查配置好的预览入口，但不会替应用创建预览。source 通知工作流合并后，
在该 source 仓库创建普通 PR，等待 control 工作流完成，然后查询只读状态并打开它
报告的 URL：

    previewmesh status --repo "$SOURCE_REPOSITORY" --pr "$PR_NUMBER"

Preview URL: appears only for a current, complete, recent ready result. The
URL root (/) is the application page; /health is the health contract. The
status command reads recorded evidence and does not probe the URL. A successful
doctor check proves the configured entry is reachable; it does not prove that
a real preview was deployed.

只有当前、完整且未过期的 ready 结果才会显示 Preview URL:。URL 根路径 / 是应用页面，
/health 是健康接口。状态命令读取已记录的证据，不会探测 URL。doctor 通过只说明配置
的入口可访问，不代表真实预览已经部署。

The URL is always generated from the source repository ID, PR number, selected
domain suffix, and product entry port:

    http://pm-r<source-repository-id>-pr<pr-number>.<domain-suffix>:18080

地址始终由 source 仓库 ID、PR 编号、所选域名后缀和产品入口端口生成：

    http://pm-r<source-repository-id>-pr<pr-number>.<domain-suffix>:18080

To inspect the format using the current configuration, replace only the
configuration path, source repository and real PR number. The ID is read from
GitHub, so the example does not bake in a repository, IP, or PR value:

根据当前配置查看格式时，只替换配置路径、source 仓库和实际 PR 编号。仓库 ID 从
GitHub 读取，因此示例不固定仓库、IP 或 PR：

    CONFIG=/path/to/setup.ini
    SOURCE_REPOSITORY=OWNER/REPO
    PR_NUMBER=YOUR_PR_NUMBER
    SOURCE_REPOSITORY_ID="$(gh repo view "$SOURCE_REPOSITORY" --json databaseId --jq '.databaseId')"
    DOMAIN_SUFFIX="$(python3 - "$CONFIG" <<'PY'
    import configparser
    import sys

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(sys.argv[1])
    lan_ip = parser.get("network", "lan_ip").strip()
    suffix = parser.get("network", "domain_suffix", fallback="auto").strip()
    print(lan_ip + ".sslip.io" if suffix == "auto" else suffix)
    PY
    )"
    PREVIEW_HOST="pm-r$SOURCE_REPOSITORY_ID-pr$PR_NUMBER.$DOMAIN_SUFFIX"
    printf 'Preview URL: http://%s:18080\n' "$PREVIEW_HOST"

If lan_ip or domain_suffix changes, rerun installation so control and the
entry checks use the new value, then redeploy open PRs. Existing preview URLs
do not migrate automatically.

如果修改 lan_ip 或 domain_suffix，请重新运行安装，使 control 和入口检查使用新值，
然后重新部署仍开放的 PR。已有预览 URL 不会自动迁移。

## Recommended automatic address: sslip.io / 推荐自动地址：sslip.io

Keep `[network] domain_suffix = auto`. The installer turns the selected
`lan_ip` into `<lan_ip>.sslip.io`; sslip.io resolves the embedded private
address. When the installer machine's DNS check passes, each browser and
runner machine must still resolve the generated hostname to the selected LAN
address; the installer's result does not prove every client can resolve it.
When DNS resolves normally, open the generated URL directly without changing
hosts. The preview remains private. If a network blocks private-IP DNS answers,
installation stops with a repair message instead of silently switching to hosts.

保持 `[network] domain_suffix = auto`。安装器会把选定的 `lan_ip` 变成
`<lan_ip>.sslip.io`，由 sslip.io 解析其中的私有地址。安装器所在机器的 DNS 检查通过
后，每台浏览器和 Runner 所在机器仍必须把生成的主机名解析到选定的局域网地址；安装器
通过不代表每台客户端都能解析。DNS 正常时可以直接打开生成的 URL，不需要修改 hosts。
预览不会因此公开。如果网络拦截私有地址的 DNS 结果，
安装会停止并提示修复，不会静默改用 hosts。

## Alternative manual DNS or hosts / 替代手工 DNS 或 hosts

Use a manual suffix when an organization owns a private DNS zone or policy
disallows sslip.io. Enter lowercase DNS labels in the setup file:

如果组织拥有内部 DNS 区域，或网络策略不允许 sslip.io，可以使用手工后缀。配置文件
中填写小写 DNS 标签：

    [network]
    lan_ip = YOUR_LAN_IPV4
    domain_suffix = YOUR_PRIVATE_DNS_SUFFIX

For managed DNS, point both `*.<domain-suffix>` and
`previewmesh-check.<domain-suffix>` to the configured `lan_ip`. The installer
and doctor check the probe before accepting the entry; each client must resolve
the generated preview hostname to that same configured LAN address.

如果使用可管理的 DNS，请将 `*.<domain-suffix>` 和
`previewmesh-check.<domain-suffix>` 都指向配置中的 `lan_ip`。安装器和 doctor 会检查
探测名称；每台客户端都应把生成的预览主机名解析到同一个配置局域网地址。

If DNS cannot provide the suffix, use hosts files as an explicit fallback.
Before installation, add the probe on the installer machine:

如果 DNS 无法提供该后缀，可明确改用 hosts 文件。安装前先在安装器所在机器添加探测名：

    <lan-ip> previewmesh-check.<domain-suffix>

After a PR exists, add the full generated preview hostname on every machine
that opens or verifies that PR:

创建 PR 后，在需要打开或验证该 PR 的每台机器上添加完整预览主机名：

    <lan-ip> pm-r<source-repository-id>-pr<pr-number>.<domain-suffix>

Use the configured `lan_ip` for the installer probe and, where the same LAN
entry is reachable, for client entries too. Hosts files do not support
wildcard records, so every PR needs its own entry. Each entry maps an IP
address to a complete hostname and must not include a port. On Linux edit
/etc/hosts; on Windows edit C:\Windows\System32\drivers\etc\hosts as an
administrator. Do not replace unrelated entries.

安装器的探测条目必须使用配置中的 `lan_ip`；客户端也应在能访问同一局域网入口时使用
该地址。hosts 不支持通配记录，因此每个 PR 都需要自己的条目。每条记录都是 IP 地址
到完整主机名的映射，不能包含端口。Linux 编辑
/etc/hosts；Windows 以管理员权限编辑 C:\Windows\System32\drivers\etc\hosts。
不要改写无关条目。

This probe requirement applies to the guided installer, whose LAN entry is
configured from `lan_ip`. The manual reference deliberately keeps a local
socket on `127.0.0.1`; for that path, use `127.0.0.1` in the full per-preview
hosts entry on a machine where the socket is exposed. It is a local check and
does not establish LAN reachability.

这个探测要求适用于引导式安装，因为它根据 `lan_ip` 配置局域网入口。手工安装参考会
保留监听 `127.0.0.1` 的本机 socket；使用该路径时，在 socket 可访问的机器上为每个
预览主机名使用 `127.0.0.1` 映射。这只是本机检查，不能证明局域网可访问。

After changing the probe entry for the guided installer, rerun the doctor
check with the same configuration path, then verify the generated hostname
from each client:

    CONFIG=/path/to/setup.ini
    bash scripts/setup.sh doctor --config "$CONFIG"
    getent ahostsv4 "$PREVIEW_HOST"
    # Windows PowerShell: $PreviewHost = 'paste-the-ready-hostname'; Resolve-DnsName -Name $PreviewHost -Type A

Compare the returned address with the configured `lan_ip`, then query the PR
status and open the exact ready URL. The manual localhost path has no installer
probe or doctor checkpoint; verify its `127.0.0.1` hosts entry and socket
instead. A probe returning HTTP 404 only proves that the request reached
Traefik before a preview route exists; it is not an application health result.

引导式安装修改探测条目后，使用相同配置路径重新执行 doctor，再从每台客户端验证生成
的主机名：

    CONFIG=/path/to/setup.ini
    bash scripts/setup.sh doctor --config "$CONFIG"
    getent ahostsv4 "$PREVIEW_HOST"
    # Windows PowerShell：$PreviewHost = '粘贴 ready URL 中的主机名'; Resolve-DnsName -Name $PreviewHost -Type A

将返回地址与配置中的 `lan_ip` 对照，再查询 PR 状态并打开完整的 ready URL。手工本机
方案没有安装器探测或 doctor 检查；请改为验证 `127.0.0.1` hosts 条目和 socket。
探测请求返回 HTTP 404 只说明请求到达了 Traefik、当前还没有预览路由；它不是应用健康
结果。

## Port and health checks / 端口与健康检查

18080 is the PreviewMesh browser and health-check entry port. The source
application port is its container/listener port and remains separate. Each
hosts entry maps an IP address to a complete hostname and contains no port.

18080 是 PreviewMesh 浏览器和健康检查的入口端口。source 应用的 port 是容器或应用
监听端口，二者独立。每条 hosts 记录把 IP 地址映射到完整主机名，不包含端口。

Use the URL printed by status for the final check. When diagnosing a route or
application, append /health to that URL and require HTTP 200 with status: ok
and the expected commit_sha:

最终检查使用 status 打印的 URL。排查路由或应用时，在 URL 后加 /health，并要求 HTTP
200、status: ok 以及预期的 commit_sha：

    PREVIEW_URL='paste-the-ready-URL-from-previewmesh-status'
    curl --fail --silent --show-error --max-time 10 "$PREVIEW_URL/health"
