#!/usr/bin/env python3
"""Personal IPTV builder: M3U + XMLTV with cautious metadata lookup.
Requires Python 3.11+, standard library only. Sources must be authorized.
"""
import concurrent.futures
import gzip
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
OUT = ROOT / 'output'
OUT.mkdir(exist_ok=True)
STATE_FILE = ROOT / 'state.json'
AGENT = 'PersonalIPTVPlaylistBuilder/2.1'


def fetch(url, limit=5_000_000, timeout=15):
    if not url.startswith(('http://', 'https://')):
        raise ValueError('Only HTTP(S) sources are supported')
    req = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Response too large')
        return data


def attrs(line):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', line))


def parse_m3u(contents, allow_bare=False):
    lines = contents.decode('utf-8-sig', errors='replace').splitlines()
    header = next((line for line in lines if line.strip()), '')
    attr = attrs(header) if header.startswith('#EXTM3U') else {}
    epgs = [u.strip() for u in (attr.get('x-tvg-url') or attr.get('url-tvg') or '').split(',') if u.strip()]
    found, ext = [], None
    for line in lines:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            ext = line
        elif line.startswith(('https://', 'http://')):
            if ext is None and not allow_bare:
                continue
            meta = attrs(ext or '')
            label = ext.split(',', 1)[1].strip() if ext and ',' in ext else ''
            found.append({'url': line, 'name': label, 'tvg_id': meta.get('tvg-id', ''),
                          'logo': meta.get('tvg-logo', ''), 'group': meta.get('group-title', '')})
            ext = None
    return found, epgs


def normalize_url(url):
    """Preserve query credentials/tokens; only remove URL fragment."""
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ''))


def normalize_name(s):
    return re.sub(r'[^a-z0-9]+', '', s.casefold())


def enrich_custom(custom, catalogue):
    """Only EXACT stream URL matches inherit metadata; never guess another channel."""
    by_url = {normalize_url(e['url']): e for e in catalogue}
    results = []
    for i, user_entry in enumerate(custom, 1):
        match = by_url.get(normalize_url(user_entry['url']))
        item = dict(user_entry)
        if match:
            for field in ('name', 'logo', 'tvg_id', 'group'):
                if not item.get(field):
                    item[field] = match.get(field, '')
            print(f'Custom entry {i}: exact URL matched; EPG ID = {item.get("tvg_id") or "none"}')
        else:
            if not item.get('name'):
                item['name'] = f'Custom Stream {i}'
            if not item.get('group'):
                item['group'] = 'Custom | Unclassified'
            print(f'Custom entry {i}: no reliable metadata match (kept as custom)')
        results.append(item)
    return results


def check(entry):
    try:
        req = urllib.request.Request(entry['url'], headers={'User-Agent': AGENT,
                                     'Accept': '*/*', 'Range': 'bytes=0-1023'})
        with urllib.request.urlopen(req, timeout=CFG.get('timeout_seconds', 9)) as resp:
            data = resp.read(2048)
            kind = resp.headers.get('Content-Type', '').lower()
            if resp.status not in (200, 206) or not data:
                return False
            if 'html' in kind or data.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                return False
            if '.m3u8' in entry['url'].lower() or 'mpegurl' in kind:
                return b'#EXTM3U' in data or b'#EXT-X-' in data
            return True
    except (ValueError, OSError, TimeoutError, urllib.error.URLError):
        return False


def safe(s):
    return str(s).replace('"', "'").replace('\r', ' ').replace('\n', ' ')


def write_m3u(entries, epg_link):
    head = '#EXTM3U' + (f' x-tvg-url="{epg_link}"' if epg_link else '')
    lines = [head]
    for e in entries:
        parameters = [f'group-title="{safe(e.get("group", ""))}"']
        for attr, field in [('tvg-id', 'tvg_id'), ('tvg-logo', 'logo')]:
            if e.get(field):
                parameters.append(f'{attr}="{safe(e[field])}"')
        lines += [f'#EXTINF:-1 {" ".join(parameters)},{safe(e.get("name") or "Unnamed")}', e['url']]
    (OUT / 'MyIPTV.m3u').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def canonical_id(channel_id):
    return channel_id.split('@', 1)[0].strip().casefold()


def epg_update(urls, live_ids):
    """Merge matching programmes across feeds; preserve last nonempty guide on failure."""
    guide = OUT / 'guide.xml'
    merged = ET.Element('tv', {'generator-info-name': 'Personal IPTV EPG Merger'})
    ids = {i: canonical_id(i) for i in live_ids if i}
    added_channels, seen_programmes = set(), set()
    total, successful = 0, 0
    for url in dict.fromkeys(urls):
        new_programmes, mapped = 0, set()
        try:
            raw = fetch(url, limit=int(CFG.get('epg_max_mb', 18))*1024*1024, timeout=25)
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
                if len(raw) > 65*1024*1024:
                    raise ValueError('Decompressed guide too large')
            root = ET.fromstring(raw)
            if root.tag != 'tv':
                raise ValueError('Not XMLTV')
            source_channels = {c.attrib.get('id', ''): c for c in root.findall('channel')}
            # Match only exact IDs, or a UNIQUE ID when resolution suffix differs.
            mapping = {}
            for source_id in source_channels:
                if source_id in ids:
                    mapping[source_id] = source_id
                else:
                    matches = [id_ for id_, base in ids.items() if base == canonical_id(source_id)]
                    if len(matches) == 1:
                        mapping[source_id] = matches[0]
            selected = []
            for programme in root.findall('programme'):
                original = programme.attrib.get('channel', '')
                dest = mapping.get(original)
                if not dest:
                    continue
                key = (dest, programme.attrib.get('start', ''), programme.attrib.get('stop', ''),
                       (programme.findtext('title') or '').strip())
                if key in seen_programmes:
                    continue
                seen_programmes.add(key)
                selected.append((programme, dest))
                mapped.add(dest)
            for original, dest in mapping.items():
                if dest in mapped and dest not in added_channels:
                    c = ET.fromstring(ET.tostring(source_channels[original]))
                    c.set('id', dest)
                    merged.append(c)
                    added_channels.add(dest)
            for programme, dest in selected:
                p = ET.fromstring(ET.tostring(programme))
                p.set('channel', dest)
                merged.append(p)
                new_programmes += 1
            if new_programmes:
                successful += 1
                total += new_programmes
            print(f'EPG source {url}: mapped_channels={len(mapped)}, added_programmes={new_programmes}')
        except Exception as exc:
            print(f'EPG source failed {url}: {exc}')
    if total:
        ET.ElementTree(merged).write(guide, encoding='utf-8', xml_declaration=True)
    elif not guide.exists():
        guide.write_text('<?xml version="1.0" encoding="utf-8"?>\n<tv/>\n', encoding='utf-8')
    print(f'EPG: merged {total} programmes from {successful} feeds')
    return total > 0 or (guide.exists() and b'<programme' in guide.read_bytes())


def main():
    try:
        old = json.loads(STATE_FILE.read_text('utf-8')) if STATE_FILE.exists() else {}
    except (json.JSONDecodeError, OSError):
        old = {}
    catalogue, discovered_epg, successful_sources = [], [], 0
    for source in CFG.get('playlist_sources', []):
        try:
            items, epgs = parse_m3u(fetch(source['url']))
            count = max(0, int(source.get('max_entries', 100)))
            selected = items[:count]
            for e in selected:
                e['group'] = f'{source.get("group_prefix", "Live TV")} | {e["group"] or "Live TV"}'
            catalogue.extend(selected)
            discovered_epg.extend(epgs)
            successful_sources += 1
            print(f'Source {source["url"]}: read {len(items)}, selected {len(selected)}')
        except Exception as exc:
            print(f'Source failed {source["url"]}: {exc}')
    custom_file = ROOT / CFG.get('custom_entries_file', 'custom_entries.m3u')
    custom = []
    if custom_file.exists():
        custom, _ = parse_m3u(custom_file.read_bytes(), allow_bare=True)
    entries = catalogue + enrich_custom(custom, catalogue)
    if not entries and successful_sources == 0:
        print('All sources unavailable: previous files retained')
        return
    unique = {}
    for entry in entries:
        # Custom entries override catalogue metadata for the same URL.
        unique[entry['url']] = entry
    current = list(unique.values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(CFG.get('max_concurrent_checks', 12))) as pool:
        checked = list(pool.map(check, current))
    new_state, kept = {}, []
    threshold = max(1, int(CFG.get('failure_threshold', 3)))
    for entry, ok in zip(current, checked):
        url = entry['url']
        prev = old.get(url, {})
        failures = 0 if ok else int(prev.get('failures', 0)) + 1
        ever_good = bool(ok or prev.get('ever_good', False))
        new_state[url] = {'failures': failures, 'ever_good': ever_good,
                          'entry': entry, 'last_checked': datetime.now(timezone.utc).isoformat()}
        if ever_good and failures < threshold:
            kept.append(entry)
    for url, prev in old.items():
        if url not in new_state and prev.get('ever_good') and int(prev.get('failures', 0)) < threshold:
            new_state[url] = prev
            kept.append(prev['entry'])
    kept = list({x['url']: x for x in kept}.values())
    live_ids = {e['tvg_id'] for e in kept if e.get('tvg_id') and
                not e.get('group', '').casefold().startswith(('movies', 'series'))}
    urls = list(CFG.get('epg_urls', []))
    if CFG.get('epg_auto_discover', True):
        urls += discovered_epg[:2]
    ready = epg_update(urls, live_ids)
    repo = os.getenv('GITHUB_REPOSITORY', '')
    branch = os.getenv('GITHUB_REF_NAME', 'main')
    epg_link = f'https://raw.githubusercontent.com/{repo}/{branch}/output/guide.xml' if repo and ready else ''
    write_m3u(kept, epg_link)
    STATE_FILE.write_text(json.dumps(new_state, indent=2, ensure_ascii=False) + '\n', 'utf-8')
    print(f'Candidates: {len(current)} | retained: {len(kept)} | EPG ready: {ready}')


if __name__ == '__main__':
    main()
