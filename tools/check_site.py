"""Check local HTML references in the integrated frontend or staged Pages site."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


PLATFORM_ROUTES = {
    'board', 'claim', 'team', 'team-profile', 'team-score', 'judge', 'board-exhibition',
    'judge-exhibition', 'board-roadshow', 'judge-roadshow', 'audience', 'admin',
    'judge-finals', 'admin-finals', 'board-finals', 'admin-roadshow',
}


class Page(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.ids, self.refs, self.tabs = [], [], []
        self.feed(path.read_text(encoding='utf-8'))
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        for key in ('src', 'href'):
            if attrs.get(key):
                self.refs.append(attrs[key])
        if attrs.get('data-day'):
            self.tabs.append(attrs['data-day'])


def check_site(root, filenames):
    root = root.resolve(strict=True)
    pages = {root / name: Page(root / name) for name in filenames}
    for path, page in pages.items():
        if len(page.ids) != len(set(page.ids)):
            raise ValueError(f'Duplicate IDs in {path.name}')
        for ref in page.refs:
            url = urlsplit(ref)
            if url.scheme or url.netloc:
                if url.scheme != 'https':
                    raise ValueError(f'Insecure public URL: {ref}')
                continue
            if url.path.startswith('api/'):
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError(f'Missing local resource: {ref} in {path.name}')
            if not url.fragment or (target == root / 'platform.html' and url.fragment in PLATFORM_ROUTES):
                continue
            destination = pages.get(target) or Page(target)
            if url.fragment not in destination.ids:
                raise ValueError(f'Missing anchor: {ref} in {path.name}')
        for tab in page.tabs:
            if tab not in page.ids:
                raise ValueError(f'Missing schedule panel: {tab}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pages-root', type=Path, help='Check a staged GitHub Pages landing page')
    args = parser.parse_args()
    if args.pages_root:
        check_site(args.pages_root, ['index.html'])
    else:
        check_site(Path(__file__).resolve().parents[1] / 'frontend', ['index.html', 'platform.html', 'teams.html'])
    print('Public resources, anchors, schedule panels and HTTPS links: passed')
