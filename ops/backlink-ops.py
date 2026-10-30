#!/usr/bin/env python3
"""Backlink operations for Kelin's catalogue mirror (s20-2, 2026-10-30). stdlib + the `gh` CLI (GH_TOKEN in Actions, keyring
locally). Every command is idempotent and derives its state from the platform itself (existing discussions, Wayback's
availability API, the gist list), so the weekly job and a local run can overlap without double posts. Nothing is posted
anywhere the company has not placed itself: only our own repository's Discussions, the Internet Archive and our own gist.

  announce  <owner/repo> <dataset.json> <prev.json|-> <release_url>
        One Discussion in "Announcements" per dataset version ("Dataset <version>"), body = what changed vs the previous
        release (series/models added, removed, changed values). Skipped when that title exists.
  spotlight <owner/repo> <dataset.json> [--min-days N]        (default 13)
        One "Series spotlight: <series> — <category>" Discussion in "Show and tell": the series summary, applications,
        model table and source, with links to the series page (?ref=gh-disc). Picks the series covered least / longest ago;
        skipped while the newest spotlight is younger than N days.
  wayback   <dataset.json> <log.jsonl> [--min-days N]          (default 28)
        Save Page Now (anonymous, no account) for the site's key pages — home, about, products, the 10 category pages,
        data, faq, contact — when the newest snapshot is older than N days. Appends one JSON line per saved page.
  gist      <dataset.json> <dataset.csv>
        Create or update the single public "quick look" gist (README.md with category links tagged ?ref=gh-gist + CSV).
        Needs the gist scope: run locally, not in Actions (GITHUB_TOKEN cannot create gists).
Exit 0 ok / skipped · 1 failed.
"""
import datetime, json, os, re, subprocess, sys, tempfile, time, urllib.parse, urllib.request

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


def opt(args, flag, default):
    return int(args[args.index(flag) + 1]) if flag in args else default


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
            f"official website {d['publisher']['officialWebsite']} · dataset files: [JSON]({d['url']}) · [CSV]({d['csv']}).")


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
    url = create_discussion(repo_id, cat, title, '\n'.join(body) + footer(d, 'gh-disc'))
    print(f'✓ spotlight posted: {title}: {url}')


# ---------------------------------------------------------------- wayback
def wayback(cur_p, log_p, min_days):
    d = load(cur_p); site = d['publisher']['website']
    urls = [site + '/', site + '/about/', d['pages']] + [c['url'] for c in d['categories']] + [site + '/data/', site + '/faq/', site + '/contact/']
    today = datetime.date.today(); saved = skipped = failed = 0
    os.makedirs(os.path.dirname(os.path.abspath(log_p)), exist_ok=True)
    with open(log_p, 'a', encoding='utf-8') as log:
        for u in urls:
            try:
                req = urllib.request.Request('https://archive.org/wayback/available?url=' + urllib.parse.quote(u, safe=''), headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=40) as r: avail = json.load(r)
                ts = (avail.get('archived_snapshots') or {}).get('closest', {}).get('timestamp')
                if ts:
                    age = (today - datetime.date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))).days
                    if age < min_days: skipped += 1; print(f'  = {u} snapshot {ts[:8]} ({age} d)'); continue
            except Exception as ex:
                print(f'  ? availability check failed for {u}: {str(ex)[:60]}')
            try:
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *a, **k): return None
                opener = urllib.request.build_opener(NoRedirect)
                req = urllib.request.Request('https://web.archive.org/save/' + u, headers={'User-Agent': UA})
                try:
                    r = opener.open(req, timeout=180); loc = r.headers.get('Content-Location') or r.headers.get('Location') or ''; code = r.status
                except urllib.error.HTTPError as he:
                    code = he.code; loc = he.headers.get('Location') or he.headers.get('Content-Location') or ''
                    if code not in (301, 302): raise
                snap = loc if loc.startswith('http') else ('https://web.archive.org' + loc if loc else '')
                log.write(json.dumps({'date': today.isoformat(), 'url': u, 'status': code, 'snapshot': snap}) + '\n'); log.flush()
                saved += 1; print(f'  ✓ {u} → {snap or code}')
            except Exception as ex:
                failed += 1; print(f'  ✗ {u}: {str(ex)[:80]}')
            time.sleep(6)
    print(f'wayback: {saved} saved, {skipped} fresh (< {min_days} d), {failed} failed')


# ---------------------------------------------------------------- gist
GIST_DESC = 'Kelin Textile hotel linen catalogue — quick look (CSV + series links) · source kelintextile.design'


def gist(cur_p, csv_p):
    d = load(cur_p)
    n_series = sum(len(c['series']) for c in d['categories']); n_models = sum(len(s['models']) for c in d['categories'] for s in c['series'])
    md = [f"# {d['name']} — quick look", '', d['description'], '',
          f"Version `{d['version']}` · {n_series} series · {n_models} model rows · full dataset: [JSON]({d['url']}) · [CSV]({d['csv']}) · "
          f"browse: [{d['publisher']['website'].replace('https://', '')}/products/]({tag(d['pages'], 'gh-gist')})", '',
          '| Category | Series | Models | Source |', '|---|---|---:|---|']
    for c in d['categories']:
        for s in c['series']:
            md.append(f"| [{c['name']}]({tag(c['url'], 'gh-gist')}) | [{s['name']}]({tag(s['url'], 'gh-gist')}) | {len(s['models'])} | {s['source']} |")
    md += ['', d['terms'], '', f"{d['publisher']['legalName']} · official website {d['publisher']['officialWebsite']} · {d['publisher']['email']}", '']
    with tempfile.TemporaryDirectory() as w:
        rp = os.path.join(w, 'README.md'); cp = os.path.join(w, os.path.basename(csv_p))
        open(rp, 'w', encoding='utf-8').write('\n'.join(md)); open(cp, 'wb').write(open(csv_p, 'rb').read())
        existing = [l.split('\t')[0] for l in gh('gist', 'list', '--limit', '100').splitlines() if 'quick look' in l and 'kelintextile.design' in l]
        if existing:
            gid = existing[0]
            cur = gh('gist', 'view', gid, '-f', 'README.md', '--raw') if True else ''
            if cur.strip() == '\n'.join(md).strip(): print(f'= gist up to date: https://gist.github.com/{gid}'); return
            gh('gist', 'edit', gid, '-f', 'README.md', rp); gh('gist', 'edit', gid, '-f', os.path.basename(csv_p), cp)
            print(f'✓ gist updated: https://gist.github.com/{gid}')
        else:
            url = gh('gist', 'create', '--public', '-d', GIST_DESC, rp, cp).strip().splitlines()[-1]
            print(f'✓ gist created: {url}')


if __name__ == '__main__':
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    cmd = a[0]
    if cmd == 'announce' and len(a) >= 5: announce(a[1], a[2], a[3], a[4])
    elif cmd == 'spotlight' and len(a) >= 3: spotlight(a[1], a[2], opt(a, '--min-days', 13))
    elif cmd == 'wayback' and len(a) >= 3: wayback(a[1], a[2], opt(a, '--min-days', 28))
    elif cmd == 'gist' and len(a) >= 3: gist(a[1], a[2])
    else: sys.exit(__doc__)
