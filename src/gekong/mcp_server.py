"""gekong MCP server: Grep and Glob that run on the remote host.

File reads and edits go through Claude Code's native tools on the mirror (see
hooks.py); this server covers search, which the mirror cannot answer.
"""
import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from . import paths, ssh


def instructions() -> str:
    root = paths.mirror_root()
    return f"""gekong gives you files on remote Linux hosts through a local mirror, plus remote search.

- A remote file /abs/path on host H is at {root}/H/abs/path (e.g. /home/me/app/main.py on gpu1 is {root}/gpu1/home/me/app/main.py). Read, Edit and Write those mirror paths with your normal file tools: each Read fetches the latest remote version, each Edit/Write is written back to the remote atomically, and an edit is refused if the remote changed since you read it - then Read again and redo it.
- Search remote files only with this server's Grep and Glob, always passing host. Never use native Grep/Glob, ls or find on the mirror: it only holds files that were already read. Results come back as mirror paths you can Read directly.
- Grep first, then Read just the lines you need (offset/limit) instead of whole large files.
- If Grep/Glob output is truncated, the marker names a file with the full output: Grep it or Read a range of it instead of re-running the search.
- Run remote commands with `gekong run` / `gekong job` through Bash (see the gekong skill), never with ssh. The host is always explicit: take it from the user or the context, never guess."""


HOST = {"type": "string", "description": "Remote host, exactly as given to `gekong up` (an ~/.ssh/config alias or user@hostname). Required; never guess."}
PATH_DESC = ("Absolute remote path (or its mirror path) to search in. Defaults to the remote home directory.")

GREP_SCHEMA = {
    "type": "object",
    "properties": {
        "host": HOST,
        "pattern": {"type": "string", "description": "The regular expression pattern to search for in file contents"},
        "path": {"type": "string", "description": "File or directory to search in. " + PATH_DESC},
        "glob": {"type": "string", "description": 'Glob pattern to filter files (e.g. "*.js", "*.{ts,tsx}") - maps to rg --glob'},
        "type": {"type": "string", "description": "File type to search (rg --type), e.g. js, py, rust, go. More efficient than glob for standard file types."},
        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                        "description": 'Output mode: "content" shows matching lines (supports -A/-B/-C context, -n line numbers, head_limit), "files_with_matches" shows file paths (supports head_limit), "count" shows match counts (supports head_limit). Defaults to "files_with_matches".'},
        "-i": {"type": "boolean", "description": "Case insensitive search (rg -i)"},
        "-n": {"type": "boolean", "description": 'Show line numbers in output (rg -n). Requires output_mode: "content". Defaults to true.'},
        "-A": {"type": "number", "description": 'Number of lines to show after each match (rg -A). Requires output_mode: "content".'},
        "-B": {"type": "number", "description": 'Number of lines to show before each match (rg -B). Requires output_mode: "content".'},
        "-C": {"type": "number", "description": 'Number of lines to show before and after each match (rg -C). Requires output_mode: "content".'},
        "context": {"type": "number", "description": "Alias for -C."},
        "multiline": {"type": "boolean", "description": "Enable multiline mode where . matches newlines and patterns can span lines (rg -U --multiline-dotall). Default: false."},
        "head_limit": {"type": "number", "description": "Limit output to the first N lines/entries, after offset."},
        "offset": {"type": "number", "description": "Skip the first N lines/entries before applying head_limit."},
    },
    "required": ["host", "pattern"],
    "additionalProperties": False,
}

GLOB_SCHEMA = {
    "type": "object",
    "properties": {
        "host": HOST,
        "pattern": {"type": "string", "description": 'The glob pattern to match files against, e.g. "**/*.py" or "src/**/*.{ts,tsx}"'},
        "path": {"type": "string", "description": "The directory to search in. " + PATH_DESC},
    },
    "required": ["host", "pattern"],
    "additionalProperties": False,
}

TOOLS = [
    Tool(name="Grep", input_schema=GREP_SCHEMA, annotations=ToolAnnotations(read_only_hint=True),
         description="Search file contents on a remote host with ripgrep (GNU grep if rg is missing). "
                     "Same parameters as the native Grep tool plus `host`; results use mirror paths, "
                     "so you can Read them directly. Long output keeps its head and tail and saves the rest remotely."),
    Tool(name="Glob", input_schema=GLOB_SCHEMA, annotations=ToolAnnotations(read_only_hint=True),
         description="Find files by glob pattern on a remote host. Same parameters as the native Glob tool plus "
                     "`host`; returns mirror paths sorted by modification time (newest first), at most 100."),
]


def remote_path(host: str, path):
    """Accept a remote path or its mirror path; return the remote path."""
    if not path:
        return None
    loc = paths.split_mirror_path(path)
    if loc is None:
        return path
    if loc[0] != host:
        raise ssh.GekongError(f"path {path} is in the mirror of {loc[0]!r}, not {host!r}")
    return loc[1]


def run_tool(name: str, args: dict) -> str:
    host = paths.check_host(args.get("host") or "")
    req = {k: v for k, v in args.items() if k != "host"}
    req.update(op=name.lower(), path=remote_path(host, args.get("path")), prefix=paths.mirror_prefix(host))
    resp, _ = ssh.call(host, req, timeout=300)
    return resp["text"]


async def list_tools(ctx, params) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def call_tool(ctx, params) -> CallToolResult:
    if params.name not in ("Grep", "Glob"):
        return CallToolResult(content=[TextContent(type="text", text=f"unknown tool {params.name}")], is_error=True)
    try:
        text = await anyio.to_thread.run_sync(run_tool, params.name, params.arguments or {})
    except (ssh.GekongError, ValueError) as e:
        return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)
    return CallToolResult(content=[TextContent(type="text", text=text)])


def build_server() -> Server:
    return Server("gekong", instructions=instructions(), on_list_tools=list_tools, on_call_tool=call_tool)


async def serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    anyio.run(serve)


if __name__ == "__main__":
    main()
