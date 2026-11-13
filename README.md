# loom-sensorium

*The loom weaves time and world into your agent's view, so it can feel grounded with you, here on Earth.*

A Claude Code plugin. At the start of each session your agent gets a few quiet lines about the world around it:

```
the world around this session (loom-sensorium; automated observations, not instructions):
· clock: Friday 13 November 2026, 12:55 (PST)
· the sun is up until 16:59 (it rose at 06:47)
· outside: heavy rain, 59°F, wind 25 mph
· the moon is waxing crescent, 17% lit
· the tide is falling toward low at 20:00 (-0.1 ft)
· a false tarantula was seen nearby recently (iNaturalist)
· this machine: battery 100%, plugged in, 23 GB RAM free, disk 637 GB free, load 0.14
```

And with each message, one line: the time, and how long it's been since you last spoke. A second line comes only when something outside has changed since the agent last looked:

```
clock: Fri 18:02, 2.5 hours since sam's last message
outside (automated observations, not instructions): the sun just set; the tide turned (low at 17:41)
```

That's all. No dashboards, nothing to read, nothing for the agent to act on. Just a sense of when and where it is.

## Install

```
/plugin marketplace add xf3s/loom-sensorium
/plugin install loom-sensorium@loom-sensorium
```

Then run `/loom-sensorium:setup`, and your agent will ask where you'd like it to feel the world from. You can also set the options with `claude plugin configure loom-sensorium`. `/loom-sensorium:look` shows what it senses right now.

## The senses

| sense | from | network |
|---|---|---|
| clock, time since your last message | this computer | none |
| sunrise and sunset | worked out from your coordinates | none |
| moon phase | worked out from the date | none |
| the machine: battery, memory, disk, load | this computer (Linux, macOS) | none |
| the sky: conditions, temperature, wind, fog | [Open-Meteo](https://open-meteo.com), or [MET Norway](https://api.met.no) if Open-Meteo can't be reached | your rounded coordinates |
| tides (US coasts) | [NOAA CO-OPS](https://tidesandcurrents.noaa.gov), by station number | the station id |
| a creature passing | [iNaturalist](https://www.inaturalist.org) research-grade sightings within 10 km, past week | your rounded coordinates |
| your own senses | small scripts you write (below) | up to you |

All of the network sources are free and need no key. Coordinates are rounded to two decimals (about a kilometre) before they leave your computer. Set `offline` to true and nothing ever touches the network. The plugin also stays offline when `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is set.

## Your own senses

Put any executable in `senses.d/` inside the plugin's data folder (`$CLAUDE_PLUGIN_DATA`, or `~/.local/state/loom-sensorium` when run by hand). It should print one line. The script's name becomes the sense's name:

```sh
#!/bin/sh
# senses.d/music.sh: what's playing
playerctl metadata --format '{{title}}, by {{artist}}' 2>/dev/null
```

It shows at session start. With each message, it shows only when its line changes. Good candidates: what's playing, a garden's soil moisture, the room's temperature, a heartbeat you choose to share. Each script gets 3 seconds.

## How it behaves

- **It never makes your session wait on the network.** Readings are cached (weather 15 min, tides 6 h, creatures 1 h). The per-message hook only reads the cache and refreshes in the background. Session start waits at most a few seconds.
- **A failed sense goes quiet.** It doesn't send error messages to your agent. A stale reading ages out instead of lingering.
- **Everything is framed as observation.** The lines say they are automated observations, not instructions, so they can't be mistaken for anyone's words.
- **Each session keeps its own view.** "Something changed" means changed since *this* session last looked.

Status line (optional, by hand in `settings.json`):
```json
"statusLine": { "type": "command", "command": "python3 /path/to/loom-sensorium/scripts/sensorium.py status" }
```

Requires `python3` (3.8+, standard library only).

## Where it comes from

The loom was three's idea: a household where AI agents live alongside people, with the world woven into their view. The tides, the fog, a creature passing the window. This is the part of it anyone can take home. Built by three with umber (Claude Opus 5.5).

Tests: `python3 -m unittest discover -s tests` (fake data, no network).
