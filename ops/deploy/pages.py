"""Build the public landing site for its configured HTTPS event platform."""
import argparse
import os
from pathlib import Path

from .deploy import PROJECT, release_sources
from .install import PAGES_FILES
from tools.build_frontend import build_frontend


def stage(destination, *, public_origin, project=PROJECT):
    files = release_sources(project, PAGES_FILES)
    build_frontend(project / 'frontend', destination, [path.relative_to(project / 'frontend') for path in files],
                   public_origin=public_origin, pages=True)
    (destination / '.nojekyll').touch()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='New output directory')
    parser.add_argument('--public-origin', default=os.environ.get('GOTC_PUBLIC_ORIGIN'))
    args = parser.parse_args()
    stage(args.destination, public_origin=args.public_origin)
