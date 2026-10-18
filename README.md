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
