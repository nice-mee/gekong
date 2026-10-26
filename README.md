# gekong

建立在 OpenSSH 连接复用之上的远端工具集：登录一次，Claude Code 就能用原生 Read / Edit / Write 读写远端文件，用 MCP 的 Grep / Glob 在远端搜索，用 `gk` 执行远端命令。结束本地命令时，远端进程组会一起结束。远端只需要 python3（≥ 3.6），不安装任何东西。设计见《gekong 设计文档》。

## 安装

```bash
uv tool install --editable .     # gekong, gk, gekong-mcp, gekong-hook -> ~/.local/bin
gekong setup claude              # hooks、权限、镜像目录、MCP server、skill（会先备份 settings.json）
gekong setup claude --dry-run    # 只打印将要写入的 settings.json
gekong setup claude --uninstall
```

如果 zsh 用了 oh-my-zsh 的 git 插件，需要在插件加载之后 `unalias gk`。

## 使用

```bash
gekong up gpu1                   # 在终端完成 2FA 等认证；之后的调用都复用这条连接
gekong status
gk run gpu1 'nvidia-smi'
gk job start --name train gpu1 'python train.py'
gk wait gpu1 --job train --pattern 'epoch 5/'
gk ssh gpu1                      # 交互式 shell；也可用于 rsync -e 'gk ssh'、GIT_SSH_COMMAND='gk ssh'
gekong ps / tail ID / kill ID    # 查看或结束 agent 正在跑的 gk 命令
gekong down gpu1
```

远端文件 `/abs/path` 对应的本地路径是 `~/.gekong/mirror/<host>/abs/path`。

- Read 之前，hook 会拉取最新版本。
- Edit / Write 之后，hook 会以 compare-and-swap 的方式原子回写远端。
- 如果远端在模型读取之后被改过，编辑会被拒绝，模型需要重新 Read。

## 远端 shell 启动很慢时

每次调用都会经过远端的登录 shell，sshd 会让它加载 `~/.bashrc`。在 HPC 登录节点上，`/etc/profile.d` 里的脚本可能要一两秒，这时 `gekong status` 显示的每次调用耗时会超过 1 秒。可以在远端 `~/.bashrc` 最顶上加一行：

```bash
[[ $BASH_EXECUTION_STRING == *gekong:call* ]] && return   # gekong 的文件读写和搜索不需要环境初始化
```

文件读写和搜索的命令行带 `gekong:call` 标记，会跳过 rc 文件。`gk run` / `gk job` 带的是 `gekong:run`，仍然完整加载环境，module 等照常可用。rc 文件每条命令只加载一次。

## 测试

```bash
uv run pytest tests/unit                          # 不需要 Docker
uv run pytest                                     # 还会起一个 sshd 容器做端到端测试，并跑 python 3.6 兼容测试
GEKONG_CLAUDE_SMOKE=1 uv run pytest tests/e2e/test_claude_smoke.py -s   # 用真实的 claude -p（消耗 token）
```
