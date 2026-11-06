"""Validate public page resources and navigation without a browser."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote

ROOT = Path(__file__).resolve().parent.parent

class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.refs, self.tabs = [], [], []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        for key in ('src', 'href'):
            if attrs.get(key):
                self.refs.append(attrs[key])
        if attrs.get('data-day'):
            self.tabs.append(attrs['data-day'])

for file in ('index.html', 'platform.html', 'teams.html'):
    page = Page()
    page.feed((ROOT / file).read_text(encoding='utf-8'))
    assert len(page.ids) == len(set(page.ids)), f'Duplicate IDs in {file}'
    for ref in page.refs:
        url = urlsplit(ref)
        if url.scheme or url.netloc:
            assert url.scheme == 'https', f'Insecure public URL: {ref}'
            continue
        if url.path.startswith('api/'):
            continue
        target = ROOT / unquote(url.path) if url.path else ROOT / file
        assert target.is_file(), f'Missing resource: {ref} in {file}'
        if target == ROOT / 'platform.html' and url.fragment in {'board','claim','team','team-score','judge','board-exhibition','judge-exhibition','board-roadshow','judge-roadshow','audience','admin'}:
            continue
        if url.fragment:
            destination = page
            if url.path:
                destination = Page()
                destination.feed(target.read_text(encoding='utf-8'))
            assert url.fragment in destination.ids, f'Missing anchor: {ref}'
    for tab in page.tabs:
        assert tab in page.ids, f'Missing schedule panel: {tab}'
print('Public resources, anchors, schedule panels and HTTPS links: passed')
