# -*- coding: utf-8 -*-
'''
server.py —— MCP 公告栏的服务器本体
====================================

这个文件是整个项目的核心。它做的事情用一句话概括：
    把一个 Markdown 公告板（board.md）包装成几个 MCP 工具，
    让 Codex / Claude Code 这些终端 AI 可以"看板、认领、汇报"。

为什么是 MCP？
    MCP（Model Context Protocol）是一种标准协议：
    任何支持 MCP 的 AI 客户端（Codex、Claude Code 等）都能直接调用
    本文件提供的工具，不需要为每个客户端单独写适配代码。

本文件只做一件事：定义工具。真正的数据存在磁盘上的 Markdown 文件里，
服务器本身不保存任何状态 —— 这保证就算服务器重启，公告板也不会丢。

运行方式（等所有文件做好后再执行）：
    uv run python server.py

本文件目前的七个工具：
    1. init_bulletin   初始化：生成必读文件 + 创建公告板
    2. get_board       看板：读取公告板全文
    3. claim_files     认领：声明我要动哪些文件（撞车会被拒绝）
    4. report_done     汇报：干完了，写结果（自动检查有没有撞车）
    5. check_conflict  查冲突：这些文件有没有被别人占着
    6. release_claim   取消认领：终端掉线/任务取消时释放文件
    7. post_decision   写决策：往共享决策区追加一条约定（协议/方案/结论）

项目身份怎么定？（重要）
    公告板按"项目"分文件。项目身份按优先级推导：
        1. 调用工具时显式传 project 参数
        2. 环境变量 BOARD_MCP_PROJECT
        3. git 仓库的 remote.origin.url（同一仓库无论 clone 几个副本、
           开几个 worktree，URL 都一样 -> 天然共享同一块板）
        4. 目录里的 .board-project 标记文件（非 git 目录用这个）
        5. 最后兜底：当前文件夹名
    这样"gongzuomulu"和"gongzuomulu-copy"两个同仓库副本会看到同一块板，
    而不是像之前那样各看各的。

其他机制：
    - 认领 TTL：占用中认领超过 2 小时（BOARD_CLAIM_TTL_MINUTES 可调）未更新，
      自动标"已过期"释放文件——治"终端掉线占坑"；get_board/check_conflict 时惰性清理。
    - 心跳：后台线程每 20 秒写一次心跳文件（含真实启动时间），
      启动自检据此清扫僵尸服务器进程。
    - 并发安全：读-改-写全程持跨进程文件锁（Windows msvcrt / Unix fcntl），
      写文件用"临时文件 + os.replace"原子替换。
'''

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# 引入 MCP 官方 SDK 的 FastMCP。
# FastMCP 是一个"装饰器"框架：把普通 Python 函数变成 MCP 工具，
# 我们不用关心协议的细节，只要写普通函数 + 一行装饰器。
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# 第一部分：配置
# ---------------------------------------------------------------------------

# 公告板默认存放在用户主目录下的 .board-mcp 文件夹里。
# 为什么不放项目里？因为多个终端可能在同一个项目的不同工作副本上干活，
# 如果公告板在项目里，每个工作副本会各有一份，就失去"共享"的意义了。
# 放在主目录，所有终端读写的是同一份文件。
BOARD_ROOT = Path(os.environ.get('BOARD_MCP_ROOT', Path.home() / '.board-mcp'))
BOARD_DIR = BOARD_ROOT / 'boards'
LOG_DIR = BOARD_ROOT / 'logs'          # 运行日志目录，出问题先看这里
RUN_DIR = BOARD_ROOT / 'run'           # 心跳文件目录，清理脚本靠它认僵尸
TS_FORMAT = '%Y-%m-%d %H:%M:%S'        # 板内统一时间格式（写板/解析共用）
HEARTBEAT_INTERVAL = 20.0              # 心跳写入间隔（秒）
CLAIM_TTL_SECONDS = int(os.environ.get('BOARD_CLAIM_TTL_MINUTES', '120')) * 60
#   认领 TTL：默认 2 小时，可用环境变量 BOARD_CLAIM_TTL_MINUTES 覆盖；
#   超过 TTL 没更新的占用中认领会被标成已过期，不再占文件（治掉线占坑）。
STALE_HEARTBEAT_SECONDS = 120          # 启动自检：心跳超过这个秒数视为僵尸残留
_GIT_URL_CACHE: dict[str, str | None] = {}   # git 查询结果缓存

# 必读文件模板的路径。模板内容单独放一个 template.md，
# 方便你以后直接改模板，不用动代码。
TEMPLATE_PATH = Path(__file__).parent / 'template.md'

# 状态机的取值：
#   - 已认领 / 干活中：文件被占用中，别人不能再碰
#   - 已汇报 / 已取消：文件释放了，别人可以认领
ACTIVE_STATUSES = {'已认领', '干活中'}     # 占用中的状态
DONE_STATUSES = {'已汇报', '已取消', '已过期'}   # 已释放的状态（已过期 = TTL 自动释放）

# 历史记录上限：认领表和变更流水最多各保留多少条。
# 公告板只增不减会越滚越大，写的时候顺手剪掉最旧的。
MAX_HISTORY = 20

# 创建 MCP 服务器实例。名字 'board' 会显示在客户端的工具列表里。
mcp = FastMCP('board')


# ---------------------------------------------------------------------------
# 第二部分：项目身份识别
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    '''
    名字要当文件名用，所以把不安全的字符（如空格、/、\、:）替换成 _，
    防止路径出错。这是写文件前的常规安全检查。
    '''
    return re.sub(r'[^\w\u4e00-\u9fff-]', '_', name)


def _git_remote_url() -> str | None:
    '''
    读取当前目录所属 git 仓库的 remote.origin.url（结果按目录缓存，避免每次调用都起子进程）。
    读不到就返回 None。子进程强制不继承 stdin：否则它攥着 MCP 管道，
    客户端退出后服务器永远收不到 EOF，变成僵尸进程。
    '''
    try:
        cwd = str(Path.cwd())
    except OSError:
        return None
    if cwd in _GIT_URL_CACHE:
        return _GIT_URL_CACHE[cwd]
    kwargs = {}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ['git', 'config', '--get', 'remote.origin.url'],
            capture_output=True, timeout=5, cwd=cwd,     # 二进制捕获（不用 text=True）：
            stdin=subprocess.DEVNULL, **kwargs,          # Windows 下 text=True 用 GBK 解码，
        )                                                # git 输出非 ASCII（中文仓库名/警告）时
    except (OSError, subprocess.TimeoutExpired):         # 读取线程崩溃 -> stdout=None -> 工具直接报错
        _GIT_URL_CACHE[cwd] = None
        return None
    stdout = result.stdout or b''
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', errors='replace')
    url = stdout.strip() if result.returncode == 0 else ''
    _GIT_URL_CACHE[cwd] = url or None
    return _GIT_URL_CACHE[cwd]


def _repo_name_from_url(url: str) -> str:
    '''
    从 remote URL 里抠出仓库显示名，比如：
        https://github.com/me/my-project.git  -> my-project
        git@github.com:me/other.git          -> other
    '''
    name = url.rstrip('/')
    if name.endswith('.git'):
        name = name[:-4]          # 删后缀要用 endswith + 切片，不能用 rstrip
    name = name.split('/')[-1].split(':')[-1]
    return name or 'repo'


def _resolve_project(project: str | None) -> tuple[str, str]:
    '''
    解析"当前是哪个项目"，返回 (项目ID, 显示名)。
        - 项目ID：唯一标识，用来当公告板文件名
        - 显示名：给人看的，写在公告板标题里
    优先级链从最稳到最兜底：
        显式参数 -> 环境变量 -> git remote -> .board-project 标记文件 -> 文件夹名
    '''
    # 1. 调用工具时显式指定（最高优先，适合手动指定）
    if project:
        return _safe_name(project), project
    # 2. 环境变量（适合统一给所有终端设置）
    env = os.environ.get('BOARD_MCP_PROJECT', '').strip()
    if env:
        return _safe_name(env), env
    # 3. git remote：同一仓库的克隆/工作副本共享同一块板。
    #    文件名 = 仓库名 + URL 哈希后 6 位。
    #    哈希是为了防止"两个不同仓库恰好同名"时撞到同一块板。
    url = _git_remote_url()
    if url:
        name = _repo_name_from_url(url)
        digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:6]
        return f'{_safe_name(name)}_{digest}', name
    # 4. 标记文件：非 git 目录用这个。复制目录时把 .board-project 一起拷走，
    #    项目身份就跟着文件走，不跟目录名走。
    marker = Path.cwd() / '.board-project'
    if marker.exists():
        try:
            marker_name = marker.read_text(encoding='utf-8').strip()
        except OSError:
            marker_name = ''
        if marker_name:
            return _safe_name(marker_name), marker_name
    # 5. 兜底：当前文件夹名（和旧版行为一致）
    try:
        cwd_name = Path.cwd().name
    except OSError:
        cwd_name = 'unknown'
    return _safe_name(cwd_name), cwd_name


def _resolve_source(project: str | None) -> str:
    '''
    返回当前项目身份的"解析来源"描述（get_board 身份注释行用）。
    与 _resolve_project 的优先级链保持一致，让 AI 一眼确认
    "我看到的这块板是从哪来的"——对不上号时能自查，而不是自己造板。
    '''
    if project:
        return '显式 project 参数'
    if os.environ.get('BOARD_MCP_PROJECT', '').strip():
        return '环境变量 BOARD_MCP_PROJECT'
    if _git_remote_url():
        return 'git remote: origin'
    if (Path.cwd() / '.board-project').exists():
        return '.board-project 标记文件'
    return '文件夹名'


# ---------------------------------------------------------------------------
# 第三部分：小工具函数（不直接暴露给 AI，只是内部帮手）
# ---------------------------------------------------------------------------

def _now() -> str:
    '''返回当前时间字符串，格式：2026-08-02 15:30:00'''
    return datetime.now().strftime(TS_FORMAT)


def _parse_ts(s: str) -> datetime | None:
    '''解析板内时间戳；失败返回 None，调用方按"已过期/残留"处理。'''
    try:
        return datetime.strptime(s, TS_FORMAT)
    except (ValueError, TypeError):
        return None


def _log(msg: str) -> None:
    '''写一行运行日志：stderr + ~/.board-mcp/logs/server.log。'''
    line = f'[{_now()}] [pid {os.getpid()}] {msg}'
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / 'server.log', 'a', encoding='utf-8') as f:
            print(line, file=f)
    except OSError:
        pass
    try:
        print(f'[board] {msg}', file=sys.stderr, flush=True)
    except OSError:
        pass


_HEARTBEAT_FILE: Path | None = None
_HEARTBEAT_STOP = threading.Event()
_HEARTBEAT_STARTED = ''              # 启动时间，main 里记一次；心跳刷新原样保留


def _heartbeat_path() -> Path:
    return RUN_DIR / f'server-{os.getpid()}.json'


def _write_heartbeat() -> None:
    '''写/刷新心跳文件；客户端死掉后心跳停更，清理脚本据此识别僵尸。
    started 在 main 里记一次，心跳刷新只更新 heartbeat——否则每次心跳
    都会把"启动时间"改写成当前时间，僵尸判定就失效了。'''
    if _HEARTBEAT_FILE is None:
        return
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        _HEARTBEAT_FILE.write_text(json.dumps(
            {'pid': os.getpid(), 'started': _HEARTBEAT_STARTED, 'heartbeat': _now()},
            ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def _stop_heartbeat() -> None:
    '''停心跳线程并删除心跳文件（正常退出/崩溃清理共用）。'''
    _HEARTBEAT_STOP.set()
    if _HEARTBEAT_FILE is not None:
        try:
            _HEARTBEAT_FILE.unlink()
        except OSError:
            pass


def _heartbeat_loop() -> None:
    '''后台线程定期刷新心跳，作为清理脚本识别僵尸进程的依据。'''
    while not _HEARTBEAT_STOP.wait(HEARTBEAT_INTERVAL):
        _write_heartbeat()


def _read_board_text(path: Path, tolerant: bool = False) -> str:
    '''读公告板；tolerant=True 时遇到坏编码不崩，用替换符兜底（只读工具用）。'''
    if tolerant:
        return path.read_text(encoding='utf-8', errors='replace')
    return path.read_text(encoding='utf-8')


def _board_path(project_id: str) -> Path:
    '''公告板文件路径：~/.board-mcp/boards/<项目ID>.md'''
    return BOARD_DIR / f'{project_id}.md'


def _new_board(name: str) -> str:
    '''
    生成一个空公告板的全文。
    公告板分三块：认领区（谁在动什么）、共享决策（约定）、最新变更（流水账）。
    '''
    return f'''# MCP 公告栏：{name}

> 最后更新：{_now()}

## 认领区

| 终端 | 任务 | 文件 | 状态 | 更新时间 |
|------|------|------|------|----------|

## 共享决策

- 暂无

## 最新变更

- 暂无
'''


def _split_sections(text: str) -> dict[str, list[str]]:
    '''
    把公告板文本按 "## 标题" 拆成几块，返回：
        {'认领区': [行1, 行2, ...], '共享决策': [...], '最新变更': [...]}
    这是 Markdown 的"最简解析"，只认 ## 开头的二级标题。
    '''
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith('## '):
            current = line[3:].strip()      # 遇到新标题，切换当前块
            sections[current] = []
        elif current is not None:
            sections[current].append(line)  # 否则把行塞进当前块
    return sections


def _parse_claims(text: str) -> list[dict]:
    '''
    从公告板文本里解析出"认领表"，返回列表，每个元素是一个认领记录：
        [{'agent': 'T1', 'task': '登录重构', 'files': 'src/auth/*',
          'status': '干活中', 'time': '...'}, ...]
    认领区是一张 Markdown 表格，我们逐行拆开：
        | 终端 | 任务 | 文件 | 状态 | 更新时间 |
        |------|------|------|------|----------|     <- 分隔行，跳过
        | T1   | ...  | ...  | ...  | ...      |     <- 数据行，保留
    '''
    rows: list[dict[str, str]] = []
    section = _split_sections(text).get('认领区', [])
    for line in section:
        line = line.strip()
        if not line.startswith('|'):
            continue
        # 去掉首尾的 |，再按 | 切开，就是一个个单元格
        cells = [c.strip() for c in line.strip('|').split('|')]
        # 跳过表头行（第一个单元格是"终端"）和分隔行（全是 - 和 :）
        if cells and cells[0] == '终端':
            continue
        if cells and all(re.fullmatch(r':?-+:?', c) for c in cells):
            continue
        if len(cells) >= 5:
            rows.append({
                'agent': cells[0],
                'task': cells[1],
                'files': cells[2],
                'status': cells[3],
                'time': cells[4],
            })
    return rows


def _esc_cell(s) -> str:
    '''Markdown 表格单元格转义：| 和换行都会破坏表格，先转义掉。
    换行替换成空格：AI 的 task/summary 里带换行很常见，不处理会把表格撑断，
    重读时那一行会错乱甚至丢失。'''
    return str(s).replace('\n', ' ').replace('|', '\\|')


def _claims_table(rows: list[dict[str, str]]) -> str:
    '''
    反向操作：把认领记录列表渲染回 Markdown 表格文本。
    读的时候"表 -> 列表"，写的时候"列表 -> 表"。
    '''
    lines = [
        '| 终端 | 任务 | 文件 | 状态 | 更新时间 |',
        '|------|------|------|------|----------|',
    ]
    for r in rows:
        lines.append(
            f"| {_esc_cell(r['agent'])} | {_esc_cell(r['task'])} | {_esc_cell(r['files'])} | "
            f"{_esc_cell(r['status'])} | {_esc_cell(r['time'])} |"
        )
    return '\n'.join(lines)


def _change_lines(text: str) -> list[str]:
    '''取"最新变更"区的现有流水行（无内容时返回空列表）。'''
    return (_section_block(text, '最新变更') or '').splitlines()


def _board_updated(text: str) -> str | None:
    '''从板头提取"最后更新"时间戳（渲染不落盘时沿用旧值）。'''
    m = re.search(r'> 最后更新：([^\n]+)', text)
    return m.group(1).strip() if m else None


def _decision_body(line: str) -> str:
    '''从决策行 "- [agent] 2026-08-04 17:00:00 正文" 里抠出正文；非决策行原样返回。'''
    parts = line.strip().split(' ', 4)
    return parts[4].strip() if len(parts) == 5 else line.strip()


def _section_block(text: str, name: str) -> str | None:
    '''
    取某一块的正文（去掉空行和"- 暂无"占位符），返回拼接后的字符串。
    用来保留"共享决策"和"最新变更"的已有内容，更新认领区时不会弄丢它们。
    '''
    lines = _split_sections(text).get(name, [])
    kept = [ln for ln in lines if ln.strip() and ln.strip() != '- 暂无']
    return '\n'.join(kept) or None


def _render_board(name: str, old_text: str, claims: list[dict], changes: list[str],
                  decisions: str | None = None,
                  last_updated: str | None = None) -> str:
    '''
    重建整个公告板文本。
    认领区用新的认领列表渲染；共享决策 = 传入的新决策，缺省时从 old_text 原样保留；
    最新变更 = 旧流水 + 新流水。
    last_updated：板头时间戳。缺省用当前时间（写盘路径刷新时间）；
    get_board 渲染路径应传文件里的旧值，避免"看到的板头时间比文件新"的误导。
    '''
    decisions = decisions if decisions is not None else (_section_block(old_text, '共享决策') or '- 暂无')
    changes_block = '\n'.join(changes) if changes else '- 暂无'
    ts = last_updated if last_updated is not None else _now()
    return f'''# MCP 公告栏：{name}

> 最后更新：{ts}

## 认领区

{_claims_table(claims)}

## 共享决策

{decisions}

## 最新变更

{changes_block}
'''


def _expire_claims(claims: list[dict[str, str]]) -> tuple[list[dict[str, str]], int, list[str]]:
    '''
    TTL 惰性清理：把超过 CLAIM_TTL_SECONDS 还没更新的占用中认领标成已过期。
    返回 (认领列表, 本次过期条数, 本次过期的 agent 列表)。
    只改内存，是否落盘由调用方决定。
    时间戳解析失败按已过期处理：宁可错放，不可错占。
    '''
    now = datetime.now()
    expired = 0
    expired_agents: list[str] = []
    for r in claims:
        if r.get('status') not in ACTIVE_STATUSES:
            continue
        t = _parse_ts(r.get('time', ''))
        stale = True if t is None else (now - t).total_seconds() > CLAIM_TTL_SECONDS
        if stale:
            r['status'] = '已过期'
            expired += 1
            expired_agents.append(r['agent'])
    return claims, expired, expired_agents


def _prune_claims(claims: list[dict[str, str]]) -> list[dict[str, str]]:
    '''
    历史修剪：认领表只保留"占用中的" + 最近 MAX_HISTORY 条已释放的。
    防止认领表无限膨胀（干一天活，表里堆几百条已汇报记录）。
    '''
    active = [r for r in claims if r['status'] in ACTIVE_STATUSES]
    done = [r for r in claims if r['status'] in DONE_STATUSES]
    done.sort(key=lambda r: r['time'], reverse=True)   # 按时间倒序，留最新的
    return active + done[:MAX_HISTORY]


def _file_list(files: str) -> list[str]:
    '''把 "a.py, b.py" 这种逗号分隔的字符串拆成列表，去掉空项。'''
    return [f.strip() for f in (files or '').split(',') if f.strip()]


def _overlap(a: str, b: str) -> bool:
    '''
    判断两个文件（或路径模式）是否重叠。
    规则很简单：
        - 完全一样 -> 重叠
        - 一个是另一个的上级目录 -> 重叠（改 src/auth/ 的人会影响 src/auth/login.py）
    "src/auth/*" 会先去掉末尾的 * 再比较，当成目录处理。
    比较前统一转小写：Windows/macOS 文件系统不区分大小写，
    "src/Auth/login.py" 与 "src/auth/login.py" 是同一个文件，不转会漏判撞车。
    这是启发式判断，不求精确，够用就行。
    '''
    a = a.strip().rstrip('*').rstrip('/').lower()
    b = b.strip().rstrip('*').rstrip('/').lower()
    if not a or not b:
        return False
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


def _find_conflicts(claims: list[dict], files: str, agent: str = '') -> list[tuple[dict, str, str]]:
    '''
    核心查冲突逻辑：给定一个文件清单，看有没有"别人的占用中认领"和它重叠。
    返回 [(认领记录, 我的文件, 对方的文件), ...]。
    '''
    hits: list[tuple] = []
    for r in claims:
        # 自己的认领不算冲突；已释放的认领也不算冲突
        if r['agent'] == agent or r['status'] not in ACTIVE_STATUSES:
            continue
        for f in _file_list(files):
            for owned in _file_list(r['files']):
                if _overlap(f, owned):
                    hits.append((r, f, owned))
    return hits


def _describe_conflicts(hits: list[tuple[dict, str, str]], mode: str = 'block') -> str:
    '''把冲突清单渲染成逐行文本；mode='block' 用于认领被拒/查冲突，
    mode='warn' 用于汇报撞车提示（措辞不同）。'''
    if mode == 'warn':
        return '\n'.join(
            f"- {f} 与 {r['agent']}（{r['task']}）的 {owned} 重叠" for r, f, owned in hits)
    return '\n'.join(
        f"- {f} 被 {r['agent']}（{r['task']}）认领：{owned}，状态：{r['status']}" for r, f, owned in hits)


# ---------------------------------------------------------------------------
# 第四部分：跨进程文件锁
# ---------------------------------------------------------------------------

def _file_lock(lock_path: Path, timeout: float = 5.0) -> int:
    '''
    多终端会同时读写同一块公告板，必须加锁防互相覆盖。
    用 .lock 文件 + 系统文件锁实现：Windows 用 msvcrt，Linux/Mac 用 fcntl。
    拿不到锁就每 0.05 秒重试，最多等 timeout 秒。
    '''
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        os.write(fd, bytes(1))        # 锁文件里必须有内容才能锁（写入 1 字节占位）
    except OSError:
        pass
    deadline = time.time() + timeout
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # 非阻塞尝试加锁
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() > deadline:
                os.close(fd)
                _log(f'lock timeout: {lock_path}')
                raise TimeoutError(f'公告板被占用，等待锁超时：{lock_path}')
            time.sleep(0.05)


def _file_unlock(fd: int) -> None:
    '''释放文件锁并关闭文件。
    锁文件刻意不删：若 A 解锁删锁、B 锁着旧 fd 重试、C 新建锁文件，
    B 与 C 会同时持有"锁"互不知晓（删除-重建竞态），read-modify-write 可能丢更新。
    锁文件永久保留 1 字节，所有进程锁同一个文件，天然无竞态；残留也无害。'''
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_atomic(path: Path, text: str) -> None:
    '''原子写入：先写临时文件再整体替换。临时文件名带 pid，避免并发时互相覆盖。'''
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def _update_board(project: str | None,
                   mutate: Callable[[str, str], tuple[str | None, str]]) -> str:
    '''
    统一的"读-改-写"入口：
        加锁 -> 读当前公告板 -> 调用 mutate(旧文本, 显示名) 得到 (新文本, 回复消息)
        -> 写回（如果新文本不是 None）-> 解锁 -> 返回回复消息
    mutate 是调用方传入的一个函数，负责具体的修改逻辑。
    '''
    project_id, project_name = _resolve_project(project)
    path = _board_path(project_id)
    lock_path = path.with_suffix('.lock')
    fd = _file_lock(lock_path)
    try:
        # 写路径也 tolerant 读：板文件一旦被写坏（并发手改、编码事故），
        # 写工具不能跟着崩——替换符兜底后仍能正常读-改-写。
        existed = path.exists()
        text = _read_board_text(path, tolerant=True) if existed else _new_board(project_name)
        new_text, msg = mutate(text, project_name)
        if new_text is not None:
            _write_atomic(path, new_text)
        if not existed:
            # 首次写盘（任何写工具都可能是第一个）：明说创建了哪块板，
            # AI 不会以为自己在"凭空操作"，也不会再去造一个。
            msg = f'已为新项目创建公告板（ID：{project_id}）。\n' + msg
        return msg
    finally:
        _file_unlock(fd)


# ---------------------------------------------------------------------------
# 第五部分：七个 MCP 工具（AI 实际会调用这些）
# ---------------------------------------------------------------------------

@mcp.tool()
def init_bulletin(project: str | None = None, include_claude: bool = False) -> str:
    '''
    初始化：每个终端接入公告栏时第一个调用。
    做两件事：
        1. 在当前目录生成必读文件 AGENTS.md（AI 每次对话都会自动读到它，
           里面写着协作纪律，等于给 AI 装上"开工前先看板"的规矩）；
        2. 创建本项目的公告板文件（如果还没有）。
    幂等：必读文件和公告板已存在时不会覆盖，所以可以放心反复调用。

    参数：
        project         项目名，不传就自动识别（git remote -> 标记文件 -> 文件夹名）
        include_claude  设为 True 时额外生成 CLAUDE.md（给 Claude Code 用）

    非 git 目录下会自动生成 .board-project 标记文件（内容 = 项目名），
    文件夹从此绑定固定项目身份：以后从任何副本/目录进入都解析到同一块板。
    '''
    project_id, project_name = _resolve_project(project)

    # --- 1. 创建公告板文件 ---
    board_path = _board_path(project_id)
    board_path.parent.mkdir(parents=True, exist_ok=True)
    if not board_path.exists():
        _write_atomic(board_path, _new_board(project_name))

    # --- 2. 生成必读文件 ---
    # 模板内容从 template.md 读；文件不存在时用内置的默认文本兜底
    if TEMPLATE_PATH.exists():
        rules = TEMPLATE_PATH.read_text(encoding='utf-8')
    else:
        rules = (
            '# 必读：MCP 公告栏协作规则\n\n'
            '1. 开工前先调用 get_board 看板，再调用 claim_files 认领你要动的文件。\n'
            '2. 绝不修改其他终端已认领的文件。\n'
            '3. 收尾时调用 report_done 汇报改动和结果。\n'
            '4. 拿不准时调用 check_conflict 查冲突。\n'
        )

    # --- 3. 固定项目身份：非 git 目录自动写 .board-project ---
    # 文件夹从此"绑定"这块板：以后从任何副本/目录进入，项目身份都一致。
    created = []
    if _git_remote_url() is None:
        marker = Path.cwd() / '.board-project'
        if not marker.exists():
            marker.write_text(project_name + '\n', encoding='utf-8')
            created.append(str(marker))

    to_write = ['AGENTS.md'] + (['CLAUDE.md'] if include_claude else [])
    for fname in to_write:
        target = Path.cwd() / fname
        if not target.exists():
            target.write_text(rules, encoding='utf-8')
            created.append(str(target))

    created_text = '、'.join(created) if created else '已存在，未改动'
    return (f'公告板就绪：{board_path}\n'
            f'项目：{project_name}（ID：{project_id}）\n'
            f'必读文件：{created_text}\n'
            '下一步：调用 get_board 看板，然后用 claim_files 认领文件。')


@mcp.tool()
def get_board(project: str | None = None) -> str:
    '''
    看板：读取本项目的公告板全文（认领区 + 共享决策 + 最新变更）。
    开工前必看。返回的就是那个 Markdown 文件的内容。
    '''
    project_id, project_name = _resolve_project(project)
    path = _board_path(project_id)
    if not path.exists():
        # 关键分支：AI 最容易在这里迷路（不知道自己在哪块板、不知道去哪初始化）。
        # 把项目身份和正确动作一次性给全，杜绝"自己造一块板"的兜底行为。
        return (f'公告板还不存在。\n'
                f'当前项目：{project_name}（ID：{project_id}，解析自{_resolve_source(project)}）\n'
                f'请调用 init_bulletin 初始化。禁止自行创建板文件。')
    text = _read_board_text(path, tolerant=True)
    # 头部加身份注释行：AI 每次看板都确认"我看的是哪块板、从哪解析来的"。
    # 不污染板内容，get_board 之外的工具读写都不受影响。
    head = f'<!-- 项目：{project_name}（ID：{project_id}，解析自{_resolve_source(project)}） -->\n'
    claims, expired, _ = _expire_claims(_parse_claims(text))
    if not expired:
        return head + text   # 快路径：没有过期认领，原样返回
    changes = _change_lines(text)
    # 渲染不落盘：板头时间戳沿用文件里的值，别让"看到的"比文件新
    last_updated = _board_updated(text)
    return head + _render_board(project_name, text, claims, changes, last_updated=last_updated)


@mcp.tool()
def claim_files(agent: str, task: str, files: str, project: str | None = None) -> str:
    '''
    认领：声明"我是谁、我在做什么、我要动哪些文件"。
    files 用逗号分隔，例如：src/auth/login.py, src/auth/schemas.py
    如果这些文件和别的终端的占用中认领重叠，会被直接拒绝，并告诉你撞了谁。
    同 agent 重复认领 = 续期：与本次文件重叠的旧声明作废、TTL 重新计时；
    不重叠的认领保留（同 agent 可同时持有多个文件组的认领）。

    参数：
        agent   你的名字/代号（比如 T1、coder-1）
        task    一句话说明任务
        files   要动的文件或目录，逗号分隔
    '''
    if not (agent or '').strip():
        return 'agent 不能为空。'
    if not (task or '').strip():
        return 'task 不能为空。'
    if not (files or '').strip():
        return 'files 不能为空。'

    def mutate(text, project_name):
        claims, _, _ = _expire_claims(_parse_claims(text))
        # 续期：同 agent 的占用中认领里，只作废"与本次文件重叠"的部分
        # （覆盖旧声明、刷新 TTL）。不重叠的认领必须保留——否则认领 a.py 后
        # 再认领 b.py，a.py 的认领会被无声删掉，别的终端会以为 a.py 没人碰。
        claimed = _file_list(files)
        claims = [r for r in claims
                  if not (r['agent'] == agent and r['status'] in ACTIVE_STATUSES
                          and any(_overlap(f, owned) for f in claimed
                                  for owned in _file_list(r['files'])))]
        # 先查冲突：有没有别人正占着这些文件
        hits = _find_conflicts(claims, files, agent)
        if hits:
            return None, ('认领被拒绝，以下文件已被其他终端认领：\n'
                          + _describe_conflicts(hits)
                          + '\n请换文件，或先和对方协调。')
        # 没冲突，写入认领表
        claims.append({
            'agent': agent,
            'task': task,
            'files': files,
            'status': '干活中',
            'time': _now(),
        })
        claims = _prune_claims(claims)
        # 提示：同 agent 名下还保留着其他不重叠的占用中认领。
        # 注意 agent 名是自由填写的，两个终端若同名会互相续期/释放——
        # 多终端协作时请用全局唯一的 agent 名（如 名字-项目-编号）。
        # 过滤后仍保留的同 agent 占用中认领，必然与本次不重叠（重叠的已在上方作废）
        kept_other = [r for r in claims
                      if r['agent'] == agent and r['status'] in ACTIVE_STATUSES
                      and r['files'] != files]   # 排除刚写入的本次认领自己
        extra = ''
        if kept_other:
            extra = '\n注意：该终端名下还有其他占用中认领：' \
                + '、'.join(r['files'] for r in kept_other) \
                + '（已保留，不影响本次认领）'
        return _render_board(project_name, text, claims, _change_lines(text)), \
            f'认领成功：{agent} 负责 {task}，文件：{files}' + extra

    return _update_board(project, mutate)


@mcp.tool()
def report_done(agent: str, summary: str, files: str = '', project: str | None = None) -> str:
    '''
    汇报：干完了。做三件事：
        1. 把自己的占用中认领标记为"已汇报"（释放文件）；
        2. 在"最新变更"里写一条流水（最多保留 MAX_HISTORY 条，旧的自动剪掉）；
        3. 自动检查这次改动有没有和别人撞车（如果撞了会提醒，不拦截）。
    如果自己的认领已超 TTL 过期，会提示先重新 claim_files 再汇报。

    参数：
        agent    你的名字/代号（要和认领时一致）
        summary  干了什么、结果如何
        files    这次实际改动的文件，逗号分隔（建议填，用于自动查冲突）
    '''
    if not (agent or '').strip():
        return 'agent 不能为空。'
    if not (summary or '').strip():
        return 'summary 不能为空。'

    def mutate(text, project_name):
        claims, _, _ = _expire_claims(_parse_claims(text))
        # 找自己的占用中认领，全部标记为已汇报
        my_active = [r for r in claims
                     if r['agent'] == agent and r['status'] in ACTIVE_STATUSES]
        if not my_active:
            hint = '（你之前的认领已过期，需重新 claim_files）' \
                if any(r['agent'] == agent and r['status'] == '已过期' for r in claims) else ''
            return None, f'没有找到 {agent} 的占用中认领，确认你之前调用过 claim_files 吗？{hint}'
        ts = _now()
        for r in my_active:
            r['status'] = '已汇报'
            r['time'] = ts

        # 自动查冲突：我的改动有没有撞到别人的占用中认领
        warn = ''
        hits = _find_conflicts(claims, files or '', agent)
        if hits:
            warn = '\n注意：这次改动和以下终端撞车了，建议尽快对齐：\n' \
                + _describe_conflicts(hits, mode='warn') + '\n'

        # 追加一条变更流水，超出的旧记录自动剪掉
        changes_list = (_change_lines(text) + [f'- [{agent}] {ts} {summary}'])[-MAX_HISTORY:]

        claims = _prune_claims(claims)
        return _render_board(project_name, text, claims, changes_list), \
            f'已汇报：{agent} 的认领已释放。{summary}' + warn

    return _update_board(project, mutate)


@mcp.tool()
def check_conflict(files: str, agent: str = '', project: str | None = None) -> str:
    '''
    查冲突：开工前或收尾前调用，确认这些文件没有被别人占用。
    files 用逗号分隔。返回冲突清单；没有冲突会明说"没有冲突"。
    过期认领（超 TTL 已自动释放）不算冲突，但会提示原属终端。
    '''
    if not (files or '').strip():
        return 'files 不能为空。'
    project_id, _ = _resolve_project(project)
    path = _board_path(project_id)
    if not path.exists():
        return '公告板还不存在，请先调用 init_bulletin 初始化。'
    text = _read_board_text(path, tolerant=True)
    claims, expired, expired_agents = _expire_claims(_parse_claims(text))
    hits = _find_conflicts(claims, files, agent)
    extra = ''
    if expired:
        names = sorted(set(expired_agents))   # 只列本次新过期的，和计数一致
        extra = f'（另有 {expired} 条认领已过期（超 TTL），原属：' + '、'.join(names) + '）\n'
    if not hits:
        return extra + '没有冲突，这些文件目前是安全的。'
    return extra + '发现冲突：\n' + _describe_conflicts(hits) + '\n'


@mcp.tool()
def release_claim(agent: str, project: str | None = None) -> str:
    '''
    取消认领：把某个终端的所有占用中认领标记为"已取消"，释放文件。
    用在：任务取消了、终端掉线了、认领卡住没人动。
    注意：只能取消占用中的认领；已汇报/已取消的不受影响。
    认领已过期（超 TTL 自动释放）时无需再取消，工具会提示。
    '''
    if not (agent or '').strip():
        return 'agent 不能为空。'

    def mutate(text, project_name):
        claims, _, _ = _expire_claims(_parse_claims(text))
        released = [r for r in claims
                    if r['agent'] == agent and r['status'] in ACTIVE_STATUSES]
        if not released:
            hint = '（你之前的认领已过期，无需再取消）' \
                if any(r['agent'] == agent and r['status'] == '已过期' for r in claims) else ''
            return None, f'{agent} 没有占用中的认领。{hint}'
        ts = _now()
        for r in released:
            r['status'] = '已取消'
            r['time'] = ts
        claims = _prune_claims(claims)
        return _render_board(project_name, text, claims, _change_lines(text)), \
            f'已取消 {agent} 的 {len(released)} 条认领，文件已释放。'

    return _update_board(project, mutate)


@mcp.tool()
def post_decision(agent: str, decision: str, project: str | None = None) -> str:
    '''
    写决策：往共享决策区追加一条约定，所有终端 get_board 时都能读到。
    用于定协议、记方案取舍、留踩坑结论——凡是"需要别人看见并遵守"的内容。

    共享决策区的内容会被保留和累积，不随认领表滚动清理；
    追加时带上 agent 和时间戳，方便追溯是谁、在什么时候定的。

    参数：
        agent     你的名字/代号（要和认领时一致）
        decision  决策内容，一句话讲清楚"定了什么、为什么"
    '''
    if not (agent or '').strip():
        return 'agent 不能为空。'
    if not (decision or '').strip():
        return 'decision 不能为空。'

    def mutate(text, project_name):
        # 与其它工具一致：先惰性过期，渲染时不会把过期认领显示成"干活中"
        claims, _, _ = _expire_claims(_parse_claims(text))
        ts = _now()
        text_decision = decision.strip()
        # 保留现有共享决策内容（去掉空行和"- 暂无"占位符），追加一条新决策；
        # 与已有内容之间隔一个空行，Markdown 渲染更清晰
        existing = _section_block(text, '共享决策')
        # 去重：同内容决策重复写入只是叠行占板，直接拒绝。
        # 按"正文"行级匹配（子串匹配会误伤"协议 v2"这类包含已有内容的合法新决策）。
        if existing and any(_decision_body(ln) == text_decision
                            for ln in existing.splitlines()):
            return None, f'该决策已存在，未重复写入：{text_decision}'
        new_line = f'- [{agent}] {ts} {text_decision}'
        decisions = f'{existing}\n\n{new_line}' if existing else new_line
        # 同步写一条变更流水，方便按时间线检索决策历史
        new_log = f'- [{agent}] {ts} 决策：{text_decision}'
        changes_list = (_change_lines(text) + [new_log])[-MAX_HISTORY:]
        # 认领区原样保留（不触碰别人的认领）
        return _render_board(project_name, text, claims, changes_list, decisions), \
            f'已写入共享决策：{text_decision}'

    return _update_board(project, mutate)


# ---------------------------------------------------------------------------
# 第六部分：启动自检
# ---------------------------------------------------------------------------

def _startup_cleanup() -> None:
    '''
    启动自检：清扫残留的服务器心跳文件。
    心跳超过 STALE_HEARTBEAT_SECONDS 的视为僵尸服务器残留：
        尽力终止对应进程（可能已死，失败忽略），再删掉心跳文件。
    无法解析的文件也直接删——活着的服务器 20 秒后会重写自己的心跳，删了无副作用。
    '''
    try:
        if not RUN_DIR.exists():
            return
        now = datetime.now()
        for f in RUN_DIR.glob('server-*.json'):
            pid = None
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('心跳文件不是 JSON 对象')
                pid = int(data.get('pid') or 0)
                hb = datetime.strptime(data.get('heartbeat', ''), TS_FORMAT)
                if (now - hb).total_seconds() <= STALE_HEARTBEAT_SECONDS:
                    continue          # 心跳新鲜：健康服务器，跳过
            except (ValueError, TypeError, OSError):
                pass                  # 无法解析：按残留处理
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    _log(f'startup cleanup: killed stale server pid={pid}')
                except OSError:
                    pass              # 进程已不存在
            try:
                f.unlink()
            except OSError:
                pass
        # 顺带清理原子写崩溃残留的 .tmp 文件（无害，但别越积越多）
        if BOARD_DIR.exists():
            for f in BOARD_DIR.glob('.*.tmp'):
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 第七部分：入口
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    _startup_cleanup()
    _HEARTBEAT_FILE = _heartbeat_path()
    _HEARTBEAT_STARTED = _now()
    _write_heartbeat()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = '?'
    _log(f'board-mcp {os.getpid()} starting, cwd={cwd}')
    try:
        mcp.run(transport='stdio')
    except BaseException as exc:
        _log(f'board-mcp crashed: {type(exc).__name__}: {exc}')
        _log(traceback.format_exc())
        raise
    finally:
        _log(f'board-mcp {os.getpid()} stopped')
        _stop_heartbeat()
