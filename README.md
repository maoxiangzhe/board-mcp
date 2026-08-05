# board-mcp

> 一个MCP小工具，MCP公告板，可以让ai工具协调工作。

多终端 AI 协作公告板 MCP 服务器。让并行工作的 Claude Code / Codex / OpenCode / Trae
在**撞车前**协调好"谁在改哪个文件"。

## 安装（只注册 MCP 工具）

要求：**Python 3.10+**，以及至少一个目标终端。

```bash
cd board-mcp
python install.py                 # 自动检测已安装的 CLI 并注册
python install.py --target all    # 注册到全部 4 个终端
python install.py --target codex  # 只注册 Codex（可逗号分隔多个）
python install.py --dry-run       # 演练：只看不改
python install.py --check         # 检查注册状态
```

| 终端 | MCP 注册位置 |
|------|--------------|
| Claude Code | `claude mcp add board`（幂等，路径不一致自动重注册） |
| Codex | `~/.codex/config.toml` → `[mcp_servers.board-mcp]`（保留 tools.* 权限子段） |
| OpenCode | `~/.config/opencode/opencode.json` → `mcp.board` |
| Trae | `%APPDATA%\Trae CN\|Trae\User\mcp.json` → `mcpServers.board` |

装完**重启对应终端会话**，即可使用 7 个工具：
`get_board` / `claim_files` / `report_done` / `check_conflict` / `release_claim`
/ `post_decision` / `init_bulletin`。

- 幂等可重复执行，改动前自动备份到 `%TEMP%\board_install_backup\`
- `--project` 只影响 Claude 的注册范围，其余终端按用户级安装

## 协作规则

每个项目第一次使用时调用 `init_bulletin` 初始化公告板，之后按流程走：
开工 `get_board` → 认领 `claim_files` → 干活 → 收尾 `report_done`。
项目根目录的 `AGENTS.md` / `CLAUDE.md` 会自动注入协作纪律（见 `template.md`）。

## 结构

```
board-mcp/
├── server.py         # MCP 服务器（公告板引擎）
├── install.py        # 多终端 MCP 注册安装器
├── test_demo.py      # 自测（12 项断言）
├── template.md       # 注入项目的协作规则模板
├── AGENTS.md         # AI 协作纪律（本目录）
└── CLAUDE.md         # Claude Code 规则副本
```