---
name: gekong
description: Work on a remote Linux host (GPU box, cluster login node, server) through gekong - run remote commands with `gekong run`, long jobs with `gekong job` + `gekong wait`, and read/edit remote files via ~/.gekong/mirror paths. Use whenever a task touches a machine reached over SSH.
---

# Remote work with gekong

The user logs in once with `gekong up HOST`; every call below reuses that connection,
so nothing here ever asks for a password.

## Rules

1. **Run remote commands with `gekong run` / `gekong job`, never `ssh`.** They are
   pre-approved; plain `ssh` needs the user's confirmation and bypasses the process cleanup.
2. **HOST is always explicit.** Take it from the user's words or the conversation (the
   name they used with `gekong up`). If it is not clear which host, ask; never guess, and
   never reuse a host from an unrelated earlier task.
3. **If any gekong command says to run `gekong up HOST`, stop** and ask the user to run it.
   Do not retry, and do not try ssh or another way to log in: the login may need 2FA.
   `gekong up`, `down`, `kill` and `setup` are the user's commands, not yours.
4. **Anything longer than a few minutes goes in `gekong job start`**, then wait with
   `gekong wait` (in the background) or watch with Monitor. Don't poll in a loop.

## Files

A remote file `/abs/path` on HOST is at `~/.gekong/mirror/HOST/abs/path`. Use Read / Edit /
Write on that mirror path: Read fetches the latest remote content, Edit/Write go back to
the remote atomically. If an edit is refused because the remote changed, Read again and
redo it. Search with the gekong MCP `Grep` / `Glob` tools (pass `host`), not with native
search, `ls` or `find` on the mirror.

## Commands

```bash
gekong run HOST 'nvidia-smi'                      # output streams; exit code is the remote one
gekong run -C /home/me/proj HOST 'make -j8 test'  # -C: remote working directory
```

- `gekong run` is an ordinary local process: run it in the background, kill it, give it a
  timeout. Killing it kills the remote process group too (GPU memory is freed).
- Commands run in the remote login shell; quote the whole command once.
- Remote paths in options (`-C`, `--file`) must be absolute or have `~` quoted
  (`-C '~/proj'`): an unquoted `~` is expanded by your local shell into a local path.
- It does not survive a dropped connection. Use a job for anything that must.

Long jobs (in remote tmux, survive disconnects and agent restarts):

```bash
gekong job start -C /home/me/proj --name train HOST 'python train.py --epochs 50'
gekong job ls HOST
gekong job tail HOST train -n 100          # last lines; -f follows until the job ends
gekong wait HOST --job train --pattern 'epoch 5/|Traceback' --timeout 3600
gekong wait HOST --job train               # until it ends; exits with the job's exit code
gekong job kill HOST train
```

- `gekong wait` exits 0 printing the matching line, 1 on timeout, 2 if the job ended
  without a match. Run it with `run_in_background` to be notified when it returns, or use
  `gekong job tail -f HOST JOB` under Monitor to react to each new line.
- `--file PATH --pattern RE` waits on any remote log file instead of a job.

Copying files and git: `rsync -e 'gekong ssh' -a src/ HOST:dst/`, and
`GIT_SSH_COMMAND='gekong ssh' git ...` (`gekong ssh` asks the user first, like plain `ssh`;
prefer `gekong run` for commands). For an interactive look at a job:
`gekong ssh -t HOST tmux attach -t gekong-<job id>` (the user does this, not you).
