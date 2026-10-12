#!/usr/bin/env python3
"""Personal IPTV playlist updater, XMLTV source merger (Python standard library only).
Only add streams and programme guide feeds that you are permitted to access/use.
"""
import concurrent.futures
import copy
import gzip
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
OUT = ROOT / 'output'
OUT.mkdir(exist_ok=True)
STATE_FILE = ROOT / 'state.json'
AGENT = 'PersonalIPTVPlaylistBuilder/2.0'


def fetch(url, limit=5_000_000, timeout=15):
    if not url.startswith(('https://', 'http://')):
        raise ValueError('Only HTTP(S) URLs supported')
    request = urllib.request.Request(url, headers={'User-Agent': AGENT, 'Accept': '*/*'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Response exceeds download limit')
    return data


def attrs(line):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', line))


def parse_m3u(contents):
    lines = contents.decode('utf-8-sig', errors='replace').splitlines()
    epg_urls = []
    if lines and lines[0].startswith('#EXTM3U'):
        header = attrs(lines[0])
        epg_urls = [s.strip() for s in (header.get('x-tvg-url') or header.get('url-tvg') or '').split(',') if s.strip()]
    entries = []
    ext = None
    for line in lines:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            ext = line
        elif ext and line.startswith(('http://', 'https://')):
            meta = attrs(ext)
            entries.append({'url': line, 'name': ext.split(',', 1)[-1].strip() if ',' in ext else 'Unnamed',
                            'tvg_id': meta.get('tvg-id', ''), 'logo': meta.get('tvg-logo', ''),
                            'group': meta.get('group-title', '')})
            ext = None
    return entries, epg_urls


def check(entry):
    try:
        req = urllib.request.Request(entry['url'], headers={'User-Agent': AGENT,
                                     'Range': 'bytes=0-1023', 'Accept': '*/*'})
        with urllib.request.urlopen(req, timeout=int(CFG.get('timeout_seconds', 9))) as resp:
            data = resp.read(2048)
            kind = resp.headers.get('Content-Type', '').lower()
            if getattr(resp, 'status', 200) not in (200, 206) or not data:
                return False
            if 'html' in kind or data.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                return False
            if '.m3u8' in entry['url'].lower() or 'mpegurl' in kind:
                return b'#EXTM3U' in data or b'#EXT-X-' in data
            return True
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return False


def normalize_name(value):
    value = re.sub(r'\([^)]*(?:\d{3,4}p|hd|sd|fhd)[^)]*\)', ' ', value, flags=re.I)
    value = re.sub(r'\b(?:4k|fhd|hd|sd|uhd|1080p|720p|576p|540p)\b', ' ', value, flags=re.I)
    return re.sub(r'[^a-z0-9]+', '', value.casefold())


def base_id(value):
    return value.split('@', 1)[0].casefold().strip()


def epg_update(urls, entries):
    """Merge independently matched programme data, keeping original playlist tvg-ids.

    Matching priority: exact XMLTV ID, same ID without @variant when unambiguous,
    unique normalized display-name. Ambiguity means skip, never guess.
    """
    guide = OUT / 'guide.xml'
    live = [e for e in entries if e.get('tvg_id') and not e.get('group', '').lower().startswith(('movies', 'series'))]
    if not live:
        print('EPG: playlist has no eligible tvg-id entries')
        return bool(guide.exists() and _has_programmes(guide))

    by_exact, by_base, by_name = {}, {}, {}
    for e in live:
        ident = e['tvg_id']
        by_exact.setdefault(ident.casefold(), set()).add(ident)
        by_base.setdefault(base_id(ident), set()).add(ident)
        if normalize_name(e['name']):
            by_name.setdefault(normalize_name(e['name']), set()).add(ident)
    merged_channels = {}
    merged_programmes = []
    seen_programmes = set()
    used_sources = 0

    for url in dict.fromkeys(u.strip() for u in urls if isinstance(u, str) and u.strip()):
        try:
            raw = fetch(url, limit=int(CFG.get('epg_max_mb', 18)) * 1024 * 1024, timeout=25)
            if raw.startswith(b'\x1f\x8b'):
                raw = gzip.decompress(raw)
            if len(raw) > 65 * 1024 * 1024:
                raise ValueError('Expanded EPG too large')
            root = ET.fromstring(raw)
            if root.tag != 'tv':
                raise ValueError('Not XMLTV <tv>')
            mapping = {}
            candidates = 0
            for channel in root.findall('channel'):
                source_id = channel.get('id', '')
                if not source_id:
                    continue
                matched = by_exact.get(source_id.casefold(), set())
                if len(matched) != 1:
                    matched = by_base.get(base_id(source_id), set())
                if len(matched) != 1:
                    candidates_by_name = set()
                    for el in channel.findall('display-name'):
                        candidates_by_name.update(by_name.get(normalize_name(el.text or ''), set()))
                    matched = candidates_by_name
                if len(matched) == 1:
                    dest_id = next(iter(matched))
                    mapping[source_id] = dest_id
                    if dest_id not in merged_channels:
                        new_channel = copy.deepcopy(channel)
                        new_channel.set('id', dest_id)
                        merged_channels[dest_id] = new_channel
                    candidates += 1
            appended = 0
            for programme in root.findall('programme'):
                dest_id = mapping.get(programme.get('channel', ''))
                if not dest_id:
                    continue
                key = (dest_id, programme.get('start', ''), programme.get('stop', ''),
                       (programme.findtext('title') or '').strip())
                if key in seen_programmes:
                    continue
                seen_programmes.add(key)
                item = copy.deepcopy(programme)
                item.set('channel', dest_id)
                merged_programmes.append(item)
                appended += 1
            if appended:
                used_sources += 1
            print(f'EPG source {url}: mapped_channels={candidates}, added_programmes={appended}')
        except Exception as exc:
            print(f'EPG source unavailable {url}: {exc}')

    if merged_programmes:
        root = ET.Element('tv', {'generator-info-name': 'Personal IPTV EPG Merger'})
        for ident in sorted(merged_channels):
            root.append(merged_channels[ident])
        for item in sorted(merged_programmes, key=lambda el: (el.get('channel', ''), el.get('start', ''))):
            root.append(item)
        data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        guide.write_bytes(data)
        print(f'EPG: merged {len(merged_programmes)} programmes from {used_sources} feeds')
        return True

    if guide.exists() and _has_programmes(guide):
        print('EPG: no matching guide today; retained last working guide')
        return True
    print('EPG: no matching guide — configure an authorized XMLTV feed with matching channels')
    # Deliberately do not publish a fake/empty guide on a first run.
    if guide.exists() and not _has_programmes(guide):
        guide.unlink()
    return False


def _has_programmes(path):
    try:
        return bool(ET.parse(path).getroot().find('programme') is not None)
    except (ET.ParseError, OSError):
        return False


def safe(s):
    return str(s).replace('"', "'").replace('\r', ' ').replace('\n', ' ')


def write_m3u(entries, epg_link):
    header = '#EXTM3U' + (f' x-tvg-url="{safe(epg_link)}"' if epg_link else '')
    lines = [header]
    for e in entries:
        props = [f'group-title="{safe(e["group"])}"']
        if e['tvg_id']:
            props.append(f'tvg-id="{safe(e["tvg_id"])}"')
        if e['logo']:
            props.append(f'tvg-logo="{safe(e["logo"])}"')
        lines.extend([f'#EXTINF:-1 {" ".join(props)},{safe(e["name"])}', e['url']])
    (OUT / 'MyIPTV.m3u').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    try:
        old = json.loads(STATE_FILE.read_text('utf-8')) if STATE_FILE.exists() else {}
    except (OSError, ValueError):
        old = {}
    sources, discovered = [], []
    successful_sources = 0
    for s in CFG['playlist_sources']:
        try:
            items, epgs = parse_m3u(fetch(s['url']))
            selected = items[:max(0, int(s.get('max_entries', 100)))]
            for e in selected:
                e['group'] = f'{s.get("group_prefix", "Live TV")} | {e["group"] or "Live TV"}'
            sources.extend(selected)
            discovered.extend(epgs)
            successful_sources += 1
            print(f'Source {s["url"]}: read {len(items)}, selected {len(selected)}')
        except Exception as exc:
            print(f'Source failed {s["url"]}: {exc}')
    custom = ROOT / CFG.get('custom_entries_file', 'custom_entries.m3u')
    if custom.exists():
        items, eps = parse_m3u(custom.read_bytes())
        sources.extend(items)
        discovered.extend(eps)
    if not sources and successful_sources == 0:
        print('No sources accessible, preserving previous output')
        return
    current = list({e['url']: e for e in sources if e['url'].startswith(('http://', 'https://'))}.values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(CFG.get('max_concurrent_checks', 12)))) as executor:
        checked = list(executor.map(check, current))
    state, kept = {}, []
    threshold = int(CFG.get('failure_threshold', 3))
    for e, ok in zip(current, checked):
        url = e['url']
        prev = old.get(url, {})
        failures = 0 if ok else int(prev.get('failures', 0)) + 1
        ever_good = ok or bool(prev.get('ever_good', False))
        state[url] = {'failures': failures, 'ever_good': ever_good, 'entry': e,
                      'last_checked': datetime.now(timezone.utc).isoformat()}
        if ever_good and failures < threshold:
            kept.append(e)
    for url, prev in old.items():
        if url not in state and prev.get('ever_good') and int(prev.get('failures', 0)) < threshold:
            state[url] = prev
            if prev.get('entry'):
                kept.append(prev['entry'])
    kept = list({e['url']: e for e in kept}.values())
    epg_urls = list(CFG.get('epg_urls', []))
    if CFG.get('epg_auto_discover', True):
        epg_urls.extend(discovered[:10])
    epg_ok = epg_update(epg_urls, kept)
    repo, branch = os.getenv('GITHUB_REPOSITORY', ''), os.getenv('GITHUB_REF_NAME', 'main')
    epg_link = f'https://raw.githubusercontent.com/{repo}/{branch}/output/guide.xml' if repo and epg_ok else ''
    write_m3u(kept, epg_link)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(f'Candidates: {len(current)} | retained: {len(kept)} | EPG ready: {epg_ok}')


if __name__ == '__main__':
    main()
