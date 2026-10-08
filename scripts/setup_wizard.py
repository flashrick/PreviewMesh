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

from setup_config import (ACCESS_GUIDE, SetupError, RFC1918, atomic_write, load_config,
                          validate_domain_suffix, validate_repo)
from setup_language import choose_language


def safe(value):
    # Never reflect URLs containing passwords or common GitHub credential formats.
    if re.search(r"gh[pousr]_|github_pat_|://[^/]*@", value, re.I):
        raise SetupError("Credentials are not configuration / 请勿填写凭据，仅填写凭据文件路径。")
    if any(ord(char) < 32 for char in value):
        raise SetupError("Invalid text / 输入包含控制字符。")
    return value


def capture(command):
    try:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('GIT_TRACE', 'GIT_CONFIG')) and key not in ('GH_DEBUG', 'GIT_CURL_VERBOSE')}
        return subprocess.run(command, capture_output=True, text=True, timeout=5,
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
        return sorted({str(ipaddress.IPv4Address(info['local'])) for item in data
                       if not item.get('ifname', '').startswith(('lo', 'docker', 'br-', 'veth', 'cni', 'flannel'))
                       for info in item.get('addr_info', []) if info.get('scope') == 'global'
                       and any(ipaddress.IPv4Address(info['local']) in net for net in RFC1918)})
    except (ValueError, KeyError, TypeError):
        return []


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
    return ask(label + ' (yes/no)', 'no', lambda v: v if v in ('yes', 'no') else int('invalid'),
               language=language) == 'yes'


def choose(label, candidates, validate=None, *, language='en'):
    candidates = list(dict.fromkeys(candidates))
    for number, value in enumerate(candidates, 1):
        print(f'  {number}. {safe(str(value))}')
    def selected(value):
        if value.isdigit() and 1 <= int(value) <= len(candidates):
            value = str(candidates[int(value) - 1])
        return validate(value) if validate else value
    suffix = ' (编号或手填)' if language == 'zh-CN' else ' (number or manual value)'
    return ask(label + suffix, '1' if len(candidates) == 1 else '', selected,
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


def private_ip(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in net for net in RFC1918):
        raise ValueError()
    return str(address)


def application_port(directory, previous=None, *, language='en'):
    ui = Prompts(language)
    candidates = ([previous] if previous else []) + discover_ports(directory)
    for port in dict.fromkeys(candidates):
        status = ui.text('available', '可用') if port_available(port) else ui.text('occupied or unverified', '已占用或无法验证')
        ui.say(f'Candidate HTTP port {port}: {status}', f'候选 HTTP 端口 {port}：{status}')
    if not candidates:
        ui.say('No application port found. Check Dockerfile EXPOSE or app listen settings; enter the HTTP port.',
               '未发现应用端口，请查看 Dockerfile 或监听配置并填写 HTTP 端口。')
    while True:
        port = int(ui.choose('Application HTTP port', '应用 HTTP 端口', candidates,
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
    safe(str(path))
    ui.say('Configuration wizard. q exits without saving.\nDo not paste tokens here; only token file paths.',
           '配置向导。输入 q 退出且不保存。\n请勿粘贴 Token，仅填写文件路径。')
    if path.is_symlink():
        raise SetupError(ui.text('Configuration must not be a symlink.', '配置不能是符号链接。'))
    previous = None
    if path.exists():
        ui.say('Existing configuration found.', '发现已有配置。')
        if not ui.confirm('Load existing settings and review replacement', '载入已有设置并确认替换'):
            return False
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
    control_dir = ui.ask('Control checkout', 'control 本地目录',
                      previous.control_dir if previous else Path(root).parent / 'previewmesh-control', location)
    repos = discover_repositories(control_dir)
    if previous:
        repos.insert(0, previous.control)
    ui.say('If discovery is empty, use the GitHub repository page OWNER/NAME.', '未发现仓库时，请从 GitHub 页面填写 OWNER/NAME。')
    control = ui.choose('Private control repository', '私有 control 仓库', repos, validate_repo)
    current_repos = discover_repositories(Path.cwd())
    template_repos = ([previous.public] if previous else [])
    if Path.cwd().resolve() == Path(root).resolve():
        template_repos += current_repos
    if not template_repos:
        template_repos = ['flashrick/PreviewMesh']
    data['project'] = dict(control_repository=control, control_directory=control_dir,
                           public_repository=ui.choose('Public template', '公开模板仓库', template_repos, validate_repo),
                           language=language)
    ips = discover_lan()
    if previous:
        ips.insert(0, previous.lan_ip)
    ui.say('Confirm the LAN address; VPN/container IPs may be unsuitable. WSL: Windows Settings > Network > IPv4; Ubuntu: Settings > Network.',
           '请核对实际 LAN 地址，勿选 VPN/容器地址。WSL：Windows 设置 > 网络 > IPv4；Ubuntu：设置 > 网络。')
    data['network'] = dict(lan_ip=ui.choose('LAN IPv4', 'LAN IPv4', ips, private_ip),
                           allowed_subnet=ui.ask('Allowed subnet', '允许网段', previous.subnet if previous else 'auto'))
    ui.say('Recommended: auto uses <LAN IPv4>.sslip.io; clients on the allowed network open the generated URL without hosts edits when DNS resolves.',
           '推荐 auto：自动使用 sslip.io，DNS 正常时无需修改 hosts。')
    ui.say('Alternative: enter a custom DNS suffix and configure wildcard DNS or per-host hosts entries on each client; include previewmesh-check.<suffix> on this installer.',
           '替代：填写手工后缀，并配置通配 DNS 或逐台客户端 hosts；安装机还需配置探测域名。')
    ui.say('Access guide: ' + ACCESS_GUIDE, '访问指南：' + ACCESS_GUIDE)
    data['network']['domain_suffix'] = ui.ask('Domain suffix', '域名后缀',
                                          previous.domain_suffix if previous else 'auto', validate_domain_suffix)
    secrets = Path.home() / '.config/previewmesh/secrets'
    data['credentials'] = dict(dispatch_token_file=ui.ask('Dispatch token FILE', '通知 Token 文件路径', previous.dispatch_file if previous else secrets / 'dispatch.token', token_location),
                                ghcr_token_file=ui.ask('GHCR token FILE', '镜像 Token 文件路径', previous.ghcr_file if previous else secrets / 'ghcr.token', token_location))
    sources = previous.sources if previous else [None]
    index = 0
    while True:
        old = sources[index] if index < len(sources) else None
        name = old.name if old else f'app{index + 1}'
        cwd = Path.cwd()
        default_dir = cwd if cwd.resolve() != Path(root).resolve() and cwd.resolve() != Path(control_dir).resolve() else Path(root).parent / name
        directory = ui.ask('Source checkout', '应用本地目录', old.directory if old else default_dir, location)
        repos = discover_repositories(directory)
        if old:
            repos.insert(0, old.repository)
        data[f'source:{name}'] = dict(repository=ui.choose('Source repository', '应用仓库', repos, validate_repo),
                                      directory=directory, port=application_port(directory, old.port if old else None, language=language),
                                      token_file=ui.ask('Source token FILE', '应用 Token 文件路径', old.token_file if old else secrets / f'{name}.token', token_location))
        index += 1
        if index >= len(sources) and not ui.confirm('Add another source', '添加另一个应用'):
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
    ui.say(f'\nReview:\nConfiguration: {path}\nControl: {config.control}\nCheckout: {config.control_dir}\nTemplate: {config.public}\nLanguage: {config.language}\nLAN: {config.lan_ip}\nSubnet: {config.subnet}',
           f'\n确认摘要：\n配置文件：{path}\nControl：{config.control}\n本地目录：{config.control_dir}\n模板：{config.public}\n语言：{config.language}\nLAN：{config.lan_ip}\n网段：{config.subnet}')
    for source in config.sources:
        ui.say(f'Source: {source.repository}; checkout: {source.directory}; HTTP: {source.port}',
               f'应用：{source.repository}；本地目录：{source.directory}；HTTP：{source.port}')
    url = f'http://pm-r<repository_id>-pr<PR>.{config.suffix}:18080'
    ui.say('Preview URL format: ' + url, '预览地址格式：' + url)
    ui.say('18080 is the preview entry port; application HTTP ports remain separate.', '18080 为预览入口端口，与应用 HTTP 端口不同。')
    ui.say('Credentials: separate file references only; values never read.', '凭据：仅保存独立文件引用，不读取凭据内容。')
    ui.say(f'Dispatch token file: {config.dispatch_file}\nGHCR token file: {config.ghcr_file}',
           f'通知 Token 文件：{config.dispatch_file}\nGHCR Token 文件：{config.ghcr_file}')
    for source in config.sources:
        ui.say(f'{source.name} token file: {source.token_file}', f'{source.name} Token 文件：{source.token_file}')
    if not ui.confirm('Save configuration (replace existing file if present)', '保存配置（若已存在则替换）'):
        ui.say('Cancelled; no file changed.', '已取消，未修改文件。')
        return False
    try:
        atomic_write(path, content.getvalue())
    except OSError:
        raise SetupError(ui.text('Cannot save configuration; check destination permissions and rerun init.', '无法保存，请检查目标目录权限后重新运行 init。'))
    ui.say('Configuration saved. Run setup.sh install with the same --config.', '配置已保存，请使用相同 --config 运行 setup.sh install。')
    return True
