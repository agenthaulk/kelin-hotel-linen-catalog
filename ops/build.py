#!/usr/bin/env python3
"""Build the GitHub mirror of Kelin's open catalogue dataset (s20, 2026-10-27; extended s20-2, 2026-10-30).

Input: the dataset JSON + CSV as served by https://kelintextile.design/data/ (ops_run.sh downloads the LIVE files, so the
mirror never shows data that is not deployed), plus an optional releases.json (`gh release list --json
tagName,publishedAt,name`) for the Atom feed and sitemap dates. Output, into <out_dir>:
  - the two data files, README.md (github.com; links tagged ?ref=gh-dataset), CITATION.cff, datapackage.json, .nojekyll
  - index.html — GitHub Pages landing page, schema.org Dataset JSON-LD (links tagged gh-pages)
  - <category-slug>/index.html × 10 — one Pages page per category with the model tables and a Dataset subset JSON-LD
    (links tagged gh-pages-cat)
  - feed.xml — Atom feed, one entry per dataset release (links tagged gh-feed); sitemap.xml + robots.txt for the Pages site
Nothing in the output depends on "today": the same inputs always give the same files, so a rebuild without a data or release
change produces no commit. No licence is declared on the company's behalf (业主待确认-汇总.md A11). stdlib only.
Usage: build.py <dataset.json> <dataset.csv> <out_dir> <owner/repo> [releases.json]
"""
import html, json, os, shutil, sys

src_json, src_csv, out, repo = sys.argv[1:5]
releases = json.load(open(sys.argv[5], encoding='utf-8')) if len(sys.argv) > 5 and os.path.exists(sys.argv[5]) else []
d = json.load(open(src_json, encoding='utf-8'))
owner, name = repo.split('/')
PAGES = f'https://{owner}.github.io/{name}/'
GH = f'https://github.com/{repo}'
SITE = d['publisher']['website']
OFFICIAL = d['publisher']['officialWebsite']
JSON_NAME, CSV_NAME = os.path.basename(d['url']), os.path.basename(d['csv'])
e = html.escape


def tag(url, ref):
    return f"{url}{'&' if '?' in url else '?'}ref={ref}"


os.makedirs(out, exist_ok=True)
shutil.copyfile(src_json, os.path.join(out, JSON_NAME))
shutil.copyfile(src_csv, os.path.join(out, CSV_NAME))
open(os.path.join(out, '.nojekyll'), 'w').close()

n_series = sum(len(c['series']) for c in d['categories'])
n_models = sum(len(s['models']) for c in d['categories'] for s in c['series'])
columns = sorted({col for c in d['categories'] for s in c['series'] for col in s['columns']})
# release dates (ISO, UTC) — the current version's release may not exist yet at build time; then it is simply absent
rel_sorted = sorted((r for r in releases if r.get('tagName', '').startswith('v-')), key=lambda r: r['publishedAt'])
latest_rel = rel_sorted[-1]['publishedAt'] if rel_sorted else None
latest_day = latest_rel[:10] if latest_rel else None

# ---------------------------------------------------------------- README.md (github.com; links tagged gh-dataset)
R = [f"# {d['name']}", '',
     f"{d['description']}", '',
     f"- **Source pages:** [{SITE.replace('https://', '')}/products/]({tag(d['pages'], 'gh-dataset')}) — every series has its own page with the model table, photos and sources.",
     f"- **Company:** {d['publisher']['legalName']} ({d['publisher']['name']}) · official website [{OFFICIAL.replace('https://', '')}]({OFFICIAL}) · {d['publisher']['email']}",
     f"- **Note:** {d['note']}",
     f"- **Version:** `{d['version']}` (content hash; changes only when the data changes) · {n_series} series, {n_models} model rows",
     f"- **Pages:** [dataset landing page]({PAGES}) · one page per category (links below) · [Atom feed of releases]({PAGES}feed.xml)",
     f"- **Updates:** [Releases]({GH}/releases) · [Announcements]({GH}/discussions/categories/announcements) (one post per dataset version, with the differences) · [Series spotlight]({GH}/discussions/categories/show-and-tell)",
     '', '## Files', '',
     f"| File | Format |", '|---|---|',
     f"| [`{JSON_NAME}`]({JSON_NAME}) | JSON: categories → series → models (`values` per column, `also_listed_as` where the company published a second value, `source`) |",
     f"| [`{CSV_NAME}`]({CSV_NAME}) | CSV, long format: `category, series, series_url, model, attribute, value, also_listed_as, source` |",
     f"| [`datapackage.json`](datapackage.json) | Frictionless Data descriptor |",
     '', 'The same files are served by the site itself: ' + f"[{JSON_NAME}]({d['url']}) · [{CSV_NAME}]({d['csv']}).",
     '', '## Contents', '', '| Category | Series | Models | Source | Mirror page |', '|---|---|---:|---|---|']
for c in d['categories']:
    for s in c['series']:
        R.append(f"| [{c['name']}]({tag(c['url'], 'gh-dataset')}) | [{s['name']}]({tag(s['url'], 'gh-dataset')}) | {len(s['models'])} | {s['source']} | [{c['slug']}]({PAGES}{c['slug']}/) |")
R += ['', '## Columns', '', 'Attributes used across series: ' + ', '.join(f'`{x}`' for x in columns) + '.', '',
      '## Terms', '', d['terms'], '',
      '## Updates', '', f"This repository mirrors the dataset published at {SITE}. It is regenerated from the live site when the catalogue changes "
      '(a scheduled GitHub Actions job in `.github/workflows/ops.yml` rebuilds it, cuts a release and posts the differences in Announcements); '
      'there are no filler commits. For quotations and current specifications, contact the company.', '',
      '---', '', '本仓库是南通科琳纺织(Kelin Textile)酒店布草产品目录的数据镜像(系列与型号规格,CSV/JSON),来源 ' + SITE + ';官网 ' + OFFICIAL + '。', '']
open(os.path.join(out, 'README.md'), 'w', encoding='utf-8').write('\n'.join(R))

# ---------------------------------------------------------------- shared HTML bits
CSS = """body{font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1040px;margin:0 auto;padding:24px 16px;color:#1b2430;background:#fbfaf7}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.2rem;margin:1.4em 0 .3em}table{border-collapse:collapse;width:100%;font-size:.92rem}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e3e0d8;vertical-align:top}
th{color:#5b6573;font-weight:600}td.n{text-align:right}a{color:#1b365d}.meta{color:#5b6573;font-size:.9rem}.btn{display:inline-block;margin:4px 8px 4px 0;padding:6px 12px;border:1px solid #1b365d;border-radius:6px;text-decoration:none}
nav.cats a{display:inline-block;margin:2px 10px 2px 0}footer{margin-top:2em;font-size:.9rem;color:#5b6573}
@media (prefers-color-scheme:dark){body{background:#14181e;color:#e7e3da}a{color:#9cc0ef}.btn{border-color:#9cc0ef}th,.meta,footer{color:#a7adb5}th,td{border-color:#2b3038}}"""
HEAD = ('<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<link rel="alternate" type="application/atom+xml" title="Dataset releases" href="{PAGES}feed.xml">')
FOOT = (f'<footer>{e(d["publisher"]["legalName"])} · official website <a href="{e(OFFICIAL)}">{e(OFFICIAL.replace("https://", ""))}</a> · '
        f'<a href="{e(GH)}">GitHub repository</a> · <a href="{e(GH)}/releases">Releases</a> · <a href="{e(GH)}/discussions">Discussions</a> · '
        f'<a href="{e(PAGES)}feed.xml">Atom feed</a><br>{e(d["terms"])}</footer>')
cats_nav = '<nav class="cats">' + ' '.join(f'<a href="{e(PAGES)}{e(c["slug"])}/">{e(c["name"])}</a>' for c in d['categories']) + '</nav>'
org = {'@type': 'Organization', 'name': d['publisher']['name'], 'legalName': d['publisher']['legalName'], 'url': OFFICIAL, 'sameAs': [SITE]}

# ---------------------------------------------------------------- index.html (GitHub Pages; links tagged gh-pages)
ld = {'@context': 'https://schema.org', '@type': 'Dataset', '@id': PAGES, 'name': d['name'], 'description': d['description'],
      'url': PAGES, 'sameAs': SITE + '/data/', 'isBasedOn': SITE + '/data/', 'version': d['version'],
      'keywords': ['hotel linen', 'hotel bedding', 'textile specifications', 'thread count', 'duvet', 'pillow', 'towel', 'Nantong'],
      # one Organization url everywhere: the official site (as the site's own #org); the catalogue site is its sameAs
      'creator': org,
      'distribution': [{'@type': 'DataDownload', 'encodingFormat': 'application/json', 'contentUrl': d['url']},
                       {'@type': 'DataDownload', 'encodingFormat': 'text/csv', 'contentUrl': d['csv']}],
      'hasPart': [{'@type': 'Dataset', '@id': f"{PAGES}{c['slug']}/", 'name': f"{c['name']} — {d['publisher']['name']}", 'url': f"{PAGES}{c['slug']}/"} for c in d['categories']],
      'variableMeasured': columns, 'usageInfo': d['terms']}
if latest_day: ld['dateModified'] = latest_day
rows = ''.join(f"<tr><td><a href=\"{e(tag(c['url'], 'gh-pages'))}\">{e(c['name'])}</a></td><td><a href=\"{e(tag(s['url'], 'gh-pages'))}\">{e(s['name'])}</a></td>"
               f"<td class=n>{len(s['models'])}</td><td>{e(s['summary'])}</td><td><a href=\"{e(PAGES)}{e(c['slug'])}/\">{e(c['slug'])}</a></td></tr>" for c in d['categories'] for s in c['series'])
H = f"""<!doctype html>
<html lang="en"><head>{HEAD}
<title>{e(d['name'])} — open dataset (CSV / JSON)</title>
<meta name="description" content="{e(d['description'][:300])}">
<link rel="canonical" href="{e(PAGES)}">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
<style>{CSS}</style>
</head><body>
<p class="meta">Open dataset · version <code>{e(d['version'])}</code> · {n_series} series · {n_models} model rows{' · released ' + e(latest_day) if latest_day else ''}</p>
<h1>{e(d['name'])}</h1>
<p>{e(d['description'])}</p>
<p><a class="btn" href="{e(CSV_NAME)}">Download CSV</a><a class="btn" href="{e(JSON_NAME)}">Download JSON</a><a class="btn" href="{e(tag(d['pages'], 'gh-pages'))}">Browse the catalogue on {e(SITE.replace('https://', ''))}</a><a class="btn" href="{e(GH)}">GitHub repository</a></p>
<p class="meta">{e(d['publisher']['legalName'])} · official website <a href="{e(OFFICIAL)}">{e(OFFICIAL.replace('https://', ''))}</a> · {e(d['note'])}</p>
<h2>Categories</h2>
{cats_nav}
<h2>Contents</h2>
<table><thead><tr><th>Category</th><th>Series</th><th>Models</th><th>Summary</th><th>Mirror page</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Terms</h2><p>{e(d['terms'])}</p>
{FOOT}
</body></html>
"""
open(os.path.join(out, 'index.html'), 'w', encoding='utf-8').write(H)

# ---------------------------------------------------------------- <category>/index.html (links tagged gh-pages-cat)
for c in d['categories']:
    cdir = os.path.join(out, c['slug']); os.makedirs(cdir, exist_ok=True)
    curl = f"{PAGES}{c['slug']}/"
    c_models = sum(len(s['models']) for s in c['series'])
    c_cols = sorted({col for s in c['series'] for col in s['columns']})
    cld = {'@context': 'https://schema.org', '@type': 'Dataset', '@id': curl, 'name': f"{c['name']} — {d['publisher']['name']} model specifications",
           'description': f"{c['name']}: {len(c['series'])} series and {c_models} model rows of {d['publisher']['legalName']}, with the values the company published ({c['source']}). Subset of the {d['name']}.",
           'url': curl, 'isPartOf': {'@type': 'Dataset', '@id': PAGES, 'name': d['name'], 'url': PAGES},
           'sameAs': c['url'], 'isBasedOn': c['url'], 'version': d['version'], 'creator': org, 'variableMeasured': c_cols, 'usageInfo': d['terms'],
           'distribution': [{'@type': 'DataDownload', 'encodingFormat': 'application/json', 'contentUrl': d['url']},
                            {'@type': 'DataDownload', 'encodingFormat': 'text/csv', 'contentUrl': d['csv']}]}
    if latest_day: cld['dateModified'] = latest_day
    parts = []
    for s in c['series']:
        apps = s.get('applications') or []
        apps_txt = ', '.join(apps) if isinstance(apps, list) else str(apps)
        head = ''.join(f'<th>{e(col)}</th>' for col in s['columns'])
        body = ''.join('<tr><td><code>' + e(m['code']) + '</code></td>' + ''.join(f"<td>{e(str(m['values'].get(col, '')))}</td>" for col in s['columns']) + '</tr>' for m in s['models'])
        parts.append(f"""<h2 id="{e(s['slug'])}">{e(s['name'])}</h2>
<p>{e(s['summary'])}</p>
{f'<p class="meta">Applications: {e(apps_txt)}</p>' if apps_txt else ''}
<table><thead><tr><th>Model</th>{head}</tr></thead><tbody>{body}</tbody></table>
<p class="meta">Source: {e(s['source'])} · <a href="{e(tag(s['url'], 'gh-pages-cat'))}">series page on {e(SITE.replace('https://', ''))}</a> (photos, sources, enquiry)</p>""")
    CH = f"""<!doctype html>
<html lang="en"><head>{HEAD}
<title>{e(c['name'])} — {e(d['publisher']['name'])} model specifications (open dataset)</title>
<meta name="description" content="{e(cld['description'][:300])}">
<link rel="canonical" href="{e(curl)}">
<script type="application/ld+json">{json.dumps(cld, ensure_ascii=False)}</script>
<style>{CSS}</style>
</head><body>
<p class="meta"><a href="{e(PAGES)}">{e(d['name'])}</a> · version <code>{e(d['version'])}</code> · {len(c['series'])} series · {c_models} model rows</p>
<h1>{e(c['name'])}</h1>
<p>Model specifications of {e(d['publisher']['legalName'])} ({e(d['publisher']['name'])}) for this category, as published by the company ({e(c['source'])}).
Category page with photos and sources: <a href="{e(tag(c['url'], 'gh-pages-cat'))}">{e(c['url'].replace('https://', ''))}</a>.</p>
<p><a class="btn" href="{e(tag(c['url'], 'gh-pages-cat'))}">Browse {e(c['name'])} on {e(SITE.replace('https://', ''))}</a><a class="btn" href="{e(PAGES)}{e(CSV_NAME)}">Download CSV (all categories)</a><a class="btn" href="{e(PAGES)}{e(JSON_NAME)}">Download JSON</a></p>
{''.join(parts)}
<h2>Other categories</h2>
{cats_nav}
{FOOT}
</body></html>
"""
    open(os.path.join(cdir, 'index.html'), 'w', encoding='utf-8').write(CH)

# ---------------------------------------------------------------- feed.xml (Atom; one entry per release) + sitemap.xml + robots.txt
def atom_entry(r):
    ver = r['tagName'][2:]
    body = (f"<p>Catalogue dataset version <code>{e(ver)}</code> ({n_series} series, {n_models} model rows at the current version). "
            f"Files: <a href=\"{e(GH)}/releases/tag/{e(r['tagName'])}\">release assets</a>, <a href=\"{e(PAGES)}\">dataset page</a>. "
            f"Source pages: <a href=\"{e(tag(d['pages'], 'gh-feed'))}\">{e(SITE.replace('https://', ''))}/products/</a> — "
            + ', '.join(f"<a href=\"{e(tag(c['url'], 'gh-feed'))}\">{e(c['name'])}</a>" for c in d['categories']) + '.</p>')
    return (f"<entry><title>{e(r.get('name') or 'Dataset ' + ver)}</title><id>{e(GH)}/releases/tag/{e(r['tagName'])}</id>"
            f"<link rel=\"alternate\" href=\"{e(GH)}/releases/tag/{e(r['tagName'])}\"/><updated>{e(r['publishedAt'])}</updated><published>{e(r['publishedAt'])}</published>"
            f"<content type=\"html\">{e(body)}</content></entry>")
feed_updated = latest_rel or '1970-01-01T00:00:00Z'
F = (f'<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom"><title>{e(d["name"])} — releases</title>'
     f'<subtitle>{e(d["description"])}</subtitle><id>{e(PAGES)}</id><link rel="self" href="{e(PAGES)}feed.xml"/><link rel="alternate" href="{e(PAGES)}"/>'
     f'<link rel="related" href="{e(tag(d["pages"], "gh-feed"))}"/><updated>{e(feed_updated)}</updated>'
     f'<author><name>{e(d["publisher"]["legalName"])}</name><uri>{e(SITE)}</uri></author>'
     + ''.join(atom_entry(r) for r in reversed(rel_sorted)) + '</feed>\n')
open(os.path.join(out, 'feed.xml'), 'w', encoding='utf-8').write(F)
lm = f'<lastmod>{e(latest_day)}</lastmod>' if latest_day else ''
S = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
     + f'<url><loc>{e(PAGES)}</loc>{lm}</url>' + ''.join(f'<url><loc>{e(PAGES)}{e(c["slug"])}/</loc>{lm}</url>' for c in d['categories']) + '</urlset>\n')
open(os.path.join(out, 'sitemap.xml'), 'w', encoding='utf-8').write(S)
open(os.path.join(out, 'robots.txt'), 'w', encoding='utf-8').write(f'User-agent: *\nAllow: /\nSitemap: {PAGES}sitemap.xml\n')

# ---------------------------------------------------------------- CITATION.cff + datapackage.json
C = ['cff-version: 1.2.0', 'message: "If you use this dataset, please cite the source pages."', 'type: dataset',
     f'title: "{d["name"]}"', 'authors:', f'  - name: "{d["publisher"]["legalName"]}"', f'    website: "{SITE}"',
     f'version: "{d["version"]}"', f'url: "{d["url"]}"', f'repository-code: "{GH}"',
     'keywords:', '  - hotel linen', '  - textile specifications', '  - dataset', '']
open(os.path.join(out, 'CITATION.cff'), 'w', encoding='utf-8').write('\n'.join(C))
dp = {'name': 'kelin-hotel-linen-catalog', 'title': d['name'], 'description': d['description'], 'homepage': SITE, 'version': d['version'],
      'sources': [{'title': 'kelintextile.design', 'path': d['url']}],
      'resources': [{'name': 'catalog-long', 'path': CSV_NAME, 'format': 'csv', 'mediatype': 'text/csv', 'encoding': 'utf-8',
                     'schema': {'fields': [{'name': f, 'type': 'string'} for f in ['category', 'series', 'series_url', 'model', 'attribute', 'value', 'also_listed_as', 'source']]}},
                    {'name': 'catalog', 'path': JSON_NAME, 'format': 'json', 'mediatype': 'application/json'}]}
json.dump(dp, open(os.path.join(out, 'datapackage.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(f'mirror built: {n_series} series, {n_models} models, {len(d["categories"])} category pages, {len(rel_sorted)} feed entries, version {d["version"]}')
