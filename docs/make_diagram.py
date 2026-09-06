"""Generate docs/system-overview.svg (and .png) — the simple presentation diagram.

Answers four questions and nothing else: what happens in what order, where the agents are,
where the code is, and which tools exist. Purple = an AI agent decides. Teal = Python runs.
Gray = you and Amazon.

    .venv\\Scripts\\python.exe docs\\make_diagram.py
"""
from __future__ import annotations

import math
from pathlib import Path

W, H = 1600, 860
FONT = "Segoe UI, Inter, system-ui, Helvetica, Arial, sans-serif"
INK, MUTED, ARROW, RED = "#1F2937", "#6B7280", "#94A3B8", "#B91C1C"

PURPLE = dict(fill="#EDE9FE", stroke="#7C3AED", text="#3B0764", tag="#7C3AED")
TEAL = dict(fill="#CCFBF1", stroke="#0D9488", text="#134E4A", tag="#0D9488")
GRAY = dict(fill="#F3F4F6", stroke="#9CA3AF", text="#374151", tag="#6B7280")
KIND = {"agent": PURPLE, "tool": TEAL, "code": TEAL, "you": GRAY}

out: list[str] = []


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size=13, weight="400", fill=INK, anchor="start"):
    out.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
               f'fill="{fill}" text-anchor="{anchor}">{esc(s)}</text>')


def rect(x, y, w, h, fill, stroke, rx=12, sw=2, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
               f'stroke="{stroke}" stroke-width="{sw}"{d}/>')


def arrow(x1, y1, x2, y2, color=ARROW, sw=2.6):
    ang = math.atan2(y2 - y1, x2 - x1)
    L, Wd = 12, 6.5
    ex, ey = x2 - (L - 3) * math.cos(ang), y2 - (L - 3) * math.sin(ang)
    out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" stroke="{color}" '
               f'stroke-width="{sw}" stroke-linecap="round"/>')
    bx, by = x2 - L * math.cos(ang), y2 - L * math.sin(ang)
    out.append(f'<polygon points="{x2:.1f},{y2:.1f} {bx + Wd * math.sin(ang):.1f},{by - Wd * math.cos(ang):.1f} '
               f'{bx - Wd * math.sin(ang):.1f},{by + Wd * math.cos(ang):.1f}" fill="{color}"/>')


out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
out.append(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')

# ----------------------------------------------------------------------- title
text(60, 64, "Shopping Hunter — how it works", 36, "700", INK)
text(60, 96, "You name a product. It finds every version of it on Amazon and tells you which one is genuinely cheapest to your door.",
     16, "400", MUTED)

for i, (col, label) in enumerate([(PURPLE, "AI agent decides"), (TEAL, "Python code runs"), (GRAY, "You / Amazon")]):
    lx = 60 + i * 210
    rect(lx, 118, 18, 18, col["fill"], col["stroke"], rx=4, sw=2)
    text(lx + 28, 133, label, 14, "500", INK)

# ----------------------------------------------------------------------- the sequence
STEPS = [
    (None, "You ask for\na product", "you", None),
    ("1", "Plan the\nsearches", "agent", None),
    ("2", "Search\nAmazon", "tool", "search_amazon"),
    ("3", "Keep only the\nreal product", "agent", None),
    ("4", "Read every\noffer", "tool", "get_offers"),
    ("5", "Work out the\nreal price", "code", None),
    ("6", "Explain the\nwinner", "agent", None),
    (None, "You click\nAdd to cart", "you", None),
]
BW, BH, GAP, Y = 166, 128, 24, 196
X0 = (W - (len(STEPS) * BW + (len(STEPS) - 1) * GAP)) / 2
centers = []

for i, (num, label, kind, tool) in enumerate(STEPS):
    x = X0 + i * (BW + GAP)
    c = KIND[kind]
    centers.append(x + BW / 2)
    rect(x, Y, BW, BH, c["fill"], c["stroke"])
    lines = label.split("\n")
    top = Y + 52 if len(lines) > 1 else Y + 62
    for j, ln in enumerate(lines):
        text(x + BW / 2, top + j * 22, ln, 17, "600", c["text"], "middle")
    tag = {"agent": "AGENT", "tool": "TOOL", "code": "CODE", "you": "YOU"}[kind]
    text(x + BW / 2, Y + BH - 16, tag, 11.5, "700", c["tag"], "middle")
    if num:
        cx, cy = x + 22, Y - 2
        out.append(f'<circle cx="{cx}" cy="{cy}" r="16" fill="{c["stroke"]}"/>')
        text(cx, cy + 6, num, 16, "700", "#FFFFFF", "middle")
    if tool:
        text(x + BW / 2, Y + BH + 26, tool, 13, "600", TEAL["tag"], "middle")
    if i < len(STEPS) - 1:
        arrow(x + BW + 3, Y + BH / 2, x + BW + GAP - 3, Y + BH / 2)

# retry loop: step 3 back to step 2 — the bit that makes it an agent
ly = Y + BH + 60
mid = (centers[2] + centers[3]) / 2
out.append(f'<path d="M {centers[3]} {Y + BH + 38} L {centers[3]} {ly} L {centers[2]} {ly} L {centers[2]} {Y + BH + 40}" '
           f'fill="none" stroke="{PURPLE["stroke"]}" stroke-width="2.2" stroke-dasharray="7,5"/>')
arrow(centers[2], ly - 10, centers[2], Y + BH + 40, color=PURPLE["stroke"], sw=2.2)
text(mid, ly + 26, "found nothing? the agent searches again, in its own words", 14, "600", PURPLE["text"], "middle")
text(mid, ly + 46, "this loop is what makes it an agent and not a script", 13, "400", MUTED, "middle")

# ----------------------------------------------------------------------- three panels
PY, PH, PW = 472, 320, 486
gap = (W - 120 - 3 * PW) / 2


def panel(idx, col, heading, sub, bullets, footer=None, footer_color=None):
    x = 60 + idx * (PW + gap)
    rect(x, PY, PW, PH, "#FFFFFF", col["stroke"], rx=14, sw=2.4)
    out.append(f'<rect x="{x}" y="{PY}" width="{PW}" height="52" rx="14" fill="{col["fill"]}"/>')
    out.append(f'<rect x="{x}" y="{PY + 38}" width="{PW}" height="14" fill="{col["fill"]}"/>')
    text(x + 24, PY + 34, heading, 19, "700", col["text"])
    text(x + 24, PY + 76, sub, 13.5, "400", MUTED)
    by = PY + 110
    for title_, desc in bullets:
        out.append(f'<circle cx="{x + 30}" cy="{by - 5}" r="4" fill="{col["stroke"]}"/>')
        text(x + 46, by, title_, 14.5, "600", INK)
        for k, d in enumerate(desc):
            text(x + 46, by + 19 + k * 17, d, 13, "400", MUTED)
        by += 24 + 17 * len(desc) + 6
    if footer:
        text(x + 24, PY + PH - 22, footer, 13.5, "700", footer_color or col["text"])


panel(0, PURPLE, "The AI — 4 small agents", "Each makes one judgement call, then hands back.", [
    ("What to search for", ["turns your words into good Amazon searches"]),
    ("Which listings are really it", ["throws out cases, bundles, older models, fakes"]),
    ("Whether to try again", ["when a marketplace comes back empty"]),
    ("How to say the answer", ["a short plain summary of the winner"]),
], "It never works out a price and never buys.")

panel(1, TEAL, "The tools it can use", "Ready-made Python functions. The AI chooses when to call them.", [
    ("search_amazon", ["give it words, get back a list of listings"]),
    ("get_offers", ["opens a listing and reads the price, shipping,",
                    "customs, stock, delivery date and seller —",
                    "for every seller of that same product"]),
], "add_to_cart exists — but only your click can run it.", RED)

panel(2, TEAL, "The code around it", "The harness: everything the AI is not allowed to do.", [
    ("Runs the steps in order", ["and caps time, searches and pages"]),
    ("Drives a real Chrome window", ["signed in as you, so the prices are yours"]),
    ("Does all the maths", ["converts to JOD, adds shipping + customs, sorts"]),
    ("Checks every row", ["anything it can't verify never reaches you"]),
])

text(W - 60, H - 26, "Python · OpenAI Agents SDK · Playwright · FastAPI", 12.5, "400", MUTED, "end")
out.append("</svg>")

path = Path(__file__).with_name("system-overview.svg")
path.write_text("\n".join(out), encoding="utf-8")
print("wrote", path)
