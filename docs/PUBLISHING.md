# Publishing Free Tonight

Three steps: make the bot public, run it somewhere that stays on, and share the invite link.

## 1. Make the bot public (5 minutes)

In the [Discord Developer Portal](https://discord.com/developers/applications), open **Free Tonight**:

1. **Bot** tab: turn **Public Bot** on. Leave every *Privileged Gateway Intent* off; the bot doesn't need them.
2. **Installation** tab: Install Link = *Discord Provided Link*. Under **Guild Install**, scopes `bot` and
   `applications.commands`, permissions **View Channels**, **Send Messages**, **Embed Links**, **Attach Files**,
   **Read Message History**. Turn **User Install** off.
3. **General Information** tab: add a short description, an app icon, and these links:
   - Privacy Policy URL: `https://github.com/jathinravilla/free-tonight/blob/main/PRIVACY.md`
   - Terms of Service URL: `https://github.com/jathinravilla/free-tonight/blob/main/TERMS.md`

Invite link (anyone can use it once the bot is public):

```text
https://discord.com/oauth2/authorize?client_id=1563675486154924082&scope=bot+applications.commands&permissions=117760
```

## 2. Keep it running

The bot only works while `python -m freetonight.discord_bot` is running somewhere.

**Option A: a cloud host (recommended, stays on 24/7).** Railway is the simplest:

1. Sign in at [railway.com](https://railway.com) with GitHub → **New Project** → **Deploy from GitHub repo** →
   pick `free-tonight`. It reads `requirements.txt` and the `Procfile` (`worker: python -m freetonight.discord_bot`).
2. **Variables**: add `DISCORD_TOKEN` (your bot token) and `FREETONIGHT_DB=/data/freetonight.db`.
   Don't set `DISCORD_GUILD_ID`, so commands register in every server. Optionally add `ANTHROPIC_API_KEY`.
3. **Volume**: add one mounted at `/data`, so profiles survive redeploys.
4. Deploy. The logs should end with `Free Tonight is online as Free Tonight#2059`.

Railway gives trial credit, then the Hobby plan is about $5 a month. Render (Background Worker) and Fly.io work the
same way.

**Option B: your Mac (free, only while it's on and awake).**

```bash
cd ~/free-tonight && git pull && source .venv/bin/activate
export DISCORD_TOKEN=...      # your bot token
unset DISCORD_GUILD_ID        # register commands everywhere, not just DevHacks S3
caffeinate -i python -m freetonight.discord_bot
```

`caffeinate -i` stops the Mac from sleeping while the bot runs. Closing the Terminal window stops the bot.

Run only one copy at a time (Mac *or* cloud), with one token.

## 3. Share it

Post the invite link. Commands registered globally can take a few minutes to show up in a new server. Once the bot
is in 75 or more servers, Discord asks you to verify it (Developer Portal → App Verification); it must be verified
to grow past 100.

## What's protecting it

- Everyone is rate limited to 8 commands or taps per 10 seconds; extra taps get "Slow down a sec".
- Any crash in a command, button or pop-up shows a friendly message and is logged, instead of "interaction failed".
- `/plan` works only in servers, one plan per channel, up to 25 people; an unstarted plan expires after 30 minutes.
- Double-tapping Start can't start two plans; people with the same display name get separate agents.
- The bot never pings @everyone, @here or roles. Calendar links must be Google, Outlook or iCloud.
- Old plan cards stop listening after a week, so memory doesn't grow forever.
