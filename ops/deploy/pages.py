"""Stage the event landing page for GitHub Pages, preserving its public URLs."""
import argparse
from pathlib import Path
import shutil

from .deploy import PROJECT, release_sources
from .install import PAGES_FILES


PLATFORM_BASE = 'https://8.141.114.201/gotc/'


def pages_landing(source):
    """Connect the static-only edition to the independently hosted platform."""
    return (source.replace('data-platform-base="./"', f'data-platform-base="{PLATFORM_BASE}"')
            .replace('data-api="api/registration"', f'data-api="{PLATFORM_BASE}api/registration"')
            .replace('href="teams.html', f'href="{PLATFORM_BASE}teams.html')
            .replace('href="platform.html', f'href="{PLATFORM_BASE}platform.html'))


def stage(destination, project=PROJECT):
    files = release_sources(project, PAGES_FILES)
    destination.mkdir()  # Refuse a preexisting directory that may contain private files.
    for source in files:
        target = destination / source.relative_to(project / 'frontend')
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.name == 'index.html':
            target.write_text(pages_landing(source.read_text(encoding='utf-8')), encoding='utf-8')
        else:
            shutil.copyfile(source, target)
    (destination / '.nojekyll').touch()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='New output directory')
    stage(parser.parse_args().destination)
