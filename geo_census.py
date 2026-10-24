#!/usr/bin/env python3
"""
Samsung TV Plus geo census - which channels play from OUTSIDE their country.

Run from a non-Samsung country (the workflow tunnels the runner through a Proton
WireGuard exit, secret WG_CENSUS). For every channel: ask jmp2.uk for the real
stream address (redirect, not followed), then request that address and record the
HTTP code. 200 = plays from abroad; anything else = geo-blocked (or dead).

Output: geo_census.tsv   id <TAB> code <TAB> video host <TAB> name
(the same format the phone script ~/stvpcensus.sh produced; generate_samsung_tvplus.py
 reads it to decide which channels get a VPN-free swap)
"""
import gzip, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from urllib.parse import urlparse

import requests

APP_URL   = 'https://i.mjh.nz/SamsungTVPlus/.channels.json.gz'
JMP2      = 'https://jmp2.uk/{slug}'
OUT       = 'geo_census.tsv'
WORKERS   = 8
TIMEOUT   = 15
UA        = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36'
# Samsung countries: censusing from inside any of these would mark that country's
# channels as fine when they're not, so refuse to run there.
SAMSUNG_COUNTRIES = {'US', 'CA', 'GB', 'DE', 'AT', 'CH', 'FR', 'ES', 'IT', 'IN', 'KR'}
MIN_COVERAGE = 0.8    # don't overwrite the census if fewer than 80% of channels answered


def exit_country():
    try:
        return requests.get('https://ipinfo.io/country', timeout=15).text.strip().upper()
    except Exception:
        return ''


def check(slug):
    s = requests.Session(); s.headers['User-Agent'] = UA
    try:
        r = s.get(JMP2.format(slug=slug), allow_redirects=False, timeout=TIMEOUT)
        loc = r.headers.get('Location', '')
        if not loc:
            return '000', ''
        host = urlparse(loc).netloc
        try:
            code = str(s.get(loc, timeout=TIMEOUT, stream=True).status_code)
        except Exception:
            code = '000'
        return code, host
    except Exception:
        return '000', ''
    finally:
        s.close()


def main():
    cc = exit_country()
    print(f'Exit country: {cc or "unknown"}')
    if not cc or cc in SAMSUNG_COUNTRIES:
        sys.exit(f'ABORT: census must run from outside the Samsung countries (got {cc or "unknown"}). '
                 'Is the WireGuard tunnel up?')

    r = requests.get(APP_URL, timeout=30); r.raise_for_status()
    data = json.loads(gzip.GzipFile(fileobj=BytesIO(r.content)).read())
    chans = {}
    for region in data['regions'].values():
        for cid, ch in region.get('channels', {}).items():
            chans.setdefault(cid, ch.get('name', ''))
    slug_fmt = data['slug']
    print(f'Checking {len(chans)} channels with {WORKERS} workers ...')

    t0, rows = time.time(), []
    with ThreadPoolExecutor(WORKERS) as pool:
        futs = {pool.submit(check, slug_fmt.format(id=cid)): cid for cid in chans}
        for i, f in enumerate(as_completed(futs), 1):
            cid = futs[f]; code, host = f.result()
            rows.append((cid, code, host, chans[cid].replace('\t', ' ')))
            if i % 250 == 0:
                print(f'  {i}/{len(chans)}')
    answered = sum(1 for r in rows if r[1] != '000')
    ok = sum(1 for r in rows if r[1] == '200')
    print(f'Done in {time.time()-t0:.0f}s: {ok} play from abroad, {len(rows)-ok} blocked/dead, '
          f'{len(rows)-answered} no answer')

    if answered < MIN_COVERAGE * len(rows):
        sys.exit(f'ABORT: only {answered}/{len(rows)} channels answered - keeping the old {OUT}')
    rows.sort()
    with open(OUT, 'w', encoding='utf-8') as f:
        for row in rows:
            f.write('\t'.join(row) + '\n')
    print(f'Wrote {OUT}')


if __name__ == '__main__':
    main()
