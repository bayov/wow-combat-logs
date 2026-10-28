"""Archive the game's combat logs here, compressed, with an index of what each holds.

Every WoW client writes its combat log to `<client>/Logs/WoWCombatLog-<date>.txt`, a new
file each time logging starts. They're large (a raid night is ~350 MB) but compress about
20 to 30 times with xz. We keep each one forever as `<client>/<name>.xz`, written once and
never changed, so the git repo only ever grows by the new files.

    python3 archive.py              archive every finished log not archived yet, and its threat
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

THREAT. The combat log has no threat in it. BayovCore's Threat module records it in game:
the raw threat the game reports for us, our pet and two group members on every mob we fight
(bayovui/addons/BayovCore/Modules/Threat.lua in the WoW repo). It keeps each fight in the
account's SavedVariables/BayovThreatLog.lua for `keepDays` (14 by default) and at most
`maxBytes` (20 MB), both written in the store. That file isn't in any version control, so
this archive is its only lasting copy. We copy each fight next to its log, as
`<client>/<log>.threat.jsonl.xz`: one fight per line, a JSON object with the fight's fields
as the addon wrote them and its rows as `readings`, each
[ms since the fight's `at`, mob, who, status, scaled %, raw %, threat], where mob and who
are 1-based places in the fight's `mobs` and `who`, a status of -1 (with nothing after it)
means that player dropped off the mob's threat list, and a value that wasn't a finite number
is null. forever-sim's tools/combat_log/threat.py reads them against the log.

A fight is in the store only once it has ended and the game saved it (logout or /reload),
so every fight we read is whole. threat.json lists the fights we have archived, by account
and start, and each run archives only the others:

- A fight goes to the log that holds its `at`, else the nearest within THREAT_SLACK sec.
  The log's first file is <log>.threat.jsonl.xz; fights that come later (a second account
  saved after the first run) go in <log>.threat.2.jsonl.xz and on. Files are never
  rewritten.
- A fight near or after the end of the newest archived log waits while the game folder has
  a log not archived yet, since that log may be the one that holds it.
- A fight no log holds though a later log is archived was played with the combat log off.
  It goes to <client>/unlogged/<date>.threat.jsonl.xz.

A store that doesn't read is reported, and the others are still archived. We warn when a
fight not archived yet is within 4 days of `keepDays`, or a store passes 80% of `maxBytes`.
--reindex rebuilds threat.json and each log's threat counts from the threat files.
"""
import argparse
import bisect
import hashlib
import json
import lzma
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import savedvars  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = '/mnt/d/Gaming/World of Warcraft'
# Logs we copied by hand before this repo existed, gzipped. Any of them the game folder no
# longer has gets archived from here.
OLD = os.path.expanduser('~/forever-logs')
INDEX = os.path.join(HERE, 'index.json')
THREAT_STORE = 'BayovThreatLog.lua'
THREAT_GLOBAL = 'BayovThreatLogDB'
# How far outside a log's first and last line a fight's start may be and still be its.
THREAT_SLACK = 5
LEDGER = os.path.join(HERE, 'threat.json')
QUIET_SECONDS = 30 * 60
LOG_NAME = re.compile(r'^(Split-)?WoWCombatLog-.*\.txt$')


def clients():
    """The game clients that have a Logs folder: (short name, client dir)."""
    if not os.path.isdir(GAME):
        return
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
        # The earliest and latest line, which aren't the first and last across the
        # autumn clock change.
        self.lo = self.hi = None
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
        self.lo = t if self.lo is None or t < self.lo else self.lo
        self.hi = t if self.hi is None or t > self.hi else self.hi
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
            'span': [iso(self.lo), iso(self.hi)],
            # Across the autumn change the last line's time is an hour behind.
            'hours': round((self.last - self.first + (3600 if self.last < self.first else 0)) / 3600, 2)
            if self.first else 0,
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


# =============================================================
# Threat
# =============================================================
# What the addon keeps when its store doesn't say (Threat.lua's keepDays and MAX_BYTES).
THREAT_KEEP_DAYS = 14
THREAT_MAX_BYTES = 20 * 1024 * 1024
# How long before a fight ages out, and at what share of the size cap, we warn.
THREAT_WARN_DAYS = 4
THREAT_WARN_SHARE = 0.8


def threat_stores(root):
    """(account, path) of every BayovThreatLog.lua under a client's WTF folder."""
    accounts = os.path.join(root, 'WTF', 'Account')
    if not os.path.isdir(accounts):
        return
    for acct in sorted(os.listdir(accounts)):
        path = os.path.join(accounts, acct, 'SavedVariables', THREAT_STORE)
        if os.path.isfile(path):
            yield acct, path


def number(v):
    """A row's field as a number, or None when it isn't a finite one."""
    try:
        n = float(v)
    except ValueError:
        return None
    if n != n or n in (float('inf'), float('-inf')):
        return None
    return int(n) if n == int(n) else n


def parse_rows(text):
    """A fight's rows string as lists of numbers."""
    return [[number(v) for v in line.split(',')] for line in (text or '').split('\n') if line]


def finite(v):
    """A value with any non-finite float in it made None, so it writes as JSON."""
    if isinstance(v, float) and (v != v or v in (float('inf'), float('-inf'))):
        return None
    if isinstance(v, dict):
        return {k: finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [finite(x) for x in v]
    return v


def local(s):
    """'2026-10-28 21:03:04.123' or '2026-10-28T21:03:04' as a naive local datetime."""
    s = s.replace('T', ' ')
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S.%f' if '.' in s else '%Y-%m-%d %H:%M:%S')


def fight_key(f):
    return '%s|%s' % (f.get('account'), f.get('at'))


class Store:
    """The fights a client's BayovThreatLog.lua files hold, with each store's limits. `bad`
    names the files that didn't read; the others are read all the same."""

    def __init__(self, root):
        self.fights, self.bad, self.stores = [], [], []
        for acct, path in threat_stores(root):
            try:
                db = savedvars.read(path).get(THREAT_GLOBAL) or {}
                if not isinstance(db, dict):
                    raise ValueError('BayovThreatLogDB is not a table')
                fights = [self.fight(acct, f) for f in db.get('fights') or []
                          if isinstance(f, dict) and isinstance(f.get('at'), str)]
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as err:
                self.bad.append('%s (%s)' % (path, err))
                continue
            keep = db.get('keepDays') if isinstance(db.get('keepDays'), (int, float)) else THREAT_KEEP_DAYS
            cap = db.get('maxBytes') if isinstance(db.get('maxBytes'), (int, float)) else THREAT_MAX_BYTES
            self.stores.append({'account': acct, 'path': path, 'keep': keep, 'cap': cap,
                                'bytes': os.path.getsize(path)})
            self.fights += fights
        self.fights.sort(key=lambda f: f['_at'])

    @staticmethod
    def fight(acct, f):
        f = finite(dict(f))
        f['account'] = acct
        rows = f.pop('rows', '')
        f['readings'] = parse_rows(rows if isinstance(rows, str) else '')
        for k in ('who', 'mobs'):
            if not isinstance(f.get(k), list):  # an empty Lua table, or nothing
                f[k] = []
        f['_at'] = local(f['at'])
        return f


def threat_base(log):
    """X.txt.xz -> X, the stem of the log's threat files."""
    return log[:-len('.txt.xz')]


def threat_files(log):
    """The threat files next to an archived log, first one first."""
    base = threat_base(log)
    folder, stem = os.path.split(os.path.join(HERE, base))
    if not os.path.isdir(folder):
        return []
    found = []
    for name in os.listdir(folder):
        m = re.fullmatch(re.escape(os.path.basename(stem)) + r'\.threat(?:\.(\d+))?\.jsonl\.xz', name)
        if m:
            found.append((int(m.group(1) or 1), os.path.join(os.path.dirname(base), name)))
    return [p for _, p in sorted(found)]


def free_name(stem):
    """<stem>.threat.jsonl.xz, or .threat.2.jsonl.xz and on when that one exists."""
    n = 1
    while True:
        path = stem + ('.threat.jsonl.xz' if n == 1 else '.threat.%d.jsonl.xz' % n)
        if not os.path.exists(os.path.join(HERE, path)):
            return path
        n += 1


def threat_summary(paths):
    fights = [f for p in paths for f in read_threat(p)]
    others = []
    for f in fights:
        for w in f.get('who') or []:
            if w.get('kind') == 'other':
                label = '%s (%s)' % (w.get('name') or '?', (w.get('class') or '?').lower())
                if label not in others:
                    others.append(label)
    return {
        'fights': len(fights),
        'readings': sum(len(f.get('readings') or []) for f in fights),
        'mobs': len({m.get('guid') for f in fights for m in f.get('mobs') or []}),
        'hidden': sum(f.get('secret') or 0 for f in fights),
        'others': others,
        'files': [os.path.basename(p) for p in paths],
        'xz_bytes': sum(os.path.getsize(os.path.join(HERE, p)) for p in paths),
    }


def read_threat(path):
    with lzma.open(os.path.join(HERE, path), 'rt', encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def write_threat(path, fights):
    """Writes the fights as JSON lines, xz-compressed, and checks it reads back the same."""
    fights = [{k: v for k, v in f.items() if not k.startswith('_')} for f in fights]
    full = os.path.join(HERE, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + '.part'
    with lzma.open(tmp, 'wt', encoding='utf-8', preset=9) as f:
        for fight in fights:
            f.write(json.dumps(fight, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')
    with lzma.open(tmp, 'rt', encoding='utf-8') as f:
        back = [json.loads(line) for line in f if line.strip()]
    if back != json.loads(json.dumps(fights)):
        os.remove(tmp)
        sys.exit('%s does not read back the fights it was given' % tmp)
    os.replace(tmp, full)


def load_ledger():
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding='utf-8') as f:
            return json.load(f)
    return {}


def write_json(path, value):
    """Writes JSON through a temporary file, so a crash never leaves half a file."""
    tmp = path + '.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def window(e):
    """A log's (earliest, latest) line, as naive local times."""
    if e.get('span') and all(e['span']):
        return local(e['span'][0]), local(e['span'][1])
    a, b = local(e['start']), local(e['end'])
    if b < a:
        # Logged before 'span' existed, across the autumn change: the repeated hour.
        return b - timedelta(hours=1), a + timedelta(hours=1)
    return a, b


def windows(index, client):
    """(earliest, latest, path) of the client's archived logs, by earliest."""
    return sorted(window(e) + (p,) for p, e in index.items()
                  if p.startswith(client + '/') and e.get('start') and e.get('end'))


def place(t, wins, starts, reach):
    """The log for a fight at t: the one that holds it, else the nearest within the slack.
    `reach[i]` is the latest end among wins[:i+1], so the scan back stops where no earlier
    log can reach t."""
    slack = timedelta(seconds=THREAT_SLACK)
    best = None
    i = bisect.bisect_right(starts, t + slack) - 1
    while i >= 0 and reach[i] >= t - slack:
        a, b, path = wins[i]
        gap = timedelta(0) if a <= t <= b else min(abs(t - a), abs(t - b))
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, path)
        i -= 1
    return best[1] if best else None


def unarchived_logs(root, index, client):
    """Whether the client's game folder has a combat log that isn't archived yet."""
    logs = os.path.join(root, 'Logs')
    if not os.path.isdir(logs):
        return False
    return any(LOG_NAME.match(n) and os.path.join(client, n + '.xz') not in index
               for n in os.listdir(logs))


def archive_threat(index, dry_run):
    """Archives the fights threat.json doesn't list yet. Returns how many files it wrote."""
    ledger = load_ledger()
    written = 0
    now = datetime.now()
    for client, root in clients():
        if not any(threat_stores(root)):
            continue
        store = Store(root)
        for bad in store.bad:
            print('threat on %s: %s does not read, so its fights wait' % (client, bad))
        done = ledger.setdefault(client, {})
        wins = windows(index, client)
        starts = [w[0] for w in wins]
        reach, top = [], None
        for a, b, _ in wins:
            top = b if top is None or b > top else top
            reach.append(top)
        newest = top
        pending = unarchived_logs(root, index, client)
        slack = timedelta(seconds=THREAT_SLACK)
        to_log, unlogged, waiting = {}, {}, []
        for f in store.fights:
            if fight_key(f) in done:
                continue
            t = f['_at']
            if newest is None or (pending and t >= newest - slack):
                waiting.append(f)
                continue
            path = place(t, wins, starts, reach)
            if path:
                to_log.setdefault(path, []).append(f)
            elif t < newest - slack:
                unlogged.setdefault(f['at'][:10], []).append(f)
            else:
                waiting.append(f)
        writes = [(free_name(threat_base(p)), fs, p) for p, fs in sorted(to_log.items())]
        writes += [(free_name(os.path.join(client, 'unlogged', day)), fs, None)
                   for day, fs in sorted(unlogged.items())]
        for path, fs, log in writes:
            if dry_run:
                print('would write %d fights to %s' % (len(fs), path))
                written += 1
                continue
            write_threat(path, fs)
            for f in fs:
                done[fight_key(f)] = path
            if log:
                index[log]['threat'] = threat_summary(threat_files(log))
            print('threat %s: %d fights, %d readings%s' % (
                path, len(fs), sum(len(f['readings']) for f in fs),
                '' if log else ' (no combat log holds them)'))
            written += 1
        if waiting:
            print('%s: %d fights wait for a combat log not archived yet' % (client, len(waiting)))
        # Anything not archived by now ages out of the store unseen if it waits too long.
        left = [f for f in store.fights if fight_key(f) not in done]
        for st in store.stores:
            mine = [f for f in left if f['account'] == st['account']]
            if mine:
                age = (now - mine[0]['_at']).total_seconds() / 86400
                if age >= st['keep'] - THREAT_WARN_DAYS:
                    print('%s: %d fights not archived yet, the oldest %.0f days old. BayovCore drops '
                          'them at %s days. They wait for their combat log to be archived, which '
                          'happens once the game no longer writes to it.'
                          % (st['path'], len(mine), age, st['keep']))
            if st['bytes'] > THREAT_WARN_SHARE * st['cap']:
                print('%s is %.1f MB. BayovCore drops its oldest fights past %.0f MB, archived or '
                      'not.' % (st['path'], st['bytes'] / 1e6, st['cap'] / 1e6))
    if written and not dry_run:
        write_json(LEDGER, ledger)
    return written


def rebuild_ledger(index):
    """threat.json and each log's threat counts, from the threat files on disk."""
    ledger = {}
    for client in sorted(os.listdir(HERE)):
        folder = os.path.join(HERE, client)
        if not os.path.isdir(folder) or client.startswith('.'):
            continue
        for d, _, names in os.walk(folder):
            for name in sorted(names):
                if name.endswith('.jsonl.xz') and '.threat.' in name:
                    path = os.path.relpath(os.path.join(d, name), HERE)
                    for f in read_threat(path):
                        ledger.setdefault(client, {})[fight_key(f)] = path
    for path, e in index.items():
        files = threat_files(path)
        if files:
            e['threat'] = threat_summary(files)
        else:
            e.pop('threat', None)
    write_json(LEDGER, ledger)


def load_index():
    if os.path.exists(INDEX):
        with open(INDEX, encoding='utf-8') as f:
            return json.load(f)
    return {}


def write_index(index):
    tmp = INDEX + '.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(dict(sorted(index.items())), f, indent=1, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, INDEX)
    mb = lambda b: '%.1f' % (b / 1e6)
    rows = sorted(index.items(), key=lambda kv: kv[1].get('start') or '')
    out = ['# Combat logs', '',
           'Generated by `archive.py` from index.json. One row per log, oldest first.',
           'Zones show minutes spent, encounters show kills and wipes, threat the fights',
           'BayovCore recorded threat in (`<log>.threat*.jsonl.xz` next to the log). Fights',
           'with no combat log are in `<client>/unlogged/`.', '',
           'Total: %d logs, %s MB raw, %s MB compressed.' % (
               len(rows), mb(sum(e['raw_bytes'] for _, e in rows)),
               mb(sum(e['xz_bytes'] for _, e in rows))), '',
           '| file | start | hours | build | characters | zones | encounters | kills / deaths '
           '| threat | raw MB | xz MB |',
           '|---|---|---|---|---|---|---|---|---|---|---|']
    for path, e in rows:
        zones = ', '.join('%s %d' % (z, m) for z, m in list(e['zones'].items())[:6])
        kills = sum(1 for x in e['encounters'] if x['kill'])
        wipes = len(e['encounters']) - kills
        enc = ('%d kills, %d wipes' % (kills, wipes)) if e['encounters'] else ''
        th = e.get('threat') or {}
        threat = ('%d fight%s, %d readings' % (th['fights'], '' if th['fights'] == 1 else 's', th['readings'])
                  if th.get('fights') else '')
        out.append('| %s | %s | %s | %s | %s | %s | %s | %d / %d | %s | %s | %s |' % (
            path, (e['start'] or '').replace('T', ' '), e['hours'], e['build'] or '',
            ', '.join(e['characters']), zones, enc, e['our_kills'], e['our_deaths'], threat,
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
    global GAME
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--reindex', action='store_true')
    ap.add_argument('--game', default=GAME, help='the World of Warcraft folder')
    args = ap.parse_args()
    GAME = args.game
    index = load_index()
    if args.reindex:
        if not os.path.isdir(GAME):
            sys.exit('no game folder at %s (--game names another), which --reindex needs for '
                     'our characters' % GAME)
        old, index = index, {}
        for client in sorted(os.listdir(HERE)):
            folder = os.path.join(HERE, client)
            if not os.path.isdir(folder) or client.startswith('.'):
                continue
            root = os.path.join(GAME, '_%s_' % client)
            chars = our_characters(root) if os.path.isdir(root) else set()
            for name in sorted(os.listdir(folder)):
                if not name.endswith('.txt.xz'):
                    continue
                path = os.path.join(client, name)
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
                was = old.get(path) or {}
                # A client that's gone from the game folder has no character list to go by,
                # so what we counted for our characters stays as it was.
                if not chars and was.get('characters'):
                    for k in ('characters', 'our_kills', 'our_deaths'):
                        if k in was:
                            e[k] = was[k]
                index[path] = e
                print('indexed', name)
        rebuild_ledger(index)
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
    threat = archive_threat(index, args.dry_run)
    if not todo and not threat:
        print('nothing new')
    elif not args.dry_run:
        write_index(index)


if __name__ == '__main__':
    main()
