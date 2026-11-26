#!/usr/bin/env python3
"""Mirror gate (s51, 2026-11-23): the same rule as the site's G9 (scripts/check-old-site.mjs, s49), applied to everything the
GitHub mirror publishes — README, Pages (index + category pages), feed, sitemap, CITATION, datapackage, the copied dataset
files, the ops/ code — and to the gist / Hugging Face / release-asset files the other scripts generate.

Decision: docs/workflow/decisions/2026-11-23-no-links-to-old-site.md. The only target domain is kelintextile.design.

Rules (each hit = failure):
  R1  no web address on the company's old domain (written below only as a regular expression) — with or without scheme,
      www. or a path, any case, also hidden behind HTML entities, %XX, \\uXXXX / \\xXX escapes, zero-width characters, unicode
      dots / hyphens or full-width letters. The ONLY exception: an email address on exactly that domain (the live inquiry
      mailbox), not one glued to a path / port and not the userinfo part of a URL (`//x@…`). Same normalisation and the same
      email rule as check-old-site.mjs (ported 1:1; s51 test runs both on the same samples). Review 30- #6: percent-encoded
      UTF-8 is decoded the way a URL parser decodes a host before IDNA mapping (`%E3%80%82` = U+3002, `%EF%BC%8E` = U+FF0E,
      `%C2%AD` = soft hyphen), then the IDNA / UTS46 folds below apply (ideographic and full-width dots → ".", full-width
      letters → ASCII, ignored characters removed, case-insensitive).
  R2  no mention at all of the related company's domain (QL Textiles) — no email exception (the company never used one there).
  R3  the company's Chinese name is 南通柯林纺织品有限公司 / 柯林纺织 (site/src/data/i18n/zh.json company.name); the s20 README
      mis-spelling (U+79D1 U+7433, kept out of this file's text so the gate can scan its own code) is a failure.
stdlib only; also runs inside the mirror checkout (ops/mirror_check.py), where build.py loads it by path (python3 -I safe).
Usage: python3 -I mirror_check.py <dir|file> [...]     Exit 0 clean · 1 hits (listed as file:line) · 2 path missing
       python3 -I mirror_check.py validate-pair <dataset.json> <dataset.csv>   (exit 1 = inconsistent)
       python3 -I mirror_check.py install <built_dir> <checkout>                (managed files only, ops/managed-files.json)
       python3 -I mirror_check.py check-origin <git remote url>                 (exit 1 = not the pinned repository)
       python3 -I mirror_check.py config <mirror-config.json> <key[.key]>       (validated config value)
"""
import json, os, re, sys, unicodedata

OLD = r'kelin-textile\.com'                 # R1 (email exception)
RELATED = r'qltextiles\.com'                # R2 (no exception)
WRONG_ZH = chr(0x79D1) + chr(0x7433)        # R3 (the s20 mis-spelling of 柯林)
ZH_LEGAL = '南通柯林纺织品有限公司'           # = site/src/data/i18n/zh.json company.name (asserted by the s51 test)
ZH_SHORT = '柯林纺织'
SITE_RE = re.compile(r'https?://kelintextile\.design[^\s"\'<>)\]|,]*', re.I)

BINARY_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif', '.ico', '.woff', '.woff2', '.ttf', '.otf', '.eot', '.pdf',
              '.mp4', '.webm', '.mp3', '.zip', '.gz', '.br', '.xlsx', '.docx', '.parquet'}
NAMED = {'amp': '&', 'hyphen': '-', 'dash': '-', 'period': '.', 'commat': '@', 'sol': '/', 'colon': ':', 'lowbar': '_', 'num': '#',
         'quest': '?', 'apos': "'", 'quot': '"', 'lt': '<', 'gt': '>', 'nbsp': ' ', 'shy': '', 'zwj': '', 'zwnj': ''}
HYPHENS = re.compile('[‐-―−﹘﹣－]')
DOTS = re.compile('[。．｡․]')
# 33- d: also the astral default-ignorable / UTS46-ignored code points — variation selectors supplement and tags
# (U+E0000–E0FFF, e.g. U+E0100), shorthand format controls (U+1BCA0–1BCA3), musical format controls (U+1D173–1D17A)
INVISIBLE = re.compile('[\u0000\t\r\n­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮'
                       '⁠-⁯ㅤ︀-️﻿ﾠ\U000E0000-\U000E0FFF\U0001BCA0-\U0001BCA3\U0001D173-\U0001D17A]')
TOKENS = [
    (re.compile(r'&#(\d{1,7});?'), lambda m: chr(min(int(m.group(1)), 0x10FFFF))),
    (re.compile(r'&#[xX]([0-9a-fA-F]{1,6});?'), lambda m: chr(min(int(m.group(1), 16), 0x10FFFF))),
    (re.compile(r'&([a-zA-Z]{2,8});'), lambda m: NAMED.get(m.group(1))),
    # review 30- #6: a percent-encoded UTF-8 sequence (2–4 bytes) → its character, as a URL parser percent-decodes a host
    # and UTF-8-decodes it before IDNA mapping; an invalid sequence is left as is
    (re.compile(r'(?:%[cdCD][0-9a-fA-F]|%[eE][0-9a-fA-F]%[89abAB][0-9a-fA-F]|%[fF][0-7]%[89abAB][0-9a-fA-F]%[89abAB][0-9a-fA-F])%[89abAB][0-9a-fA-F]'),
     lambda m: _utf8(m.group(0))),
    (re.compile(r'%([0-9a-fA-F]{2})'), lambda m: chr(int(m.group(1), 16)) if int(m.group(1), 16) < 0x80 else None),
    (re.compile(r'\\u([0-9a-fA-F]{4})'), lambda m: chr(int(m.group(1), 16))),
    (re.compile(r'\\x([0-9a-fA-F]{2})'), lambda m: chr(int(m.group(1), 16))),
]


def _utf8(pct):
    try: return bytes.fromhex(pct.replace('%', '')).decode('utf-8')
    except UnicodeDecodeError: return None


def _pass(text, base):
    """One decoding pass (as decodePass in check-old-site.mjs): returns (s, map) with map[i] = offset in the original."""
    out, mp, i = [], [], 0
    while i < len(text):
        rest, adv, rep = text[i:i + 12], 0, None
        for rx, fn in TOKENS:
            m = rx.match(rest)
            if m:
                r = fn(m)
                if r is not None: adv, rep = len(m.group(0)), r; break
        if rep is None: rep, adv = text[i], 1
        if rep and not INVISIBLE.fullmatch(rep):          # '' (named &shy; …) and invisible characters are dropped
            if HYPHENS.fullmatch(rep): rep = '-'
            elif DOTS.fullmatch(rep): rep = '.'
            for ch in unicodedata.normalize('NFKC', rep):
                out.append(ch); mp.append(base[i] if base else i)
        i += adv
    return ''.join(out), mp


def normalise(text):
    s, mp = str(text), None
    for _ in range(4):
        n, nm = _pass(s, mp)
        if n == s: break
        s, mp = n, nm
    return s, (mp if mp is not None else list(range(len(s))))


_EMAIL = re.compile(r'(?<![\w.+%-])(?<!//)[\w.+-]+@' + OLD + r'(?![\w-]|\.[\w-]|[/\\]|:\d)', re.I | re.A)
_USERINFO = re.compile(r'//[^\s/"\'<>?#]*@(?:[\w-]+\.)*' + OLD, re.I | re.A)
_OLD = re.compile(OLD, re.I)
_RELATED = re.compile(RELATED, re.I)


def hits(text):
    """[(line, rule, snippet)] for one text (R1–R3)."""
    raw = str(text)
    s, mp = normalise(raw)
    found, seen = [], set()

    def add(idx, ln, rule):
        o = mp[idx] if idx < len(mp) else 0
        if (o, rule) in seen: return
        seen.add((o, rule))
        ls = raw.rfind('\n', 0, o) + 1
        found.append((raw.count('\n', 0, o) + 1, rule, re.sub(r'\s+', ' ', raw[max(ls, o - 40):o + ln + 40])))
    for m in _USERINFO.finditer(s): add(m.start(), len(m.group(0)), 'R1 old-domain address')
    masked = _EMAIL.sub(lambda m: '\u0001' * len(m.group(0)), s)
    for m in _OLD.finditer(masked): add(m.start(), len(m.group(0)), 'R1 old-domain address')
    for m in _RELATED.finditer(s): add(m.start(), len(m.group(0)), 'R2 related-company domain')
    for m in re.finditer(WRONG_ZH, s): add(m.start(), len(m.group(0)), 'R3 wrong Chinese company name')
    return sorted(found)


def emails(text):
    return len(_EMAIL.findall(normalise(text)[0]))


def walk(path, skip=('.git',)):
    if os.path.isfile(path): return [path]
    out = []
    for dp, dns, fns in os.walk(path):
        dns[:] = sorted(d for d in dns if d not in skip)
        out += [os.path.join(dp, f) for f in sorted(fns) if os.path.splitext(f)[1].lower() not in BINARY_EXT]
    return out


def check(paths, skip=('.git',)):
    """{'files': n, 'failures': [...], 'emails': n, 'site_links': {relpath: count}}"""
    res = {'files': 0, 'failures': [], 'emails': 0, 'site_links': {}}
    for p in paths:
        for f in walk(p, skip):
            b = open(f, 'rb').read()
            if b'\x00' in b[:4096]: continue
            t = b.decode('utf-8', 'replace'); res['files'] += 1
            rel = os.path.relpath(f, p) if os.path.isdir(p) else os.path.basename(f)
            res['emails'] += emails(t)
            n = len(SITE_RE.findall(t))
            if n: res['site_links'][rel] = n
            for ln, rule, snip in hits(t): res['failures'].append(f'{rel}:{ln}: {rule}: …{snip}…')
    return res


def report(res, label='mirror check (G9 rule, s51)'):
    if res['failures']:
        print(f"✗ {label}: {len(res['failures'])} hit(s) in {res['files']} text files")
        print('\n'.join('  ' + x for x in res['failures'][:200]) + (f"\n  … {len(res['failures']) - 200} more" if len(res['failures']) > 200 else ''))
        return 1
    print(f"✓ {label}: {res['files']} text files, 0 old-domain / related-domain addresses, 0 wrong Chinese name; "
          f"{res['emails']} email mention(s) on the old domain kept; {sum(res['site_links'].values())} kelintextile.design addresses")
    return 0


# ---------------------------------------------------------------- shared: licence settings (build.py, hf-sync.py, zenodo.py)
LICENSES = {
    'CC-BY-4.0': {'title': 'Creative Commons Attribution 4.0 International', 'short': 'CC BY 4.0',
                  'url': 'https://creativecommons.org/licenses/by/4.0/', 'legalcode': 'https://creativecommons.org/licenses/by/4.0/legalcode',
                  'zenodo': 'cc-by-4.0', 'hf': 'cc-by-4.0',
                  'summary': 'You may share and adapt the data for any purpose, including commercially, provided you give appropriate credit, '
                             'provide a link to the licence and indicate if changes were made.'},
    'CC0-1.0': {'title': 'Creative Commons Zero v1.0 Universal (CC0 1.0 Public Domain Dedication)', 'short': 'CC0 1.0',
                'url': 'https://creativecommons.org/publicdomain/zero/1.0/', 'legalcode': 'https://creativecommons.org/publicdomain/zero/1.0/legalcode',
                'zenodo': 'cc0-1.0', 'hf': 'cc0-1.0',
                'summary': 'The data is dedicated to the public domain: you may copy, modify and distribute it for any purpose without asking permission.'},
}


# ---------------------------------------------------------------- shared: pinned targets (review 30- #1 / #5)
# The only repository / branch / gist these scripts may write to. mirror-config.json repeats them; a config that names
# anything else is refused (double lock: an edited config alone cannot redirect a run).
# s58 (2026-11-26): the one Hugging Face dataset repository hf-sync.py may write to (huggingface.repo_id: null or exactly this).
PINNED = {'repo': 'agenthaulk/kelin-hotel-linen-catalog', 'branch': 'main',
          'gist': {'owner': 'agenthaulk', 'id': '99f5d821ccf302604bc4e7af035b9ebd'},
          'huggingface': 'kelintextile/hotel-linen-catalog'}
ORIGIN_RE = re.compile(r'^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)'
                       + re.escape(PINNED['repo']) + r'(?:\.git)?/?$')


def load_config(path):
    cfg = {'data_license': None, 'zenodo': {}, 'huggingface': {}, 'repo': PINNED['repo'], 'branch': PINNED['branch'],
           'gist': dict(PINNED['gist']), 'wayback': {}}
    if path and os.path.exists(path):
        cfg.update({k: v for k, v in json.load(open(path, encoding='utf-8')).items() if not k.startswith('_')})
    for k in ('repo', 'branch'):
        if cfg.get(k) != PINNED[k]: sys.exit(f"✗ {path}: {k} {cfg.get(k)!r} is not the pinned {PINNED[k]!r}")
    g = cfg.get('gist') or {}
    if (g.get('owner'), g.get('id')) != (PINNED['gist']['owner'], PINNED['gist']['id']):
        sys.exit(f"✗ {path}: gist {g!r} is not the pinned {PINNED['gist']!r}")
    h = cfg.get('huggingface') or {}
    if not isinstance(h, dict): sys.exit(f'✗ {path}: huggingface must be an object')
    if h.get('repo_id') not in (None, PINNED['huggingface']):
        sys.exit(f"✗ {path}: huggingface.repo_id {h.get('repo_id')!r} is not the pinned {PINNED['huggingface']!r}")
    if 'publish_without_licence' in h and not isinstance(h['publish_without_licence'], bool):
        sys.exit(f'✗ {path}: huggingface.publish_without_licence must be true or false')
    return cfg


def check_repo(repo):
    """The pinned repository or exit 64."""
    if repo != PINNED['repo']: sys.exit(f'✗ repository {repo!r} is not the pinned {PINNED["repo"]!r}')
    return repo


def check_origin(url):
    return bool(ORIGIN_RE.match((url or '').strip()))


# ---------------------------------------------------------------- shared: existence checks that decide a write
def gh_exists(api_path):
    """True / False for `gh api <api_path>` — False ONLY for a real "Not Found (HTTP 404)"; any other failure (network,
    auth, rate limit, 5xx) exits 1, so a failed read is never taken as "does not exist" and never leads to a create."""
    import subprocess
    r = subprocess.run(['gh', 'api', api_path, '--silent'], capture_output=True, text=True)
    if r.returncode == 0: return True
    if '(HTTP 404)' in (r.stderr or ''): return False
    sys.exit(f'✗ cannot tell whether {api_path} exists — gh failed: {(r.stderr or "").strip()[:200]} (stopping; nothing was changed)')


# ---------------------------------------------------------------- shared: licence (review 30- #7)
def resolve_license(cli, cfg, strict=False):
    """Licence id or None. strict (any --execute path, the Zenodo checks): mirror-config.json only — a --license or
    $DATA_LICENSE override is refused. Dry runs may override (trial builds)."""
    env = os.environ.get('DATA_LICENSE') or None
    if strict:
        if cli: sys.exit('✗ refused: --license is a dry-run override; an execute uses mirror-config.json data_license only')
        if env and env != cfg.get('data_license'): sys.exit(f'✗ refused: $DATA_LICENSE={env!r} differs from mirror-config.json ({cfg.get("data_license")!r}); unset it for an execute')
        lic = cfg.get('data_license') or None
    else:
        lic = cli or env or cfg.get('data_license') or None
    if lic and lic not in LICENSES: sys.exit(f'✗ unknown licence {lic!r}: allowed {sorted(LICENSES)} (or unset)')
    return lic


def license_problem(d, lic_id):
    """'' when the dataset's own `license` agrees with the licence the mirror states; else the problem (review 30- #7)."""
    have = (d.get('license') or {}).get('id') if isinstance(d.get('license'), dict) else None
    if lic_id and have != lic_id:
        return f'dataset license {have!r} ≠ mirror licence {lic_id!r}'
    if have and not lic_id:
        return f'dataset states license {have!r} but the mirror licence is unset'
    if have and (d['license'].get('url') != LICENSES[have]['url']):
        return f"dataset license url {d['license'].get('url')!r} ≠ {LICENSES[have]['url']!r}"
    return ''


# ---------------------------------------------------------------- shared: dataset file pair (review 30- #2)
CSV_HEADER = ['category', 'series', 'series_url', 'model', 'attribute', 'value', 'also_listed_as', 'source',
              'series_name_zh', 'category_name_zh', 'hs_codes', 'cn_tariff_codes']


def _nn(*xs):
    """JS `a ?? b ?? c`: the first value that is not None (an empty string counts, as in site/src/lib/dataset.ts)."""
    for x in xs:
        if x is not None: return x
    return None


def expected_csv_rows(d):
    """The CSV rows site/src/lib/dataset.ts buildDatasetCsv() writes for this JSON (same order)."""
    rows = []
    for c in d['categories']:
        for s in c['series']:
            for m in s['models']:
                for col in s['columns']:
                    v = (m.get('values') or {}).get(col)
                    if v:
                        rows.append([c['name'], s['name'], s['url'], m['code'], col, v, _nn((m.get('also_listed_as') or {}).get(col), ''),
                                     _nn((m.get('field_sources') or {}).get(col), m.get('source'), s['source']),
                                     s['name_zh'], c['name_zh'], s['hs_codes'], s['cn_tariff_codes']])
    return rows


def validate_pair(json_p, csv_p):
    """[] when the JSON and the CSV are one consistent dataset: JSON parses, has categories, its `version` is the content
    hash of its categories (as dataset.ts computes it), and the CSV has exactly the expected header and rows."""
    import csv as _csv, hashlib
    probs = []
    try: d = json.load(open(json_p, encoding='utf-8'))
    except Exception as ex: return [f'JSON unreadable: {str(ex)[:120]}']
    if not d.get('categories') or not re.fullmatch(r'[0-9a-f]{12}', str(d.get('version', ''))): return ['JSON has no categories or no 12-hex version']
    h = hashlib.sha256(json.dumps({'categories': d['categories']}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()[:12]
    if h != d['version']: probs.append(f"JSON version {d['version']} ≠ content hash {h} (file altered or truncated)")
    try: rows = list(_csv.reader(open(csv_p, encoding='utf-8', newline='')))
    except Exception as ex: return probs + [f'CSV unreadable: {str(ex)[:120]}']
    if not rows or rows[0] != CSV_HEADER: probs.append(f'CSV header {rows[0] if rows else None} ≠ {CSV_HEADER}'); return probs
    exp = expected_csv_rows(d)
    if len(rows) - 1 != len(exp): probs.append(f'CSV has {len(rows) - 1} rows, the JSON gives {len(exp)}')
    for i, (a, b) in enumerate(zip(rows[1:], exp)):
        if a != b: probs.append(f'CSV row {i + 2} ≠ JSON: {a[:6]} vs {b[:6]}'); break
    return probs


def sha256_file(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 16), b''): h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- shared: managed files (review 30- #1)
MANIFEST = 'ops/managed-files.json'
NEVER_MANAGED = ('.git', 'ops', '.github')


def _inside(dest, rel):
    """Absolute path of `rel` inside `dest`, or exit: rel must be relative, without '..', and its real path (symlinks
    resolved, also for parents that exist) must stay inside the real path of dest; the file itself may not be a symlink."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel) or rel.startswith(('/', '\\')) or '\\' in rel \
            or any(part in ('', '.', '..') for part in rel.split('/')):
        sys.exit(f'✗ refused: managed path {rel!r} is not a plain relative path inside the checkout')
    root = os.path.realpath(dest)
    q = os.path.join(root, *rel.split('/'))
    if os.path.commonpath([root, os.path.realpath(q)]) != root or os.path.islink(q):
        sys.exit(f'✗ refused: {rel!r} resolves outside the checkout or is a symlink')
    return q


def install_managed(built, dest, manifest_rel=MANIFEST):
    """Replace only the files the mirror build manages (review 30- #1, 33- a): every file of `built` is copied into `dest`;
    files listed in the previous manifest that the build no longer produces are removed; nothing else is touched (.git,
    ops/, .github/, any file the build never wrote). Every path — manifest entries and built files — must resolve inside
    the real checkout (no absolute paths, no '..', no symlink out of it), else the install stops before changing anything.
    Without a manifest (the first run) NOTHING is deleted: files the build does not produce are only reported (`left`).
    Returns (written, removed, left)."""
    import shutil
    if os.path.realpath(built) == os.path.realpath(dest): sys.exit('✗ refused: build directory = checkout')
    new = []
    for dp, dns, fns in os.walk(built):
        dns.sort()
        if any(os.path.islink(os.path.join(dp, x)) for x in dns): sys.exit(f'✗ refused: symlinked directory in the build ({dp})')
        for f in sorted(fns):
            if os.path.islink(os.path.join(dp, f)): sys.exit(f'✗ refused: symlink in the build ({os.path.join(dp, f)})')
            rel = os.path.relpath(os.path.join(dp, f), built).replace(os.sep, '/')
            if rel.split('/')[0] in NEVER_MANAGED: sys.exit(f'✗ build wrote {rel} inside a never-managed directory')
            new.append(rel)
    mp = _inside(dest, manifest_rel)
    prev = json.load(open(mp, encoding='utf-8'))['files'] if os.path.exists(mp) else None
    # validate EVERYTHING before the first change
    targets = {rel: _inside(dest, rel) for rel in new}
    stale = {}
    for rel in (prev or []):
        if rel in targets: continue
        q = _inside(dest, rel)
        if rel.split('/')[0] in NEVER_MANAGED: sys.exit(f'✗ refused: manifest lists {rel!r} inside a never-managed directory')
        stale[rel] = q
    left = []
    if prev is None:                                              # first run: report, never delete
        for dp, dns, fns in os.walk(dest):
            dns[:] = sorted(x for x in dns if not (dp == dest and x in NEVER_MANAGED))
            for f in sorted(fns):
                rel = os.path.relpath(os.path.join(dp, f), dest).replace(os.sep, '/')
                if rel not in targets: left.append(rel)
    removed = []
    for rel, q in sorted(stale.items()):
        if os.path.isfile(q): os.remove(q); removed.append(rel)
    root = os.path.realpath(dest)
    for rel in removed:                                           # empty directories left by removed files (inside dest only)
        p = os.path.dirname(os.path.join(root, *rel.split('/')))
        while p != root and os.path.commonpath([root, p]) == root and os.path.isdir(p) and not os.listdir(p):
            os.rmdir(p); p = os.path.dirname(p)
    for rel in new:
        q = targets[rel]; os.makedirs(os.path.dirname(q), exist_ok=True)
        _inside(dest, rel)                                        # parents now exist: re-check against symlinks
        shutil.copyfile(os.path.join(built, rel), q)
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    json.dump({'_doc': 'Files the mirror build manages (written by ops/mirror_check.py install); anything else is left alone.',
               'files': new}, open(mp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    open(mp, 'a', encoding='utf-8').write('\n')
    return new, removed, left


if __name__ == '__main__':
    ps = sys.argv[1:]
    if not ps: sys.exit(__doc__)
    if ps[0] == 'validate-pair' and len(ps) == 3:            # ops_run.sh / sync: one verified dataset file set
        pr = validate_pair(ps[1], ps[2])
        print(('✗ dataset files inconsistent: ' + '; '.join(pr)) if pr else f'✓ dataset files consistent (JSON version = content hash, CSV = JSON rows)')
        sys.exit(1 if pr else 0)
    if ps[0] == 'install' and len(ps) == 3:                  # ops_run.sh: copy an isolated build into the checkout
        w, r, l = install_managed(ps[1], ps[2])
        print(f'installed {len(w)} managed file(s); removed {len(r)} no longer built' + (f': {r[:10]}' if r else '')
              + (f'; first install: {len(l)} other file(s) left untouched (not deleted): {l[:10]}' if l else ''))
        sys.exit(0)
    if ps[0] == 'gh-exists' and len(ps) == 2:                # yes / no; exit 1 on anything but a real 404
        print('yes' if gh_exists(ps[1]) else 'no'); sys.exit(0)
    if ps[0] == 'check-origin' and len(ps) == 2:   # a configured or effective push URL
        sys.exit(0 if check_origin(ps[1]) else 1)
    if ps[0] == 'config' and len(ps) == 3:                   # print one pinned / configured value (validated)
        c = load_config(ps[1]); v = c
        for k in ps[2].split('.'): v = (v or {}).get(k)
        print('' if v is None else (json.dumps(v) if isinstance(v, (dict, list, bool)) else v)); sys.exit(0)
    for p in ps:
        if not os.path.exists(p): print(f'✗ {p} does not exist'); sys.exit(2)
    sys.exit(report(check(ps)))
