#!/usr/bin/env python3
"""
Samsung TV Plus M3U and EPG Generator for TiviMate
Generates static .m3u playlist and .xml EPG files for use with TiviMate IPTV player
"""

import os
import re
import json
import gzip
import requests
from datetime import datetime
from io import BytesIO
from urllib.parse import quote

# Configuration
APP_URL = 'https://i.mjh.nz/SamsungTVPlus/.channels.json.gz'
EPG_URL = 'https://i.mjh.nz/SamsungTVPlus/{region}.xml.gz'
PLAYBACK_URL = 'https://jmp2.uk/{slug}'
TIMEOUT = (10, 30)  # connect, read timeouts
OUTPUT_DIR = 'output'
PLAYLIST_FILE = 'samsung_tvplus.m3u'
EPG_FILE = 'samsung_tvplus.xml'

# Default settings - can be overridden by environment variables
DEFAULT_REGIONS = os.getenv('REGIONS', 'all')  # comma-separated regions or 'all'
DEFAULT_GROUPS = os.getenv('GROUPS', '')  # comma-separated groups to include (empty = all)
INCLUDE_DRM = os.getenv('INCLUDE_DRM', 'true').lower() == 'true'
START_CHNO = int(os.getenv('START_CHNO', '1'))  # Starting channel number
SORT_BY = os.getenv('SORT_BY', 'chno')  # 'chno' or 'name'
# group-title: 'country' (default) = one group per country, e.g. "United States";
#              'country_genre' = "United States | News"; 'genre' = Samsung's own genre
GROUP_BY = os.getenv('GROUP_BY', 'country')
# The old Google-Script playlist never tagged names with " [DRM]"; adding it would rename
# those channels and break TiviMate favourites, so it's off by default.
DRM_NAME_TAG = os.getenv('DRM_NAME_TAG', 'false').lower() == 'true'

REGION_NAMES = {
    'us': 'United States', 'ca': 'Canada', 'gb': 'United Kingdom', 'de': 'Germany',
    'at': 'Austria', 'ch': 'Switzerland', 'fr': 'France', 'es': 'Spain', 'it': 'Italy',
    'in': 'India', 'kr': 'South Korea',
}
# ── VPN-free swaps ───────────────────────────────────────────────────────────
# Channels that won't play from outside their country (per GEO_CENSUS, made on
# Taqi's phone in Jeddah with the VPN off by ~/stvpcensus.sh) get their stream
# swapped for the SAME channel (exact name match) from a source that plays
# VPN-free: Plex direct links, AltPluto, Rockyou/Roku. tvg-id stays Samsung's, so
# the Samsung guide still applies. Anything unmatched keeps its jmp2.uk link.
VPN_FREE_SWAPS = os.getenv('VPN_FREE_SWAPS', 'true').lower() == 'true'
GEO_CENSUS = 'geo_census.tsv'          # id <TAB> http code <TAB> host <TAB> name
PLEX_DIRECT_URL = 'https://raw.githubusercontent.com/s-digweed/PlexTV/main/direct_urls.json'
ALTPLUTO_URL = 'https://raw.githubusercontent.com/s-digweed/AltPluto/main/output/pluto_{cc}.m3u'
ROKU_URL = 'https://raw.githubusercontent.com/s-digweed/Rockyou/main/roku.m3u'
# Samsung region -> AltPluto playlist (same-language feed first, US as fallback)
ALTPLUTO_REGION = {'us': 'us', 'ca': 'ca', 'gb': 'uk', 'de': 'de', 'at': 'de', 'ch': 'de',
                   'fr': 'fr', 'es': 'es', 'it': 'it'}
SWAP_REPORT = 'vpn_free_swaps.txt'

# playlist order of the countries (anything else follows alphabetically)
REGION_ORDER = ['us', 'ca', 'gb', 'de', 'at', 'ch', 'fr', 'es', 'it', 'in', 'kr']

def download_and_decompress(url):
    """Download and decompress gzipped content from URL"""
    print(f"Downloading {url}...")
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    
    # Decompress gzipped content
    with gzip.GzipFile(fileobj=BytesIO(response.content)) as gz:
        return gz.read()

def get_channel_data():
    """Download and parse Samsung TV Plus channel data"""
    json_data = download_and_decompress(APP_URL)
    return json.loads(json_data)

def get_epg_data(region='all'):
    """Download EPG data for specified region"""
    url = EPG_URL.format(region=region)
    return download_and_decompress(url)

def filter_channels(data, regions, groups):
    """Filter channels based on regions and groups"""
    if regions == ['all']:
        regions = list(data['regions'].keys())
    
    channels = {}
    for region in regions:
        if region in data['regions']:
            region_channels = data['regions'][region].get('channels', {})
            for cid, ch in region_channels.items():
                if cid not in channels:            # first region listed keeps the channel
                    channels[cid] = {**ch, '_region': region}
    
    # Filter by groups if specified
    if groups:
        filtered_channels = {}
        for channel_id, channel in channels.items():
            if channel.get('group', '').lower() in [g.lower() for g in groups]:
                filtered_channels[channel_id] = channel
        channels = filtered_channels
    
    return channels


# ── VPN-free swap helpers ────────────────────────────────────────────────────
def _norm_name(name):
    name = re.sub(r'\s*\[DRM\]', '', name or '').lower().replace('&', 'and')
    name = re.sub(r'\bpluto tv\b', '', name)
    return re.sub(r'[^a-z0-9]', '', name)


def _m3u_name_map(text):
    """{normalized channel name: stream url} from an M3U (first entry wins)."""
    out, lines = {}, (text or '').splitlines()
    for i, line in enumerate(lines):
        if line.startswith('#EXTINF') and i + 1 < len(lines) and lines[i + 1].startswith('http'):
            m = re.search(r'"\s*,(.*)$', line)          # display name = text after the last attribute
            name = m.group(1) if m else line.split(',', 1)[-1]
            out.setdefault(_norm_name(name), lines[i + 1].strip())
    return out


def _get_text(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        return r.text if r.status_code == 200 else None
    except Exception as e:
        print(f"  swap source unavailable: {url} ({e})")
        return None


CENSUS = {}   # id -> (http code, video host), filled by load_geo_blocked()


def load_geo_blocked():
    """Samsung channel ids that did NOT return 200 from outside their country."""
    try:
        with open(GEO_CENSUS, encoding='utf-8') as f:
            rows = [l.rstrip('\n').split('\t') for l in f if l.strip()]
    except FileNotFoundError:
        print(f"  no {GEO_CENSUS} in repo - VPN-free swaps skipped")
        return set()
    for r in rows:
        if len(r) > 2:
            CENSUS[r[0]] = (r[1], r[2])
    return {r[0] for r in rows if len(r) > 1 and r[1] != '200'}


def load_swap_sources():
    plex = {}
    txt = _get_text(PLEX_DIRECT_URL)
    if txt:
        try:
            for v in json.loads(txt).values():
                if v.get('url') and v.get('name'):
                    plex.setdefault(_norm_name(v['name']), v['url'])
        except Exception as e:
            print(f"  Plex direct_urls.json unreadable ({e})")
    pluto = {cc: _m3u_name_map(_get_text(ALTPLUTO_URL.format(cc=cc)))
             for cc in sorted(set(ALTPLUTO_REGION.values()))}
    roku = _m3u_name_map(_get_text(ROKU_URL))
    print(f"  swap sources: Plex {len(plex)}, AltPluto {sum(len(v) for v in pluto.values())}, Roku {len(roku)}")
    return plex, pluto, roku


def find_swaps(channel, sources):
    """Every VPN-free copy of this channel, best source first: [(url, label), ...]."""
    plex, pluto, roku = sources
    key = _norm_name(channel['name'])
    out = []
    if not key:
        return out
    if key in plex:
        out.append((plex[key], 'Plex'))
    region = channel.get('_region', '')
    for cc in dict.fromkeys([ALTPLUTO_REGION.get(region), 'us']):
        if cc and key in pluto.get(cc, {}):
            out.append((pluto[cc][key], f'AltPluto {cc}'))
            break
    if key in roku:
        out.append((roku[key], 'Roku'))
    return out


# ── quality guard: never swap Samsung's picture for a softer one ─────────────
QUALITY_CACHE = os.path.join(OUTPUT_DIR, 'swap_quality.json')
QUALITY_TTL_H = 72          # re-measure a stream after this many hours
SAMSUNG_DEFAULT_H = 1080    # Samsung is FHD; assume 1080 when its playlist doesn't say
QUALITY_WORKERS = 8


def _top_height(url):
    """Tallest RESOLUTION listed in an HLS master playlist (follows redirects), or None."""
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200 or '#EXTM3U' not in r.text[:200]:
            return None
        hs = [int(h) for h in re.findall(r'RESOLUTION=\d+x(\d+)', r.text)]
        return max(hs) if hs else None
    except Exception:
        return None


def pick_swaps(data, channels, blocked, sources):
    """{channel_id: (url, label)} for swaps that are NOT a quality downgrade,
    and {channel_id: reason} for blocked channels that keep their Samsung link."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    try:
        with open(QUALITY_CACHE, encoding='utf-8') as f:
            qc = json.load(f)
    except Exception:
        qc = {}
    now = time.time()
    cands = {cid: find_swaps(channels[cid], sources) for cid in blocked if cid in channels}
    cands = {cid: c for cid, c in cands.items() if c}

    def cache_key(kind, cid, label=''):
        return f"{kind}|{cid}|{label}"
    todo = {}
    for cid, lst in cands.items():
        todo[cache_key('orig', cid)] = PLAYBACK_URL.format(slug=data['slug'].format(id=cid))
        for url, label in lst:
            todo[cache_key('alt', cid, label)] = url
    todo = {k: u for k, u in todo.items()
            if k not in qc or now - qc[k].get('t', 0) > QUALITY_TTL_H * 3600}
    if todo:
        print(f"  measuring picture quality of {len(todo)} streams ...")
        with ThreadPoolExecutor(QUALITY_WORKERS) as pool:
            for k, h in zip(todo, pool.map(_top_height, todo.values())):
                qc[k] = {'h': h, 't': now}
    live = set()
    for cid, lst in cands.items():
        live.add(cache_key('orig', cid)); live.update(cache_key('alt', cid, l) for _, l in lst)
    qc = {k: v for k, v in qc.items() if k in live}
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(QUALITY_CACHE, 'w', encoding='utf-8') as f:
        json.dump(qc, f, separators=(',', ':'))

    chosen, kept = {}, {}
    for cid in blocked:
        lst = cands.get(cid)
        if not lst:
            kept[cid] = 'no VPN-free copy'
            continue
        orig_h = (qc.get(cache_key('orig', cid)) or {}).get('h') or SAMSUNG_DEFAULT_H
        best_seen = None
        for url, label in lst:
            h = (qc.get(cache_key('alt', cid, label)) or {}).get('h')
            if h and h >= orig_h:
                chosen[cid] = (url, f'{label} {h}p')
                break
            if h and (best_seen is None or h > best_seen):
                best_seen = h
        else:
            kept[cid] = (f'VPN-free copy only {best_seen}p, Samsung {orig_h}p' if best_seen
                         else 'VPN-free copy, quality unknown')
    print(f"  quality guard: {len(chosen)} swaps kept, "
          f"{sum(1 for r in kept.values() if r != 'no VPN-free copy')} skipped as a downgrade/unknown")
    return chosen, kept


def generate_m3u_playlist(data, channels, blocked=frozenset(), choices=None, report=None, status=None):
    """Generate M3U playlist content"""
    lines = ['#EXTM3U']
    
    # Sort channels: by country first (REGION_ORDER), then chno or name within it
    def region_rank(ch):
        r = ch.get('_region', '')
        return (REGION_ORDER.index(r) if r in REGION_ORDER else len(REGION_ORDER), r)
    if SORT_BY == 'name':
        sorted_channels = sorted(channels.items(), key=lambda x: (region_rank(x[1]), x[1]['name'].strip().lower()))
    else:
        sorted_channels = sorted(channels.items(), key=lambda x: (region_rank(x[1]), x[1].get('chno', 999999)))
    
    current_chno = START_CHNO
    
    for channel_id, channel in sorted_channels:
        # Skip DRM channels if not including them
        if channel.get('license_url') and not INCLUDE_DRM:
            continue
        
        name = channel['name']
        logo = channel['logo']
        region = channel.get('_region', '')
        # Same country names as the old Google-Script playlist (Matt's own region names),
        # so TiviMate favourites / custom groups keep matching. Fallback: our table.
        country = (data.get('regions', {}).get(region, {}).get('name')
                   or REGION_NAMES.get(region, region.upper() or 'Other'))
        if GROUP_BY == 'genre':
            group = channel['group']
        elif GROUP_BY == 'country_genre':
            group = f"{country} | {channel['group']}"
        else:
            group = country
        group = group.replace('"', "'")
        url = PLAYBACK_URL.format(slug=data['slug'].format(id=channel_id))
        swapped, why = None, ''
        if choices and channel_id in blocked:
            chosen, kept = choices
            if channel_id in chosen:
                url, swapped = chosen[channel_id]
            else:
                why = kept.get(channel_id, '')
            if report is not None:
                report.append((channel.get('_region', ''), channel['name'], swapped or '-', why))

        if status is not None:
            status[channel_id] = (swapped, channel_id in blocked, why)

        # Add DRM indicator if applicable (a swapped-in copy is never DRM)
        drm = bool(channel.get('license_url')) and not swapped
        if drm and DRM_NAME_TAG:
            name = f"{name} [DRM]"
        
        # Use original channel number or sequential numbering
        if channel.get('chno') and START_CHNO == 1:
            chno = channel['chno']
        else:
            chno = current_chno
            current_chno += 1
        
        # Generate M3U entry
        extinf_line = f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{name}" tvg-logo="{logo}" group-title="{group}" tvg-chno="{chno}"'
        
        # Add DRM attributes if applicable
        if drm:
            extinf_line += f' drm="true" license-url="{channel["license_url"]}"'
        
        extinf_line += f',{name}'
        
        lines.extend([extinf_line, url])
    
    return '\n'.join(lines)

def save_files(playlist_content, epg_content):
    """Save M3U and EPG files to output directory"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Save M3U playlist
    playlist_path = os.path.join(OUTPUT_DIR, PLAYLIST_FILE)
    with open(playlist_path, 'w', encoding='utf-8') as f:
        f.write(playlist_content)
    print(f"Saved playlist: {playlist_path}")
    
    # Save EPG XML
    epg_path = os.path.join(OUTPUT_DIR, EPG_FILE)
    with open(epg_path, 'wb') as f:
        f.write(epg_content)
    print(f"Saved EPG: {epg_path}")
    
    return playlist_path, epg_path

def _host_family(host):
    for key, label in (('samsungtv.plus', 'Samsung'), ('amagi', 'Amagi'), ('pluto', 'Pluto'),
                       ('wurl', 'Wurl'), ('cloudfront', 'CloudFront'), ('akamai', 'Akamai'),
                       ('rakuten', 'Rakuten'), ('its-newid', 'NEW ID'), ('stingray', 'Stingray')):
        if key in (host or ''):
            return label
    return host or '?'


def write_status(channels, status, report=None):
    from status_page import update_status, epg_stats
    regions = {}
    for cid, ch in channels.items():
        swapped, is_blocked, why = status.get(cid, (None, False, ''))
        code, host = CENSUS.get(cid, (None, None))
        if swapped:
            st, detail = 'free', f'swapped · {swapped}'
        elif code == '200':
            st, detail = 'free', 'plays abroad'
        elif code is not None:
            st = 'blocked'
            detail = f'{_host_family(host)} · {"no answer" if code == "000" else code}'
            if why and why != 'no VPN-free copy':
                detail += f' · {why}'
        else:
            st, detail = 'unknown', 'not in census yet'
        if ch.get('license_url') and not swapped:
            detail += ' · DRM'
        r = ch.get('_region', '')
        regions.setdefault(r, {'name': REGION_NAMES.get(r, r.upper()), 'channels': []})['channels'].append(
            {'id': cid, 'name': ch['name'], 'logo': ch.get('logo', ''), 'group': ch.get('group', ''),
             'status': st, 'detail': detail})
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    notes = []
    if CENSUS:
        notes.append(f"Geo census: {sum(1 for c, _ in CENSUS.values() if c == '200')} of {len(CENSUS)} "
                     "channels play from abroad as-is")
    else:
        notes.append(f"No {GEO_CENSUS} yet - every channel shows as unknown")
    if report:
        sw = sum(1 for r in report if r[2] != '-')
        kept = sum(1 for r in report if r[2] == '-' and r[3].startswith('VPN-free copy'))
        notes.append(f"{sw} blocked channels swapped to a VPN-free copy; "
                     f"{kept} kept on Samsung FHD instead of a softer copy")
    health = {
        'repo': 's-digweed/SamsungTVPlus', 'every_h': 1,
        'm3u_updated': now,
        'epg_updated': now,
        'notes': notes,
    }
    health.update(epg_stats(os.path.join(OUTPUT_DIR, EPG_FILE)))
    update_status('Samsung TV Plus', regions,
                  os.path.join(OUTPUT_DIR, 'status.json'), os.path.join(OUTPUT_DIR, 'status_state.json'),
                  health=health)


def write_swap_report(report, total):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    swapped = [r for r in report if r[2] != '-']
    from collections import Counter
    by_src = Counter(r[2].split()[0] for r in swapped)
    lines = [f"Samsung TV Plus VPN-free swaps ({datetime.utcnow():%Y-%m-%d %H:%M} UTC)",
             f"Channels: {total}   geo-blocked (per {GEO_CENSUS}): {len(report)}   "
             f"swapped to a VPN-free copy: {len(swapped)}   still need VPN: {len(report) - len(swapped)} "
             f"({sum(1 for r in report if r[2] == '-' and r[3].startswith('VPN-free copy'))} of them kept at "
             f"Samsung quality instead of a softer copy)",
             "By source: " + ", ".join(f"{k} {v}" for k, v in by_src.most_common()), ""]
    for region in REGION_ORDER + sorted({r[0] for r in report} - set(REGION_ORDER)):
        rows = [r for r in report if r[0] == region]
        if not rows:
            continue
        lines.append(f"[{REGION_NAMES.get(region, region.upper())}]  swapped {sum(1 for r in rows if r[2] != '-')}/{len(rows)}")
        lines += [f"  {'+' if src != '-' else ' '} {name}  ({src if src != '-' else (why or '-')})"
                  for _, name, src, why in sorted(rows, key=lambda r: (r[2] == '-', r[1].lower()))]
        lines.append("")
    with open(os.path.join(OUTPUT_DIR, SWAP_REPORT), 'w', encoding='utf-8') as f:
        f.write("\n".join(lines) + "\n")
    print(lines[1]); print(lines[2])


def generate_readme():
    """Generate README with usage information"""
    readme_content = f"""# Samsung TV Plus for TiviMate

Generated on: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC

## Files

- `{PLAYLIST_FILE}` - M3U playlist file for TiviMate
- `{EPG_FILE}` - EPG (Electronic Program Guide) XML file

## TiviMate Setup

1. In TiviMate, go to Settings → Playlists
2. Add a new playlist using the raw GitHub URL for the M3U file:
   ```
   https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/output/{PLAYLIST_FILE}
   ```
3. For EPG, go to Settings → EPG and add:
   ```
   https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/output/{EPG_FILE}
   ```

## Configuration

This playlist was generated with the following settings:
- Regions: {DEFAULT_REGIONS}
- Groups: {DEFAULT_GROUPS if DEFAULT_GROUPS else 'All'}
- Include DRM: {INCLUDE_DRM}
- Starting Channel Number: {START_CHNO}
- Sort By: {SORT_BY}

## Automatic Updates

This playlist is automatically updated daily at midnight UTC via GitHub Actions.

## Notes

- DRM-protected channels are marked with [DRM] suffix
- Some channels may require compatible players that support DRM
- EPG data is region-specific and updates daily
"""
    
    readme_path = os.path.join(OUTPUT_DIR, 'README.md')
    with open(readme_path, 'w', encoding='utf-8') as f:
        f.write(readme_content)
    print(f"Generated README: {readme_path}")

def main():
    """Main execution function"""
    print("Samsung TV Plus M3U/EPG Generator for TiviMate")
    print("=" * 50)
    
    try:
        # Parse configuration
        regions = [r.strip() for r in DEFAULT_REGIONS.split(',') if r.strip()]
        groups = [g.strip() for g in DEFAULT_GROUPS.split(',') if g.strip()] if DEFAULT_GROUPS else []
        
        print(f"Regions: {regions}")
        print(f"Groups: {groups if groups else 'All'}")
        print(f"Include DRM: {INCLUDE_DRM}")
        print(f"Sort by: {SORT_BY}")
        print()
        
        # Get channel data
        data = get_channel_data()
        print(f"Downloaded channel data with {len(data.get('regions', {}))} regions")
        
        # Filter channels
        channels = filter_channels(data, regions, groups)
        print(f"Filtered to {len(channels)} channels")
        
        # VPN-free swaps for channels geo-blocked outside their country
        blocked, sources, report = set(), None, []
        if VPN_FREE_SWAPS:
            blocked = load_geo_blocked()
            if blocked:
                sources = load_swap_sources()

        # Generate M3U playlist
        choices = pick_swaps(data, channels, blocked, sources) if sources else None
        status = {}
        playlist_content = generate_m3u_playlist(data, channels, blocked, choices, report, status)
        if sources:
            write_swap_report(report, len(channels))
        
        # Get EPG data (use first region or 'all')
        epg_region = regions[0] if len(regions) == 1 and regions[0] != 'all' else 'all'
        epg_content = get_epg_data(epg_region)
        print(f"Downloaded EPG data for region: {epg_region}")
        
        # Save files
        playlist_path, epg_path = save_files(playlist_content, epg_content)
        
        # Status page data (output/status.json for status.html)
        try:
            write_status(channels, status, report)
        except Exception as e:
            print(f"Status page step failed ({e}) - continuing")

        # Generate README
        generate_readme()
        
        print("\nGeneration completed successfully!")
        print(f"Files saved in '{OUTPUT_DIR}' directory")
        
        # Print statistics
        playlist_lines = playlist_content.count('\n')
        channel_count = playlist_content.count('#EXTINF')
        drm_count = playlist_content.count('[DRM]')
        
        print(f"\nStatistics:")
        print(f"- Total channels: {channel_count}")
        print(f"- DRM channels: {drm_count}")
        print(f"- Playlist lines: {playlist_lines}")
        print(f"- EPG size: {len(epg_content):,} bytes")
        
    except Exception as e:
        print(f"Error: {e}")
        raise

if __name__ == '__main__':
    main()
