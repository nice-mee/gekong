# gekong

A remote toolkit for coding agents, built on OpenSSH connection multiplexing. Log in once, and Claude Code reads and edits remote files with its native Read / Edit / Write, searches the remote with MCP Grep / Glob, and runs remote commands with `gekong run`. Stopping a local command stops its remote process group too. The remote only needs python3 (>= 3.6); nothing is installed there. See the gekong design doc for the reasoning behind it.

## Install

```bash
uv tool install --editable .     # gekong, gekong-mcp, gekong-hook -> ~/.local/bin
gekong setup claude              # hooks, permissions, mirror directory, MCP server, skill (backs up settings.json first)
gekong setup claude --dry-run    # print the settings.json it would write
gekong setup claude --uninstall  # remove only what setup added
```

Setup pre-approves the subcommands agents work with and the read-only ones (`gekong run / job / wait / ssh / status / ps / tail`). `gekong up / down / kill / setup` and plain `ssh` still ask you first.

## Use

```bash
gekong up gpu1                   # log in once in your terminal (2FA etc.); every later call reuses it
gekong status
gekong run gpu1 'nvidia-smi'
gekong job start --name train gpu1 'python train.py'
gekong wait gpu1 --job train --pattern 'epoch 5/'
gekong ssh gpu1                  # interactive shell; also rsync -e 'gekong ssh', GIT_SSH_COMMAND='gekong ssh'
gekong ps / tail ID / kill ID    # see or stop the commands agents are running
gekong down gpu1
```

A remote file `/abs/path` is at `~/.gekong/mirror/<host>/abs/path` locally.

- Before a Read, a hook pulls the latest version.
- After an Edit / Write, a hook writes it back to the remote atomically, as a compare-and-swap.
- If the remote changed since the model read it, the edit is refused and the model reads it again.

## Slow remote shells

Every call goes through the remote login shell, and sshd makes it source `~/.bashrc`. On HPC login nodes the `/etc/profile.d` scripts can take a second or two; `gekong status` then reports more than 1 s per call. Put this line at the very top of the remote `~/.bashrc`:

```bash
[[ $BASH_EXECUTION_STRING == *gekong:call* ]] && return   # gekong's file reads, writes and searches need no setup
```

File reads, writes and searches are tagged `gekong:call` and skip the rc file. `gekong run` / `gekong job` are tagged `gekong:run` and still load the full environment, so `module` and the like work as usual. Either way the rc file is sourced at most once per command.

## Tests

```bash
uv run pytest tests/unit                          # no Docker needed
uv run pytest                                     # also starts an sshd container for end-to-end tests, and a python 3.6 test
GEKONG_CLAUDE_SMOKE=1 uv run pytest tests/e2e/test_claude_smoke.py -s   # real `claude -p` (spends tokens)
```
