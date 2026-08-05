# 功能自测：验证 git 身份共享 / 标记文件 / 取消认领 / 历史修剪
import os
import subprocess
import tempfile
from pathlib import Path

# 公告板写入独立的临时目录（必须在 import server 之前设置，
# 因为 server 在导入时就固定了 BOARD_ROOT）：
# 保证测试可重复运行（上次的认领不会残留），也不污染真实的 ~/.board-mcp
os.environ['BOARD_MCP_ROOT'] = tempfile.mkdtemp(prefix='board-test-')

import server

# 测试板是真实文件：每次运行都会往板上写认领/汇报记录，
# 不清理的话下次运行会撞上自己上次留下的"干活中"认领。
# 跑之前先清掉本套件用到的测试板，保证可重复执行。
for _stale in server.BOARD_DIR.glob('demo-project_*.md'):
    _stale.unlink()
_stale = server.BOARD_DIR / 'marker-proj.md'
if _stale.exists():
    _stale.unlink()

PASS = 0

def check(name, cond):
    global PASS
    if cond:
        PASS += 1
        print(f'  [OK] {name}')
    else:
        print(f'  [FAIL] {name}')
        raise SystemExit(1)


def git(cwd, *args):
    subprocess.run(['git'] + list(args), cwd=str(cwd), capture_output=True, text=True)


# ---- 场景 1：两个不同目录、同一个 git remote -> 共享同一块板 ----
print('=== 场景 1：git 身份共享 ===')
root = Path(tempfile.mkdtemp())
repo_url = 'https://github.com/me/demo-project.git'

dir_a = root / 'gongzuomulu'
dir_b = root / 'gongzuomulu-copy'
for d in (dir_a, dir_b):
    d.mkdir(parents=True)
    git(d, 'init', '-q')
    git(d, 'config', 'remote.origin.url', repo_url)

os.chdir(dir_a)
print(server.init_bulletin())
print(server.claim_files('T1', '登录重构', 'src/auth/login.py'))

os.chdir(dir_b)
board_b = server.get_board()
print(board_b)
check('B 目录能看到 A 目录的认领', 'T1' in board_b and '登录重构' in board_b)

# ---- 场景 2：非 git 目录 + .board-project 标记文件 ----
print('=== 场景 2：标记文件兜底 ===')
dir_c = root / 'folder-copy'
dir_c.mkdir(parents=True)
(dir_c / '.board-project').write_text('marker-proj', encoding='utf-8')
os.chdir(dir_c)
print(server.init_bulletin())
check('标记文件项目建了自己的板', 'marker-proj' in server.get_board())
check('标记文件板文件名正确', (server.BOARD_DIR / 'marker-proj.md').exists())

# ---- 场景 3：取消认领 release_claim ----
print('=== 场景 3：取消认领 ===')
os.chdir(dir_a)
print(server.claim_files('T2', '支付', 'src/payments/*'))
print(server.release_claim('T2'))
board_after = server.get_board()
check('T2 认领已取消', '已取消' in board_after)
check('T2 文件已释放，T3 可以认领', '支付模块接手' in server.claim_files('T3', '支付模块接手', 'src/payments/*'))

# ---- 场景 4：历史修剪 ----
print('=== 场景 4：历史修剪（最多 20 条） ===')
for i in range(1, 26):
    server.claim_files(f'agent-{i}', f'任务{i}', f'src/f{i}.py')
    server.report_done(f'agent-{i}', f'完成{i}', f'src/f{i}.py')
board_final = server.get_board()

claims_lines = [ln for ln in board_final.splitlines()
                if ln.strip().startswith('|') and '终端' not in ln and '---' not in ln]
changes_lines = [ln for ln in board_final.splitlines() if ln.strip().startswith('- [')]
done_rows = [ln for ln in claims_lines if '已汇报' in ln]
check('认领表最多 20 条已释放记录', len(done_rows) <= 20)
check('变更流水最多 20 条', len(changes_lines) <= 20)

# ---- 场景 5：post_decision 写共享决策（回归保护，防后续改动破坏） ----
# 注意：与 get_board 一样走默认项目解析（当前 git remote），
# 不能显式传 project——显式参数会解析到另一块板，写读就错位了。
print('=== 场景 5：写共享决策 ===')
os.chdir(dir_a)
print(server.post_decision('arch', '绘图操作统一 { op, payload } 格式'))
board_dec = server.get_board()
check('决策写入共享决策区', '{ op, payload }' in board_dec)
check('决策带 agent 和时间戳', '[arch]' in board_dec)

# 追加第二条，验证累积（旧内容不丢）
print(server.post_decision('dev', '本地渲染与同步解耦，rAF 节流'))
board_dec2 = server.get_board()
check('第二条决策累积', 'rAF 节流' in board_dec2 and '{ op, payload }' in board_dec2)

# 空参数拒绝
check('空 agent 拒绝', 'agent 不能为空' in server.post_decision('', 'x'))
check('空 decision 拒绝', 'decision 不能为空' in server.post_decision('a', ''))

print(f'\n全部通过：{PASS} 项')

