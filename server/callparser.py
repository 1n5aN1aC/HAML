# CallParser — Python port of VE3NEA's CallParser (Delphi), translated from
# the JavaScript port (callparser.js).
#
# Original code: Alex Shovkoplyas, VE3NEA — MPL 1.1
# This Python port keeps the same algorithm and data-file format.
#
# Resolves amateur radio callsigns to DXCC entities via the Prefix.lst
# database. Module API:
#   init(path)                        — read & parse Prefix.lst (idempotent)
#   is_loaded()                       — boolean
#   lookup(call)                      — best PrefixData result or None
#   lookup_all(call)                  — all PrefixData results (list)
#   distance_miles(data, lat, lon)    — Haversine miles from lat/lon, or None
#   coords(data)                      — (lat, lon) floats or None

import math
import re

# ── Constants ────────────────────────────────────────────────────────────────

CP_DIGITS = '0123456789'
CP_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
CP_CHARS = CP_DIGITS + CP_LETTERS
CP_HI_CHAR = len(CP_CHARS)

CP_DIGIT_SET = set(CP_DIGITS)
CP_LETTER_SET = set(CP_LETTERS)
CP_ALLOWED_SET = set(CP_DIGITS) | set(CP_LETTERS) | {'/'}

# CPPrefixKind
PF_NONE = 0
PF_DXCC = 1
PF_PROVINCE = 2
PF_STATION = 3
PF_DEL_DXCC = 4
PF_OLD_PREFIX = 5
PF_NON_DXCC = 6
PF_INVALID_PREFIX = 7
PF_DEL_PROVINCE = 8
PF_CITY = 9

# CPPrefixMatch
PF_NE = 0
PF_LT = 1
PF_GE = 2

# CPEndingMatch
ED_NE = 0
ED_P = 1
ED_M = 2
ED_EQ = 3

CP_RESULT_FOR_TOP = [
    [False, False, False, False],
    [True, True, True, True],
    [False, False, False, True],
]
CP_RESULT_FOR_CHILD = [
    [False, False, False, False],
    [False, False, False, False],
    [False, False, False, True],
]

CP_ALLOWED_FOR_TOP = frozenset([PF_DXCC, PF_NON_DXCC, PF_PROVINCE, PF_STATION, PF_CITY])
CP_ALLOWED_FOR_SUB = frozenset([PF_PROVINCE, PF_STATION, PF_CITY])

CP_SAFE_ONE_CHAR_PREFIXES = frozenset('UGFIKNW')
CP_ONE_CHAR_PREFIXES = CP_SAFE_ONE_CHAR_PREFIXES | frozenset(['R', 'B', 'M'])
CP_ENDING_IGNORE = frozenset(['AM', 'MM', 'QRP', 'A', 'B', 'BCN', 'LH'])

# JavaScript's WhiteSpace + LineTerminator set (what /\s/, trim() and
# parseInt's leading-whitespace skip match). Differs from Python's \s:
# JS includes U+FEFF but not U+001C..U+001F / U+0085.
_JS_WS = ('\t\n\x0b\x0c\r \u00a0\u1680'
          '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a'
          '\u2028\u2029\u202f\u205f\u3000\ufeff')
_JS_WS_SET = frozenset(_JS_WS)

_WS_RE = re.compile('[' + _JS_WS + ']+')
_ALL_DIGITS_RE = re.compile(r'^[0-9]+$')


def _js_trim(s):
    return s.strip(_JS_WS)


# ── JS numeric semantics ─────────────────────────────────────────────────────

# Emulates JavaScript parseInt(s, radix): skips leading whitespace, accepts an
# optional sign, consumes the longest valid-digit prefix, returns None for NaN.
def _js_parse_int(s, radix):
    if s is None:
        return None
    i = 0
    n = len(s)
    while i < n and s[i] in _JS_WS_SET:
        i += 1
    sign = 1
    if i < n and s[i] in '+-':
        if s[i] == '-':
            sign = -1
        i += 1
    if radix == 16 and i + 1 < n and s[i] == '0' and s[i + 1] in 'xX':
        i += 2
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'[:radix]
    j = i
    while j < n and s[j].lower() in digits:
        j += 1
    if j == i:
        return None
    return sign * int(s[i:j], radix)


# ── Helpers ──────────────────────────────────────────────────────────────────

def cp_make_prefix_data():
    return {
        'locationX': None, 'locationY': None,
        'territory': '', 'prefix': '', 'cq': '', 'itu': '',
        'continent': '', 'tz': '', 'adif': '',
        'provinceCode': '', 'province': '', 'city': '',
        'attributes': []
    }


class CPPrefixEntry:
    __slots__ = ('data', 'id', 'kind', 'level', 'mask', 'parent', 'children')

    def __init__(self):
        self.data = cp_make_prefix_data()
        self.id = -1
        self.kind = PF_NONE
        self.level = 0
        self.mask = ''
        self.parent = -1
        self.children = []


def cp_split_comma(s):
    if not s:
        return []
    return [t for t in (p.strip() for p in s.split(',')) if len(t) > 0]


def cp_find_slash(mask):
    in_bracket = False
    for i, ch in enumerate(mask):
        if ch == '[':
            in_bracket = True
        elif ch == ']':
            in_bracket = False
        elif ch == '/' and not in_bracket:
            return i
    return -1


# ── PrefixList ───────────────────────────────────────────────────────────────

class CPPrefixList:
    def __init__(self):
        self.entries = []
        self.count = 0
        self.index = [[[] for _ in range(CP_HI_CHAR)] for _ in range(CP_HI_CHAR)]

    def load_from_path(self, path):
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        self.load_from_string(text)

    def load_from_string(self, text):
        lines = re.split(r'\r?\n', text)
        self._load_from_lines(lines)
        self._build_relations()
        self._build_index()

    def _load_from_lines(self, lines):
        self.entries = []
        self.count = 0
        if len(lines) < 4:
            return

        valid_kinds = frozenset([PF_DXCC, PF_PROVINCE, PF_STATION, PF_NON_DXCC, PF_CITY])

        for line_no in range(3, len(lines)):
            line = lines[line_no]
            if not line:
                continue
            tokens = line.split('|')
            if len(tokens) < 4:
                continue

            tok0 = tokens[0]
            if len(tok0) > 0 and tok0[0] in ('L', 'M', '-'):
                tok0 = tok0[1:]

            kind_val = _js_parse_int(tok0[0:2], 16)
            if kind_val is None or kind_val not in valid_kinds:
                continue

            entry = CPPrefixEntry()
            entry.id = self.count
            entry.kind = kind_val
            entry.level = _js_parse_int(tok0[2:4], 16) or 0

            loc_x = _js_parse_int(tokens[1], 10)
            loc_y = _js_parse_int(tokens[2], 10)
            entry.data['locationX'] = loc_x
            entry.data['locationY'] = loc_y

            def tok(i):
                return tokens[i] if i < len(tokens) else ''

            entry.data['territory'] = tok(3) or ''
            entry.data['prefix'] = tok(4) or ''
            entry.data['cq'] = tok(5) or ''
            entry.data['itu'] = tok(6) or ''
            entry.data['continent'] = tok(7) or ''
            entry.data['tz'] = tok(8) or ''
            entry.data['adif'] = tok(9) or ''
            entry.data['provinceCode'] = tok(10) or ''
            entry.mask = tok(13) or ''

            self.entries.append(entry)
            self.count += 1

    def _build_relations(self):
        for i in range(self.count):
            parent_idx = self._parent_of(i)
            self.entries[i].parent = parent_idx
            if parent_idx > -1:
                self.entries[parent_idx].children.append(i)

    def _parent_of(self, entry_no):
        if self.entries[entry_no].level == 0:
            return -1
        for i in range(entry_no - 1, -1, -1):
            if self.entries[i].level < self.entries[entry_no].level:
                return i
        return -1

    def _build_index(self):
        for i in range(CP_HI_CHAR):
            for j in range(CP_HI_CHAR):
                self.index[i][j] = []

        for pno in range(self.count):
            entry = self.entries[pno]
            if entry.kind != PF_DXCC and entry.kind != PF_NON_DXCC:
                continue

            masks = cp_split_comma(entry.mask)
            for raw_mask in masks:
                mask = raw_mask
                l1_chars, l1_rest = self.chop(mask)
                mask = l1_rest
                l2_chars = CP_CHARS if len(mask) == 0 else self.chop(mask)[0]
                for c1 in l1_chars:
                    for c2 in l2_chars:
                        self._add_to_index(c1, c2, entry)

    def _add_to_index(self, c1, c2, entry):
        p1 = CP_CHARS.find(c1)
        p2 = CP_CHARS.find(c2)
        if p1 < 0 or p2 < 0:
            return
        arr = self.index[p1][p2]
        for e in arr:
            if e is entry:
                return
        arr.append(entry)

    def chop(self, s):
        if not s or len(s) == 0:
            return ('', '')

        first = s[0]

        if first == '#':
            chars = CP_DIGITS
            rest = s[1:]
        elif first == '@':
            chars = CP_LETTERS
            rest = s[1:]
        elif first == '?':
            chars = CP_CHARS
            rest = s[1:]
        elif first == '[':
            close_bracket = s.find(']')
            if close_bracket < 0:
                return ('', s[1:])
            inner = s[1:close_bracket]
            rest = s[close_bracket + 1:]
            pre_expanded = ''
            for ch in inner:
                if ch == '#':
                    pre_expanded += CP_DIGITS
                elif ch == '@':
                    pre_expanded += CP_LETTERS
                elif ch == '?':
                    pre_expanded += CP_CHARS
                else:
                    pre_expanded += ch
            inner = pre_expanded
            expanded = ''
            i = 0
            while i < len(inner):
                if 0 < i < len(inner) - 1 and inner[i] == '-':
                    frm = ord(inner[i - 1]) + 1
                    to = ord(inner[i + 1])
                    for c in range(frm, to + 1):
                        expanded += chr(c)
                    i += 1
                else:
                    expanded += inner[i]
                i += 1
            chars = expanded
        else:
            chars = first
            rest = s[1:]
        return (chars, rest)


# ── CallParser core ──────────────────────────────────────────────────────────

class CPCallParser:
    def __init__(self):
        self.prefix_list = CPPrefixList()
        self.call_list = {}
        self._call = ''
        self._hit_tree = []
        self.hit_list = []

    def load_prefix_file(self, path):
        self.prefix_list.load_from_path(path)

    def set_call(self, value):
        self._call = value
        self.hit_list = []
        self._hit_tree = []
        formatted = self._format_call()
        if formatted is not None:
            self._call = formatted
            self._resolve_call()
        return self.hit_list

    def _format_call(self):
        s = _WS_RE.sub('', (self._call or '').upper())

        mapped = self.call_list.get(s)
        if mapped:
            return 'ADIF' + mapped if _ALL_DIGITS_RE.match(mapped) else mapped

        if s.endswith('/MM'):
            return None
        if s.endswith('/ANT'):
            return 'ADIF013'
        if len(s) < 2 or s[0] == '/':
            return None
        if '//' in s:
            return None
        if s.endswith('/'):
            s = s[:-1]
        for ch in s:
            if ch != '/' and ch not in CP_ALLOWED_SET:
                return None

        parts = s.split('/')
        for i in range(len(parts) - 1, 0, -1):
            if parts[i] in CP_ENDING_IGNORE:
                del parts[i]
        if len(parts) < 1 or len(parts) > 3:
            return None

        s1 = parts[0]
        s2 = parts[1] if len(parts) > 1 else ''
        s3 = parts[2] if len(parts) > 2 else ''
        if len(s3) > 1:
            return None

        if s1.startswith('HK') and s2 == '0M':
            return s1 + '/' + s2
        if s1.startswith('FR') and s2 == 'G':
            return s1 + '/' + s2

        if len(s2) == 1 and s2 in CP_DIGIT_SET:
            if s3 != '' and s3 in CP_DIGIT_SET:
                return None
            if len(s1) > 1 and s1[1] in CP_DIGIT_SET and s1[0] in 'IKNWR':
                s1 = s1[0]
            elif len(s1) > 2 and s1[2] in CP_DIGIT_SET:
                s1 = s1[0:2]
            elif len(s1) > 3 and s1[3] in CP_DIGIT_SET:
                s1 = s1[0:3]
            else:
                return None
            s1 = s1 + s2
            s2 = ''

        if len(s1) == 1 and s1 in CP_ONE_CHAR_PREFIXES:
            s1 = s1 + '0'
        elif len(s2) == 1 and len(s3) == 1 and s1[:1] in CP_ONE_CHAR_PREFIXES:
            s2 = s2 + '0'
        elif len(s2) == 1 and s2 in CP_SAFE_ONE_CHAR_PREFIXES:
            s2 = s2 + '0'

        if len(s2) > 1 and len(s2) < len(s1):
            body = s2
        else:
            body = s1
        if len(body) < 2:
            return None

        ending = ''
        if len(s2) == 1:
            if s3 != '':
                return None
            ending = s2
        else:
            ending = s3

        return body + '/' + ending if ending else body

    def _resolve_call(self):
        if self._call.startswith('ADIF'):
            adif = _js_parse_int(self._call[4:], 10)
            if adif:
                item = self._get_adif_item(adif)
                if item:
                    self.hit_list = [item]
                    return

        c1 = CP_CHARS.find(self._call[0])
        c2 = CP_CHARS.find(self._call[1])
        if c1 < 0 or c2 < 0:
            return

        arr = self.prefix_list.index[c1][c2]
        for entry in arr:
            if self._try_mask(entry, True):
                hit = self._add_hit(entry, -1)
                self._add_sub_hits(entry, hit['id'])
        self._pack_hits()

    def _add_sub_hits(self, parent_entry, parent_id):
        for child_idx in parent_entry.children:
            child_entry = self.prefix_list.entries[child_idx]
            if self._try_mask(child_entry, False):
                hit = self._add_hit(child_entry, parent_id)
                self._add_sub_hits(child_entry, hit['id'])

    def _add_hit(self, entry, parent_id):
        hit = {
            'data': {**entry.data, 'attributes': list(entry.data['attributes'])},
            'kind': entry.kind, 'id': len(self._hit_tree),
            'parent': parent_id, 'children': []
        }
        self._hit_tree.append(hit)
        if parent_id >= 0:
            self._hit_tree[parent_id]['children'].append(hit['id'])
        return hit

    def _try_mask(self, entry, top_level):
        allowed = CP_ALLOWED_FOR_TOP if top_level else CP_ALLOWED_FOR_SUB
        if entry.kind not in allowed:
            return False
        masks = cp_split_comma(entry.mask)
        for mask in masks:
            pm = self._compare_prefix(mask)
            em = self._compare_ending(mask)
            table = CP_RESULT_FOR_TOP if top_level else CP_RESULT_FOR_CHILD
            if table[pm][em]:
                return True
        return False

    def _compare_prefix(self, mask):
        call = self._call
        pp = call.find('/')
        pm = cp_find_slash(mask)
        if pp >= 0:
            call = call[0:pp]
        if pm >= 0:
            mask = mask[0:pm]
        if not mask or not call:
            return PF_NE

        for p in range(len(call)):
            if not mask:
                return PF_GE
            chars, mask = self.prefix_list.chop(mask)
            if chars.find(call[p]) < 0:
                return PF_NE

        if not mask or mask == '.':
            return PF_GE
        if mask[-1] == '.':
            if call[-1] not in CP_DIGIT_SET:
                return PF_NE
        return PF_LT

    def _compare_ending(self, mask):
        pp = self._call.find('/')
        pm = cp_find_slash(mask)
        if pp < 0 and pm >= 0:
            return ED_M
        if pp >= 0 and pm < 0:
            if pp == len(self._call) - 2 and self._call[pp + 1] in 'MP':
                return ED_EQ
            return ED_P
        if pp < 0 and pm < 0:
            return ED_EQ
        if self._call[pp:] == mask[pm:]:
            return ED_EQ
        return ED_NE

    def _pack_hits(self):
        for i in range(len(self._hit_tree) - 1, -1, -1):
            hit = self._hit_tree[i]
            if hit['data']['locationX'] is None:
                hit['id'] = -1
                if hit['parent'] >= 0:
                    self._hit_tree[hit['parent']]['data']['attributes'].append(hit['data']['territory'])
        for i in range(len(self._hit_tree) - 1, -1, -1):
            if self._hit_tree[i]['id'] <= -1:
                continue
            dst = cp_make_prefix_data()
            self._merge_prefix_data(dst, self._hit_tree[i])
            self.hit_list.append(dst)
        self._hit_tree = []

    def _merge_prefix_data(self, dst, src):
        src['id'] = -1
        kind = src['kind']
        if kind == PF_DXCC or kind == PF_NON_DXCC:
            dst['territory'] = src['data']['territory']
        elif kind == PF_PROVINCE:
            dst['province'] = (src['data']['territory'] + ', ' + dst['province']) if dst['province'] else src['data']['territory']
        elif kind == PF_CITY:
            dst['city'] = src['data']['territory']
        elif kind == PF_STATION:
            if src['data']['locationX'] is not None:
                dst['city'] = src['data']['territory']
        if dst['locationX'] is None:
            dst['locationX'] = src['data']['locationX']
            dst['locationY'] = src['data']['locationY']
        if src['data']['locationX'] is not None:
            if not dst['prefix']:
                dst['prefix'] = src['data']['prefix']
            if not dst['cq']:
                dst['cq'] = src['data']['cq']
            if not dst['itu']:
                dst['itu'] = src['data']['itu']
            if not dst['continent']:
                dst['continent'] = src['data']['continent']
            if not dst['tz']:
                dst['tz'] = src['data']['tz']
            if not dst['adif']:
                dst['adif'] = src['data']['adif']
            if not dst['provinceCode']:
                dst['provinceCode'] = src['data']['provinceCode']
        dst['attributes'] = dst['attributes'] + src['data']['attributes']
        if src['parent'] >= 0:
            self._merge_prefix_data(dst, self._hit_tree[src['parent']])

    def _get_adif_item(self, adif):
        # Deliberate divergence from callparser.js: the JS compares
        # String(adif) === data.adif and never matches the zero-padded ADIF
        # fields below 100 (so '/ANT' -> ADIF013 fell through and resolved the
        # literal "ADIF013" as a callsign -> United States). The Delphi
        # original (CallParser.pas GetAdifItem) compares numerically via
        # StrToInt; restore that so '/ANT' resolves to Antarctica.
        for i in range(self.prefix_list.count):
            e = self.prefix_list.entries[i]
            if e.kind != PF_DXCC:
                continue
            try:
                entry_adif = int(e.data['adif'], 10)
            except ValueError:
                continue
            if entry_adif == adif:
                return {**e.data, 'attributes': list(e.data['attributes'])}
        return None


# ── Module API ───────────────────────────────────────────────────────────────

_parser = None
_loaded = False


# Read & parse the prefix database. Idempotent: repeat calls no-op once loaded.
def init(prefix_path='Prefix.lst'):
    global _parser, _loaded
    if _loaded:
        return
    p = CPCallParser()
    p.load_prefix_file(prefix_path)
    _parser = p
    _loaded = True


def is_loaded():
    return _loaded


# Look up a callsign. Returns the best (first) PrefixData result or None.
def lookup(call):
    if not _loaded or not call:
        return None
    results = _parser.set_call(_js_trim(call.upper()))
    return results[0] if len(results) > 0 else None


# Look up a callsign. Returns all PrefixData results (list).
def lookup_all(call):
    if not _loaded or not call:
        return []
    return _parser.set_call(_js_trim(call.upper()))


# Haversine distance in miles from a reference point (degrees) to a
# PrefixData location, or None when the entity has no coordinates.
def distance_miles(prefix_data, ref_lat, ref_lon):
    if not prefix_data or prefix_data['locationX'] is None or prefix_data['locationY'] is None:
        return None

    # Prefix.lst stores coords as degrees × 180
    lat2 = prefix_data['locationY'] / 180
    lon2 = prefix_data['locationX'] / 180

    r = 3958.8  # Earth radius in miles
    d_lat = (lat2 - ref_lat) * math.pi / 180
    d_lon = (lon2 - ref_lon) * math.pi / 180
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(ref_lat * math.pi / 180) * math.cos(lat2 * math.pi / 180) *
         math.sin(d_lon / 2) ** 2)
    # JS Math.sqrt(negative) is NaN (a can exceed 1 by rounding near antipodes);
    # NaN then propagates through atan2/round exactly as in JS.
    sqrt_a = math.sqrt(a) if a >= 0 else float('nan')
    sqrt_1a = math.sqrt(1 - a) if 1 - a >= 0 else float('nan')
    c = 2 * math.atan2(sqrt_a, sqrt_1a)
    val = r * c
    if math.isnan(val):
        return float('nan')
    # JS Math.round rounds .5 toward +Infinity (unlike Python's round)
    return int(math.floor(val + 0.5))


# Decode the ×180 coordinate encoding: (lat, lon) floats or None.
def coords(prefix_data):
    if not prefix_data or prefix_data['locationX'] is None or prefix_data['locationY'] is None:
        return None
    return (prefix_data['locationY'] / 180, prefix_data['locationX'] / 180)
