#!/usr/bin/env python3
"""Daily M3U builder. Standard-library only. Public/source list != rights guarantee."""
import concurrent.futures
import gzip
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text('utf-8'))
OUT = ROOT / 'output'
OUT.mkdir(exist_ok=True)
STATE_FILE = ROOT / 'state.json'
AGENT = 'PersonalIPTVPlaylistBuilder/1.0 (stream availability checker)'


def fetch(url, limit=5_000_000, timeout=15):
    if not url.startswith(('https://', 'http://')):
        raise ValueError('URL must be HTTP(S)')
    req = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Response too large')
        return data


def attrs(ext):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', ext))


def parse_m3u(contents):
    lines = contents.decode('utf-8-sig', 'replace').splitlines()
    epg_urls = []
    if lines and lines[0].startswith('#EXTM3U'):
        epg_header = attrs(lines[0]).get('x-tvg-url', '') or attrs(lines[0]).get('url-tvg', '')
        epg_urls = [u.strip() for u in epg_header.split(',') if u.strip()]
    result, ext = [], None
    for line in lines:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            ext = line
        elif line.startswith(('https://', 'http://')) and ext:
            meta = attrs(ext)
            name = ext.split(',', 1)[-1].strip() if ',' in ext else 'Unnamed'
            result.append({'url': line, 'name': name, 'tvg_id': meta.get('tvg-id',''),
                           'logo': meta.get('tvg-logo',''), 'group': meta.get('group-title','')})
            ext = None
    return result, epg_urls


def check(entry):
    """Positive probe validates accessible HLS manifest/media bytes, not end-to-end playback."""
    url = entry['url']
    try:
        req = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Range': 'bytes=0-1023',
                                                  'Accept': '*/*'})
        with urllib.request.urlopen(req, timeout=CFG['timeout_seconds']) as resp:
            status = getattr(resp, 'status', 200)
            data = resp.read(2048)
            kind = resp.headers.get('Content-Type', '').lower()
            if status not in (200, 206) or not data:
                return False
            if ('html' in kind or data.lstrip().lower().startswith((b'<!doctype html', b'<html'))):
                return False
            if '.m3u8' in url.lower() or 'mpegurl' in kind:
                return b'#EXTM3U' in data or b'#EXT-X-' in data
            return True
    except (ValueError, OSError, urllib.error.URLError, TimeoutError):
        return False


def safe(s):
    return str(s).replace('"', "'").replace('\r', ' ').replace('\n', ' ')


def write_m3u(entries, epg_link):
    header = '#EXTM3U' + (f' x-tvg-url="{safe(epg_link)}"' if epg_link else '')
    lines = [header]
    for e in entries:
        a = [f'group-title="{safe(e["group"])}"']
        if e['tvg_id']:
            a.append(f'tvg-id="{safe(e["tvg_id"])}"')
        if e['logo']:
            a.append(f'tvg-logo="{safe(e["logo"])}"')
        lines.extend([f'#EXTINF:-1 {" ".join(a)},{safe(e["name"])}', e['url']])
    (OUT / 'MyIPTV.m3u').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def epg_update(urls, live_ids):
    """Only download configured or source-advertised XMLTV. Never fabricate programme data."""
    guide = OUT / 'guide.xml'
    for url in dict.fromkeys(urls):
        try:
            raw = fetch(url, limit=CFG.get('epg_max_mb',18) * 1024 * 1024, timeout=25)
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
                if len(raw) > 65 * 1024 * 1024:
                    raise ValueError('Uncompressed EPG is too large')
            root = ET.fromstring(raw)
            if root.tag != 'tv':
                raise ValueError('Not XMLTV')
            if not live_ids:
                raise ValueError('No live tvg-id values')
            # Retain only matching channel IDs and programmes, preserving XMLTV header.
            for node in list(root):
                if node.tag in ('channel', 'programme') and (
                    node.attrib.get('id' if node.tag == 'channel' else 'channel', '') not in live_ids
                ):
                    root.remove(node)
            if not any(x.tag == 'programme' for x in root):
                raise ValueError('Guide has no matching programmes')
            ET.ElementTree(root).write(guide, encoding='utf-8', xml_declaration=True)
            return url
        except Exception as exc:
            print(f'EPG candidate unavailable: {url}: {exc}')
    if not guide.exists():
        guide.write_text('<?xml version="1.0" encoding="utf-8"?>\n<tv/>\n', 'utf-8')
    return None


def main():
    old = json.loads(STATE_FILE.read_text('utf-8')) if STATE_FILE.exists() else {}
    sources, discovered_epg = [], []
    successful_sources = 0
    for s in CFG['playlist_sources']:
        try:
            data = fetch(s['url'])
            entries, epgs = parse_m3u(data)
            selected = entries[:max(0, int(s.get('max_entries', 100)))]
            for e in selected:
                e['group'] = f'{s.get("group_prefix", "Live TV")} | {e["group"] or "Live TV"}'
            sources.extend(selected)
            discovered_epg.extend(epgs)
            successful_sources += 1
            print(f'Source {s["url"]}: read {len(entries)}; selected {len(selected)}')
        except Exception as exc:
            print(f'Source failed {s["url"]}: {exc}')
    custom = ROOT / CFG['custom_entries_file']
    if custom.exists():
        custom_entries, _ = parse_m3u(custom.read_bytes())
        sources.extend(custom_entries)
    # Avoid erasing output on upstream failure. Repo state still reflects last good run.
    if successful_sources == 0 and not sources:
        print('No source available: retaining last successful output')
        return
    unique = {e['url']: e for e in sources if e['url'].startswith(('https://', 'http://'))}
    current = list(unique.values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(CFG['max_concurrent_checks'])) as executor:
        results = list(executor.map(check, current))
    state, kept = {}, []
    for e, ok in zip(current, results):
        url = e['url']
        previous = old.get(url, {})
        failures = 0 if ok else int(previous.get('failures', 0)) + 1
        # New URLs must first pass probe; known-good URLs have 3 consecutive chances.
        had_success = bool(ok or previous.get('ever_good', False))
        state[url] = {'failures': failures, 'ever_good': had_success, 'entry': e,
                      'last_checked': datetime.now(timezone.utc).isoformat()}
        if had_success and failures < int(CFG['failure_threshold']):
            kept.append(e)
    # Keep previously-good streams through temporary source outage, but don't increment
    # counters for links that were not present in today's source.
    for url, prev in old.items():
        if url not in state and prev.get('ever_good') and prev.get('failures', 0) < CFG['failure_threshold']:
            state[url] = prev
            kept.append(prev['entry'])
    kept = list({e['url']: e for e in kept}.values())
    live_ids = {e['tvg_id'] for e in kept if e['tvg_id'] and not e['group'].startswith(('Movies', 'Series'))}
    urls = list(CFG.get('epg_urls', []))
    if CFG.get('epg_auto_discover', True):
        urls += discovered_epg[:2]
    used_epg = epg_update(urls, live_ids)
    # A full absolute playlist URL isn't known until the repo is created.
    # GitHub Actions supplies it through the canonical raw GitHub URL calculated from context.
    import os
    repo = os.getenv('GITHUB_REPOSITORY', '')
    branch = os.getenv('GITHUB_REF_NAME', 'main')
    guide_link = (f'https://raw.githubusercontent.com/{repo}/{branch}/output/guide.xml'
                  if repo and used_epg else '')
    write_m3u(kept, guide_link)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + '\n', 'utf-8')
    print(f'Current candidates: {len(current)} | retained: {len(kept)} | EPG: {used_epg or "no matching guide"}')


if __name__ == '__main__':
    main()
