# Free Tonight privacy policy

Last updated: October 25, 2026

Free Tonight is a Discord bot that helps friends plan hangouts. This page explains what it stores and why.

## What we store

When you run `/setup`, the bot saves your profile so your personal agent can plan for you:

- your Discord user ID and display name
- the times you said you're usually free, or the busy times you typed
- your food needs and allergies, your hangout budget and how far you'll walk
- if you link one, your calendar's private iCal address

That's all. Profiles are stored in one database on the machine that runs the bot, readable only by that
machine's owner account.

## What we read from a linked calendar

Only when each event starts and ends, at the moment you link it and when a plan starts. Event names, places,
notes and attendees are never read or stored.

## What other people see

Other members of a plan see only: the final plan, who is going or busy, the group's pooled filters (for example
"needs vegetarian options, under $20") with no names attached, and your yes/no answers. They never see your
schedule, your exact budget or which filter is yours. Every plan has a privacy ledger showing exactly what was
shared.

## Messages

The bot reads only its own slash commands, buttons and pop-ups. It does not read your other messages.

## Claude

If the bot's operator set an Anthropic API key, schedule text you type in `/setup` that the built-in parser can't
read is sent to Anthropic's Claude to turn it into times. Nothing else is sent.

## Deleting your data

Run `/forget`, or `/profile` → Delete my data. Your profile is deleted right away.

## Contact

Open an issue at https://github.com/jathinravilla/free-tonight/issues.
