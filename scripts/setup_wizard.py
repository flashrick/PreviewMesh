"""Local, credential-free discovery and confirmation for installer configuration."""
import configparser
import io
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess

from setup_config import (ACCESS_GUIDE, SetupError, RFC1918, atomic_write, config_draft_path,
                          load_config, validate_domain_suffix, validate_repo)
from setup_language import choose_language


def safe(value):
    # Never reflect URLs containing passwords or common GitHub credential formats.
    if re.search(r"gh[pousr]_|github_pat_|://[^/]*@", value, re.I):
        raise SetupError("Credentials are not configuration / 请勿填写凭据，仅填写凭据文件路径。")
    if any(ord(char) < 32 for char in value):
        raise SetupError("Invalid text / 输入包含控制字符。")
    return value


def capture(command, *, timeout=5):
    try:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('GIT_TRACE', 'GIT_CONFIG')) and key not in ('GH_DEBUG', 'GIT_CURL_VERBOSE')}
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              check=True, env=env).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ""


def discover_repositories(directory):
    values = []
    for line in capture(['git', '-C', str(directory), 'remote', '-v']).splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        # Match only credential-free GitHub transports; never display raw remotes.
        match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+)', parts[1])
        if match:
            repo = match[1].removesuffix('.git')
            try:
                safe(repo)
                validate_repo(repo)
            except SetupError:
                continue
            if repo not in values:
                values.append(repo)
    return values


def discover_lan():
    # WSL guest NAT addresses cannot identify the Windows LAN entry point.
    if 'microsoft' in platform.release().lower():
        return []
    try:
        data = json.loads(capture(['ip', '-j', '-4', 'address', 'show', 'up']))
        excluded = ('lo', 'docker', 'br-', 'veth', 'cni', 'flannel', 'virbr', 'podman',
                    'tun', 'tap', 'wg', 'tailscale', 'ppp', 'vboxnet', 'vmnet', 'zt')
        return sorted({str(ipaddress.IPv4Address(info['local'])) for item in data
                       if not item.get('ifname', '').startswith(excluded)
                       and item.get('operstate') in (None, 'UP', 'UNKNOWN')
                       for info in item.get('addr_info', []) if info.get('scope') == 'global'
                       and any(ipaddress.IPv4Address(info['local']) in net for net in RFC1918)})
    except (ValueError, KeyError, TypeError):
        return []


def discover_windows_lan():
    # Query Windows physical adapters so WSL NAT, container and VPN interfaces
    # cannot be mistaken for the host's LAN entry. No network settings are changed.
    command = """$ErrorActionPreference = 'Stop'
$physical = @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up' |
    Select-Object -ExpandProperty ifIndex)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred |
    Where-Object { $_.InterfaceIndex -in $physical -and -not $_.SkipAsSource } |
    Select-Object -ExpandProperty IPAddress)
ConvertTo-Json -InputObject $addresses -Compress
"""
    try:
        values = json.loads(capture(['powershell.exe', '-NoProfile', '-NonInteractive',
                                     '-Command', command], timeout=20))
    except (ValueError, TypeError):
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    addresses = set()
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            address = ipaddress.IPv4Address(value)
        except ValueError:
            continue
        if any(address in network for network in RFC1918):
            addresses.add(str(address))
    return sorted(addresses)


def discover_ports(directory):
    try:
        content = (Path(directory) / 'Dockerfile').read_text()
    except (OSError, UnicodeError):
        return []
    ports = []
    for line in content.splitlines():
        if re.match(r'^\s*EXPOSE\s', line, re.I):
            for value in line.split()[1:]:
                if re.fullmatch(r'\d+(?:/tcp)?', value):
                    port = int(value.split('/')[0])
                    if 1 <= port <= 65535 and port not in ports:
                        ports.append(port)
    return ports


def port_available(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('0.0.0.0', port))
        return True
    except OSError:
        return False


def ask(label, default='', validate=None, *, language='en'):
    while True:
        answer = input(f'{label}' + (f' [{safe(str(default))}]' if default else '') + ': ').strip()
        if answer.lower() == 'q':
            raise EOFError()
        answer = answer or str(default)
        try:
            safe(answer)
            if not answer:
                raise ValueError()
            return validate(answer) if validate else answer
        except (SetupError, ValueError, OSError):
            print('输入无效，请重试或输入 q 退出。' if language == 'zh-CN'
                  else 'Invalid value; retry or q to exit.')


def confirm(label, *, language='en'):
    suffix = ' (yes=是 / no=否)' if language == 'zh-CN' else ' (yes/no)'
    return ask(label + suffix, 'no', lambda v: v if v in ('yes', 'no') else int('invalid'),
               language=language) == 'yes'


def choose(label, candidates, validate=None, *, language='en'):
    candidates = list(dict.fromkeys(candidates))
    for number, value in enumerate(candidates, 1):
        print(f'  {number}. {safe(str(value))}')
    def selected(value):
        if value.isdigit() and 1 <= int(value) <= len(candidates):
            value = str(candidates[int(value) - 1])
        return validate(value) if validate else value
    suffix = ' (编号或直接填写)' if language == 'zh-CN' else ' (number or value)'
    # Show the actual default so Enter's meaning is clear without decoding an index.
    default = str(candidates[0]) if len(candidates) == 1 else ''
    return ask(label + suffix, default, selected,
               language=language)


class Prompts:
    """Keep a wizard's display language local to that invocation."""

    def __init__(self, language):
        self.language = language

    def text(self, en, zh):
        return zh if self.language == 'zh-CN' else en

    def say(self, en, zh):
        print(self.text(en, zh))

    def ask(self, en, zh, default='', validate=None):
        return ask(self.text(en, zh), default, validate, language=self.language)

    def choose(self, en, zh, candidates, validate=None):
        return choose(self.text(en, zh), candidates, validate, language=self.language)

    def confirm(self, en, zh):
        return confirm(self.text(en, zh), language=self.language)


def choose_network_environment(ui):
    ui.say('Choose where PreviewMesh will run; the default is detected from this session.\n'
           'The wizard will query that environment for LAN addresses.\n'
           '  1. Ubuntu directly (outside WSL): use this Ubuntu machine\'s address.\n'
           '  2. Inside WSL: use the Windows host\'s address.',
           '选择 PreviewMesh 的运行方式，默认值根据当前环境检测。\n'
           '向导会自动获取对应环境的局域网地址。\n'
           '  1. Ubuntu 直接安装（非 WSL）：使用这台 Ubuntu 机器的地址。\n'
           '  2. WSL 内安装：使用 Windows 主机的地址。')
    default = 'WSL' if 'microsoft' in platform.release().lower() else 'Ubuntu'
    def selected(value):
        choices = {'1': 'ubuntu', 'ubuntu': 'ubuntu', '2': 'wsl', 'wsl': 'wsl'}
        if value.lower() not in choices:
            raise ValueError()
        return choices[value.lower()]
    return ui.ask('Installation environment (1/2 or Ubuntu/WSL)', '安装运行方式（1/2 或 Ubuntu/WSL）',
                  default, selected)


def private_ip(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in net for net in RFC1918):
        raise ValueError()
    return str(address)


def application_port(directory, previous=None, *, language='en'):
    ui = Prompts(language)
    ui.say('Enter the HTTP port your application listens on inside its container,\n'
           'such as 8080 or 3000.\n'
           'Check Dockerfile EXPOSE or the application settings.\n'
           'Preview URLs use the separate entry port 18080.',
           '填写应用在容器内实际监听的 HTTP 端口，例如 8080 或 3000。\n'
           '可查看 Dockerfile 的 EXPOSE 或应用配置。\n'
           '浏览器访问预览时使用另一个入口端口 18080。')
    candidates = ([previous] if previous else []) + discover_ports(directory)
    for port in dict.fromkeys(candidates):
        status = ui.text('available', '可用') if port_available(port) else ui.text('occupied or unverified', '已占用或无法验证')
        ui.say(f'Candidate HTTP port {port}: {status}', f'候选 HTTP 端口 {port}：{status}')
    if not candidates:
        ui.say('No application port found. Check Dockerfile EXPOSE or app listen settings; enter the HTTP port.',
               '未发现应用端口，请查看 Dockerfile 或监听配置并填写 HTTP 端口。')
    while True:
        port = int(ui.choose('Application HTTP port inside the container', '容器内应用 HTTP 端口', candidates,
                          lambda value: int(value) if 1 <= int(value) <= 65535 else int('invalid')))
        if port_available(port):
            ui.say(f'Port {port}: local bind check passed.', f'端口 {port}：本机端口检查通过。')
            return str(port)
        ui.say('Port occupied or cannot be checked. This is the container application port, not the host entry.',
               '端口已占用或无法检查；此处为容器应用端口。')
        if ui.confirm('Keep this application port', '确认保留此应用端口'):
            return str(port)
        candidates = []


def run_wizard(path, root, *, language=None):
    language = language or choose_language()
    ui = Prompts(language)
    path = Path(path).expanduser().absolute()
    draft_path = config_draft_path(path)
    safe(str(path))
    ui.say('Configuration wizard: choose repositories, network settings and credential file paths,\n'
           'then review and save. Run install afterwards to set up PreviewMesh.\n'
           'Press Enter to accept a value in brackets.\n'
           'For a numbered list, enter a number or your own value.\n'
           'GitHub repositories use OWNER/NAME, such as your-name/your-app.\n'
           'q exits without replacing the active configuration. A draft is kept after the final review.\n'
           'Do not paste tokens here; only token file paths.',
           '配置向导：依次选择仓库、网络设置和凭据文件路径，最后检查并保存。\n'
           '保存后再运行 install，开始安装 PreviewMesh。\n'
           '提示中有 [默认值] 时可直接回车。\n'
           '有编号列表时可输入编号，也可直接填写实际值。\n'
           'GitHub 仓库填写“用户名或组织名/仓库名”，例如 your-name/your-app。\n'
           '输入 q 退出且不替换当前配置；完成最终检查后会保留一份草稿。\n'
           '请勿粘贴 Token，仅填写文件路径。')
    if path.is_symlink():
        raise SetupError(ui.text('Configuration must not be a symlink.', '配置不能是符号链接。'))
    if draft_path.is_symlink():
        raise SetupError(ui.text('Configuration draft must not be a symlink.', '配置草稿不能是符号链接。'))
    previous = None
    if draft_path.exists():
        ui.say('An unfinished configuration draft was found.\n'
               'Load it to resume the previous wizard run, or choose no to review another configuration.',
               '发现未完成的配置草稿。\n'
               '选择 yes 可恢复上次向导，选择 no 可改为检查其他配置。')
        if ui.confirm('Load unfinished configuration draft', '载入未完成的配置草稿'):
            try:
                previous = load_config(draft_path, root)
            except (SetupError, OSError, ValueError, UnicodeError):
                ui.say('The configuration draft is invalid; enter values manually.',
                       '配置草稿无效，请手动填写。')
    if previous is None and path.exists():
        ui.say('Existing configuration found.', '发现已有配置。')
        ui.say('Load its settings as defaults for this run.\n'
               'Choose no to configure from scratch.\n'
               'You can change them before the final save, which replaces this file.',
               '可以载入已有设置作为本次填写的默认值，逐项检查或修改。\n'
               '输入 no 重新填写配置。\n'
               '最后确认保存时会替换这个配置文件。')
        # Reusing defaults is optional; saving is confirmed separately at the end.
        if ui.confirm('Load existing settings as defaults', '载入已有设置作为默认值'):
            try:
                previous = load_config(path, root)
            except (SetupError, OSError, ValueError, UnicodeError):
                ui.say('Existing configuration is invalid; enter values manually.', '已有配置无效，请手动填写。')
    data = configparser.ConfigParser(interpolation=None)
    def location(value):
        if '$' in value:
            raise ValueError()
        return str(Path(value).expanduser().absolute())
    def token_location(value):
        # A bare pasted token must never become a filename in the generated INI.
        if not value.startswith(('/', '~/', './', '../')):
            raise ValueError()
        return location(value)
    ui.say('\n[1/5] Repositories:\n'
           'PreviewMesh code, deployment management and applications have separate roles.\n'
           'The public PreviewMesh code repository supplies the initial tools.\n'
           'Your private deployment management repository stores application registrations\n'
           'and runs deployment automation.\n'
           'Application repositories contain the applications you want to preview.',
           '\n[1/5] 仓库角色：PreviewMesh 代码、部署管理和应用源码各有用途。\n'
           '公开的 PreviewMesh 代码仓库提供初始工具。\n'
           '你的私有部署管理仓库保存应用登记并运行部署自动化。\n'
           '应用源码仓库保存需要预览的实际应用。')
    ui.say('This local directory holds your deployment management repository.\n'
           'Use a separate path, such as ~/workspace/previewmesh-control;\n'
           'it can be a new directory or a checkout of that same repository.',
           '部署管理本地目录用于存放部署管理仓库的代码副本。\n'
           '请使用独立路径，例如 ~/workspace/previewmesh-control；\n'
           '可以是新目录，也可以是该仓库已有的本地目录。')
    control_dir = ui.ask('Deployment management local directory', '部署管理本地目录',
                      previous.control_dir if previous else Path(root).parent / 'previewmesh-control', location)
    repos = discover_repositories(control_dir)
    if previous:
        repos.insert(0, previous.control)
    ui.say('Choose a dedicated private GitHub repository for deployment management,\n'
           'such as your-name/previewmesh-control.\n'
           'install creates it if it does not exist.\n'
           'An existing repository must already contain PreviewMesh deployment tools\n'
           'and you must administer it.\n'
           'Enter your application repository later in the application section.',
           '为部署管理填写一个专用私有 GitHub 仓库，\n'
           '例如 your-name/previewmesh-control。\n'
           '仓库不存在时，install 会创建它。\n'
           '已有仓库需包含 PreviewMesh 部署工具，且你的账号需有管理权限。\n'
           '实际应用仓库稍后在“应用”步骤填写。')
    ui.say('If discovery is empty, use OWNER/NAME from the GitHub repository page.', '没有发现候选仓库时，请从 GitHub 仓库页面填写“用户名或组织名/仓库名”。')
    control = ui.choose('Deployment management repository (private)', '部署管理仓库（私有）', repos, validate_repo)
    current_repos = discover_repositories(Path.cwd())
    template_repos = ([previous.public] if previous else [])
    if Path.cwd().resolve() == Path(root).resolve():
        template_repos += current_repos
    if not template_repos:
        template_repos = ['flashrick/PreviewMesh']
    ui.say('The public PreviewMesh code repository provides the installer and deployment tools.\n'
           'When creating a new deployment management repository,\n'
           'install copies this code into it.\n'
           'Usually keep flashrick/PreviewMesh;\n'
           'choose another repository only when using a compatible PreviewMesh fork.',
           '公开的 PreviewMesh 代码仓库提供安装器和部署工具。\n'
           '新建部署管理仓库时，install 会以这里的代码为基础创建它。\n'
           '通常使用 flashrick/PreviewMesh；\n'
           '使用兼容的 PreviewMesh 分支仓库时才需要更改。')
    data['project'] = dict(control_repository=control, control_directory=control_dir,
                           public_repository=ui.choose('PreviewMesh code repository (public)', 'PreviewMesh 代码仓库（公开）', template_repos, validate_repo),
                           language=language)
    ui.say('\n[2/5] Preview access: choose the server address, allowed client network and preview domain.',
           '\n[2/5] 预览访问：确定服务器地址、允许访问的网段和预览域名。')
    environment = choose_network_environment(ui)
    ips = discover_windows_lan() if environment == 'wsl' else discover_lan()
    if ips:
        ui.say('LAN IPv4 addresses were detected automatically.\n'
               'Confirm the address colleagues can reach:\n'
               'press Enter for a single candidate, or choose from multiple candidates.\n'
               'You can also enter an address manually.',
               '已自动获取局域网 IPv4 地址。\n'
               '请确认同事能访问的地址：\n'
               '只有一个候选时可直接回车，多个候选时请选择；也可以手动填写。')
    elif environment == 'wsl':
        ui.say('No Windows LAN address was found or PowerShell could not run.\n'
               'Check Windows interoperability in WSL and retry init.\n'
               'For a virtual bridge or other custom network, find the Windows host LAN IPv4\n'
               'in Windows Settings > Network and enter it manually.',
               '未发现 Windows 局域网地址，或 PowerShell 未能执行。\n'
               '请检查 WSL 的 Windows 互操作功能后重试 init。\n'
               '使用虚拟桥接等自定义网络时，可在 Windows“设置 > 网络”\n'
               '查看主机的局域网 IPv4，并手动填写。')
    else:
        ui.say('No Ubuntu LAN address was found.\n'
               'Check that this is Ubuntu outside WSL and that its network is connected.\n'
               'You can find the LAN IPv4 in Ubuntu Settings > Network and enter it manually.',
               '未发现 Ubuntu 局域网地址。\n'
               '请确认这是非 WSL 的 Ubuntu 且网络已连接。\n'
               '也可以在 Ubuntu“设置 > 网络”查看局域网 IPv4，并手动填写。')
    if previous:
        ui.say(f'Previously configured LAN IPv4: {previous.lan_ip}.\n'
               'It is also listed below for review.',
               f'已有配置中的局域网 IPv4：{previous.lan_ip}。\n'
               '下面也会列出这个地址，供你核对。')
        ips.insert(0, previous.lan_ip)
    data['network'] = dict(lan_ip=ui.choose('Preview server LAN IPv4', '预览服务器的局域网 IPv4', ips, private_ip))
    ui.say('The allowed subnet limits which client addresses can access previews.\n'
           'auto uses the actual network and prefix length of the interface\n'
           'with your selected LAN IPv4.\n'
           'You can also enter a private IPv4 subnet containing that address,\n'
           'such as 192.168.1.0/24.',
           '允许访问的网段决定哪些客户端地址能打开预览。\n'
           'auto 会使用所选局域网 IPv4 所属网卡的实际网络和前缀长度。\n'
           '也可以填写包含该地址的私有 IPv4 网段，例如 192.168.1.0/24。')
    data['network']['allowed_subnet'] = ui.ask('Allowed client subnet', '允许访问的客户端网段', previous.subnet if previous else 'auto')
    ui.say('The domain suffix is the end of each preview hostname,\n'
           'allowing different pull requests to have their own URLs.',
           '域名后缀是每个预览域名的结尾，\n'
           '用来为不同的拉取请求（PR）生成各自的访问地址。')
    ui.say('Recommended: auto uses <LAN IPv4>.sslip.io.\n'
           'Clients on the allowed network open the generated URL\n'
           'without hosts edits when DNS resolves.',
           '推荐 auto：自动使用 sslip.io，DNS 正常时无需修改 hosts。')
    ui.say('Alternative: enter a custom DNS suffix and configure wildcard DNS\n'
           'or per-host hosts entries on each client.\n'
           'Include previewmesh-check.<suffix> on this installer.',
           '替代：填写手工后缀，并配置通配 DNS 或逐台客户端 hosts。\n'
           '安装机还需配置探测域名。')
    ui.say('Access guide: ' + ACCESS_GUIDE, '访问指南：' + ACCESS_GUIDE)
    data['network']['domain_suffix'] = ui.ask('Preview domain suffix', '预览域名后缀',
                                          previous.domain_suffix if previous else 'auto', validate_domain_suffix)
    secrets = Path.home() / '.config/previewmesh/secrets'
    ui.say('\n[3/5] Shared credentials:\n'
           'Choose separate token file paths outside your Git directories.\n'
           'install explains how to create each token\n'
           'and offers a hidden input when its file is missing.',
           '\n[3/5] 共用凭据：为每种 Token 指定独立文件路径，放在 Git 仓库目录之外。\n'
           'install 会说明 Token 的创建方式，文件缺失时提供隐藏输入。')
    ui.say('The deployment notification token lets application workflows start deployment\n'
           'workflows in your private deployment management repository.\n'
           'This token is authorized for that management repository;\n'
           'enter the path of the file that will hold it.',
           '部署通知 Token 让应用的工作流能启动私有部署管理仓库中的部署工作流。\n'
           '这个 Token 授权给部署管理仓库。\n'
           '这里填写用于保存它的文件路径。')
    data['credentials'] = dict(dispatch_token_file=ui.ask('Deployment notification token file', '部署通知 Token 文件路径', previous.dispatch_file if previous else secrets / 'dispatch.token', token_location))
    ui.say('The image download token lets the cluster pull preview images\n'
           'from GitHub Container Registry (GHCR), including private images.\n'
           'Enter a different file path for this read:packages token.',
           '镜像下载 Token 让集群能从 GitHub 镜像仓库（GHCR）拉取预览镜像，\n'
           '包括私有镜像。\n'
           '请为这个 read:packages Token 填写另一个文件路径。')
    data['credentials']['ghcr_token_file'] = ui.ask('Image download token file (GHCR)', '镜像下载 Token 文件路径（GHCR）', previous.ghcr_file if previous else secrets / 'ghcr.token', token_location)
    ui.say('\n[4/5] Applications:\n'
           'Register each application you want PreviewMesh to build\n'
           'and preview for pull requests.',
           '\n[4/5] 应用：\n'
           '逐个登记需要由 PreviewMesh 为拉取请求（PR）构建和部署预览的应用。')
    sources = previous.sources if previous else [None]
    index = 0
    while True:
        old = sources[index] if index < len(sources) else None
        name = old.name if old else f'app{index + 1}'
        cwd = Path.cwd()
        default_dir = cwd if cwd.resolve() != Path(root).resolve() and cwd.resolve() != Path(control_dir).resolve() else Path(root).parent / name
        ui.say(f'\nApplication {index + 1}: this local directory holds the application code.\n'
               'install can clone the application repository into a new directory\n'
               'and prepares a notification workflow here for review.\n'
               'Use a directory separate from PreviewMesh code and deployment management.',
               f'\n应用 {index + 1}：应用代码本地目录用于存放实际应用源码。\n'
               '目录不存在时，install 会从应用仓库下载代码，\n'
               '并在这里准备通知工作流文件供你审核。\n'
               '请与 PreviewMesh 代码目录、部署管理目录分开。')
        directory = ui.ask('Application code local directory', '应用代码本地目录', old.directory if old else default_dir, location)
        repos = discover_repositories(directory)
        if old:
            repos.insert(0, old.repository)
        ui.say('Enter this application\'s GitHub repository, such as your-name/your-app.\n'
               'Pull requests in this repository trigger its previews\n'
               'after the notification workflow is merged.',
               '填写这个应用的 GitHub 仓库，例如 your-name/your-app。\n'
               '通知工作流合并后，该仓库的拉取请求（PR）会触发预览。')
        application = dict(repository=ui.choose('Application repository', '应用源码仓库', repos, validate_repo),
                           directory=directory, port=application_port(directory, old.port if old else None, language=language))
        ui.say('The application access token lets deployment automation read this repository\n'
               'and update its pull request feedback.\n'
               'Authorize it for this application only and give it its own file,\n'
               'separate from the notification and image tokens.\n'
               'Required repository permissions:\n'
               '  Contents: Read-only\n'
               '  Metadata: Read-only\n'
               '  Pull requests: Read and write\n'
               '  Commit statuses: Read and write (publishes the PreviewMesh commit status).',
               '应用访问 Token 让部署自动化读取这个应用仓库，\n'
               '并更新拉取请求的反馈信息。\n'
               '请仅授权给这个应用，并使用独立文件，与部署通知和镜像下载 Token 分开。\n'
               '所需仓库权限（Repository permissions）：\n'
               '  Contents：Read-only（只读）\n'
               '  Metadata：Read-only（只读）\n'
               '  Pull requests：Read and write（读写）\n'
               '  Commit statuses：Read and write（读写，用于回写 PreviewMesh 提交状态）。')
        application['token_file'] = ui.ask('Application access token file', '应用访问 Token 文件路径', old.token_file if old else secrets / f'{name}.token', token_location)
        data[f'source:{name}'] = application
        index += 1
        if index >= len(sources):
            ui.say('Choose yes to register another application\n'
                   'with its own repository, directory, port and token file.\n'
                   'no continues to the final review.',
                   '输入 yes 可继续登记另一个应用的仓库、目录、端口和 Token 文件。\n'
                   '输入 no 则进入最后的配置检查。')
            if not ui.confirm('Add another application', '添加另一个应用'):
                break
    content = io.StringIO()
    data.write(content)
    # Validate in memory before displaying or writing anything from the configuration.
    try:
        safe(content.getvalue().replace('\n', ''))
        config = load_config(path, root, content=content.getvalue())
    except (SetupError, OSError, ValueError):
        raise SetupError(ui.text('Configuration validation failed. Check distinct repositories/directories, subnet and separate token paths outside checkouts; rerun init.',
                                 '配置校验失败，请检查仓库和目录不重复、网段有效、各 Token 路径独立且在仓库外，再运行 init。'))
    # Preserve the fully validated candidate before the final review so Ctrl+C
    # cannot make a later install silently fall back to an older configuration.
    atomic_write(draft_path, content.getvalue())
    ui.say('\n[5/5] Review and save:\n'
           'Check the repository roles and access settings\n'
           'before writing the configuration.',
           '\n[5/5] 检查并保存：确认仓库角色与访问设置，再写入配置文件。')
    ui.say(f'Review:\nConfiguration: {path}\nDeployment management repository (private): {config.control}\nDeployment management local directory: {config.control_dir}\nPreviewMesh code repository (public): {config.public}\nLanguage: {config.language}\nPreview server LAN IPv4: {config.lan_ip}\nAllowed client subnet: {config.subnet}\nPreview domain suffix: {config.suffix}',
           f'确认摘要：\n配置文件：{path}\n部署管理仓库（私有）：{config.control}\n部署管理本地目录：{config.control_dir}\nPreviewMesh 代码仓库（公开）：{config.public}\n语言：{config.language}\n预览服务器的局域网 IPv4：{config.lan_ip}\n允许访问的客户端网段：{config.subnet}\n预览域名后缀：{config.suffix}')
    for source in config.sources:
        ui.say(f'Application repository: {source.repository}\n'
               f'Local directory: {source.directory}\n'
               f'Container HTTP port: {source.port}',
               f'应用源码仓库：{source.repository}\n'
               f'代码本地目录：{source.directory}\n'
               f'容器内 HTTP 端口：{source.port}')
    url = f'http://pm-r<repository_id>-pr<PR>.{config.suffix}:18080'
    ui.say('Preview URL format: ' + url, '预览地址格式：' + url)
    ui.say('18080 is the preview entry port; application HTTP ports remain separate.', '18080 为预览入口端口，与应用 HTTP 端口不同。')
    ui.say('Credentials: separate file references only; values never read.', '凭据：仅保存独立文件引用，不读取凭据内容。')
    ui.say(f'Deployment notification token file: {config.dispatch_file}\nImage download token file (GHCR): {config.ghcr_file}',
           f'部署通知 Token 文件：{config.dispatch_file}\n镜像下载 Token 文件（GHCR）：{config.ghcr_file}')
    for source in config.sources:
        ui.say(f'{source.name} application access token file: {source.token_file}', f'{source.name} 应用访问 Token 文件：{source.token_file}')
    ui.say('Saving writes these settings to the configuration file.\n'
           'The next install command uses them to install services\n'
           'and configure the GitHub repositories.',
           '保存会把以上设置写入配置文件。\n'
           '随后运行 install，才会使用这些设置安装服务并配置 GitHub 仓库。')
    if not ui.confirm('Save configuration (replace existing file if present)', '保存配置（若已存在则替换）'):
        ui.say(f'Cancelled; no active configuration changed. Draft saved at {draft_path}.',
               f'已取消；当前配置未修改。草稿已保存到 {draft_path}。')
        return False
    try:
        atomic_write(path, content.getvalue())
        if draft_path.exists():
            draft_path.unlink()
    except OSError:
        raise SetupError(ui.text('Cannot save configuration; check destination permissions and rerun init.', '无法保存，请检查目标目录权限后重新运行 init。'))
    ui.say('Configuration saved. Run setup.sh install with the same --config.', '配置已保存，请使用相同 --config 运行 setup.sh install。')
    return True
