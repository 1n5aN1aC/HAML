// CallParser — JavaScript port of VE3NEA's CallParser (Delphi)
//
// Original code: Alex Shovkoplyas, VE3NEA — MPL 1.1
// This JS port keeps the same algorithm and data-file format.
//
// Resolves amateur radio callsigns to DXCC entities via the Prefix.lst
// database (served from public/). Module API:
//   await init()                      — fetch & parse Prefix.lst (idempotent)
//   isLoaded()                        — boolean
//   lookup(call)                      — best PrefixData result or null
//   lookupAll(call)                   — all PrefixData results (array)
//   distanceMiles(data, lat, lon)     — Haversine miles from lat/lon, or null

// ── Constants ────────────────────────────────────────────────────────────────

const CP_DIGITS = '0123456789';
const CP_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const CP_CHARS = CP_DIGITS + CP_LETTERS;
const CP_HI_CHAR = CP_CHARS.length;

const CP_DIGIT_SET = new Set(CP_DIGITS);
const CP_LETTER_SET = new Set(CP_LETTERS);
const CP_ALLOWED_SET = new Set([...CP_DIGITS, ...CP_LETTERS, '/']);

const CPPrefixKind = Object.freeze({
    pfNone: 0, pfDXCC: 1, pfProvince: 2, pfStation: 3,
    pfDelDXCC: 4, pfOldPrefix: 5, pfNonDXCC: 6,
    pfInvalidPrefix: 7, pfDelProvince: 8, pfCity: 9
});

const CPPrefixMatch = Object.freeze({ pfNE: 0, pfLT: 1, pfGE: 2 });
const CPEndingMatch = Object.freeze({ edNE: 0, edP: 1, edM: 2, edEQ: 3 });

const CP_RESULT_FOR_TOP = [
    [false, false, false, false],
    [true, true, true, true],
    [false, false, false, true]
];
const CP_RESULT_FOR_CHILD = [
    [false, false, false, false],
    [false, false, false, false],
    [false, false, false, true]
];

const CP_ALLOWED_FOR_TOP = new Set([
    CPPrefixKind.pfDXCC, CPPrefixKind.pfNonDXCC,
    CPPrefixKind.pfProvince, CPPrefixKind.pfStation, CPPrefixKind.pfCity
]);
const CP_ALLOWED_FOR_SUB = new Set([
    CPPrefixKind.pfProvince, CPPrefixKind.pfStation, CPPrefixKind.pfCity
]);

const CP_SAFE_ONE_CHAR_PREFIXES = new Set('UGFIKNW');
const CP_ONE_CHAR_PREFIXES = new Set([...CP_SAFE_ONE_CHAR_PREFIXES, 'R', 'B', 'M']);
const CP_ENDING_IGNORE = new Set(['AM', 'MM', 'QRP', 'A', 'B', 'BCN', 'LH']);

// ── Helpers ──────────────────────────────────────────────────────────────────

function cpMakePrefixData() {
    return {
        locationX: null, locationY: null,
        territory: '', prefix: '', cq: '', itu: '',
        continent: '', tz: '', adif: '',
        provinceCode: '', province: '', city: '',
        attributes: []
    };
}

function cpMakePrefixEntry() {
    return {
        data: cpMakePrefixData(), id: -1,
        kind: CPPrefixKind.pfNone, level: 0,
        mask: '', parent: -1, children: []
    };
}

function cpSplitComma(s) {
    if (!s) return [];
    return s.split(',').map(t => t.trim()).filter(t => t.length > 0);
}

function cpFindSlash(mask) {
    let inBracket = false;
    for (let i = 0; i < mask.length; i++) {
        if (mask[i] === '[') inBracket = true;
        else if (mask[i] === ']') inBracket = false;
        else if (mask[i] === '/' && !inBracket) return i;
    }
    return -1;
}

// ── PrefixList ───────────────────────────────────────────────────────────────

class CPPrefixList {
    constructor() {
        this.entries = [];
        this.count = 0;
        this.index = [];
        for (let i = 0; i < CP_HI_CHAR; i++) {
            this.index[i] = [];
            for (let j = 0; j < CP_HI_CHAR; j++) {
                this.index[i][j] = [];
            }
        }
    }

    async loadFromFile(url) {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`fetching ${url} failed: ${resp.status}`);
        const text = await resp.text();
        this.loadFromString(text);
    }

    loadFromString(text) {
        const lines = text.split(/\r?\n/);
        this._loadFromLines(lines);
        this._buildRelations();
        this._buildIndex();
    }

    _loadFromLines(lines) {
        this.entries = [];
        this.count = 0;
        if (lines.length < 4) return;

        const VALID_KINDS = new Set([
            CPPrefixKind.pfDXCC, CPPrefixKind.pfProvince,
            CPPrefixKind.pfStation, CPPrefixKind.pfNonDXCC, CPPrefixKind.pfCity
        ]);

        for (let lineNo = 3; lineNo < lines.length; lineNo++) {
            const line = lines[lineNo];
            if (!line) continue;
            const tokens = line.split('|');
            if (tokens.length < 4) continue;

            let tok0 = tokens[0];
            if (tok0.length > 0 && (tok0[0] === 'L' || tok0[0] === 'M' || tok0[0] === '-'))
                tok0 = tok0.substring(1);

            const kindVal = parseInt(tok0.substring(0, 2), 16);
            if (isNaN(kindVal) || !VALID_KINDS.has(kindVal)) continue;

            const entry = cpMakePrefixEntry();
            entry.id = this.count;
            entry.kind = kindVal;
            entry.level = parseInt(tok0.substring(2, 4), 16) || 0;

            const locX = parseInt(tokens[1], 10);
            const locY = parseInt(tokens[2], 10);
            entry.data.locationX = isNaN(locX) ? null : locX;
            entry.data.locationY = isNaN(locY) ? null : locY;

            entry.data.territory = tokens[3] || '';
            entry.data.prefix = tokens[4] || '';
            entry.data.cq = tokens[5] || '';
            entry.data.itu = tokens[6] || '';
            entry.data.continent = tokens[7] || '';
            entry.data.tz = tokens[8] || '';
            entry.data.adif = tokens[9] || '';
            entry.data.provinceCode = tokens[10] || '';
            entry.mask = tokens[13] || '';

            this.entries.push(entry);
            this.count++;
        }
    }

    _buildRelations() {
        for (let i = 0; i < this.count; i++) {
            const parentIdx = this._parentOf(i);
            this.entries[i].parent = parentIdx;
            if (parentIdx > -1) this.entries[parentIdx].children.push(i);
        }
    }

    _parentOf(entryNo) {
        if (this.entries[entryNo].level === 0) return -1;
        for (let i = entryNo - 1; i >= 0; i--) {
            if (this.entries[i].level < this.entries[entryNo].level) return i;
        }
        return -1;
    }

    _buildIndex() {
        for (let i = 0; i < CP_HI_CHAR; i++)
            for (let j = 0; j < CP_HI_CHAR; j++)
                this.index[i][j] = [];

        for (let pno = 0; pno < this.count; pno++) {
            const entry = this.entries[pno];
            if (entry.kind !== CPPrefixKind.pfDXCC && entry.kind !== CPPrefixKind.pfNonDXCC) continue;

            const masks = cpSplitComma(entry.mask);
            for (const rawMask of masks) {
                let mask = rawMask;
                const l1 = this.chop(mask);
                mask = l1.rest;
                const l2chars = mask.length === 0 ? CP_CHARS : this.chop(mask).chars;
                for (const c1 of l1.chars) {
                    for (const c2 of l2chars) {
                        this._addToIndex(c1, c2, entry);
                    }
                }
            }
        }
    }

    _addToIndex(c1, c2, entry) {
        const p1 = CP_CHARS.indexOf(c1);
        const p2 = CP_CHARS.indexOf(c2);
        if (p1 < 0 || p2 < 0) return;
        const arr = this.index[p1][p2];
        if (arr.indexOf(entry) === -1) arr.push(entry);
    }

    chop(str) {
        if (!str || str.length === 0) return { chars: '', rest: '' };

        const first = str[0];
        let chars, rest;

        switch (first) {
            case '#': chars = CP_DIGITS; rest = str.substring(1); break;
            case '@': chars = CP_LETTERS; rest = str.substring(1); break;
            case '?': chars = CP_CHARS; rest = str.substring(1); break;
            case '[': {
                const closeBracket = str.indexOf(']');
                if (closeBracket < 0) { chars = ''; rest = str.substring(1); break; }
                let inner = str.substring(1, closeBracket);
                rest = str.substring(closeBracket + 1);
                let preExpanded = '';
                for (let i = 0; i < inner.length; i++) {
                    if (inner[i] === '#') preExpanded += CP_DIGITS;
                    else if (inner[i] === '@') preExpanded += CP_LETTERS;
                    else if (inner[i] === '?') preExpanded += CP_CHARS;
                    else preExpanded += inner[i];
                }
                inner = preExpanded;
                let expanded = '';
                for (let i = 0; i < inner.length; i++) {
                    if (i > 0 && i < inner.length - 1 && inner[i] === '-') {
                        const from = inner.charCodeAt(i - 1) + 1;
                        const to = inner.charCodeAt(i + 1);
                        for (let c = from; c <= to; c++) expanded += String.fromCharCode(c);
                        i++;
                    } else {
                        expanded += inner[i];
                    }
                }
                chars = expanded;
                break;
            }
            default: chars = first; rest = str.substring(1); break;
        }
        return { chars, rest };
    }
}

// ── CallParser core ──────────────────────────────────────────────────────────

class CPCallParser {
    constructor() {
        this.prefixList = new CPPrefixList();
        this.callList = new Map();
        this._call = '';
        this._hitTree = [];
        this.hitList = [];
    }

    async loadPrefixFile(url) {
        await this.prefixList.loadFromFile(url);
    }

    setCall(value) {
        this._call = value;
        this.hitList = [];
        this._hitTree = [];
        const formatted = this._formatCall();
        if (formatted !== false) {
            this._call = formatted;
            this._resolveCall();
        }
        return this.hitList;
    }

    _formatCall() {
        let s = (this._call || '').toUpperCase().replace(/\s+/g, '');

        const mapped = this.callList.get(s);
        if (mapped) {
            return /^\d+$/.test(mapped) ? 'ADIF' + mapped : mapped;
        }

        if (s.endsWith('/MM')) return false;
        if (s.endsWith('/ANT')) return 'ADIF013';
        if (s.length < 2 || s[0] === '/') return false;
        if (s.includes('//')) return false;
        if (s.endsWith('/')) s = s.slice(0, -1);
        for (const ch of s) {
            if (ch !== '/' && !CP_ALLOWED_SET.has(ch)) return false;
        }

        let parts = s.split('/');
        for (let i = parts.length - 1; i >= 1; i--) {
            if (CP_ENDING_IGNORE.has(parts[i])) parts.splice(i, 1);
        }
        if (parts.length < 1 || parts.length > 3) return false;

        let s1 = parts[0];
        let s2 = parts.length > 1 ? parts[1] : '';
        let s3 = parts.length > 2 ? parts[2] : '';
        if (s3.length > 1) return false;

        if (s1.startsWith('HK') && s2 === '0M') return s1 + '/' + s2;
        if (s1.startsWith('FR') && s2 === 'G') return s1 + '/' + s2;

        if (s2.length === 1 && CP_DIGIT_SET.has(s2)) {
            if (s3 !== '' && CP_DIGIT_SET.has(s3)) return false;
            if (s1.length > 1 && CP_DIGIT_SET.has(s1[1]) && 'IKNWR'.includes(s1[0])) {
                s1 = s1[0];
            } else if (s1.length > 2 && CP_DIGIT_SET.has(s1[2])) {
                s1 = s1.substring(0, 2);
            } else if (s1.length > 3 && CP_DIGIT_SET.has(s1[3])) {
                s1 = s1.substring(0, 3);
            } else {
                return false;
            }
            s1 = s1 + s2;
            s2 = '';
        }

        if (s1.length === 1 && CP_ONE_CHAR_PREFIXES.has(s1)) {
            s1 = s1 + '0';
        } else if (s2.length === 1 && s3.length === 1 && CP_ONE_CHAR_PREFIXES.has(s1[0])) {
            s2 = s2 + '0';
        } else if (s2.length === 1 && CP_SAFE_ONE_CHAR_PREFIXES.has(s2)) {
            s2 = s2 + '0';
        }

        let body;
        if (s2.length > 1 && s2.length < s1.length) {
            body = s2;
        } else {
            body = s1;
        }
        if (body.length < 2) return false;

        let ending = '';
        if (s2.length === 1) {
            if (s3 !== '') return false;
            ending = s2;
        } else {
            ending = s3;
        }

        return ending ? body + '/' + ending : body;
    }

    _resolveCall() {
        if (this._call.startsWith('ADIF')) {
            const adif = parseInt(this._call.substring(4), 10);
            if (adif) {
                const item = this._getAdifItem(adif);
                if (item) { this.hitList = [item]; return; }
            }
        }

        const c1 = CP_CHARS.indexOf(this._call[0]);
        const c2 = CP_CHARS.indexOf(this._call[1]);
        if (c1 < 0 || c2 < 0) return;

        const arr = this.prefixList.index[c1][c2];
        for (const entry of arr) {
            if (this._tryMask(entry, true)) {
                const hit = this._addHit(entry, -1);
                this._addSubHits(entry, hit.id);
            }
        }
        this._packHits();
    }

    _addSubHits(parentEntry, parentId) {
        for (const childIdx of parentEntry.children) {
            const childEntry = this.prefixList.entries[childIdx];
            if (this._tryMask(childEntry, false)) {
                const hit = this._addHit(childEntry, parentId);
                this._addSubHits(childEntry, hit.id);
            }
        }
    }

    _addHit(entry, parentId) {
        const hit = {
            data: { ...entry.data, attributes: [...entry.data.attributes] },
            kind: entry.kind, id: this._hitTree.length,
            parent: parentId, children: []
        };
        this._hitTree.push(hit);
        if (parentId >= 0) this._hitTree[parentId].children.push(hit.id);
        return hit;
    }

    _tryMask(entry, topLevel) {
        const allowed = topLevel ? CP_ALLOWED_FOR_TOP : CP_ALLOWED_FOR_SUB;
        if (!allowed.has(entry.kind)) return false;
        const masks = cpSplitComma(entry.mask);
        for (const mask of masks) {
            const pm = this._comparePrefix(mask);
            const em = this._compareEnding(mask);
            const table = topLevel ? CP_RESULT_FOR_TOP : CP_RESULT_FOR_CHILD;
            if (table[pm][em]) return true;
        }
        return false;
    }

    _comparePrefix(mask) {
        let call = this._call;
        let pp = call.indexOf('/');
        let pm = cpFindSlash(mask);
        if (pp >= 0) call = call.substring(0, pp);
        if (pm >= 0) mask = mask.substring(0, pm);
        if (!mask || !call) return CPPrefixMatch.pfNE;

        for (let p = 0; p < call.length; p++) {
            if (!mask) return CPPrefixMatch.pfGE;
            const chopped = this.prefixList.chop(mask);
            mask = chopped.rest;
            if (chopped.chars.indexOf(call[p]) < 0) return CPPrefixMatch.pfNE;
        }

        if (!mask || mask === '.') return CPPrefixMatch.pfGE;
        if (mask[mask.length - 1] === '.') {
            if (!CP_DIGIT_SET.has(call[call.length - 1])) return CPPrefixMatch.pfNE;
        }
        return CPPrefixMatch.pfLT;
    }

    _compareEnding(mask) {
        const pp = this._call.indexOf('/');
        const pm = cpFindSlash(mask);
        if (pp < 0 && pm >= 0) return CPEndingMatch.edM;
        if (pp >= 0 && pm < 0) {
            if (pp === this._call.length - 2 && 'MP'.includes(this._call[pp + 1]))
                return CPEndingMatch.edEQ;
            return CPEndingMatch.edP;
        }
        if (pp < 0 && pm < 0) return CPEndingMatch.edEQ;
        if (this._call.substring(pp) === mask.substring(pm)) return CPEndingMatch.edEQ;
        return CPEndingMatch.edNE;
    }

    _packHits() {
        for (let i = this._hitTree.length - 1; i >= 0; i--) {
            const hit = this._hitTree[i];
            if (hit.data.locationX === null) {
                hit.id = -1;
                if (hit.parent >= 0)
                    this._hitTree[hit.parent].data.attributes.push(hit.data.territory);
            }
        }
        for (let i = this._hitTree.length - 1; i >= 0; i--) {
            if (this._hitTree[i].id <= -1) continue;
            const dst = cpMakePrefixData();
            this._mergePrefixData(dst, this._hitTree[i]);
            this.hitList.push(dst);
        }
        this._hitTree = [];
    }

    _mergePrefixData(dst, src) {
        src.id = -1;
        switch (src.kind) {
            case CPPrefixKind.pfDXCC:
            case CPPrefixKind.pfNonDXCC:
                dst.territory = src.data.territory; break;
            case CPPrefixKind.pfProvince:
                dst.province = dst.province ? src.data.territory + ', ' + dst.province : src.data.territory; break;
            case CPPrefixKind.pfCity:
                dst.city = src.data.territory; break;
            case CPPrefixKind.pfStation:
                if (src.data.locationX !== null) dst.city = src.data.territory; break;
        }
        if (dst.locationX === null) {
            dst.locationX = src.data.locationX;
            dst.locationY = src.data.locationY;
        }
        if (src.data.locationX !== null) {
            if (!dst.prefix) dst.prefix = src.data.prefix;
            if (!dst.cq) dst.cq = src.data.cq;
            if (!dst.itu) dst.itu = src.data.itu;
            if (!dst.continent) dst.continent = src.data.continent;
            if (!dst.tz) dst.tz = src.data.tz;
            if (!dst.adif) dst.adif = src.data.adif;
            if (!dst.provinceCode) dst.provinceCode = src.data.provinceCode;
        }
        dst.attributes = dst.attributes.concat(src.data.attributes);
        if (src.parent >= 0) this._mergePrefixData(dst, this._hitTree[src.parent]);
    }

    _getAdifItem(adif) {
        const target = String(adif);
        for (let i = 0; i < this.prefixList.count; i++) {
            const e = this.prefixList.entries[i];
            if (e.kind === CPPrefixKind.pfDXCC && e.data.adif === target)
                return { ...e.data, attributes: [...e.data.attributes] };
        }
        return null;
    }
}

// ── Module API ───────────────────────────────────────────────────────────────

let parser = null;
let loaded = false;
let initPromise = null;

// Fetch & parse the prefix database. Idempotent: concurrent and repeat
// callers share the same load.
export function init(prefixUrl = '/Prefix.lst') {
    if (!initPromise) {
        initPromise = (async () => {
            const p = new CPCallParser();
            await p.loadPrefixFile(prefixUrl);
            parser = p;
            loaded = true;
        })().catch((err) => {
            initPromise = null; // allow a retry after a failed fetch
            throw err;
        });
    }
    return initPromise;
}

export function isLoaded() {
    return loaded;
}

// Look up a callsign. Returns the best (first) PrefixData result or null.
export function lookup(call) {
    if (!loaded || !call) return null;
    const results = parser.setCall(call.toUpperCase().trim());
    return results.length > 0 ? results[0] : null;
}

// Look up a callsign. Returns all PrefixData results (array).
export function lookupAll(call) {
    if (!loaded || !call) return [];
    return parser.setCall(call.toUpperCase().trim());
}

// Haversine distance in miles from a reference point (degrees) to a
// PrefixData location, or null when the entity has no coordinates.
export function distanceMiles(prefixData, refLat, refLon) {
    if (!prefixData || prefixData.locationX === null || prefixData.locationY === null) return null;

    // Prefix.lst stores coords as degrees × 180
    const lat2 = prefixData.locationY / 180;
    const lon2 = prefixData.locationX / 180;

    const R = 3958.8; // Earth radius in miles
    const dLat = (lat2 - refLat) * Math.PI / 180;
    const dLon = (lon2 - refLon) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(refLat * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
              Math.sin(dLon / 2) ** 2;
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    return Math.round(R * c);
}
