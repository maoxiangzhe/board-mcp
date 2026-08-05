#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
board-workflow 通用安装器
========================

一键把 board MCP 服务器 装进 4 种 AI 终端：

    claude     Claude Code   claude mcp add（幂等）
    codex      Codex CLI     ~/.codex/config.toml
    opencode   OpenCode      ~/.config/opencode/opencode.json
    trae       Trae          %APPDATA%\\Trae CN|Trae\\User\\mcp.json

用法：
    python install.py                  # 自动检测已安装的 CLI，逐个安装
    python install.py --target all     # 4 个终端全装（不检测，强制）
    python install.py --target codex   # 只装 Codex（可逗号分隔：claude,trae）
    python install.py --dry-run        # 演练：只检查环境，不实际写入
    python install.py --check          # 检查各终端安装状态（可按 --target 过滤）
    python install.py --project        # Claude 注册到当前项目级（默认用户级，仅影响 claude）

装完重启对应终端会话，即可使用 7 个 board 工具：
    get_board / claim_files / report_done / check_conflict /
    release_claim / post_decision / init_bulletin
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVER_NAME = 'board'            # claude / opencode / trae 里的服务器名
CODEX_SERVER_NAME = 'board-mcp'  # codex 沿用现有命名，避免破坏既有权限配置
HERE = Path(__file__).resolve().parent
SERVER_PY = HERE / 'server.py'
TOOLS = ('get_board / claim_files / report_done / check_conflict / '
         'release_claim / post_decision / init_bulletin')
TARGETS = ('claude', 'codex', 'opencode', 'trae')
TARGET_LABELS = {
    'claude': 'Claude Code',
    'codex': 'Codex CLI',
    'opencode': 'OpenCode',
    'trae': 'Trae',
}

def run(cmd: list[str], dry_run: bool) -> bool:
    '''执行命令（dry_run 时只打印不执行）。返回是否成功。'''
    print('  $', ' '.join(cmd))
    if dry_run:
        return True
    return subprocess.run(cmd).returncode == 0

def ensure_python() -> bool:
    '''检查 Python >= 3.10（server.py 的语法要求）。'''
    ok = sys.version_info >= (3, 10)
    print(f'[1] Python {sys.version.split()[0]} '
          f'({"满足要求 >=3.10" if ok else "不满足，请先安装 Python 3.10+"})')
    return ok

def install_deps(dry_run: bool) -> bool:
    '''安装 mcp SDK（服务器唯一依赖）。'''
    try:
        import mcp  # noqa: F401
        print('[2] mcp SDK 已安装，跳过')
        return True
    except ImportError:
        print('[2] 安装 mcp SDK（唯一依赖）...')
        return run([sys.executable, '-m', 'pip', 'install', 'mcp>=1.5,<2'], dry_run)

def backup_file(path: Path) -> None:
    '''改动前备份到 %TEMP%/board_install_backup/，防止误改配置。'''
    if not path.exists():
        return
    bdir = Path(os.environ.get('TEMP', Path.home())) / 'board_install_backup'
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    name = str(path).replace(':', '_').replace('\\', '_').replace('/', '_')
    shutil.copy2(path, bdir / f'{name}.{stamp}.bak')

def _toml_literal(value: str) -> str:
    '''TOML 字符串：优先单引号字面量（Windows 路径反斜杠免转义）。'''
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

def update_codex(python_cmd: str, server_py: Path, dry_run: bool) -> bool:
    '''更新 ~/.codex/config.toml 的 [mcp_servers.board-mcp] 段，保留其他配置与子段。'''
    cfg = Path.home() / '.codex' / 'config.toml'
    header = f'[mcp_servers.{CODEX_SERVER_NAME}]'
    prefix = f'[mcp_servers.{CODEX_SERVER_NAME}.'
    new_block = [
        header,
        'type = "stdio"',
        f'command = {_toml_literal(python_cmd)}',
        f'args = [{_toml_literal(str(server_py))}]',
    ]
    raw = cfg.read_text(encoding='utf-8') if cfg.exists() else ''
    lines = raw.splitlines()
    sep = '\r\n' if '\r\n' in raw else '\n'
    idx = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if idx is None:
        new_lines = lines + ([''] if lines else []) + new_block
        print(f'      新增 {header} -> {cfg}')
    else:
        end = idx + 1
        while end < len(lines):
            s = lines[end].strip()
            if s.startswith('[') and not s.startswith(prefix):
                break
            end += 1
        sub_start = next((i for i in range(idx + 1, end)
                          if lines[i].strip().startswith(prefix)), None)
        kept = lines[sub_start:end] if sub_start is not None else []
        if kept:
            kept = [''] + kept
        new_lines = lines[:idx] + new_block + kept + lines[end:]
        print(f'      更新 {header} -> {cfg}（保留 {max(len(kept) - 1, 0)} 行子配置）')
    if new_lines == lines:
        print(f'      {cfg} 已是最新，跳过')
        return True
    if not dry_run:
        backup_file(cfg)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(sep.join(new_lines) + sep, encoding='utf-8')
    return True

def update_json(path: Path, dry_run: bool, mutate) -> bool:
    '''就地更新 JSON 配置（保留其他键）。mutate(dict) 修改数据。'''
    data = {}
    if path.exists():
        raw = path.read_text(encoding='utf-8')
        if raw.strip():
            try:
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                print(f'      读取失败 {path}（{exc}），跳过')
                return False
    before = json.dumps(data, ensure_ascii=False, indent=2)
    mutate(data)
    after = json.dumps(data, ensure_ascii=False, indent=2)
    if before == after:
        print(f'      {path} 已是最新，跳过')
        return True
    print(f'      {"将更新" if dry_run else "更新"} {path}')
    if not dry_run:
        backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after + '\n', encoding='utf-8')
    return True

def _claude_registration(claude: str) -> tuple[bool, str, str]:
    '''查询 claude 里 board 的注册信息，返回 (是否已注册, command, args)。'''
    check = subprocess.run([claude, 'mcp', 'get', SERVER_NAME],
                           capture_output=True, text=True,
                           errors='replace')  # claude 输出含 √ 等非 GBK 字节
    if check.returncode != 0:
        return False, '', ''
    cmd = args = ''
    for line in check.stdout.splitlines():
        line = line.strip()
        if line.startswith('Command:'):
            cmd = line[len('Command:'):].strip().strip('"\'')
        elif line.startswith('Args:'):
            args = line[len('Args:'):].strip().strip('"\'')
    return True, cmd, args

def _same_path(a: str, b: str) -> bool:
    '''忽略大小写与斜杠方向比较路径。'''
    return a.replace('/', '\\').lower() == b.replace('/', '\\').lower()

def register_claude(python_cmd: str, server_py: Path, scope: str, dry_run: bool) -> bool:
    '''注册进 Claude Code：已存在且指向一致则跳过，指向旧路径则重注册。'''
    claude = shutil.which('claude')
    if not claude:
        print('      未找到 claude 命令行，跳过（请先安装 Claude Code）')
        return False
    if not dry_run:
        exists, cmd, args = _claude_registration(claude)
        if exists:
            if _same_path(cmd, python_cmd) and _same_path(args, str(server_py)):
                print(f'      MCP 服务器已存在且一致：{SERVER_NAME}，跳过注册')
                return True
            print(f'      MCP 服务器已存在但指向旧路径，重新注册：{SERVER_NAME}')
            run([claude, 'mcp', 'remove', SERVER_NAME, '--scope', scope], dry_run=False)
    print(f'      注册 MCP 服务器（{scope} 级）...')
    ok = run([claude, 'mcp', 'add', SERVER_NAME, '--scope', scope, '--',
              python_cmd, str(server_py)], dry_run)
    if ok:
        print(f'      MCP 服务器已注册：{SERVER_NAME} -> {python_cmd} {server_py}')
    return ok

def register_opencode(python_cmd: str, server_py: Path, dry_run: bool) -> bool:
    '''注册进 OpenCode：~/.config/opencode/opencode.json 顶层 mcp 键。'''
    cfg = Path.home() / '.config' / 'opencode' / 'opencode.json'

    def mutate(data: dict) -> None:
        data.setdefault('mcp', {})[SERVER_NAME] = {
            'type': 'local',
            'command': [python_cmd, str(server_py)],
            'enabled': True,
        }

    print(f'      注册 MCP 服务器：{SERVER_NAME} -> {cfg}')
    return update_json(cfg, dry_run, mutate)

def trae_json_paths() -> list[Path]:
    '''Trae 的 mcp.json 路径：国内版 Trae CN 与国际版 Trae。'''
    appdata = Path(os.environ.get('APPDATA', Path.home()))
    return [appdata / 'Trae CN' / 'User' / 'mcp.json',
            appdata / 'Trae' / 'User' / 'mcp.json']

def register_trae(python_cmd: str, server_py: Path, dry_run: bool) -> bool:
    '''注册进 Trae：写入 mcpServers（Claude 风格）。'''
    paths = trae_json_paths()
    targets = [p for p in paths if p.exists() or p.parent.exists()]
    if not targets:
        targets = [paths[0]]
        print(f'      未检测到 Trae 目录，将写入默认路径 {paths[0]}（可在设置里改）')

    def mutate(data: dict) -> None:
        data.setdefault('mcpServers', {})[SERVER_NAME] = {
            'command': python_cmd,
            'args': [str(server_py)],
        }

    ok = True
    for path in targets:
        print(f'      注册 MCP 服务器：{SERVER_NAME} -> {path}')
        ok &= update_json(path, dry_run, mutate)
    return ok

def register(target: str, python_cmd: str, server_py: Path, scope: str, dry_run: bool) -> bool:
    '''按目标分派 MCP 注册。'''
    if target == 'claude':
        return register_claude(python_cmd, server_py, scope, dry_run)
    if target == 'codex':
        return update_codex(python_cmd, server_py, dry_run)
    if target == 'opencode':
        return register_opencode(python_cmd, server_py, dry_run)
    return register_trae(python_cmd, server_py, dry_run)

def detect_targets() -> list[str]:
    '''auto：检测本机已安装的 CLI。'''
    found = []
    if shutil.which('claude'):
        found.append('claude')
    if shutil.which('codex') or (Path.home() / '.codex' / 'config.toml').exists():
        found.append('codex')
    if shutil.which('opencode') or (Path.home() / '.config' / 'opencode').exists():
        found.append('opencode')
    if any(p.exists() or p.parent.exists() for p in trae_json_paths()):
        found.append('trae')
    return found

def resolve_targets(spec: str) -> list[str]:
    '''把 --target 参数解析成目标列表；未知值抛 ValueError。'''
    if spec == 'all':
        return list(TARGETS)
    if spec == 'auto':
        return detect_targets()
    targets = [t.strip().lower() for t in spec.split(',') if t.strip()]
    bad = [t for t in targets if t not in TARGETS]
    if bad:
        raise ValueError(f'未知目标：{", ".join(bad)}（可选：all / auto / {", ".join(TARGETS)}）')
    return targets

def check_claude() -> tuple[bool, list[str]]:
    ok = True
    lines = []
    claude = shutil.which('claude')
    if claude:
        lines.append(f'  [claude]  cli: {claude}')
        r = subprocess.run([claude, 'mcp', 'get', SERVER_NAME],
                           capture_output=True, text=True, errors='replace')
        reg = r.returncode == 0
        lines.append(f'  [claude]  mcp: {SERVER_NAME} {"已注册" if reg else "未注册"}')
        ok &= reg
    else:
        lines.append('  [claude]  cli: 未检测到 claude，跳过')
    return ok, lines

def check_codex() -> tuple[bool, list[str]]:
    ok = True
    lines = []
    cfg = Path.home() / '.codex' / 'config.toml'
    if cfg.exists():
        has = f'[mcp_servers.{CODEX_SERVER_NAME}]' in cfg.read_text(encoding='utf-8', errors='replace')
        lines.append(f'  [codex]  config: {cfg}  {CODEX_SERVER_NAME} {"已配置" if has else "未配置"}')
        ok &= has
    else:
        lines.append(f'  [codex]  config: 不存在 {cfg}')
        ok = False
    return ok, lines

def check_opencode() -> tuple[bool, list[str]]:
    ok = True
    lines = []
    cfg = Path.home() / '.config' / 'opencode' / 'opencode.json'
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            data = {}
        has = SERVER_NAME in data.get('mcp', {})
        lines.append(f'  [opencode]  config: {cfg}  mcp.{SERVER_NAME} {"已配置" if has else "未配置"}')
        ok &= has
    else:
        lines.append(f'  [opencode]  config: 不存在 {cfg}')
        ok = False
    return ok, lines

def check_trae() -> tuple[bool, list[str]]:
    ok = True
    lines = []
    found = False
    for path in trae_json_paths():
        if not path.exists():
            continue
        found = True
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            data = {}
        has = SERVER_NAME in data.get('mcpServers', {})
        lines.append(f'  [trae]  {path}  mcpServers.{SERVER_NAME} {"已配置" if has else "未配置"}')
        ok &= has
    if not found:
        lines.append('  [trae]  mcp.json 未检测到（Trae CN / Trae 都没装？）')
        ok = False
    return ok, lines

CHECKERS = {
    'claude': check_claude,
    'codex': check_codex,
    'opencode': check_opencode,
    'trae': check_trae,
}

def parse_args(argv: list[str]):
    '''解析命令行参数；非法参数返回 None（调用方以 exit 2 收尾）。'''
    from types import SimpleNamespace
    dry_run = check = project = False
    target = 'auto'
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ('-h', '--help'):
            print(__doc__)
            raise SystemExit(0)
        if arg == '--dry-run':
            dry_run = True
        elif arg == '--check':
            check = True
        elif arg == '--project':
            project = True
        elif arg == '--target':
            i += 1
            if i >= len(argv) or argv[i].startswith('--'):
                print('--target 需要值：all / auto / claude / codex / opencode / trae（可逗号分隔）')
                return None
            target = argv[i]
        elif arg.startswith('--target='):
            target = arg.split('=', 1)[1]
        else:
            print(f'未知参数：{arg}\n\n{__doc__}')
            return None
        i += 1
    return SimpleNamespace(dry_run=dry_run, check=check, project=project,
                           scope='project' if project else 'user', target=target)

def cmd_install(args) -> int:
    targets = resolve_targets(args.target)
    print('board-workflow 通用安装器')
    if args.dry_run:
        print('（演练模式：只检查环境，不实际写入）\n')
    ok = ensure_python()
    ok &= install_deps(args.dry_run)
    if args.project and any(t != 'claude' for t in targets):
        print('（--project 仅影响 Claude 的注册范围，其余终端按用户级安装）')
    src = 'auto 自动检测' if args.target == 'auto' else f'--target {args.target}'
    print(f'目标终端：{"、".join(TARGET_LABELS[t] for t in targets)}（来源：{src}）')
    for target in targets:
        print(f'[{target}] {TARGET_LABELS[target]}')
        ok &= register(target, sys.executable, SERVER_PY, args.scope, args.dry_run)
    print()
    if ok:
        print('[OK] 安装完成。重启对应终端会话后生效：')
        print(f'  7 个 board 工具：{TOOLS}')
    else:
        print('[FAIL] 安装未完成，请按上面提示处理。')
    return 0 if ok else 1

def cmd_check(args) -> int:
    targets = resolve_targets(args.target)
    print('board-workflow 安装状态检查')
    try:
        import mcp  # noqa: F401
        print(f'  [环境]  mcp SDK: 已安装（{sys.executable}）')
        mcp_ok = True
    except ImportError:
        print('  [环境]  mcp SDK: 未安装（运行 install.py 会自动安装）')
        mcp_ok = False
    ok = mcp_ok
    for target in targets:
        target_ok, lines = CHECKERS[target]()
        ok &= target_ok
        print('\n'.join(lines))
    print()
    print('[OK] 全部就绪' if ok else '[FAIL] 有组件缺失，运行 install.py 补装')
    return 0 if ok else 1

def main(argv: list[str]) -> int:
    # Windows 控制台常见 GBK 编码：先加固 stdout，避免特殊符号打印时崩溃
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    args = parse_args(argv)
    if args is None:
        return 2
    try:
        targets = resolve_targets(args.target)
    except ValueError as exc:
        print(exc)
        return 2
    if not targets:
        print('未检测到已安装的 AI 终端（claude / codex / opencode / trae）。')
        print('可用 --target all 强制安装到全部终端，或先安装对应 CLI。')
        return 1
    if args.check:
        return cmd_check(args)
    return cmd_install(args)

if __name__ == '__main__':
    sys.exit(main(sys.argv))
