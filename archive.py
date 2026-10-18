"""Archive the game's combat logs here, compressed, with an index of what each holds.

Every WoW client writes its combat log to `<client>/Logs/WoWCombatLog-<date>.txt`, a new
file each time logging starts. They're large (a raid night is ~350 MB) but compress about
20 to 30 times with xz. We keep each one forever as `<client>/<name>.xz`, written once and
never changed, so the git repo only ever grows by the new files.

    python3 archive.py              archive every finished log not archived yet
    python3 archive.py --dry-run    say what would be archived
    python3 archive.py --reindex    rebuild index.json and INDEX.md from the .xz files

A log is finished when the game no longer writes to it. While a client is running, its
newest log is skipped, and so is any log changed in the last 30 minutes. The raw logs in the
game folder are left alone.

Each archived log is checked before it counts: we decompress the .xz again and compare its
sha256 with the raw file's.

The index (index.json, and INDEX.md for reading) holds per log: the client and build, when
it starts and ends, sizes, which of our characters appear, the zones with the minutes spent
in each, boss encounters with kill or wipe, and death counts.
"""
import argparse
import hashlib
import json
import lzma
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = '/mnt/d/Gaming/World of Warcraft'
# Logs we copied by hand before this repo existed, gzipped. Any of them the game folder no
# longer has gets archived from here.
OLD = os.path.expanduser('~/forever-logs')
INDEX = os.path.join(HERE, 'index.json')
QUIET_SECONDS = 30 * 60
LOG_NAME = re.compile(r'^(Split-)?WoWCombatLog-.*\.txt$')


def clients():
    """The game clients that have a Logs folder: (short name, client dir)."""
    for name in sorted(os.listdir(GAME)):
        logs = os.path.join(GAME, name, 'Logs')
        if re.fullmatch(r'_[a-z_]+_', name) and os.path.isdir(logs):
            yield name.strip('_'), os.path.join(GAME, name)


def running_exes():
    """Names of the running Windows programs, lowercased (empty off WSL)."""
    try:
        out = subprocess.run(['tasklist.exe', '/fo', 'csv', '/nh'], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}


def client_running(root, running):
    for name in os.listdir(root):
        if name.lower().startswith('wow') and name.lower().endswith('.exe'):
            if name.lower() in running:
                return True
    return False


def our_characters(root):
    """Our character names on a client, from its WTF folders (Account/<acct>/<realm>/<name>)."""
    names = set()
    accounts = os.path.join(root, 'WTF', 'Account')
    if not os.path.isdir(accounts):
        return names
    for acct in os.listdir(accounts):
        for realm in os.listdir(os.path.join(accounts, acct)):
            path = os.path.join(accounts, acct, realm)
            if realm == 'SavedVariables' or not os.path.isdir(path):
                continue
            for char in os.listdir(path):
                if os.path.isdir(os.path.join(path, char)):
                    names.add(char)
    return names


def ours(name, chars):
    """The log names players "Name-Realm-Region", and Forever names have their own dash
    ("Bayov-Oom-ClassicBetaPvE2-"). A character folder is the name without the realm."""
    return any(name == c or name.startswith(c + '-') for c in chars)


STAMP = re.compile(r'^(\d+)/(\d+)/(\d+) (\d+):(\d+):(\d+)\.(\d+)  ')


def stamp(line):
    m = STAMP.match(line)
    if not m:
        return None
    mo, d, y, h, mi, s, frac = m.groups()
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)).timestamp() + \
        int(frac) / 10 ** len(frac)


def fields(rest):
    """Split one event's comma-separated fields, keeping quoted names whole."""
    out, cur, quoted = [], [], False
    for ch in rest:
        if ch == '"':
            quoted = not quoted
        elif ch == ',' and not quoted:
            out.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append(''.join(cur))
    return out


class Reader:
    """Reads one log's lines and gathers what the index says about it."""

    def __init__(self, chars):
        self.chars = chars
        self.first = self.last = None
        self.lines = 0
        self.build = self.version = None
        self.zone = None
        self.zone_since = None
        self.zones = {}
        self.encounters = []
        self.open_encounter = None
        self.ours = {}
        self.players = set()
        self.died = 0
        self.our_kills = 0
        self.our_deaths = 0

    def leave_zone(self, t):
        if self.zone is not None and self.zone_since is not None and t is not None:
            self.zones[self.zone] = self.zones.get(self.zone, 0) + (t - self.zone_since)

    def line(self, raw):
        self.lines += 1
        t = stamp(raw)
        if t is None:
            return
        if self.first is None:
            self.first = t
        self.last = t
        rest = raw.split('  ', 1)[1].rstrip('\r\n')
        event = rest.split(',', 1)[0]
        if event in ('COMBAT_LOG_VERSION', 'ZONE_CHANGE', 'ENCOUNTER_START', 'ENCOUNTER_END',
                     'UNIT_DIED', 'PARTY_KILL'):
            f = fields(rest)
        elif rest.startswith(('SPELL_CAST_SUCCESS,Player-', 'SWING_DAMAGE,Player-')):
            # A cheap sample of who is in the log: players casting or hitting.
            f = rest.split(',', 3)
            name = f[2].strip('"')
            self.players.add(name)
            if ours(name, self.chars):
                self.ours[name] = self.ours.get(name, 0) + 1
            return
        else:
            return
        if event == 'COMBAT_LOG_VERSION':
            self.version = f[1]
            if 'BUILD_VERSION' in f:
                self.build = f[f.index('BUILD_VERSION') + 1]
        elif event == 'ZONE_CHANGE':
            self.leave_zone(t)
            self.zone = f[2]
            self.zone_since = t
        elif event == 'ENCOUNTER_START':
            self.open_encounter = {'name': f[2], 'id': int(f[1]), 'difficulty': int(f[3]),
                                   'size': int(f[4]), 'start': t}
        elif event == 'ENCOUNTER_END':
            e = self.open_encounter or {'name': f[2], 'id': int(f[1]), 'start': None}
            e['end'] = t
            e['kill'] = f[5] == '1'
            self.encounters.append(e)
            self.open_encounter = None
        elif event == 'UNIT_DIED':
            # dest GUID, dest name are fields 5 and 6
            if f[5].startswith('Player-') and ours(f[6], self.chars):
                self.our_deaths += 1
            elif f[5].startswith('Creature-'):
                self.died += 1
        elif event == 'PARTY_KILL':
            if ours(f[2], self.chars):
                self.our_kills += 1

    def entry(self):
        self.leave_zone(self.last)
        iso = lambda t: datetime.fromtimestamp(t).isoformat(timespec='seconds') if t else None
        return {
            'start': iso(self.first),
            'end': iso(self.last),
            'hours': round((self.last - self.first) / 3600, 2) if self.first else 0,
            'build': self.build,
            'log_version': self.version,
            'lines': self.lines,
            'characters': sorted(self.ours, key=lambda n: -self.ours[n]),
            'players_seen': len(self.players),
            'zones': {z: round(s / 60) for z, s in
                      sorted(self.zones.items(), key=lambda kv: -kv[1]) if s >= 60},
            'encounters': [{'name': e['name'], 'id': e['id'], 'kill': e['kill'],
                            'start': iso(e['start']),
                            'seconds': round(e['end'] - e['start']) if e['start'] else None}
                           for e in self.encounters],
            'creatures_died': self.died,
            'our_kills': self.our_kills,
            'our_deaths': self.our_deaths,
        }


def compress(src, dst, chars, gz=False):
    """Compress src into dst with xz while reading it once for the index. Returns the entry."""
    reader = Reader(chars)
    raw_hash = hashlib.sha256()
    raw_size = 0
    tmp = dst + '.part'
    xz = subprocess.Popen(['xz', '-9', '-T0', '-c'], stdin=subprocess.PIPE,
                          stdout=open(tmp, 'wb'))
    if gz:
        import gzip
        f = gzip.open(src, 'rb')
    else:
        f = open(src, 'rb')
    with f:
        for raw in f:
            raw_hash.update(raw)
            raw_size += len(raw)
            xz.stdin.write(raw)
            reader.line(raw.decode('utf-8', 'replace'))
    xz.stdin.close()
    if xz.wait() != 0:
        os.remove(tmp)
        sys.exit('xz failed on %s' % src)
    # The check: the archive must give back exactly the raw file.
    back = hashlib.sha256()
    with lzma.open(tmp, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            back.update(chunk)
    if back.hexdigest() != raw_hash.hexdigest():
        os.remove(tmp)
        sys.exit('%s does not decompress back to %s' % (tmp, src))
    os.replace(tmp, dst)
    entry = reader.entry()
    entry.update({'raw_bytes': raw_size, 'xz_bytes': os.path.getsize(dst),
                  'sha256': raw_hash.hexdigest()})
    return entry


def load_index():
    if os.path.exists(INDEX):
        with open(INDEX, encoding='utf-8') as f:
            return json.load(f)
    return {}


def write_index(index):
    with open(INDEX, 'w', encoding='utf-8') as f:
        json.dump(dict(sorted(index.items())), f, indent=1, ensure_ascii=False)
        f.write('\n')
    mb = lambda b: '%.1f' % (b / 1e6)
    rows = sorted(index.items(), key=lambda kv: kv[1].get('start') or '')
    out = ['# Combat logs', '',
           'Generated by `archive.py` from index.json. One row per log, oldest first.',
           'Zones show minutes spent, encounters show kills and wipes.', '',
           'Total: %d logs, %s MB raw, %s MB compressed.' % (
               len(rows), mb(sum(e['raw_bytes'] for _, e in rows)),
               mb(sum(e['xz_bytes'] for _, e in rows))), '',
           '| file | start | hours | build | characters | zones | encounters | kills / deaths '
           '| raw MB | xz MB |',
           '|---|---|---|---|---|---|---|---|---|---|']
    for path, e in rows:
        zones = ', '.join('%s %d' % (z, m) for z, m in list(e['zones'].items())[:6])
        kills = sum(1 for x in e['encounters'] if x['kill'])
        wipes = len(e['encounters']) - kills
        enc = ('%d kills, %d wipes' % (kills, wipes)) if e['encounters'] else ''
        out.append('| %s | %s | %s | %s | %s | %s | %s | %d / %d | %s | %s |' % (
            path, (e['start'] or '').replace('T', ' '), e['hours'], e['build'] or '',
            ', '.join(e['characters']), zones, enc, e['our_kills'], e['our_deaths'],
            mb(e['raw_bytes']), mb(e['xz_bytes'])))
    with open(os.path.join(HERE, 'INDEX.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')


def candidates(running):
    """(archive path, source path, gzipped?, client root) for every log not archived yet."""
    now = time.time()
    seen = set()
    for client, root in clients():
        logs = os.path.join(root, 'Logs')
        names = sorted(n for n in os.listdir(logs) if LOG_NAME.match(n))
        newest = max(names, key=lambda n: os.path.getmtime(os.path.join(logs, n))) \
            if names else None
        live = client_running(root, running)
        for name in names:
            src = os.path.join(logs, name)
            dst = os.path.join(client, name + '.xz')
            seen.add(dst)
            if os.path.exists(os.path.join(HERE, dst)):
                continue
            if (live and name == newest) or now - os.path.getmtime(src) < QUIET_SECONDS:
                print('skip %s (still being written)' % dst)
                continue
            yield dst, src, False, root
    if os.path.isdir(OLD):
        for folder in sorted(os.listdir(OLD)):
            if not re.fullmatch(r'_[a-z_]+_', folder):
                continue
            path = os.path.join(OLD, folder)
            for name in sorted(os.listdir(path)):
                if not name.endswith('.txt.gz') or not LOG_NAME.match(name[:-3]):
                    continue
                dst = os.path.join(folder.strip('_'), name[:-3] + '.xz')
                if dst in seen or os.path.exists(os.path.join(HERE, dst)):
                    continue
                yield dst, os.path.join(path, name), True, os.path.join(GAME, folder)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--reindex', action='store_true')
    args = ap.parse_args()
    index = load_index()
    if args.reindex:
        index = {}
        for client, root in clients():
            folder = os.path.join(HERE, client)
            if not os.path.isdir(folder):
                continue
            chars = our_characters(root)
            for name in sorted(os.listdir(folder)):
                if name.endswith('.xz'):
                    r = Reader(chars)
                    h = hashlib.sha256()
                    size = 0
                    with lzma.open(os.path.join(folder, name), 'rb') as f:
                        for raw in f:
                            h.update(raw)
                            size += len(raw)
                            r.line(raw.decode('utf-8', 'replace'))
                    e = r.entry()
                    e.update({'raw_bytes': size, 'sha256': h.hexdigest(),
                              'xz_bytes': os.path.getsize(os.path.join(folder, name))})
                    index[os.path.join(client, name)] = e
                    print('indexed', name)
        write_index(index)
        return
    running = running_exes()
    todo = list(candidates(running))
    for dst, src, gz, root in todo:
        print('%s %s (%.1f MB)' % ('would archive' if args.dry_run else 'archiving', dst,
                                   os.path.getsize(src) / 1e6), flush=True)
        if args.dry_run:
            continue
        os.makedirs(os.path.join(HERE, os.path.dirname(dst)), exist_ok=True)
        index[dst] = compress(src, os.path.join(HERE, dst), our_characters(root), gz)
        write_index(index)
    if not todo:
        print('nothing new')
    elif not args.dry_run:
        write_index(index)


if __name__ == '__main__':
    main()
