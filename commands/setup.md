---
description: Tell the loom where you are, so your agent can feel the sun, sky and tide with you
---
Help the person set up loom-sensorium, gently and briefly.

1. Ask where they'd like their agent to feel the world from. A city or town is enough; they can give coordinates instead if they'd rather not name a place. Say plainly: the place is stored only in Claude Code's plugin settings on this computer, the coordinates are rounded to about a kilometre, and they go only to Open-Meteo or MET Norway (weather), iNaturalist (creatures) and NOAA (tides, by station number). They can choose `offline` instead, and the agent still gets the clock, the sun and moon, and the machine.
2. Find the latitude and longitude for the place (two decimals is plenty). If they're on a US coast and want tides, find the nearest NOAA tide station id (tidesandcurrents.noaa.gov). Ask what name the clock line should use for them, and whether they want metric or imperial units.
3. Save it with one command, for example:
   `printf '%s' '{"latitude":"37.77","longitude":"-122.42","units":"imperial","person":"sam","tide":"9414290"}' | claude plugin configure loom-sensorium --values-stdin`
   Leave out anything they didn't choose.
4. Show them what their agent will see: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sensorium.py" show < /dev/null`. Tell them it takes effect at the next session start, and that the lines are framed as automated observations, never instructions.

Their own senses (music, a garden's soil sensor, a heartbeat they choose to share) can be added later as small scripts: see the README.
