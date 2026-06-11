#!/usr/bin/env python3
"""
Daily AI News Agent — runs on your Claude subscription (no API key needed).

Uses the Claude Code CLI in headless mode (`claude -p`) with web search,
so usage counts against your Claude Pro/Max plan instead of API billing.

It also reads what you've already saved — your readings.csv reading list,
Chrome bookmarks, and Safari bookmarks/Reading List (if Full Disk Access
is granted) — to tailor the briefing and avoid recommending things you
already have.

Usage:
    python3 ai_news_agent.py           # run with settings from config.yaml
    python3 ai_news_agent.py --dry-run # show config + saved items, no Claude call

Scheduling: see setup_macos.sh
Delivery: stdout, .md file, email, Claude.ai chat, or local webpage — configure in config.yaml
"""
import csv
import glob
import json
import os
import plistlib
import re
import smtplib
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip3 install pyyaml")


# ── Output format (kept short to limit usage on your plan) ────────────────────
FORMAT_SPEC = """Output ONLY this exact format — no preamble, no sign-off:

## 3 Key Developments

1. **[Headline]** — [2-sentence summary]. Source: [URL]
2. **[Headline]** — [2-sentence summary]. Source: [URL]
3. **[Headline]** — [2-sentence summary]. Source: [URL]

## Reading List

- [Title](URL) — [one-line note]
- [Title](URL) — [one-line note]
- [Title](URL) — [one-line note]
- [Title](URL) — [one-line note]
- [Title](URL) — [one-line note]

## From Your Saved Pile

- [Title](URL) — [one line: why today's news makes this worth finally reading]
- [Title](URL) — [one line: why today's news makes this worth finally reading]"""


# ── Config loading ────────────────────────────────────────────────────────────

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        sys.exit(f"config.yaml not found at {config_path}")
    with config_path.open() as f:
        return yaml.safe_load(f)


def build_source_block(cfg: dict) -> str:
    """Turn config.yaml sources/topics into a compact user-message block."""
    parts = []
    sources = cfg.get("sources", {})

    pubs = sources.get("publications", [])
    if pubs:
        parts.append("Priority publications: " + ", ".join(pubs))

    subs = sources.get("substacks", [])
    if subs:
        parts.append("Priority substacks/newsletters: " + ", ".join(subs))

    other = sources.get("newsletters", [])
    if other:
        parts.append("Other newsletters: " + ", ".join(other))

    topics = cfg.get("topics", [])
    if topics:
        parts.append("Rank stories by: " + ", ".join(topics))

    return "\n".join(parts)


# ── Saved items (reading list + bookmarks) ────────────────────────────────────

def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


DEFAULT_AI_KEYWORDS = [
    "ai", "a.i.", "artificial intelligence", "machine learning", "llm",
    "gpt", "claude", "anthropic", "openai", "gemini", "deepmind", "agi",
    "neural", "alignment", "interpretability", "chatbot", "transformer",
    "deep learning", "frontier model",
]


def _ai_filter(items: list[str], cfg: dict) -> list[str]:
    """Keep only items whose title/domain mentions an AI-related keyword."""
    if not cfg.get("ai_filter", True):
        return items
    keywords = cfg.get("ai_keywords", DEFAULT_AI_KEYWORDS)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b", re.IGNORECASE)
    return [i for i in items if pattern.search(i)]


def _read_readings_csv(path: Path, max_items: int) -> list[str]:
    """Unread items from the readings.csv reading list."""
    items = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Status", "").strip().lower() != "unread":
                continue
            title = row.get("Title", "").strip()
            if not title:
                continue
            link = row.get("Link", "").strip()
            year = row.get("Year", "").strip()
            tag = f" ({year})" if year else ""
            items.append(f"{title}{tag} — {link}" if link else f"{title}{tag}")
    return items[:max_items]


def _read_chrome_bookmarks(max_items: int, max_age_days: int) -> list[str]:
    """Most recent Chrome bookmarks (Chrome stores date_added as µs since 1601)."""
    path = Path.home() / "Library/Application Support/Google/Chrome/Default/Bookmarks"
    data = json.loads(path.read_text())
    found = []

    def walk(node):
        if node.get("type") == "url":
            found.append((node.get("name", ""), node.get("url", ""),
                          int(node.get("date_added", 0))))
        for child in node.get("children", []):
            walk(child)

    for root in data.get("roots", {}).values():
        if isinstance(root, dict):
            walk(root)

    found.sort(key=lambda x: x[2], reverse=True)
    epoch_1601 = datetime(1601, 1, 1)
    out = []
    for name, url, date_us in found[: max_items * 3]:
        added = epoch_1601 + timedelta(microseconds=date_us)
        if added < datetime.now() - timedelta(days=max_age_days):
            continue
        out.append(f"{name[:90]} — {_domain(url)} (saved {added.date()})")
        if len(out) >= max_items:
            break
    return out


def _read_safari_bookmarks(reading_list_max: int, bookmarks_max: int) -> list[str]:
    """Safari Reading List (newest first) + the most recently added bookmarks.

    Safari only stores a date on Reading List items, so "most recent bookmarks"
    means the last ones added to the bookmark folders (Safari appends new
    bookmarks at the end). Raises PermissionError without Full Disk Access.
    """
    path = Path.home() / "Library/Safari/Bookmarks.plist"
    with path.open("rb") as f:
        data = plistlib.load(f)
    found = []

    def walk(node, in_reading_list=False):
        kind = node.get("WebBookmarkType")
        if kind == "WebBookmarkTypeList":
            is_rl = in_reading_list or node.get("Title") == "com.apple.ReadingList"
            for child in node.get("Children", []):
                walk(child, is_rl)
        elif kind == "WebBookmarkTypeLeaf":
            title = node.get("URIDictionary", {}).get("title", "")
            url = node.get("URLString", "")
            date = node.get("ReadingList", {}).get("DateAdded")
            found.append((in_reading_list, title, url, date))

    walk(data)
    reading_list = sorted((i for i in found if i[0]),
                          key=lambda x: x[3] or datetime.min,
                          reverse=True)[:reading_list_max]
    # Last entries in traversal order ≈ most recently added
    bookmarks = [i for i in found if not i[0]][-bookmarks_max:]
    bookmarks.reverse()
    out = [f"{t[:90]} — {_domain(u)} (Safari Reading List)"
           for _, t, u, _d in reading_list]
    out += [f"{t[:90]} — {_domain(u)} (Safari bookmark)"
            for _, t, u, _d in bookmarks]
    return out


def collect_saved_items(cfg: dict) -> tuple[str, list[str]]:
    """Returns (prompt_block, status_notes) describing the user's saved items."""
    saved_cfg = cfg.get("saved_items", {})
    notes, sections = [], []

    csv_rel = saved_cfg.get("reading_list_csv")
    if csv_rel:
        csv_path = (Path(__file__).parent / csv_rel).resolve()
        try:
            items = _read_readings_csv(csv_path, saved_cfg.get("max_unread", 20))
            if items:
                sections.append("Unread items on my reading list:\n" +
                                "\n".join(f"- {i}" for i in items))
            notes.append(f"✓ reading list: {len(items)} unread items")
        except FileNotFoundError:
            notes.append(f"✗ reading list not found: {csv_path}")
        except Exception as e:
            notes.append(f"✗ reading list error: {e}")

    if saved_cfg.get("chrome_bookmarks", True):
        max_age = saved_cfg.get("chrome_max_age_days", 7)
        try:
            raw = _read_chrome_bookmarks(saved_cfg.get("chrome_max_bookmarks", 15),
                                         max_age)
            items = _ai_filter(raw, saved_cfg)
            if items:
                sections.append(f"My Chrome bookmarks from the last {max_age} days:\n" +
                                "\n".join(f"- {i}" for i in items))
            notes.append(f"✓ Chrome: {len(items)} AI-related of {len(raw)} "
                         f"bookmarks from the last {max_age} days")
        except FileNotFoundError:
            notes.append("✗ Chrome bookmarks file not found")
        except Exception as e:
            notes.append(f"✗ Chrome bookmarks error: {e}")

    if saved_cfg.get("safari_bookmarks", True):
        try:
            raw = _read_safari_bookmarks(
                saved_cfg.get("safari_reading_list_max", 20),
                saved_cfg.get("safari_recent_bookmarks", 10))
            items = _ai_filter(raw, saved_cfg)
            if items:
                sections.append("My Safari saved items:\n" +
                                "\n".join(f"- {i}" for i in items))
            notes.append(f"✓ Safari: {len(items)} AI-related of {len(raw)} items")
        except PermissionError:
            notes.append("✗ Safari skipped (grant Full Disk Access to enable — "
                         "System Settings → Privacy & Security → Full Disk Access)")
        except Exception as e:
            notes.append(f"✗ Safari error: {e}")

    return "\n\n".join(sections), notes


# ── Claude Code CLI (headless, runs on your subscription) ────────────────────

def find_claude_bin() -> str:
    """Locate the claude CLI: PATH first, then the Claude desktop app bundle."""
    from shutil import which
    for candidate in (which("claude"),
                      str(Path.home() / ".local/bin/claude")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate

    bundled = glob.glob(str(Path.home() / "Library/Application Support/Claude"
                            "/claude-code/*/claude.app/Contents/MacOS/claude"))
    if bundled:
        # Highest version number wins
        def version_key(p):
            m = re.search(r"claude-code/([\d.]+)/", p)
            return tuple(int(x) for x in m.group(1).split(".")) if m else ()
        return max(bundled, key=version_key)

    sys.exit("Claude Code CLI not found. Install it (https://claude.com/claude-code) "
             "or make sure the Claude desktop app is installed.")


def build_prompt(cfg: dict, saved_block: str) -> str:
    today = datetime.now().strftime("%B %d, %Y")
    source_block = build_source_block(cfg)

    prompt = (
        f"You are a concise AI news curator. Use web search to find the most "
        f"important AI news for {today} — what is newly announced or trending "
        f"in the last 24 hours.\n\n{source_block}\n"
    )

    if saved_block:
        prompt += (
            f"\n=== WHAT I HAVE ALREADY SAVED ===\n{saved_block}\n"
            f"=== END SAVED ITEMS ===\n\n"
            f"Use my saved items to infer my interests and tailor the Reading List "
            f"to them. NEVER recommend anything already in my saved items. "
            f"If a key development relates to one of my saved items, end its summary "
            f'with "(relates to your saved: <title>)". '
            f'For "From Your Saved Pile", pick the 2 saved items most relevant to '
            f"today's news.\n\n"
        )
    else:
        prompt += "\nOmit the 'From Your Saved Pile' section entirely.\n\n"

    prompt += FORMAT_SPEC
    return prompt


def fetch_ai_news(cfg: dict, saved_block: str) -> tuple[str, dict]:
    """Run claude headless with web search. Returns (markdown_body, usage_stats)."""
    claude_bin = find_claude_bin()
    prompt = build_prompt(cfg, saved_block)

    cmd = [
        claude_bin, "-p", prompt,
        "--model", cfg.get("model", "claude-haiku-4-5"),
        "--allowedTools", "WebSearch", "WebFetch",
        "--output-format", "json",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=900,
        cwd=str(Path(__file__).parent),
    )

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        if "Not logged in" in err or "login" in err.lower():
            sys.exit(
                "Claude Code CLI is not logged in.\n"
                f"One-time fix — open Terminal and run:\n\n"
                f'  "{claude_bin}"\n\n'
                "then type /login and sign in with your Claude account "
                "(your subscription, no API key needed). Then re-run this agent."
            )
        sys.exit(f"claude CLI failed (exit {proc.returncode}): {err[:500]}")

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Fall back to treating stdout as the raw briefing
        return proc.stdout.strip(), {"input": 0, "output": 0, "cached": 0}

    if result.get("is_error"):
        sys.exit(f"claude CLI returned an error: {result.get('result', '')[:500]}")

    body = result.get("result", "").strip()
    usage = result.get("usage", {})
    stats = {
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cached": usage.get("cache_read_input_tokens", 0),
    }
    return body, stats


# ── Delivery ──────────────────────────────────────────────────────────────────

def deliver(body: str, stats: dict, cfg: dict) -> None:
    today_str = datetime.now().strftime("%B %d, %Y")
    meta = f"tokens: {stats['input']}↑  {stats['output']}↓  {stats['cached']} cached · subscription (no API cost)"
    divider = f"{'─' * 60}\n AI News — {today_str}   ({meta})\n{'─' * 60}"
    full_text = f"{divider}\n\n{body}\n"

    delivery = cfg.get("delivery", {})

    if delivery.get("stdout", True):
        print(full_text)

    if delivery.get("save_file", False):
        fname = Path(__file__).parent / f"ai_news_{datetime.now().strftime('%Y%m%d')}.md"
        fname.write_text(full_text)
        print(f"Saved → {fname}", file=sys.stderr)

    email_cfg = delivery.get("email", {})
    if email_cfg.get("enabled") and email_cfg.get("from_addr"):
        _send_email(body, stats, email_cfg)

    claude_cfg = delivery.get("claude_chat", {})
    if claude_cfg.get("enabled"):
        _open_claude_chat(body, claude_cfg)

    webpage_cfg = delivery.get("webpage", {})
    if webpage_cfg.get("enabled"):
        _save_webpage(body, stats, webpage_cfg)


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_email(body: str, stats: dict, cfg: dict) -> None:
    password = os.environ.get("EMAIL_PASSWORD", "")
    if not password:
        print("✗ Email skipped: EMAIL_PASSWORD env var not set", file=sys.stderr)
        return

    today_str = datetime.now().strftime("%B %d, %Y")
    subject = cfg.get("subject", "AI News — {date}").format(date=today_str)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(cfg.get("to_addrs", []))

    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"),
                          cfg.get("smtp_port", 587)) as server:
            server.starttls()
            server.login(cfg["from_addr"], password)
            server.sendmail(
                cfg["from_addr"],
                cfg.get("to_addrs", []),
                msg.as_string(),
            )
        print(f"✓ Email sent to {cfg.get('to_addrs')}", file=sys.stderr)
    except Exception as e:
        print(f"✗ Email failed: {e}", file=sys.stderr)


# ── Claude.ai chat ────────────────────────────────────────────────────────────

def _open_claude_chat(body: str, cfg: dict) -> None:
    """Open Claude.ai with today's briefing pre-loaded in the chat input."""
    prefix = cfg.get(
        "prompt_prefix",
        "Here is today's AI news briefing. Feel free to ask me follow-up questions:\n\n"
    )
    full_prompt = prefix + body
    url = "https://claude.ai/new?q=" + urllib.parse.quote(full_prompt)
    try:
        subprocess.run(["open", url], check=True)
        print("✓ Opened Claude.ai chat with today's briefing", file=sys.stderr)
    except Exception as e:
        print(f"✗ Claude.ai open failed: {e}", file=sys.stderr)


# ── Webpage ───────────────────────────────────────────────────────────────────

def _md_to_html(md: str) -> str:
    """Convert the agent's specific markdown output to HTML (no external deps)."""
    lines = md.split("\n")
    html_lines = []
    in_list = False

    for line in lines:
        # h2
        if line.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h2>{line[3:]}</h2>")
        # numbered item — bold headline
        elif re.match(r"^\d+\.\s", line):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            # bold
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            # inline links
            line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', line)
            # Source: bare URL
            line = re.sub(
                r"Source: (https?://\S+)",
                r'Source: <a href="\1">\1</a>',
                line,
            )
            html_lines.append(f"<p class='item'>{line}</p>")
        # bullet
        elif line.startswith("- "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            item = line[2:]
            item = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', item)
            html_lines.append(f"<li>{item}</li>")
        # blank
        elif line.strip() == "":
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<p>{line}</p>")

    if in_list:
        html_lines.append("</ul>")

    return "\n".join(html_lines)


def _save_webpage(body: str, stats: dict, cfg: dict) -> None:
    today_str = datetime.now().strftime("%B %d, %Y")
    meta = f"{stats['input']}↑ &nbsp;{stats['output']}↓ &nbsp;{stats['cached']} cached &middot; subscription"
    content_html = _md_to_html(body)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI News — {today_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #0f1117;
    color: #e2e8f0;
    line-height: 1.7;
    padding: 2rem 1rem;
  }}
  .card {{
    max-width: 740px;
    margin: 0 auto;
    background: #1a1d27;
    border-radius: 12px;
    padding: 2.5rem 2.5rem 3rem;
    border: 1px solid #2d3148;
  }}
  header {{
    border-bottom: 1px solid #2d3148;
    padding-bottom: 1.25rem;
    margin-bottom: 2rem;
  }}
  header h1 {{
    font-size: 1.4rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: #f1f5f9;
  }}
  header .meta {{
    font-size: 0.78rem;
    color: #64748b;
    margin-top: 0.35rem;
  }}
  h2 {{
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: #6366f1;
    margin: 2rem 0 1rem;
  }}
  p.item {{
    margin-bottom: 1.1rem;
    padding-left: 0.5rem;
    border-left: 2px solid #2d3148;
    font-size: 0.95rem;
  }}
  p.item strong {{ color: #f1f5f9; }}
  ul {{
    list-style: none;
    padding: 0;
  }}
  ul li {{
    padding: 0.55rem 0;
    border-bottom: 1px solid #1e2130;
    font-size: 0.93rem;
  }}
  ul li:last-child {{ border-bottom: none; }}
  a {{ color: #818cf8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  footer {{
    margin-top: 2.5rem;
    padding-top: 1rem;
    border-top: 1px solid #2d3148;
    font-size: 0.75rem;
    color: #475569;
    text-align: right;
  }}
</style>
</head>
<body>
<div class="card">
  <header>
    <h1>AI News &mdash; {today_str}</h1>
    <div class="meta">{meta}</div>
  </header>
  {content_html}
  <footer>Generated by ai_news_agent &middot; {today_str}</footer>
</div>
</body>
</html>"""

    fname = Path(__file__).parent / f"ai_news_{datetime.now().strftime('%Y%m%d')}.html"
    fname.write_text(html, encoding="utf-8")
    print(f"✓ Webpage saved → {fname}", file=sys.stderr)

    if cfg.get("auto_open", True):
        try:
            subprocess.run(["open", str(fname)], check=True)
        except Exception as e:
            print(f"  (auto-open failed: {e})", file=sys.stderr)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()
    saved_block, saved_notes = collect_saved_items(cfg)

    if "--dry-run" in sys.argv:
        print("Config loaded OK:")
        delivery = cfg.get("delivery", {})
        active = []
        for k, v in delivery.items():
            if isinstance(v, dict) and v.get("enabled"):
                active.append(k)
            elif k in ("stdout", "save_file") and v:
                active.append(k)
        print(f"  Claude CLI: {find_claude_bin()}")
        print(f"  Sources: {sum(len(v) for v in cfg.get('sources', {}).values())} entries")
        print(f"  Delivery: {active}")
        print(f"  Schedule: {cfg.get('schedule', {})}")
        print("  Saved items:")
        for note in saved_notes:
            print(f"    {note}")
        print(f"\nPrompt would be:\n{'-' * 40}\n{build_prompt(cfg, saved_block)}")
        return

    for note in saved_notes:
        print(f"  {note}", file=sys.stderr)

    body, stats = fetch_ai_news(cfg, saved_block)
    deliver(body, stats, cfg)


if __name__ == "__main__":
    main()
