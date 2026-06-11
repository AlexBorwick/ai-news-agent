# AI News Agent 📰

A daily AI news briefing that runs on your **Claude subscription** — no API
key, no per-token billing. Every morning it searches the web and delivers:

- **3 Key Developments** — the day's most important AI news, with cited sources
- **Reading List** — 5 articles tailored to your interests
- **From Your Saved Pile** — picks from your own bookmarks/reading list that
  today's news makes worth finally reading

It personalises the briefing by (optionally) looking at what you've already
saved — your Safari Reading List, recent Chrome bookmarks, and a reading-list
CSV — so it never recommends things you already have.

## Requirements

- macOS
- A Claude subscription (Pro or Max) with the
  [Claude Code CLI](https://claude.com/claude-code) installed and logged in
  (`claude` → `/login` — sign in with your Claude account, no API key)
- Python 3.9+

## Quick start

```bash
git clone https://github.com/AlexBorwick/ai-news-agent.git
cd ai-news-agent
python3 ai_news_agent.py          # run once, right now
```

To run automatically every morning:

```bash
bash setup_macos.sh               # installs a launchd job (default 8:00 am)
```

## Add your own favourite sites

Open `config.yaml` and edit the `sources:` section — it ships with a set of
recommended publications and newsletters, and you can add or remove anything:

```yaml
sources:
  publications:
    - "MIT Technology Review"
    - "Your favourite site here"
```

The `topics:` list controls how stories are ranked, and `schedule:` sets the
daily run time. Everything is plain YAML — edit and re-run.

## Personalisation (optional)

Configured under `saved_items:` in `config.yaml`:

| Source | What it reads | Notes |
|---|---|---|
| Reading-list CSV | Rows with `Status=Unread` | Set `reading_list_csv:` to your file — see `readings.example.csv` |
| Chrome | Bookmarks saved in the last 7 days | Window adjustable via `chrome_max_age_days` |
| Safari | Reading List + most recently added bookmarks | Needs Full Disk Access for Terminal |

**Privacy:** by default an AI-relevance filter (`ai_filter: true`) keeps
non-AI items — your personal browsing — out of the prompt entirely. Only
the titles and domains of AI-related saved items are sent, and only to your
own Claude account, same as typing them into a chat.

## Delivery options

Set in `config.yaml` under `delivery:` — any combination of:

- **stdout** — printed to the terminal / log
- **save_file** — dated `.md` file
- **webpage** — dark-mode HTML page that auto-opens in your browser (default on)
- **claude_chat** — opens Claude.ai with the briefing pre-loaded for follow-up questions
- **email** — via SMTP (e.g. a Gmail app password)

## How it works (and what it costs)

The agent builds one compact prompt (your sources + topics + AI-related saved
items) and runs it through `claude -p` — the Claude Code CLI's headless mode —
with web search enabled, using the fast Haiku model. Usage counts against
your existing subscription like any other Claude conversation; there is no
API key and no extra bill. A daily run is a small, single request.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.ainews.daily.plist
rm ~/Library/LaunchAgents/com.ainews.daily.plist
```

## License

MIT — see [LICENSE](LICENSE).
