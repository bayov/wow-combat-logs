# WoW combat logs

Every combat log our WoW clients wrote, kept forever. Each is the game's own
`WoWCombatLog-<date>.txt`, compressed with xz (20 to 30 times smaller), under a folder per
client: `classic_beta/`, `anniversary/`, later `forever/`.

[INDEX.md](INDEX.md) lists what each log holds: when, which build, our characters in it, the
zones with minutes spent, boss encounters with kills and wipes, and our kills and deaths.
`index.json` is the same with the raw file's size and sha256.

`python3 archive.py` adds the logs that are new since the last run. The /sync skill in the
WoW repo runs it. Each archive is decompressed again and compared with the raw file before it
counts, and an archived file is never rewritten, so the repo only grows by new logs.

Read one with `xz -dc classic_beta/WoWCombatLog-101826_221610.txt.xz | less`.

## Threat

The combat log has no threat in it. BayovCore's Threat module records the raw threat the game
reports for us, our pet and two group members on every mob we fight, and keeps it in game only
for `keepDays` (14 by default) and 20 MB. That file isn't in version control, so this repo is
its only lasting copy. `archive.py` puts each fight next to its log as `<log>.threat.jsonl.xz`
(one fight per line, as JSON; the format is in `archive.py`'s docstring), and fights that reach
the game's file later as `<log>.threat.2.jsonl.xz` and on. Fights played with the combat log
off go to `<client>/unlogged/<date>.threat.jsonl.xz`. `threat.json` lists every fight archived,
so a run never copies one twice, and `INDEX.md` counts each log's fights and readings. It
warns when fights not archived yet are near the end of `keepDays`, or the file nears its size
cap. forever-sim's `tools/combat_log/threat.py <log>` lines a log's threat up with the log.

`savedvars.py` reads the game's SavedVariables files for it (a copy of forever-sim's
`history.py` parser).
