"""Read the Lua tables WoW writes into a SavedVariables file.

A copy of forever-sim's tools/combat_log/history.py parser, so this repo needs nothing but
Python. archive.py reads BayovThreatLog.lua with it.

    from savedvars import read
    read(path)  ->  {global name: value}
"""
import re


class LuaParser:
    """Reads the Lua table constructors WoW writes into a SavedVariables file.

    Only what the client writes: `name = value` at the top, tables with `["key"] = value`,
    `[1] = value` and bare list values, strings, numbers, booleans and nil, and `-- [n]`
    comments after list values.
    """

    TOKEN = re.compile(r'\s+|--[^\n]*|"(?:[^"\\]|\\.)*"|[{}\[\]=,;]|[^\s{}\[\]=,;"]+', re.S)

    def __init__(self, text):
        self.tokens = [t for t in self.TOKEN.findall(text) if t.strip() and not t.startswith('--')]
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self, want=None):
        t = self.peek()
        if want is not None and t != want:
            raise ValueError('expected %r at token %d, got %r' % (want, self.i, t))
        self.i += 1
        return t

    def globals(self):
        out = {}
        while self.peek() is not None:
            name = self.take()
            self.take('=')
            out[name] = self.value()
        return out

    def value(self):
        t = self.peek()
        if t == '{':
            return self.table()
        self.take()
        if t.startswith('"'):
            return unescape(t[1:-1])
        if t == 'true':
            return True
        if t == 'false':
            return False
        if t == 'nil':
            return None
        try:
            return int(t)
        except ValueError:
            return float(t)

    def table(self):
        self.take('{')
        keyed, listed = {}, []
        while self.peek() != '}':
            if self.peek() == '[':
                self.take('[')
                key = self.value()
                self.take(']')
                self.take('=')
                keyed[key] = self.value()
            elif self.i + 1 < len(self.tokens) and self.tokens[self.i + 1] == '=':
                key = self.take()
                self.take('=')
                keyed[key] = self.value()
            else:
                listed.append(self.value())
            if self.peek() in (',', ';'):
                self.take()
        self.take('}')
        if listed and not keyed:
            return listed
        for n, v in enumerate(listed, 1):
            keyed[n] = v
        # A list WoW wrote as [1] = ..., [2] = ... is a list too.
        if keyed and all(isinstance(k, int) for k in keyed) and sorted(keyed) == list(range(1, len(keyed) + 1)):
            return [keyed[k] for k in sorted(keyed)]
        return keyed


ESCAPES = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"', "'": "'", '\n': '\n', 'a': '\a', 'b': '\b',
           'f': '\f', 'v': '\v'}


def unescape(s):
    """A Lua string literal's body as text. The client writes UTF-8 bytes as they are."""
    out, i = bytearray(), 0
    raw = s.encode('utf-8')
    while i < len(raw):
        c = raw[i]
        if c != 0x5c:
            out.append(c)
            i += 1
            continue
        nxt = chr(raw[i + 1])
        if nxt.isdigit():
            m = re.match(rb'\d{1,3}', raw[i + 1:i + 4])
            out.append(int(m.group()))
            i += 1 + len(m.group())
        else:
            out += ESCAPES.get(nxt, nxt).encode('utf-8')
            i += 2
    return out.decode('utf-8', errors='replace')


def read(path):
    """Every global a SavedVariables file sets, as Python values."""
    with open(path, encoding='utf-8', errors='replace') as f:
        return LuaParser(f.read()).globals()
