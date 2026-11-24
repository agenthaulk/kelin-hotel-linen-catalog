#!/usr/bin/env python3
"""Backlink operations for Kelin's catalogue mirror (s20-2, 2026-10-30; s51, 2026-11-23). stdlib + the `gh` CLI (GH_TOKEN in
Actions, keyring locally). Every command is idempotent and derives its state from the platform itself (existing discussions,
Wayback's CDX index, the gist list), so the weekly job and a local run can overlap without double posts. Nothing is posted
anywhere the company has not placed itself: only our own repository's Discussions, the Internet Archive and our own gist.

s51: every command is a DRY RUN unless --execute is given (it then prints what it would post / save and changes nothing;
read-only GETs still happen). ops_run.sh passes --execute only in its own --execute mode (the weekly job).

  announce  <owner/repo> <dataset.json> <prev.json|-> <release_url> [--execute]
        One Discussion in "Announcements" per dataset version ("Dataset <version>"), body = what changed vs the previous
        release (series/models added, removed, changed values). Skipped when that title exists.
  spotlight <owner/repo> <dataset.json> [--min-days N] [--execute]        (default 13)
        One "Series spotlight: <series> — <category>" Discussion in "Show and tell": the series summary, applications,
        model table and source, with links to the series page (?ref=gh-disc). Picks the series covered least / longest ago;
        skipped while the newest spotlight is younger than N days.
  wayback   <dataset.json> <checkpoint.jsonl> [--extra-log FILE ...] [--max N] [--per-minute R] [--retries K] [--budget-min M]
            [--plan-out FILE] [--min-age-days D] [--execute]
        Save Page Now (anonymous, no account) for every page in the live sitemap (sitemap-index.xml -> child sitemaps) plus
        the machine layer (/llms.txt, /llms-full.txt, /robots.txt, /sitemap-index.xml, the child sitemaps, /data/, the
        dataset JSON + CSV). Plan from ONE domain-level CDX query: a URL is due when it has no 200 snapshot ("never") or its
        newest snapshot is older than its sitemap lastmod (machine files: Last-Modified header, else the newest lastmod) -
        i.e. it changed in a deploy ("stale"). Never-archived first. Resume = the plan itself + our own log (a save newer
        than the reference time counts, because the CDX index lags behind a save). Defaults: --max 10, --per-minute 10
        (at most one request start every 6 s; SPN itself takes ~1 min per page), --retries 2 (backoff 20 s / 40 s on 5xx
        or network errors), --budget-min 0 (no limit), --min-age-days 14 (a changed page whose newest snapshot is younger
        than this waits — "recent" — so routine deploys do not re-save the whole site; never-archived pages are always
        due). HTTP 429 or a login wall -> stop at once, exit 3. One JSON line per attempt.
        Review 30- (2026-11-23): <checkpoint.jsonl> is the ONE shared log (the mirror's ops/wayback.jsonl — the weekly job
        and scripts/wayback-after-deploy.sh both read and append to it; --extra-log adds read-only older logs). A save
        counts only when the snapshot is a /web/<ts>/<same address> capture made for this request and it opens without
        a login or a redirect to another address. result = ok | retryable (network, 5xx, 408, no snapshot yet) |
        permanent (other 4xx, redirected away; held 30 days unless the page changes) | rate-limited. Inside each group
        URLs that failed recently go last (rotation). Exit 0 = nothing due any more · 4 = addresses still due ·
        1 = failures and no save · 3 = rate-limited / login wall.
  checkpoint-merge <into.jsonl> <from.jsonl>
        Union of two checkpoint files (used when a push of the checkpoint races another run).
  wayback-coverage <dataset.json> [--log FILE] [--py PYTHON] [--execute]
        Coverage (same plan, no save): totals + the trackers.py commands that record it on BL-148 (and the managed row
        "wayback"). With --execute and --py (the openpyxl venv python) it runs them; otherwise it prints them.
  gist      <dataset.json> <dataset.csv> [--out DIR] [--config FILE] [--execute]
        Update the single public "quick look" gist (README.md with category links tagged ?ref=gh-gist + CSV). Review 30- #5:
        only the gist pinned in mirror-config.json (owner agenthaulk, id 99f5d821…); it must exist, belong to that owner and
        hold exactly README.md + the CSV, else the command stops (a new gist is never created). Both files change in ONE
        `PATCH /gists/<id>` (one revision), read back and compared afterwards. The dataset pair is validated first.
        Dry run: writes the new files and the PATCH body to --out, prints old/new counts and the one command. Needs the
        gist scope: run locally, not in Actions.
Exit 0 ok / skipped / dry run · 1 failed · 3 Wayback rate-limited (429).
"""
import datetime, importlib.util, json, os, re, shlex, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True        # no __pycache__ in the mirror checkout (it would be committed)
_spec = importlib.util.spec_from_file_location('mirror_check', os.path.join(HERE, 'mirror_check.py'))
mc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(mc)
EXECUTE = '--execute' in sys.argv

UA = 'Mozilla/5.0 (compatible; kelin-catalogue-ops/1.0; +https://kelintextile.design)'
UTC = datetime.timezone.utc


def gh(*args, data=None):
    r = subprocess.run(['gh', *args], input=data, capture_output=True, text=True)
    if r.returncode: sys.exit(f'✗ gh {" ".join(args)[:80]}…: {r.stderr.strip()[:400]}')
    return r.stdout


def graphql(query, **vars):
    args = ['api', 'graphql', '-f', f'query={query}']
    for k, v in vars.items(): args += ['-f' if isinstance(v, str) else '-F', f'{k}={v}']
    return json.loads(gh(*args))['data']


def load(p):
    return json.load(open(p, encoding='utf-8'))


def opt(args, flag, default, cast=int):
    """Value after `flag` (round-1 should-fix: a value may not itself be an option, e.g. `--plan-out --execute`)."""
    if flag not in args: return default
    i = args.index(flag)
    if i + 1 >= len(args) or args[i + 1].startswith('--'): sys.exit(f'✗ {flag} needs a value')
    return cast(args[i + 1])


def tag(url, ref):
    return f"{url}{'&' if '?' in url else '?'}ref={ref}"


def repo_meta(repo):
    o, n = repo.split('/')
    q = '''query($o:String!,$n:String!){ repository(owner:$o,name:$n){ id discussionCategories(first:20){ nodes{ id slug } } } }'''
    r = graphql(q, o=o, n=n)['repository']
    cats = {c['slug']: c['id'] for c in r['discussionCategories']['nodes']}
    return r['id'], cats


def discussions(repo, category_id):
    o, n = repo.split('/')
    q = '''query($o:String!,$n:String!,$c:ID!){ repository(owner:$o,name:$n){ discussions(first:100,categoryId:$c,orderBy:{field:CREATED_AT,direction:DESC}){ nodes{ title url createdAt } } } }'''
    return graphql(q, o=o, n=n, c=category_id)['repository']['discussions']['nodes']


def create_discussion(repo_id, category_id, title, body):
    q = '''mutation($r:ID!,$c:ID!,$t:String!,$b:String!){ createDiscussion(input:{repositoryId:$r,categoryId:$c,title:$t,body:$b}){ discussion{ url } } }'''
    return graphql(q, r=repo_id, c=category_id, t=title, b=body)['createDiscussion']['discussion']['url']


def series_index(d):
    return {(c['slug'], s['slug']): (c, s) for c in d['categories'] for s in c['series']}


def model_table(s):
    cols = s['columns']
    out = ['| Model | ' + ' | '.join(cols) + ' |', '|---|' + '---|' * len(cols)]
    for m in s['models']:
        out.append('| `' + m['code'] + '` | ' + ' | '.join(str(m['values'].get(c, '')).replace('|', '\\|') for c in cols) + ' |')
    return '\n'.join(out)


def footer(d, ref):
    return (f"\n\n---\nValues as published by {d['publisher']['legalName']} ({d['publisher']['name']}); {d['terms']} "
            f"Catalogue: [{d['publisher']['website'].replace('https://', '')}/products/]({tag(d['pages'], ref)}) · "
            f"official website {tag(d['publisher']['officialWebsite'], ref)} · dataset files: [JSON]({d['url']}) · [CSV]({d['csv']}).")


# ---------------------------------------------------------------- announce
def diff(prev, cur):
    lines = []
    pi, ci = series_index(prev), series_index(cur)
    for k in ci.keys() - pi.keys():
        c, s = ci[k]; lines.append(f"- **New series:** [{s['name']}]({tag(s['url'], 'gh-disc')}) ({c['name']}, {len(s['models'])} models)")
    for k in pi.keys() - ci.keys():
        c, s = pi[k]; lines.append(f"- **Series removed:** {s['name']} ({c['name']})")
    for k in ci.keys() & pi.keys():
        c, s = ci[k]; _, ps = pi[k]
        pm = {m['code']: m['values'] for m in ps['models']}; cm = {m['code']: m['values'] for m in s['models']}
        added, removed = sorted(cm.keys() - pm.keys()), sorted(pm.keys() - cm.keys())
        changed = []
        for code in sorted(cm.keys() & pm.keys()):
            for col in sorted(set(cm[code]) | set(pm[code])):
                a, b = pm[code].get(col), cm[code].get(col)
                if a != b: changed.append(f"`{code}` {col}: {a!r} → {b!r}")
        meta = [f for f in ('name', 'summary', 'applications', 'source', 'columns') if ps.get(f) != s.get(f)]
        if added or removed or changed or meta:
            lines.append(f"- **[{s['name']}]({tag(s['url'], 'gh-disc')})** ({c['name']}):"
                         + (f" models added {', '.join('`' + a + '`' for a in added)};" if added else '')
                         + (f" models removed {', '.join('`' + r + '`' for r in removed)};" if removed else '')
                         + (f" {len(changed)} value change(s);" if changed else '')
                         + (f" text updated ({', '.join(meta)});" if meta else ''))
            lines += [f"  - {x}" for x in changed[:30]]
            if len(changed) > 30: lines.append(f"  - … and {len(changed) - 30} more")
    return lines


def announce(repo, cur_p, prev_p, release_url):
    cur = load(cur_p); ver = cur['version']; title = f"Dataset {ver}"
    repo_id, cats = repo_meta(repo)
    cat = cats.get('announcements') or sys.exit('✗ no Announcements category')
    if any(x['title'] == title for x in discussions(repo, cat)): print(f'= announcement exists: {title}'); return
    n_series = sum(len(c['series']) for c in cur['categories']); n_models = sum(len(s['models']) for c in cur['categories'] for s in c['series'])
    body = [f"Catalogue dataset version `{ver}` — {n_series} series, {n_models} model rows across {len(cur['categories'])} categories. "
            f"Release: {release_url}", '']
    if prev_p != '-' and os.path.exists(prev_p):
        prev = load(prev_p); lines = diff(prev, cur)
        body += [f"**Changes since `{prev['version']}`:**", ''] + (lines or ['- No series or model values changed (text, sources or metadata only).'])
    else:
        body += ['**First release of the dataset.** Categories: ' + ', '.join(f"[{c['name']}]({tag(c['url'], 'gh-disc')})" for c in cur['categories']) + '.']
    if not EXECUTE: print(f'(dry run) would post announcement {title!r}'); return
    url = create_discussion(repo_id, cat, title, '\n'.join(body) + footer(cur, 'gh-disc'))
    print(f'✓ announced {title}: {url}')


# ---------------------------------------------------------------- spotlight
def spotlight(repo, cur_p, min_days):
    d = load(cur_p)
    repo_id, cats = repo_meta(repo)
    cat = cats.get('show-and-tell') or sys.exit('✗ no Show and tell category')
    posts = discussions(repo, cat)
    spots = [p for p in posts if p['title'].startswith('Series spotlight: ')]
    if spots:
        newest = datetime.datetime.fromisoformat(spots[0]['createdAt'].replace('Z', '+00:00'))
        age = (datetime.datetime.now(UTC) - newest).days
        if age < min_days: print(f'= newest spotlight is {age} d old (< {min_days}); nothing posted'); return
    # least-covered series first, then catalogue order; ties broken by oldest coverage
    cover = {}
    for p in reversed(spots):
        m = re.match(r'Series spotlight: (.+) — (.+)$', p['title'])
        if m: cover.setdefault((m.group(2), m.group(1)), []).append(p['createdAt'])
    order = [(c, s) for c in d['categories'] for s in c['series']]
    def key(cs):
        c, s = cs; hits = cover.get((c['name'], s['name']), [])
        return (len(hits), hits[-1] if hits else '', order.index(cs))
    c, s = min(order, key=key)
    apps = s.get('applications') or []; apps_txt = ', '.join(apps) if isinstance(apps, list) else str(apps)
    title = f"Series spotlight: {s['name']} — {c['name']}"
    body = [f"**{s['name']}** is one of {len(c['series'])} series in [{c['name']}]({tag(c['url'], 'gh-disc')}) by {d['publisher']['legalName']} ({d['publisher']['name']}), Nantong, China.", '',
            s['summary'], '']
    if apps_txt: body += [f"**Applications (as listed by the company):** {apps_txt}", '']
    body += [f"**Models ({len(s['models'])}):**", '', model_table(s), '',
             f"Source: {s['source']}. Series page with photos, sources and the enquiry form: [{s['url'].replace('https://', '')}]({tag(s['url'], 'gh-disc')}). "
             f"Mirror page for the category: https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/{c['slug']}/"]
    if not EXECUTE: print(f'(dry run) would post spotlight {title!r}'); return
    url = create_discussion(repo_id, cat, title, '\n'.join(body) + footer(d, 'gh-disc'))
    print(f'✓ spotlight posted: {title}: {url}')


# ---------------------------------------------------------------- wayback (s51: whole site from the live sitemap)
# Review 30- #8 / #9 (2026-11-23): ONE shared checkpoint (the mirror's ops/wayback.jsonl; the laptop wrapper reads and
# appends to the same file), every save verified (the snapshot must be a fresh /web/<ts>/<same URL> capture — a login page
# or a redirect to another address is not a save), failures split into retryable (network, 5xx, 408) and permanent (other
# 4xx, redirected away), rotation (URLs that failed recently go to the back of their group; a permanent failure waits
# PERMANENT_DAYS unless the page changes again), exit 4 while due addresses remain.
CDX = 'https://web.archive.org/cdx/search/cdx'
SPN = 'https://web.archive.org/save/'
PERMANENT_DAYS = 30
LOGIN_RE = re.compile(r'archive\.org/(?:account/)?login', re.I)
SNAP_RE = re.compile(r'^https://web\.archive\.org/web/(\d{14})(?:[a-z]{2}_)?/(.+)$')


def http_get(url, timeout=60, method='GET'):
    req = urllib.request.Request(url, headers={'User-Agent': UA}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), (r.read() if method == 'GET' else b'')


def norm_url(u):
    """Comparison key: no scheme, no www., no default port, path kept (trailing slash significant), query kept."""
    p = urllib.parse.urlsplit(u.strip())
    host = (p.hostname or '').lower()
    host = host[4:] if host.startswith('www.') else host
    return host + (p.path or '/') + ('?' + p.query if p.query else '')


def ts_of(dt):
    return dt.strftime('%Y%m%d%H%M%S')


def dt_of(ts):
    return datetime.datetime.strptime(ts[:14].ljust(14, '0'), '%Y%m%d%H%M%S').replace(tzinfo=UTC)


def parse_iso(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(UTC)


def _get_or_stop(url, timeout=60, method='GET'):
    """http_get; HTTP 429 from the archive or the site stops the whole run (exit 3) — 33- c."""
    try: return http_get(url, timeout=timeout, method=method)
    except urllib.error.HTTPError as he:
        if he.code == 429: print(f'✗ HTTP 429 from {urllib.parse.urlsplit(url).hostname} — stopping (exit 3); run again later'); sys.exit(3)
        raise


def wayback_targets(d):
    """[(url, lastmod datetime|None, kind)] — sitemap pages + machine layer on the site's own host only (an off-site <loc>
    is dropped), de-duplicated, sitemap order."""
    site = d['publisher']['website'].rstrip('/')
    host = urllib.parse.urlsplit(site).hostname
    own = lambda u: urllib.parse.urlsplit(u).scheme == 'https' and urllib.parse.urlsplit(u).hostname == host
    _, _, idx = _get_or_stop(site + '/sitemap-index.xml')
    children = [c for c in re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', idx.decode('utf-8')) if own(c)]
    out, seen = [], set()
    for ch in children:
        _, _, x = _get_or_stop(ch)
        for blk in re.findall(r'<url>(.*?)</url>', x.decode('utf-8'), re.S):
            loc = re.search(r'<loc>\s*([^<\s]+)\s*</loc>', blk).group(1)
            if not own(loc): print(f'  ! off-site address in the sitemap ignored: {loc}'); continue
            lm = re.search(r'<lastmod>\s*([^<\s]+)\s*</lastmod>', blk)
            if loc not in seen: seen.add(loc); out.append((loc, parse_iso(lm.group(1)) if lm else None, 'page'))
    machine = [site + p for p in ('/llms.txt', '/llms-full.txt', '/robots.txt', '/sitemap-index.xml')] + children + [site + '/data/', d['url'], d['csv']]
    newest = max((lm for _, lm, _ in out if lm), default=None)
    for u in machine:
        if u in seen or not own(u): continue
        seen.add(u); ref = None
        for attempt in range(3):                 # its Last-Modified decides whether it is saved again: no silent fallback
            try:
                _, h, _ = _get_or_stop(u, timeout=40, method='HEAD')
                if h.get('Last-Modified'): ref = datetime.datetime.strptime(h['Last-Modified'], '%a, %d %b %Y %H:%M:%S GMT').replace(tzinfo=UTC)
                break
            except SystemExit: raise
            except Exception as ex:
                if attempt == 2: sys.exit(f'✗ HEAD {u} failed 3 times ({str(ex)[:80]}) — stopping before planning')
                time.sleep(5 * (attempt + 1))
        out.append((u, ref or newest, 'machine'))
    return out


def cdx_newest(host):
    """{norm_url: newest 200 timestamp} from ONE domain-level CDX query (read-only). 429 → exit 3 at once (no retry)."""
    q = CDX + '?' + urllib.parse.urlencode({'url': host, 'matchType': 'domain', 'output': 'json', 'fl': 'original,timestamp', 'filter': 'statuscode:200'})
    for i in range(3):
        try:
            _, _, b = _get_or_stop(q, timeout=120); rows = json.loads(b.decode('utf-8') or '[]'); break
        except SystemExit: raise
        except Exception as ex:
            if i == 2: raise SystemExit(f'✗ CDX query failed: {str(ex)[:120]}')
            time.sleep(10 * (i + 1))
    newest = {}
    for orig, ts in rows[1:]:
        k = norm_url(orig)
        if ts > newest.get(k, ''): newest[k] = ts
    return newest


def read_log(paths):
    """{norm_url: {'ok': newest VERIFIED ok ts, 'fails': failed attempts since then, 'last': newest attempt ts,
    'last_fail': newest failure ts, 'permanent': newest permanent-failure ts after the newest ok}} from one or more
    checkpoint files (missing = empty). 33- c: only an ok written with verified=true (the snapshot was checked) counts;
    older ok lines are history and the plan relies on CDX for them. Legacy 'failed' counts as retryable."""
    st = {}
    for p in [x for x in ([paths] if isinstance(paths, str) else (paths or [])) if x]:
        if not os.path.exists(p): continue
        for n, line in enumerate(open(p, encoding='utf-8'), 1):
            if not line.strip(): continue
            try: r = json.loads(line)
            except Exception: sys.exit(f'✗ checkpoint {p}:{n} is not valid JSON — fix the line first (the plan decides what is saved again)')
            t = r.get('ts') or (r['date'].replace('-', '') + '000000' if r.get('date') else '')
            if not t or not r.get('url'): continue
            e = st.setdefault(norm_url(r['url']), {'ok': '', 'fails': [], 'last': '', 'permanent': '', 'last_fail': ''})
            e['last'] = max(e['last'], t)
            res = r.get('result', 'ok')
            if res == 'ok' and r.get('verified') is True: e['ok'] = max(e['ok'], t)
            elif res in ('retryable', 'failed', 'permanent'): e['fails'].append((t, res))
    for e in st.values():
        later = [(t, res) for t, res in e['fails'] if t > e['ok']]
        e['permanent'] = max((t for t, res in later if res == 'permanent'), default='')
        e['last_fail'] = max((t for t, _ in later), default='')
        e['fails'] = len(later)
    return st


def backoff_hours(fails):
    return min(6 * 2 ** (max(fails, 1) - 1), 168)


def wayback_plan(d, log_paths, min_age_days=14, now=None, targets=None, newest=None):
    now = now or datetime.datetime.now(UTC)
    targets = targets if targets is not None else wayback_targets(d)
    newest = newest if newest is not None else cdx_newest(urllib.parse.urlsplit(d['publisher']['website']).hostname)
    logged = read_log(log_paths)
    plan = []
    for i, (u, ref, kind) in enumerate(targets):
        k = norm_url(u); snap = newest.get(k); lg = logged.get(k, {}); mine = lg.get('ok') or None
        best = max(x for x in (snap, mine, '') if x is not None) or None
        ref_ts = ts_of(ref) if ref else None
        if not snap and not mine: state = 'never'
        elif ref_ts and best < ref_ts:
            state = 'stale' if (now - dt_of(best)).days >= min_age_days else 'recent'   # changed, archived < min_age_days ago: wait
        else: state = 'fresh'
        perm, lf, fails = lg.get('permanent') or '', lg.get('last_fail') or '', lg.get('fails', 0)
        if state in ('never', 'stale') and perm and (now - dt_of(perm)).days < PERMANENT_DAYS and not (ref_ts and ref_ts > perm):
            state = 'held'                       # permanent failure, page unchanged since: retried after PERMANENT_DAYS
        elif state in ('never', 'stale') and fails and lf and (now - dt_of(lf)) < datetime.timedelta(hours=backoff_hours(fails)):
            state = 'backoff'                    # retryable failure just now: waits 6 h, 12 h, 24 h … ≤ 7 days
        plan.append({'url': u, 'kind': kind, 'lastmod': ref.isoformat() if ref else None, 'cdx_newest': snap, 'our_log': mine,
                     'state': state, 'fails': fails, 'last_attempt': lg.get('last') or None, 'order': i})
    rank = {'never': 0, 'stale': 1, 'recent': 2, 'backoff': 3, 'held': 4, 'fresh': 5}
    # rotation (33- c): addresses without recent failures come first, whatever their group, so failing 'never' pages can
    # never crowd out 'stale' ones; then by group, fewest failures, oldest attempt, sitemap order
    return sorted(plan, key=lambda p: (p['fails'] > 0, rank[p['state']], p['fails'], p['last_attempt'] or '', p['order']))


def spn_save(u):
    """(http_status, snapshot_url) — anonymous Save Page Now; redirects not followed (the Location is the snapshot)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(SPN + u, headers={'User-Agent': UA})
    try:
        r = opener.open(req, timeout=180); code = r.status; loc = r.headers.get('Content-Location') or r.headers.get('Location') or ''
    except urllib.error.HTTPError as he:
        code = he.code; loc = he.headers.get('Location') or he.headers.get('Content-Location') or ''
    snap = loc if loc.startswith('http') else ('https://web.archive.org' + loc if loc else '')
    return code, snap


def confirm_snapshot(snap):
    """(status, final URL) after following the snapshot's redirects (HEAD of the archived capture); (None, '') on a network error."""
    try:
        req = urllib.request.Request(snap, headers={'User-Agent': UA}, method='HEAD')
        with urllib.request.urlopen(req, timeout=60) as r: return r.status, r.geturl()
    except urllib.error.HTTPError as he: return he.code, ''
    except Exception: return None, ''


def classify(target, code, snap, started, confirm=None):
    """(result, reason): ok | retryable | permanent | rate-limited | login (the archive wants a login: stop the run).
    ok only when the capture opened at the end of its redirects is /web/<ts>/<same address> with ts not older than this
    request (a redirect to an older capture of the same page is not a save — 33- c); 429 at the save OR the check stops."""
    confirm = confirm or confirm_snapshot
    if code == 429: return 'rate-limited', 'HTTP 429 (save)'
    if LOGIN_RE.search(snap or '') or code == 401: return 'login', f'HTTP {code} → {snap or "-"}'
    if code is None or code == 408 or code >= 500: return 'retryable', f'HTTP {code or "network error"}'
    if code not in (200, 301, 302): return 'permanent', f'HTTP {code}'
    floor = started - datetime.timedelta(hours=2)
    m = SNAP_RE.match(snap or '')
    if not m: return 'retryable', f'no snapshot address ({snap or "-"})'
    if norm_url(m.group(2)) != norm_url(target): return 'permanent', f'snapshot is of another address: {m.group(2)}'
    if dt_of(m.group(1)) < floor: return 'retryable', f'snapshot {m.group(1)} predates this request (not a new save)'
    st, final = confirm(snap)
    if st == 429: return 'rate-limited', 'HTTP 429 (snapshot check)'
    if LOGIN_RE.search(final or ''): return 'login', f'snapshot redirects to {final}'
    if st != 200 or not final: return 'retryable', f'snapshot not reachable yet (HTTP {st})'
    fm = SNAP_RE.match(final)
    if not fm or norm_url(fm.group(2)) != norm_url(target): return 'permanent', f'snapshot redirects to another address: {final}'
    if dt_of(fm.group(1)) < floor: return 'retryable', f'snapshot redirects to an older capture {fm.group(1)}'
    return 'ok', ''


def summarise(plan):
    c = {k: sum(1 for p in plan if p['state'] == k) for k in ('never', 'stale', 'recent', 'backoff', 'held', 'fresh')}
    archived = sum(1 for p in plan if p.get('cdx_newest') or p.get('our_log'))           # a real (CDX or verified) snapshot
    return c | {'total': len(plan), 'archived': archived, 'complete': bool(plan) and c['fresh'] == len(plan)}


def checkpoint_merge(into, frm):
    """Union of checkpoint lines: `into` keeps its order, lines of `frm` it does not have are appended."""
    have = open(into, encoding='utf-8').read().splitlines() if os.path.exists(into) else []
    seen = set(have)
    add = [l for l in (open(frm, encoding='utf-8').read().splitlines() if os.path.exists(frm) else []) if l.strip() and l not in seen]
    with open(into, 'a', encoding='utf-8') as f:
        for l in add: f.write(l + '\n')
    return len(add)


def checkpoint_push(repo, ck, branch, keep_dir, message, attempts=3):
    """Commit the checkpoint file `ck` (relative to the checkout `repo`) and push it to origin/<branch>; when the push is
    rejected (another run pushed meanwhile) merge the lines (union) and retry — never force. Success is declared only after
    origin/<branch> is fetched again and contains every local line. Otherwise a copy is kept in keep_dir and 1 returned."""
    has_ident = subprocess.run(['git', '-C', repo, 'config', 'user.email'], capture_output=True, text=True).stdout.strip()
    ident = [] if has_ident else ['-c', 'user.name=Kelin catalogue mirror', '-c', 'user.email=kelin-catalogue-mirror@users.noreply.github.com']
    g = lambda *a: subprocess.run(['git', '-C', repo, *ident, *a], capture_output=True, text=True)
    path = os.path.join(repo, ck)
    mine = [l for l in open(path, encoding='utf-8').read().splitlines() if l.strip()] if os.path.exists(path) else []
    g('add', ck)
    if g('diff', '--cached', '--quiet').returncode: g('commit', '-qm', message)
    for i in range(attempts):
        if g('push', '-q', 'origin', f'HEAD:{branch}').returncode == 0: break
        if g('fetch', '-q', 'origin', branch).returncode: continue
        g('reset', '-q', '--hard', f'origin/{branch}')
        have = open(path, encoding='utf-8').read().splitlines() if os.path.exists(path) else []
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            for l in mine:
                if l not in have: f.write(l + '\n')
        g('add', ck)
        if g('diff', '--cached', '--quiet').returncode: g('commit', '-qm', message + ' (merged)')
    g('fetch', '-q', 'origin', branch)
    remote = g('show', f'origin/{branch}:{ck}')
    missing = [l for l in mine if l not in set((remote.stdout or '').splitlines())] if remote.returncode == 0 else mine
    if missing:
        os.makedirs(keep_dir, exist_ok=True)
        kp = os.path.join(keep_dir, f'wayback-checkpoint-pending-{datetime.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")}.jsonl')
        open(kp, 'w', encoding='utf-8').write('\n'.join(mine) + '\n')
        print(f'✗ checkpoint not pushed: {len(missing)} line(s) missing on origin/{branch}; local copy kept at {kp}')
        return 1
    print(f'✓ checkpoint verified on origin/{branch} ({len(mine)} line(s))')
    return 0


def wayback(cur_p, log_p, max_n, per_minute, retries, budget_min, plan_out, min_age_days=14, extra_logs=(), save=None, sleep=time.sleep, now=None, plan=None):
    # round-1 should-fix: bounded inputs; the plan file may never overwrite a log or the dataset
    if not (isinstance(max_n, int) and 1 <= max_n <= 200): sys.exit(f'✗ --max must be 1–200 (got {max_n})')
    if not (0 < per_minute <= 10): sys.exit(f'✗ --per-minute must be > 0 and ≤ 10 (got {per_minute})')
    if not (isinstance(retries, int) and 0 <= retries <= 5): sys.exit(f'✗ --retries must be 0–5 (got {retries})')
    if budget_min < 0: sys.exit('✗ --budget-min must be ≥ 0')
    if plan_out and os.path.abspath(plan_out) in {os.path.abspath(x) for x in (log_p, cur_p, *extra_logs) if x}:
        sys.exit('✗ --plan-out may not be a log or the dataset file')
    d = load(cur_p)
    plan = plan if plan is not None else wayback_plan(d, [log_p, *extra_logs], min_age_days, now)
    due = [p for p in plan if p['state'] in ('never', 'stale')]
    s = summarise(plan)
    print(f"wayback plan: {s['total']} addresses — never archived {s['never']}, older than their last change {s['stale']}, "
          f"changed but archived < {min_age_days} d ago {s['recent']}, waiting after a failure {s['backoff']}, held after a permanent failure {s['held']}, fresh {s['fresh']}; "
          f"due {len(due)}, this run takes at most {max_n}")
    for p in due[:max_n]: print(f"  → {p['state']:5} {p['url']}  (lastmod {p['lastmod'] or '-'}, newest snapshot {p['cdx_newest'] or '-'}, earlier failures {p['fails']})")
    if len(due) > max_n: print(f'  … {len(due) - max_n} more in later runs')
    if plan_out:
        os.makedirs(os.path.dirname(os.path.abspath(plan_out)), exist_ok=True)
        json.dump({'summary': s, 'plan': plan}, open(plan_out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if not EXECUTE: print('(dry run: nothing submitted to the Wayback Machine)'); return 0
    save = save or spn_save
    os.makedirs(os.path.dirname(os.path.abspath(log_p)), exist_ok=True)
    gap, start, last_start = 60.0 / per_minute, time.monotonic(), -1e9
    saved, failed, held = set(), 0, 0
    over = lambda extra=0: budget_min and time.monotonic() + extra - start > budget_min * 60
    with open(log_p, 'a', encoding='utf-8') as log:
        for p in due[:max_n]:
            if over(): print(f'= time budget {budget_min} min used; rest next run'); break
            for attempt in range(1, retries + 2):
                wait = last_start + gap - time.monotonic()
                if over(max(wait, 0)): break
                if wait > 0: sleep(wait)
                last_start = time.monotonic(); started = datetime.datetime.now(UTC)
                try: code, snap = save(p['url'])
                except Exception as ex: code, snap, err = None, '', str(ex)[:80]
                else: err = ''
                res, why = classify(p['url'], code, snap, started)
                rec = {'date': started.date().isoformat(), 'ts': ts_of(started), 'url': p['url'], 'status': code, 'snapshot': snap,
                       'attempt': attempt, 'result': 'retryable' if res == 'login' else res, 'state_before': p['state']}
                if res == 'ok': rec['verified'] = True
                if err or why: rec['error'] = err or why
                log.write(json.dumps(rec) + '\n'); log.flush()
                if res in ('rate-limited', 'login'):
                    print(f"✗ {p['url']}: {why} — stopping (exit 3; retried in a later run). {len(saved)} saved, {failed} failed this run"); return 3
                if res == 'ok': saved.add(p['url']); print(f"  ✓ {p['url']} → {snap}"); break
                print(f"  ✗ {p['url']}: {res}: {why} (attempt {attempt}/{retries + 1})")
                if res == 'permanent': held += 1; break
                if attempt <= retries and not over(20 * attempt): sleep(20 * attempt)
                elif attempt <= retries: break
            else:
                failed += 1
    left = [p for p in plan if p['state'] != 'fresh' and p['url'] not in saved]
    print(f'wayback: {len(saved)} saved, {failed} retryable failure(s), {held} permanent failure(s) (held {PERMANENT_DAYS} d), '
          f'{len(left)} address(es) still without a current verified snapshot')
    if failed and not saved: return 1
    return 4 if left else 0


def wayback_coverage(cur_p, log_paths, py):
    d = load(cur_p); plan = wayback_plan(d, log_paths); s = summarise(plan)
    today = datetime.date.today().isoformat()
    result = (f"{today} 覆盖核对:{s['total']} 个地址(sitemap 全部页面 + 机器层文件)中 {s['archived']} 个有存档,"
              f"其中 {s['fresh']} 个的最新快照不早于该页最近一次改动;从未存档 {s['never']} 个,快照早于改动 {s['stale'] + s['recent']} 个"
              + (f",失败后等待重试 {s['backoff']} 个" if s['backoff'] else '') + (f",因永久性失败暂缓 {s['held']} 个" if s['held'] else ''))
    cmds = [[py or '$PY', '-I', 'scripts/trackers.py', 'log', 'bl', 'BL-148', '--action', 'Wayback 覆盖核对', '--result', result,
             '--evidence', 'https://web.archive.org/web/*/kelintextile.design/*'],
            [py or '$PY', '-I', 'scripts/trackers.py', 'update', 'bl', 'wayback',
             '--set', 'name=Internet Archive — Wayback Machine snapshots (all sitemap pages + machine layer)',
             '--set', f"anchor=archived copies of {s['archived']}/{s['total']} addresses ({s['fresh']} current)"]]
    print(result)
    # 33- c: complete only when EVERY address has a snapshot not older than its last change (CDX or a verified save)
    print('first pass complete: every address has a current snapshot — wayback.local_first_pass can be set to false' if s['complete']
          else f"first pass NOT complete: {s['total'] - s['fresh']} of {s['total']} address(es) without a current snapshot")
    if not (EXECUTE and py):
        print('(dry run) the register would be updated with:')
        for c in cmds: print('  ' + shlex.join(c))
        return 0
    root = os.path.dirname(os.path.dirname(HERE))
    for c in cmds:
        r = subprocess.run(c, cwd=root)
        if r.returncode: return 1
    return 0


# ---------------------------------------------------------------- gist (review 30- #5: the pinned gist only, one atomic update)
def gist_files(d):
    n_series = sum(len(c['series']) for c in d['categories']); n_models = sum(len(s['models']) for c in d['categories'] for s in c['series'])
    md = [f"# {d['name']} — quick look", '', d['description'], '',
          f"Version `{d['version']}` · {n_series} series · {n_models} model rows · full dataset: [JSON]({d['url']}) · [CSV]({d['csv']}) · "
          f"browse: [{d['publisher']['website'].replace('https://', '')}/products/]({tag(d['pages'], 'gh-gist')})", '',
          '| Category | Series | Models | Source |', '|---|---|---:|---|']
    for c in d['categories']:
        for s in c['series']:
            md.append(f"| [{c['name']}]({tag(c['url'], 'gh-gist')}) | [{s['name']}]({tag(s['url'], 'gh-gist')}) | {len(s['models'])} | {s['source']} |")
    md += ['', d['terms'], '', f"{d['publisher']['legalName']} · official website {tag(d['publisher']['officialWebsite'], 'gh-gist')} · "
           f"[{d['publisher']['email']}](mailto:{d['publisher']['email']})", '']
    return '\n'.join(md)


def fetch_raw(url):
    _, _, b = http_get(url, timeout=120)
    return b.decode('utf-8')


def gist_get(gid):
    """The gist's JSON (gh api GET); None only for a real HTTP 404; any other failure stops (exit 1)."""
    r = subprocess.run(['gh', 'api', f'gists/{gid}'], capture_output=True, text=True)
    if r.returncode == 0: return json.loads(r.stdout)
    if '(HTTP 404)' in (r.stderr or ''): return None
    sys.exit(f'✗ cannot read gist {gid}: {(r.stderr or "").strip()[:200]} — stopping; nothing changed')


def gist_content(f):
    return fetch_raw(f['raw_url']) if f.get('truncated') or f.get('content') is None else f['content']


def gist(cur_p, csv_p, out_dir, cfg_p):
    cfg = mc.load_config(cfg_p); owner, gid = cfg['gist']['owner'], cfg['gist']['id']
    pair = mc.validate_pair(cur_p, csv_p)
    if pair: print('✗ dataset files inconsistent — gist not touched: ' + '; '.join(pair)); return 1
    d = load(cur_p); md = gist_files(d); csv_name = os.path.basename(d['csv'])
    w = out_dir or tempfile.mkdtemp(prefix='kelin-gist-')
    os.makedirs(w, exist_ok=True)
    rp = os.path.join(w, 'README.md'); cp = os.path.join(w, csv_name)
    open(rp, 'w', encoding='utf-8').write(md); shutil.copyfile(csv_p, cp)
    new_csv = open(cp, encoding='utf-8').read()
    gate = mc.check([rp, cp]); rc = mc.report(gate, 'gist gate (new files)')
    info = gist_get(gid)
    if not info: print(f'✗ gist {gid} (mirror-config.json) not found (HTTP 404) — stopping; a new gist is never created'); return 1
    if (info.get('owner') or {}).get('login') != owner: print(f"✗ gist {gid} belongs to {(info.get('owner') or {}).get('login')!r}, not {owner!r} — stopping"); return 1
    files = info.get('files') or {}
    if set(files) != {'README.md', csv_name}: print(f'✗ gist {gid} has files {sorted(files)}, expected README.md + {csv_name} — stopping (edit by hand)'); return 1
    old_md, old_csv = gist_content(files['README.md']), gist_content(files[csv_name])
    old = os.path.join(w, 'current'); os.makedirs(old, exist_ok=True)
    open(os.path.join(old, 'README.md'), 'w', encoding='utf-8').write(old_md); open(os.path.join(old, csv_name), 'w', encoding='utf-8').write(old_csv)
    before = mc.check([old])
    print(f"current gist {gid}: {len(before['failures'])} old/related-domain hits, {sum(before['site_links'].values())} kelintextile.design addresses "
          f"(README {before['site_links'].get('README.md', 0)}); new: {len(gate['failures'])} hits, {sum(gate['site_links'].values())} addresses "
          f"(README {gate['site_links'].get('README.md', 0)})")
    if old_md == md and old_csv == new_csv: print(f'= gist up to date: https://gist.github.com/{gid}'); return 0
    if rc: print('✗ new gist files fail the gate — not written'); return 1
    payload = os.path.join(w, 'gist-patch.json')
    json.dump({'files': {'README.md': {'content': md}, csv_name: {'content': new_csv}}}, open(payload, 'w', encoding='utf-8'), ensure_ascii=False)
    cmd = ['gh', 'api', '-X', 'PATCH', f'gists/{gid}', '--input', payload]
    if not EXECUTE:
        print(f'(dry run) new files in {w}; one update of both files would run:'); print('  ' + shlex.join(cmd)); return 0
    gh(*cmd[1:])
    after = gist_get(gid) or {}
    af = after.get('files') or {}
    if set(af) != {'README.md', csv_name} or gist_content(af['README.md']) != md or gist_content(af[csv_name]) != new_csv:
        print(f'✗ gist {gid}: the content read back differs from what was sent — check https://gist.github.com/{gid}'); return 1
    print(f'✓ gist updated (both files, one revision, verified): https://gist.github.com/{gid}')
    return 0


if __name__ == '__main__':
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    cmd = a[0]
    extra = [a[i + 1] for i, x in enumerate(a) if x == '--extra-log' and i + 1 < len(a)]
    if cmd == 'announce' and len(a) >= 5: announce(a[1], a[2], a[3], a[4])
    elif cmd == 'spotlight' and len(a) >= 3: spotlight(a[1], a[2], opt(a, '--min-days', 13))
    elif cmd == 'wayback' and len(a) >= 3:
        sys.exit(wayback(a[1], a[2], opt(a, '--max', 10), opt(a, '--per-minute', 10.0, float), opt(a, '--retries', 2),
                         opt(a, '--budget-min', 0.0, float), opt(a, '--plan-out', None, str), opt(a, '--min-age-days', 14), extra))
    elif cmd == 'wayback-coverage' and len(a) >= 2: sys.exit(wayback_coverage(a[1], [opt(a, '--log', None, str), *extra], opt(a, '--py', None, str)))
    elif cmd == 'checkpoint-merge' and len(a) == 3: print(f'checkpoint: {checkpoint_merge(a[1], a[2])} line(s) merged')
    elif cmd == 'checkpoint-push' and len(a) >= 4:
        sys.exit(checkpoint_push(a[1], a[2], a[3], opt(a, '--keep', os.path.join(a[1], '..', 'wayback-pending'), str), opt(a, '--message', 'Wayback snapshots', str)))
    elif cmd == 'gist' and len(a) >= 3: sys.exit(gist(a[1], a[2], opt(a, '--out', None, str), opt(a, '--config', os.path.join(HERE, 'mirror-config.json'), str)))
    else: sys.exit(__doc__)
