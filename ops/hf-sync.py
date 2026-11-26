#!/usr/bin/env python3
"""Hugging Face dataset copy of Kelin's catalogue dataset (s51, 2026-11-23; s58, 2026-11-26). DRY RUN unless --execute.

Builds the files a Hugging Face dataset repository gets — README.md (dataset card: YAML metadata + description with links to
kelintextile.design/data/, /products/ and the 10 category pages, tagged ?ref=hf-dataset), the dataset CSV and JSON — into
--out, runs the mirror gate on them and lists them. Facts come only from the dataset itself (the live file in normal use).
The dataset's top-level fields are checked first: `name` must be one line of text (it is written to the card as a quoted
YAML string), and `url` / `csv` must end in exactly kelin-hotel-linen-catalog.json / .csv (the file names here are fixed).

--execute uploads them with huggingface_hub to https://huggingface.co (fixed endpoint): the three files are compared with
the remote copies (get_paths_info: git blob sha1, or the LFS sha256) and ONLY the files that differ go into one
upload_folder commit; nothing differs → "no change", no commit, no request that writes. The repository must already exist:
it is created only with --create (a first run by hand; the weekly job never passes it, so a deleted repository is not
re-created). The execute phase has a hard time limit (--timeout, default 240 s; exit 3 when it is reached).
It refuses (exit 2) unless ALL of:
  - a licence setting: data_license is set (mirror-config.json) — the card's `license:` comes from it — OR, with
    data_license null, huggingface.publish_without_licence is true: then no licence statement is published (no `license:`
    line, no licence wording in the card, the JSON or the CSV; checked before upload — the dataset's citation-request
    `terms` sentence stays), the same as the GitHub mirror; the dataset itself may not carry a `license` / `licence` key;
  - huggingface.repo_id is set — and equals mirror_check.PINNED['huggingface'] (kelintextile/hotel-linen-catalog; load_config
    refuses any other value);
  - the access token is in the environment variable KELIN_HF_TOKEN, in the form hf_ + at least 20 letters / digits after
    surrounding whitespace is trimmed (a "write" token; read from the environment only — never written to a file, printed,
    logged or passed on a command line; everything this script prints goes through a filter that masks the token by value
    and anything shaped like a token or an Authorization header; no traceback is printed);
  - HF_ENDPOINT is unset or https://huggingface.co, HUGGINGFACE_CO_STAGING is unset, REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE
    are unset (nothing may send the token to another host or let another certificate authority read it);
  - the dataset's own `license` agrees with the mirror's (a dataset that states a licence while the mirror states none is refused);
  - the gate passes on every file.
Dependency (execute only), in its own virtual environment, never the site's package.json — the exact pinned set with hashes:
  python3 -m venv ~/.venvs/kelin-hf && ~/.venvs/kelin-hf/bin/python -m pip install --require-hashes --only-binary=:all: \\
      -r scripts/dataset-mirror/hf-requirements.txt
  ~/.venvs/kelin-hf/bin/python -I scripts/dataset-mirror/hf-sync.py <dataset.json> <dataset.csv> --execute
The weekly job (ops_run.sh step 6) runs it with --execute when KELIN_HF_TOKEN is set, in a throwaway venv, with a minimal
environment and a time limit.
Usage: hf-sync.py <dataset.json> <dataset.csv> [--out DIR] [--license ID] [--repo-id ID] [--config FILE] [--timeout S]
                  [--create] [--execute]
  --license / --repo-id: trial overrides for a dry run (refused together with --execute: execute uses the config only).
Exit 0 ok / dry run · 1 failed · 2 refused · 3 timed out
"""
import argparse, contextlib, csv, hashlib, importlib.util, io, json, os, re, shutil, signal, sys, tempfile
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True        # no __pycache__ in the mirror checkout (it would be committed)
_spec = importlib.util.spec_from_file_location('mirror_check', os.path.join(HERE, 'mirror_check.py'))
mc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(mc)
TOKEN_ENV = 'KELIN_HF_TOKEN'
TOKEN_RE = re.compile(r'hf_[A-Za-z0-9]{20,}')
HF_HOST = 'https://huggingface.co'                     # the only host the token is ever sent to
REF = 'hf-dataset'
CSV_NAME, JSON_NAME = 'kelin-hotel-linen-catalog.csv', 'kelin-hotel-linen-catalog.json'
NAMES = ['README.md', CSV_NAME, JSON_NAME]
# with no licence set, none of this may appear in the card, the JSON or the CSV (it would read as a licence statement or an open-data claim)
LICENCE_WORDS = re.compile(r'licen[cs]e|creative\s*commons|\bcc[\s-]?by\b|\bcc0\b|public\s+domain|open\s+(?:data|licen)|'
                           r'freely|free\s+(?:to|for)\s+(?:use|any)|royalty|for\s+any\s+purpose|without\s+(?:asking\s+)?permission', re.I)
LICENCE_KEY = re.compile(r'licen[cs]e', re.I)
# masks applied to everything printed, in addition to the token's own value: anything shaped like a Hugging Face token
# (also inside a repr() with escapes around it), a Bearer credential, an Authorization header value
SCRUB_RES = ((re.compile(r'hf_[A-Za-z0-9]{8,}'), '***'), (re.compile(r'(?i)\bbearer\s+\S+'), 'Bearer ***'),
             (re.compile(r'''(?i)(authorization['"]?\s*[:=]\s*['"]?)[^\s'",}]+'''), r'\1***'))


class HfTimeout(BaseException):                        # BaseException: no library `except Exception` can swallow it
    pass


def scrub(text, token=''):
    """`text` with the token (by value) and anything token- / credential-shaped masked."""
    text = str(text)
    if token and len(token) >= 8: text = text.replace(token, '***')
    for rx, rep in SCRUB_RES: text = rx.sub(rep, text)
    return text


class _ScrubStream(io.TextIOBase):
    """Write-through wrapper that masks every write (library warnings, logging, progress bars, our own prints)."""
    def __init__(self, inner, token): self._inner, self._token = inner, token
    def write(self, s): self._inner.write(scrub(s, self._token)); return len(s)
    def flush(self): self._inner.flush()
    def isatty(self): return False
    @property
    def encoding(self): return getattr(self._inner, 'encoding', 'utf-8')


@contextlib.contextmanager
def scrubbed_output(token):
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _ScrubStream(old[0], token), _ScrubStream(old[1], token)
    try: yield
    finally:
        try: sys.stdout.flush(); sys.stderr.flush()
        finally: sys.stdout, sys.stderr = old


def one_line(ex):
    """`Type: message` of an exception, whitespace collapsed, masked, ≤300 characters — the only form an error is printed in."""
    msg = re.sub(r'\s+', ' ', str(ex))
    return scrub(f'{type(ex).__name__}: {msg}', os.environ.get(TOKEN_ENV, '').strip())[:300]


def licence_wording(text):
    """Lines of `text` that read as a licence statement / open-data claim (must be empty when no licence is set)."""
    return [ln for ln in text.splitlines() if LICENCE_WORDS.search(ln)]


def _walk(o, path='$'):
    """(path, key-or-None, string) for every key and string value in a JSON document."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield path, str(k), None
            yield from _walk(v, f'{path}.{k}')
    elif isinstance(o, list):
        for i, v in enumerate(o): yield from _walk(v, f'{path}[{i}]')
    elif isinstance(o, str):
        yield path, None, o


def dataset_problems(d):
    """Problems with the top-level fields the card and the file names come from ([] = fine)."""
    p = []
    name = d.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or re.search(r'[\x00-\x1f\x7f\u0085\u2028\u2029]', name):
        p.append('name must be one line of text (1–200 characters, no control characters or line breaks)')
    for key, want in (('url', JSON_NAME), ('csv', CSV_NAME)):
        v = d.get(key)
        path = urlparse(v).path if isinstance(v, str) else ''
        if not isinstance(v, str) or not v.startswith('https://') or path.rsplit('/', 1)[-1] != want or '..' in path or '\\' in v:
            p.append(f'{key} must be an https:// address of a file named exactly {want}')
    return p


def licence_in_data(d, csv_text):
    """(licence keys, wording hits) in the dataset JSON (every key and string, at any depth) and the CSV."""
    keys, words = [], []
    for path, k, s in _walk(d):
        if k is not None and LICENCE_KEY.search(k): keys.append(f'{path}.{k}')
        if k is not None and LICENCE_WORDS.search(k): words.append(f'JSON key {path}.{k}')
        if s is not None and LICENCE_WORDS.search(s): words.append(f'JSON {path}: {s[:120]}')
    for i, ln in enumerate(csv_text.splitlines(), 1):
        if LICENCE_WORDS.search(ln): words.append(f'CSV line {i}: {ln[:120]}')
    return keys, words


def env_problems():
    """Environment settings that would send the token elsewhere or let another certificate authority read it."""
    p = []
    ep = os.environ.get('HF_ENDPOINT')
    if ep is not None and ep.strip().rstrip('/') != HF_HOST:
        p.append(f'HF_ENDPOINT is set to another address (value not shown) — unset it; the token goes only to {HF_HOST}')
    for v in ('HUGGINGFACE_CO_STAGING', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'):
        if os.environ.get(v): p.append(f'{v} is set — unset it for an execute')
    return p


def git_blob_sha1(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def changed_files(api, repo_id, out, names):
    """The names whose remote copy differs from out/<name> (absent remotely = differs)."""
    remote = {getattr(i, 'path', None): i for i in api.get_paths_info(repo_id, names, repo_type='dataset')}
    diff = []
    for n in names:
        data = open(os.path.join(out, n), 'rb').read(); i = remote.get(n)
        if i is None or not hasattr(i, 'blob_id'): diff.append(n); continue
        lfs = getattr(i, 'lfs', None)
        if lfs:
            want = getattr(lfs, 'sha256', None) or (lfs.get('sha256') if isinstance(lfs, dict) else None)
            same = want == hashlib.sha256(data).hexdigest()
        else:
            same = i.blob_id == git_blob_sha1(data)
        if not same: diff.append(n)
    return diff


def tag(url):
    return f"{url}{'&' if '?' in url else '?'}ref={REF}"


def size_category(n):
    for lim, name in ((1000, 'n<1K'), (10000, '1K<n<10K'), (100000, '10K<n<100K'), (1000000, '100K<n<1M')):
        if n < lim: return name
    return 'n>1M'


def card(d, csv_rows, lic_id):
    lic = mc.LICENSES.get(lic_id)
    n_series = sum(len(c['series']) for c in d['categories']); n_models = sum(len(s['models']) for c in d['categories'] for s in c['series'])
    site = d['publisher']['website'].rstrip('/')
    y = ['---']
    if lic: y.append(f"license: {lic['hf']}")
    y += ['language:', '- en', '- zh', f"pretty_name: {json.dumps(d['name'], ensure_ascii=False)}", 'size_categories:', f'- {size_category(csv_rows)}',
          'tags:', '- hotel-linen', '- textiles', '- bedding', '- specifications', '- hs-codes',
          'configs:', '- config_name: default', '  data_files:', '  - split: train', f'    path: {CSV_NAME}', '---', '']
    b = [f"# {d['name']}", '', d['description'], '',
         f"- **Publisher:** {d['publisher']['legalName']} ({d['publisher']['name']}) · [{site.replace('https://', '')}]({tag(d['publisher']['officialWebsite'])})",
         f"- **Source and current version:** [{site.replace('https://', '')}/data/]({tag(site + '/data/')}) — the files here are copies of "
         f"[{JSON_NAME}]({d['url']}) and [{CSV_NAME}]({d['csv']})",
         f"- **Version:** `{d['version']}` (content hash) · {n_series} series, {n_models} model rows, {len(d['categories'])} categories",
         f"- **Series pages** (photos, sources, enquiry): [{d['pages'].replace('https://', '')}]({tag(d['pages'])})", '',
         '## Categories', '', '| Category | Series | Models |', '|---|---:|---:|']
    for c in d['categories']:
        b.append(f"| [{c['name']}]({tag(c['url'])}) | {len(c['series'])} | {sum(len(s['models']) for s in c['series'])} |")
    b += ['', '## Files', '', '| File | Contents |', '|---|---|',
          f"| `{CSV_NAME}` | long format, one row per model attribute: `category, series, series_url, model, attribute, value, also_listed_as, source` ({csv_rows} rows) |",
          f"| `{JSON_NAME}` | categories → series (with HS codes, China tariff codes, Chinese names, applications, source) → models |", '',
          '## Terms', '', d['terms'], '']
    if lic: b += ['## Licence', '', f"[{lic['title']}]({lic['url']}). {lic['summary']}", '']
    b += ['## Updates', '', f"Copied from {site}/data/ when the dataset version changes. For quotations and current specifications, "
          f"contact the company: [{d['publisher']['email']}](mailto:{d['publisher']['email']}).", '']
    return '\n'.join(y + b)


def _upload(a, repo_id, out, d):
    """The execute phase (network). Runs under the time limit; every library error is caught by the caller."""
    from huggingface_hub import HfApi
    try:
        from huggingface_hub import constants as hc
        if getattr(hc, 'ENDPOINT', HF_HOST).rstrip('/') != HF_HOST:
            print(f'✗ refused: huggingface_hub resolved its endpoint to another host — refusing; only {HF_HOST} is allowed'); return 2
    except ImportError:
        pass
    token = os.environ[TOKEN_ENV].strip()
    api = HfApi(endpoint=HF_HOST, token=token)
    if not api.repo_exists(repo_id, repo_type='dataset'):
        if not a.create:
            print(f'✗ refused: {HF_HOST}/datasets/{repo_id} does not exist (or is not visible to this access key) — nothing uploaded; '
                  'it is created only with --create, by hand'); return 2
        api.create_repo(repo_id, repo_type='dataset', exist_ok=True, private=False); print(f'✓ created dataset repository {repo_id}')
    names = list(NAMES)
    diff = changed_files(api, repo_id, out, names)
    if not diff:
        print(f'✓ {HF_HOST}/datasets/{repo_id} up to date (dataset {d["version"]}) — no change, nothing uploaded'); return 0
    info = api.upload_folder(repo_id=repo_id, repo_type='dataset', folder_path=out, allow_patterns=diff,
                             commit_message=f"Dataset {d['version']} (from {d['url']})")
    print(f'✓ uploaded {", ".join(diff)} to {HF_HOST}/datasets/{repo_id} ({getattr(info, "commit_url", "") or "commit"})')
    return 0


def _main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dataset_json'); ap.add_argument('dataset_csv')
    ap.add_argument('--out'); ap.add_argument('--license'); ap.add_argument('--repo-id'); ap.add_argument('--execute', action='store_true')
    ap.add_argument('--create', action='store_true', help='create the Hugging Face repository if it does not exist (by hand only)')
    ap.add_argument('--timeout', type=int, default=240, help='hard limit in seconds for the execute phase (1–3600, default 240)')
    ap.add_argument('--config', default=os.path.join(HERE, 'mirror-config.json'))
    a = ap.parse_args(argv)
    if not 1 <= a.timeout <= 3600: print('✗ --timeout must be 1–3600 seconds'); return 1
    if a.execute and (a.license or a.repo_id): print('✗ refused: --license / --repo-id are dry-run overrides; execute reads mirror-config.json only'); return 2
    cfg = mc.load_config(a.config)
    # an execute takes the licence from the config only ($DATA_LICENSE / --license refused); a dry run may try one
    lic_id = mc.resolve_license(a.license, cfg, strict=a.execute) if (a.license or a.execute) else (cfg.get('data_license') or None)
    if lic_id: mc.resolve_license(lic_id, {})
    hf = cfg.get('huggingface') or {}
    repo_id = a.repo_id or hf.get('repo_id')
    no_lic_ok = (not lic_id) and hf.get('publish_without_licence') is True     # opt-in from the config only (no CLI override)
    pair = mc.validate_pair(a.dataset_json, a.dataset_csv)
    if pair: print('✗ dataset files inconsistent: ' + '; '.join(pair)); return 1
    d = json.load(open(a.dataset_json, encoding='utf-8'))
    dp = dataset_problems(d)
    if dp: print('✗ dataset fields not usable for the card / file names — nothing written: ' + '; '.join(dp)); return 1
    csv_text = open(a.dataset_csv, encoding='utf-8').read()
    lp = mc.license_problem(d, lic_id) if (lic_id or no_lic_ok) else ''
    lic_keys, data_words = licence_in_data(d, csv_text) if not lic_id else ([], [])
    if not lp and lic_keys: lp = f"the dataset has a licence field ({', '.join(lic_keys[:3])}) but the mirror licence is unset"
    if lp and a.execute: print(f'✗ refused: {lp}'); return 2
    if lp: print(f'⚠ {lp} (dry run: continuing)')
    rows = sum(1 for _ in csv.reader(io.StringIO(csv_text))) - 1
    out = a.out or tempfile.mkdtemp(prefix='kelin-hf-')
    if os.path.isdir(out) and set(os.listdir(out)) - set(NAMES):
        print(f'✗ --out {out} is not empty (has {sorted(set(os.listdir(out)) - set(NAMES))[:5]}) — use a new directory; nothing written'); return 1
    os.makedirs(out, exist_ok=True)
    text = card(d, rows, lic_id)
    open(os.path.join(out, 'README.md'), 'w', encoding='utf-8').write(text)
    shutil.copyfile(a.dataset_csv, os.path.join(out, CSV_NAME))
    shutil.copyfile(a.dataset_json, os.path.join(out, JSON_NAME))
    res = mc.check([out]); rc = mc.report(res, 'Hugging Face files gate')
    print(f'files for huggingface.co/datasets/{repo_id or "<repo_id not set>"} (in {out}):')
    for f in sorted(os.listdir(out)): print(f'  {f:36} {os.path.getsize(os.path.join(out, f)):>8} B')
    lic_note = lic_id or ('NOT SET — published without a licence statement (huggingface.publish_without_licence: true)' if no_lic_ok
                          else 'NOT SET (no license field written)')
    print(f"  dataset card: licence {lic_note}; {res['site_links'].get('README.md', 0)} kelintextile.design addresses")
    worded = [] if lic_id else [f'card: {ln}' for ln in licence_wording(text)] + data_words
    if worded:
        print('✗ no licence is set, but the card / dataset reads as a licence / open-data statement — fix the dataset text, nothing uploaded:')
        for ln in worded[:10]: print(f'    {ln[:160]}')
        rc = 1
    tok = os.environ.get(TOKEN_ENV, '').strip()
    missing = [x for x, ok in (('data_license (mirror-config.json)', lic_id or no_lic_ok), ('huggingface.repo_id (mirror-config.json)', repo_id),
                               (f'environment variable {TOKEN_ENV}', tok)) if not ok]
    bad = ([f'{TOKEN_ENV} is not in the form hf_ + at least 20 letters / digits after trimming whitespace (value not shown)']
           if tok and not TOKEN_RE.fullmatch(tok) else []) + env_problems()
    if rc: return 1
    if not a.execute:
        why = ([f"missing: {', '.join(missing)}"] if missing else []) + bad
        print('(dry run: nothing uploaded)' + (f" — execute would refuse now, {'; '.join(why)}" if why
              else f' — execute would upload the files that differ to huggingface.co/datasets/{repo_id}'
                   + (' (creating the repository first if it does not exist: --create)' if a.create else ' (the repository must exist; no --create)')))
        return 0
    if missing: print(f"✗ refused: missing {', '.join(missing)}"); return 2
    if bad: print('✗ refused: ' + '; '.join(bad)); return 2
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'; os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    for v in ('HF_ENDPOINT', 'HUGGINGFACE_CO_STAGING', 'HF_DEBUG', 'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'): os.environ.pop(v, None)
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print('✗ huggingface_hub is not installed in this interpreter — see the install line in this file'); return 1
    alarm = hasattr(signal, 'SIGALRM')
    if alarm:
        def _on_alarm(signum, frame): raise HfTimeout()
        old_handler = signal.signal(signal.SIGALRM, _on_alarm); signal.alarm(a.timeout)
    try:
        return _upload(a, repo_id, out, d)
    except HfTimeout:
        print(f'✗ Hugging Face step timed out after {a.timeout} s — stopped; nothing more is sent (retried next run)'); return 3
    except Exception as ex:                                  # never let a traceback (or a message) carry the token out
        msg = one_line(ex)
        print(f'✗ Hugging Face request failed: {msg}'); return 1
    finally:
        if alarm: signal.alarm(0); signal.signal(signal.SIGALRM, old_handler)


def main(argv=None):
    """_main with every printed byte masked and no traceback: errors become one masked line (exit 1)."""
    token = os.environ.get(TOKEN_ENV, '').strip()
    with scrubbed_output(token):
        try:
            return _main(argv)
        except SystemExit as ex:                           # argparse (int code, message already printed) / load_config (text)
            if ex.code is None or isinstance(ex.code, int): raise
            msg = scrub(ex.code)
            print(msg); return 1
        except KeyboardInterrupt:
            print('✗ interrupted'); return 130
        except Exception as ex:
            msg = one_line(ex)
            print(f'✗ failed: {msg}'); return 1


def _hook(kind):
    def h(*args):
        ex = args[1] if kind == 'sys' else args[0].exc_value     # sys.excepthook(type, value, tb) / thread + unraisable: .exc_value
        sys.__stderr__.write(f'✗ {kind} error: {one_line(ex)}\n')
    return h


if __name__ == '__main__':
    sys.excepthook = _hook('sys')                            # last resort: no traceback is ever printed
    import threading
    threading.excepthook = _hook('thread'); sys.unraisablehook = _hook('unraisable')
    sys.exit(main())
