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
account's SavedVariables/BayovThreatLog.lua for 14 days. We copy each log's fights next to
it, as `<client>/<log>.threat.jsonl.xz`: one fight per line, a JSON object with the fight's
fields as the addon wrote them and its rows as `readings`, each
[ms since the fight's `at`, mob, who, status, scaled %, raw %, threat], where mob and who
are 1-based places in the fight's `mobs` and `who`, and a status of -1 (with nothing after
it) means that player dropped off the mob's threat list. forever-sim's
tools/combat_log/threat.py reads them against the log.

Each fight belongs to one log: the one whose first and last line hold its `at`, else the
nearest within THREAT_SLACK seconds. The game writes BayovThreatLog.lua only at logout and
/reload, and stamps `savedAt` then, so a log's threat is written only once a store was saved
after the log ended. That holds with the game open too. It's written once: a log that got
its threat, or was found to have none, is marked in the index. Logs that ended before the
account started recording (`since`) get nothing. A store that doesn't read is reported and
leaves its client's logs for the next run. We warn when fights not archived yet are near
the 14 days or the 20 MB the addon keeps.
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


# =============================================================
# Threat
# =============================================================
# The addon keeps 14 days and at most 20 MB (Threat.lua's keepDays and MAX_BYTES). We warn
# a few days and a few MB before either.
THREAT_WARN_DAYS = 10
THREAT_WARN_BYTES = 16 * 1024 * 1024


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


class Store:
    """What a client's BayovThreatLog.lua files hold: the fights, and when recording began and
    the files were last saved. `bad` names the files that didn't read."""

    def __init__(self, root):
        self.fights, self.since, self.saved, self.bad, self.big = [], None, None, [], []
        for acct, path in threat_stores(root):
            try:
                db = savedvars.read(path).get(THREAT_GLOBAL) or {}
                since = local(db['since']) if isinstance(db.get('since'), str) else None
                saved = local(db['savedAt']) if isinstance(db.get('savedAt'), str) else None
                fights = [self.fight(acct, f) for f in db.get('fights') or []
                          if isinstance(f, dict) and isinstance(f.get('at'), str)]
            except (ValueError, KeyError, IndexError, TypeError) as err:
                self.bad.append('%s (%s)' % (path, err))
                continue
            if since and (self.since is None or since < self.since):
                self.since = since
            if saved and (self.saved is None or saved > self.saved):
                self.saved = saved
            if os.path.getsize(path) > THREAT_WARN_BYTES:
                self.big.append(path)
            self.fights += fights
        self.fights.sort(key=lambda f: f['_at'])

    @staticmethod
    def fight(acct, f):
        f = finite(dict(f))
        f['account'] = acct
        f['readings'] = parse_rows(f.pop('rows', ''))
        for k in ('who', 'mobs'):
            if isinstance(f.get(k), dict):  # an empty Lua table
                f[k] = []
        f['_at'] = local(f['at'])
        return f


def threat_name(log):
    """The threat file next to an archived log: X.txt.xz -> X.threat.jsonl.xz."""
    return log[:-len('.txt.xz')] + '.threat.jsonl.xz'


def threat_summary(fights, path):
    others = []
    for f in fights:
        for w in f.get('who') or []:
            if w.get('kind') == 'other':
                label = '%s (%s)' % (w.get('name') or '?', (w.get('class') or '?').lower())
                if label not in others:
                    others.append(label)
    return {
        'fights': len(fights),
        'readings': sum(len(f['readings']) for f in fights),
        'mobs': len({m.get('guid') for f in fights for m in f.get('mobs') or []}),
        'hidden': sum(f.get('secret') or 0 for f in fights),
        'others': others,
        'file': os.path.basename(path),
        'xz_bytes': os.path.getsize(os.path.join(HERE, path)),
    }


def read_threat(path):
    with lzma.open(os.path.join(HERE, path), 'rt', encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def write_threat(path, fights):
    """Writes the fights as JSON lines, xz-compressed, and checks it reads back the same."""
    fights = [{k: v for k, v in f.items() if not k.startswith('_')} for f in fights]
    full = os.path.join(HERE, path)
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


def windows(index, client):
    """(start, end, path) of the client's archived logs, by start. A log whose end comes before
    its start (it ran across the autumn clock change) is left out."""
    out = []
    for path, e in index.items():
        if path.startswith(client + '/') and e.get('start') and e.get('end'):
            a, b = local(e['start']), local(e['end'])
            if b < a:
                print('%s runs across a clock change, so no threat is matched to it' % path)
                continue
            out.append((a, b, path))
    return sorted(out)


def place(fights, wins):
    """Each fight's log: the one that holds its `at`, else the nearest within the slack.
    Returns {path: [fights]} and the fights no log takes."""
    slack = timedelta(seconds=THREAT_SLACK)
    starts = [w[0] for w in wins]
    placed, loose = {}, []
    for f in fights:
        t = f['_at']
        i = bisect.bisect_right(starts, t + slack)
        best = None
        # Logs that start before t + slack; only the last few can still hold t.
        for a, b, path in reversed(wins[max(0, i - 4):i]):
            gap = timedelta(0) if a <= t <= b else min(abs(t - a), abs(t - b))
            if gap <= slack and (best is None or gap < best[0]):
                best = (gap, path)
        if best:
            placed.setdefault(best[1], []).append(f)
        else:
            loose.append(f)
    return placed, loose


def archive_threat(index, dry_run):
    """Puts each archived log's fights next to it. Returns how many logs changed."""
    changed = 0
    for client, root in clients():
        if not any(threat_stores(root)):
            continue
        store = Store(root)
        for bad in store.bad:
            print('skip threat on %s: %s does not read' % (client, bad))
        if store.bad:
            continue
        wins = windows(index, client)
        placed, loose = place(store.fights, wins)
        waiting = []
        for a, b, path in wins:
            e = index[path]
            if 'threat' in e or store.since is None or b < store.since:
                continue
            if store.saved is None or store.saved < b:
                # The game hasn't saved since this log ended, so its last fights may be
                # missing. The next run after a logout or /reload takes it.
                waiting.append(path)
                continue
            mine = placed.get(path, [])
            if dry_run:
                print('would add %d fights to %s' % (len(mine), path))
                continue
            if mine:
                tpath = threat_name(path)
                if not os.path.exists(os.path.join(HERE, tpath)):
                    write_threat(tpath, mine)
                e['threat'] = threat_summary(read_threat(tpath), tpath)
                print('threat %s: %d fights, %d readings' % (
                    tpath, e['threat']['fights'], e['threat']['readings']))
            else:
                e['threat'] = {'fights': 0}
            changed += 1
        if waiting:
            print('%s: %d logs wait for their threat until the game saves (logout or /reload)'
                  % (client, len(waiting)))
        # Fights no log takes, though a later log is archived: the combat log was off.
        # Fights after the newest archived log wait for it.
        newest = wins[-1][1] if wins else None
        orphans = [f for f in loose if newest and f['_at'] <= newest]
        if orphans:
            print('%s: %d fights fall in no combat log (was the log off?), oldest %s' % (
                client, len(orphans), orphans[0]['at']))
        # Fights in no marked log yet, and how close the oldest is to aging out.
        archived = {id(f) for p, fs in placed.items() if 'threat' in index[p] for f in fs}
        lost = {id(f) for f in orphans}
        unsaved = [f for f in store.fights if id(f) not in archived and id(f) not in lost]
        if unsaved:
            age = (datetime.now() - unsaved[0]['_at']).days
            if age >= THREAT_WARN_DAYS:
                print('%s: %d fights are not archived yet, the oldest %d days old. BayovCore drops '
                      'them at 14 days: archive the logs they belong to before then.'
                      % (client, len(unsaved), age))
        for path in store.big:
            print('%s is %.1f MB. BayovCore keeps at most 20 MB and drops the oldest fights past '
                  'that, archived or not.' % (path, os.path.getsize(path) / 1e6))
    return changed


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
           'Zones show minutes spent, encounters show kills and wipes, threat the fights',
           'BayovCore recorded threat in (`<log>.threat.jsonl.xz` next to the log, "none" when',
           'it recorded none during the log).', '',
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
                  if th.get('fights') else 'none' if 'fights' in th else '')
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
                # A client that's gone from the game folder has no character list to go by.
                if not chars and was.get('characters'):
                    e['characters'] = was['characters']
                if os.path.exists(os.path.join(HERE, threat_name(path))):
                    e['threat'] = threat_summary(read_threat(threat_name(path)), threat_name(path))
                elif 'threat' in was:
                    e['threat'] = was['threat']
                index[path] = e
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
    threat = archive_threat(index, args.dry_run)
    if not todo and not threat:
        print('nothing new')
    elif not args.dry_run:
        write_index(index)


if __name__ == '__main__':
    main()
