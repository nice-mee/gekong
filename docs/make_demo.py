"""Generate docs/demo.svg, the animated README demo (pure CSS keyframes, no scripts).

Terminal lines are real gekong output captured against the e2e sshd container;
the Claude Code half is an illustrative transcript. Run: python docs/make_demo.py
"""
from pathlib import Path
from xml.sax.saxutils import escape

W, FS, LH = 880, 13.5, 22
CW = FS * 0.6                      # advance of one monospace character
X0, Y0 = 22, 72                    # first baseline
D = 25.5                           # loop length, seconds
FADE = 0.5

BG, BAR = "#15171c", "#1f2229"
COLORS = {"fg": "#d6dae0", "dim": "#7d8590", "prompt": "#7ee787", "bullet": "#d97757",
          "tool": "#79c0ff", "del": "#ff7b72", "add": "#7ee787", "pill": "#56d4bd", "user": "#e3b341"}

# Scene windows: (start, end) visible, fading in/out around them.
SCENES = {"a": (0.0, 5.6), "b": (6.6, 24.6)}
TITLES = {"a": "you · zsh", "b": "claude · working on gpu1"}

# Each line: (scene, time it appears, segments, opts). Segments are (text, color key).
# opts: type=seconds to type it out, pill=(text, time) note drawn at the right edge.
LINES = [
    ("a", 0.3, [("$ ", "prompt"), ("gekong up gpu1", "fg")], {"type": 0.8}),
    ("a", 1.5, [("gekong: connected to gpu1 (a40-node, python 3.12.3, rg, tmux)", "fg")],
     {"pill": ("one SSH master, 2FA once", 1.9)}),
    ("a", 1.6, [("  remote files: ~/.gekong/mirror/gpu1/<absolute remote path>", "dim")], {}),
    ("a", 2.5, [("$ ", "prompt"), ("gekong status", "fg")], {"type": 0.7}),
    ("a", 3.5, [("gpu1      connected  41 ms per call; a40-node, python 3.12.3, rg, tmux", "fg")],
     {"pill": ("every call reuses it", 3.9)}),
    ("a", 4.4, [("$ ", "prompt"), ("claude", "fg")], {"type": 0.4}),

    ("b", 6.9, [("> ", "user"), ("make the dataloader in ~/app/train.py on gpu1 faster, then start training", "fg")],
     {"type": 1.9}),
    ("b", None, [], {}),
    ("b", 9.4, [("⏺ ", "bullet"), ("gekong:Grep", "tool"), ('(pattern: "DataLoader", path: "/home/me/app", host: "gpu1")', "fg")],
     {}),
    ("b", 9.9, [("  ⎿  Found 1 file", "dim")], {"pill": ("searched on the remote", 10.0)}),
    ("b", 10.7, [("⏺ ", "bullet"), ("Read", "tool"), ("(~/.gekong/mirror/gpu1/home/me/app/train.py)", "fg")],
     {"pill": ("↓ pulled fresh from gpu1", 10.9)}),
    ("b", 11.2, [("  ⎿  Read 84 lines", "dim")], {}),
    ("b", 12.1, [("⏺ ", "bullet"), ("Update", "tool"), ("(~/.gekong/mirror/gpu1/home/me/app/train.py)", "fg")],
     {"pill": ("↑ written back, compare-and-swap", 13.1)}),
    ("b", 12.5, [("  ⎿  ", "dim"), ("- loader = DataLoader(ds, batch_size=64)", "del")], {}),
    ("b", 12.7, [("     ", "dim"), ("+ loader = DataLoader(ds, batch_size=64, num_workers=8, pin_memory=True)", "add")], {}),
    ("b", 14.2, [("⏺ ", "bullet"), ("Bash", "tool"), ("(gekong job start --name train -C /home/me/app gpu1 'python train.py')", "fg")],
     {}),
    ("b", 14.7, [("  ⎿  started job k3f9x2 on gpu1 (tmux session gekong-k3f9x2, log /tmp/gekong-1001/jobs/…)", "dim")],
     {}),
    ("b", 15.6, [("⏺ ", "bullet"), ("Bash", "tool"), ("(gekong wait gpu1 --job train --pattern 'epoch 5/')", "fg")],
     {"pill": ("blocks until the log matches", 16.0)}),
    ("b", 17.6, [("  ⎿  epoch 5/8  loss 0.200", "dim")], {}),
    ("b", 18.6, [("⏺ ", "bullet"), ("Done: 8 loader workers, and training is running on gpu1 as job ", "fg"),
                 ("train", "tool"), (".", "fg")], {}),
]


def pct(t):
    return f"{max(0.0, min(100.0, t / D * 100)):.3f}%"


def main():
    css, body = [], {s: [] for s in SCENES}
    n = 0

    def appear(name, t, dur=0.25):
        css.append(f"@keyframes {name}{{0%,{pct(t)}{{opacity:0}}{pct(t + dur)},100%{{opacity:1}}}}"
                   f".{name}{{animation:{name} {D}s linear infinite both}}")

    for scene, (a, b) in SCENES.items():
        name = f"s{scene}"
        frames = f"0%,{pct(a - FADE)}{{opacity:0}}{pct(a)},{pct(b)}{{opacity:1}}{pct(b + FADE)},100%{{opacity:0}}"
        if a == 0:
            frames = f"0%,{pct(b)}{{opacity:1}}{pct(b + FADE)},100%{{opacity:0}}"
        css.append(f"@keyframes {name}{{{frames}}}.{name}{{animation:{name} {D}s linear infinite both}}")

    rows = {s: 0 for s in SCENES}
    for scene, t, segs, opts in LINES:
        y = Y0 + rows[scene] * LH
        rows[scene] += 1
        if t is None:
            continue
        n += 1
        spans = "".join(f'<tspan fill="{COLORS[c]}">{escape(s)}</tspan>' for s, c in segs)
        out = body[scene]
        if "type" in opts:
            # Typed line: the text is there from the start, under a background-colored cover
            # that slides right one character at a time, dragging the cursor with it.
            prefix = len(segs[0][0])
            chars = sum(len(s) for s, _ in segs) - prefix
            dist = (chars + 1) * CW
            t1 = t + opts["type"]
            appear(f"l{n}", t - 0.2, 0.01)
            out.append(f'<g class="l{n}"><text x="{X0}" y="{y}">{spans}</text>')
            css.append(f"@keyframes c{n}{{0%{{transform:translateX(0)}}{pct(t)}{{transform:translateX(0);"
                       f"animation-timing-function:steps({chars + 1},end)}}{pct(t1)},100%{{transform:translateX({dist:.1f}px)}}}}"
                       f".c{n}{{animation:c{n} {D}s linear infinite both}}")
            cx = X0 + prefix * CW
            css.append(f"@keyframes k{n}{{0%,{pct(t - 0.2)}{{opacity:0}}{pct(t - 0.2)},{pct(t1 + 0.6)}{{opacity:1}}"
                       f"{pct(t1 + 0.6)},100%{{opacity:0}}}}.k{n}{{animation:k{n} {D}s step-end infinite both}}")
            out.append(f'<g class="c{n}"><rect x="{cx:.1f}" y="{y - FS}" width="{W}" height="{LH}" fill="{BG}"/>'
                       f'<rect class="k{n}" x="{cx:.1f}" y="{y - FS + 1}" width="{CW:.1f}" height="{FS + 3}" fill="{COLORS["fg"]}" opacity=".85"/></g></g>')
        else:
            appear(f"l{n}", t)
            out.append(f'<text class="l{n}" x="{X0}" y="{y}">{spans}</text>')
        if "pill" in opts:
            text, pt = opts["pill"]
            w = len(text) * 11 * 0.6 + 18
            x = W - 18 - w
            appear(f"p{n}", pt, 0.3)
            out.append(f'<g class="p{n}"><rect x="{x:.1f}" y="{y - 14}" width="{w:.1f}" height="19" rx="9.5" '
                       f'fill="{COLORS["pill"]}" fill-opacity=".14" stroke="{COLORS["pill"]}" stroke-opacity=".5"/>'
                       f'<text x="{x + w / 2:.1f}" y="{y - 0.5}" text-anchor="middle" style="font-size:11px" '
                       f'fill="{COLORS["pill"]}">{escape(text)}</text></g>')

    H = Y0 + max(rows.values()) * LH + 10
    scenes = "".join(
        f'<g class="s{s}"><text x="{W / 2}" y="23" text-anchor="middle" style="font-size:12px" fill="{COLORS["dim"]}">'
        f'{escape(TITLES[s])}</text>{"".join(body[s])}</g>' for s in SCENES)
    reduced = ("@media (prefers-reduced-motion:reduce){*{animation:none!important}"
               ".sa,[class^=c] rect:first-child{display:none}[class^=k]{display:none}}")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H:.0f}" viewBox="0 0 {W} {H:.0f}" role="img" aria-labelledby="t d">
<title id="t">gekong demo</title>
<desc id="d">Log in to gpu1 once with gekong up; Claude Code then searches, reads and edits remote files through the local mirror and starts and waits on a training job with gekong job and gekong wait.</desc>
<style>text{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;font-size:{FS}px;white-space:pre}}
{"".join(css)}
{reduced}</style>
<rect width="{W}" height="{H:.0f}" rx="10" fill="{BG}"/>
<path d="M0 10a10 10 0 0 1 10-10h{W - 20}a10 10 0 0 1 10 10v26H0z" fill="{BAR}"/>
<circle cx="20" cy="18" r="6" fill="#ff5f57"/><circle cx="40" cy="18" r="6" fill="#febc2e"/><circle cx="60" cy="18" r="6" fill="#28c840"/>
{scenes}
</svg>
"""
    out = Path(__file__).with_name("demo.svg")
    out.write_text(svg)
    print(f"wrote {out} ({len(svg.encode()) / 1024:.1f} KB, {W}x{H:.0f})")


if __name__ == "__main__":
    main()
