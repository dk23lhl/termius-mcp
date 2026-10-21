# Termius MCP

[English](README.md)

**让 AI 直接协作于你正在使用的 Termius 会话。**

读取终端、执行命令、切换会话、跟踪 SFTP 上传，都可以从 Codex 等 MCP 客户端发起。
保留你已配置的主机和熟悉的 Termius 窗口，让 Agent 通过明确的会话 ID、终端文本和传输状态完成重复操作。

本项目基于 [deltaguita/termius-mcp](https://github.com/deltaguita/termius-mcp)，
适配 **macOS 上的 Termius 9.38.1**，属于社区项目，与 Termius 官方无隶属关系。

## 它改善了 Computer Use 的哪些痛点？

Computer Use 适合导航界面、选择面板和处理弹窗。终端操作还需要准确读取文本、定位会话、跟踪运行状态，
反复截图和点击会让这些步骤变得繁琐。Termius MCP 为这些操作提供了专用工具。

| 截图与点击流程中的困难 | 本项目提供的能力 |
| --- | --- |
| 长日志不断滚动，需要多次截图读取 | 直接读取 xterm 缓冲区，通过 marker 跟踪命令输出；历史丢失时明确报告，最多返回 200 行。 |
| 多个标签页间切换，容易丢失目标会话 | 读取和输入使用明确的会话 ID，并提供切换、关闭指定会话的工具。 |
| 屏幕静止了，但不清楚命令是否结束 | 返回超时、等待输入或观察到提示符等状态；提示符仍是启发式判断，不代表退出码为零。 |
| 需要盯着 SFTP 底部进度条判断是否成功 | 按传输 ID 跟踪原生进度、错误和完成事件；等待超时后继续查询同一任务。 |
| 多个 Agent 的终端输入相互穿插 | 通过认领和跨进程锁协调 MCP 调用；不阻止人手输入。 |

两者可以配合使用：Computer Use 完成界面准备和弹窗处理，MCP 承担重复的终端与传输操作。

例如，你可以直接说：“列出 Termius 会话，读取我选中会话的输出”，或者
“把这个文件上传到已经准备好的 SFTP 目录，持续查询同一传输并报告结果”。
无需手动把终端输出复制回聊天。

## 支持什么

- 读取已保存的主机、终端会话和终端内容。
- 连接已保存的主机，认领终端后执行命令或发送按键。
- 切换到指定终端，关闭已认领的终端。
- 通过原生 SFTP 队列上传单个普通文件，跟踪进度、错误及最终状态。

这里直接操作 Termius 已有的会话与传输队列，没有另建独立 SSH/SFTP 客户端。
其他 Termius 版本会在连接调试接口前被拒绝；尚未实现 Windows、Linux 桌面版支持。

## 安装与使用

需要安装在 `/Applications/Termius.app` 的 Termius 9.38.1，以及 Python 3.11 以上版本。
验证环境使用 Python 3.12。Node.js 仅用于开发测试，运行 MCP 不需要它。

```sh
git clone https://github.com/dk23lhl/termius-mcp.git
cd termius-mcp
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

先保存工作并退出 Termius，再按需开启本机调试接口。重启会断开现有终端连接。

```sh
open -a /Applications/Termius.app --args --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222
lsof -nP -iTCP:9222 -sTCP:LISTEN
```

确认只监听 `127.0.0.1:9222` 后再使用。**本机其他进程也能通过此接口访问 renderer 中的数据**；
回环地址不等于身份认证。用完后退出 Termius，再普通启动，端口即不再开启。
本 MCP 不会自动开启端口或重启应用。详见 [安全说明](SECURITY.md)。

将 [配置样例](codex-mcp.toml.example) 合并到 `~/.codex/config.toml`，把两处
`/absolute/path/to/termius-mcp` 替换成实际目录，保留原有配置。客户端未加载时，重启或重新加载客户端。
不要同时启用本项目与上游服务，两者共用 renderer bridge 和锁目录。

在 Codex 中通过 `/mcp` 查看状态，然后直接说“用 termius MCP 列出主机和终端会话”。
MCP 配置本身不会生成同名的 `@` 应用或 `/` 技能入口。样例启用 14 个工具，排除了身份元数据工具
`list_keychain`。连接、输入和上传应依据用户的明确请求执行。

## SFTP 上传

先在 Termius 打开一个 SFTP 页面，一侧连接 Local，另一侧连接目标主机并进入目标目录。
调用 `list_sftp_sessions` 获取远端会话 ID，再向 `sftp_upload` 提供本地绝对路径、该 ID 和目标目录。
本版本只上传单个普通文件，不上传目录或符号链接；`connect_host` 自动连接的是终端，SFTP 面板仍需手动准备。

等待超时不会取消上传。使用返回的 `transfer_id` 查询 `sftp_transfer_status`，不要重复上传。
工具读取原生队列的进度、错误和结束事件，不是识别进度条截图。100% 或队列行消失都不单独代表成功。
覆盖冲突等原生弹窗留给用户处理。

## 结果边界与测试

`prompt-observed` 只表示观察到疑似提示符，不代表远端命令退出码为零。输出来自终端缓冲区，
最多返回 200 行；滚动历史丢失或窗口改变会报告 `history-lost`。认领机制只协调 MCP 调用者，不阻止人手输入。

```sh
.venv/bin/python -m pytest tests -q
npm ci --ignore-scripts
npm test
```

离线测试不连接 Termius 或远端主机。真实验证范围和未覆盖场景见 [验证说明](docs/VALIDATION.md)。
上游 MIT 版权声明保留在 [LICENSE](LICENSE)，本仓库不包含 Termius 安装包或其打包源码。
