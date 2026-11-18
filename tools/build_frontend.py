"""Build an allowlisted, content-addressed static site without a JS toolchain."""
import base64
from collections.abc import Iterable
from hashlib import sha256, sha384
import json
from pathlib import Path
import posixpath
import re
from tempfile import TemporaryDirectory
from typing import TypedDict
from urllib.parse import urlsplit


LOADER = "static/platform/routes.js"
ROUTE_ASSET_LIST = "static/platform/route-assets.js"
MANIFEST_REFERENCE = "const builtRouteManifest = null;"
MANIFEST_TABLE = "const platformReleaseAssets = {};"
STATIC_TAG = re.compile(r"<(?:script|link)\b[^>]*>", re.IGNORECASE)
ASSET_ATTRIBUTE = re.compile(r'\b(src|href)="([^"]+\.(?:js|css))"')


class AssetInfo(TypedDict):
    path: str
    integrity: str


class FrontendManifest(TypedDict):
    version: int
    assets: dict[str, AssetInfo]


def platform_base(origin: str) -> str:
    """Return a Pages destination for the explicit event HTTPS origin."""
    value = urlsplit(origin)
    if (value.scheme != "https" or not value.hostname or value.username is not None
            or value.password is not None or value.path not in ("", "/") or value.query
            or value.fragment or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", value.netloc)):
        raise ValueError("Pages needs an HTTPS public origin without credentials or a path")
    if value.port == 0:
        raise ValueError("Invalid public origin port")
    return f"https://{value.netloc}/gotc/"


def pages_landing(html: str, origin: str) -> str:
    base = platform_base(origin)
    return (html.replace('data-platform-base="./"', f'data-platform-base="{base}"')
            .replace('data-api="api/registration"', f'data-api="{base}api/registration"')
            .replace('href="teams.html', f'href="{base}teams.html')
            .replace('href="platform.html', f'href="{base}platform.html'))


def build_frontend(source: Path, destination: Path, files: Iterable[str | Path], *,
                   public_origin: str | None = None, pages: bool = False) -> FrontendManifest:
    """Stage exactly the provided frontend-relative regular files into a new directory."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    source = source.resolve(strict=True)
    if pages and not public_origin:
        raise ValueError("Set GOTC_PUBLIC_ORIGIN for the separately hosted Pages site")
    if public_origin:
        platform_base(public_origin)
    inputs = {}
    for entry in files:
        rel = Path(entry)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError(f"Invalid frontend source path: {entry}")
        item = source / rel
        if item.resolve(strict=True) != item or not item.is_file():
            raise ValueError(f"Expected regular frontend source: {entry}")
        inputs[rel.as_posix()] = item
    assets: dict[str, AssetInfo] = {}
    with TemporaryDirectory(dir=destination.parent) as staging:
        root = Path(staging) / "site"
        root.mkdir()

        def emit(name: str, data: bytes) -> None:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        def emit_asset(name: str, data: bytes) -> None:
            path = Path(name)
            fingerprint = sha256(data).hexdigest()[:16]
            target = path.with_name(f"{path.stem}.{fingerprint}{path.suffix}").as_posix()
            sri = "sha384-" + base64.b64encode(sha384(data).digest()).decode("ascii")
            assets[name] = {"path": target, "integrity": sri}
            emit(target, data)

        for name, path in sorted(inputs.items()):
            if name not in (LOADER, ROUTE_ASSET_LIST) and path.suffix in (".js", ".css"):
                emit_asset(name, path.read_bytes())

        if LOADER in inputs:
            loader = inputs[LOADER].read_text(encoding="utf-8")
            if ROUTE_ASSET_LIST not in inputs:
                raise ValueError("The platform loader requires its release asset table")
            template = inputs[ROUTE_ASSET_LIST].read_text(encoding="utf-8")
            if loader.count(MANIFEST_REFERENCE) != 1 or template.count(MANIFEST_TABLE) != 1:
                raise ValueError("Missing route asset manifest declaration")
            # Hash leaves, then the manifest, then the loader. No file contains its own digest.
            route_assets = {
                posixpath.relpath(name, "static/platform"): meta for name, meta in sorted(assets.items())
                if name.startswith(("static/platform/", "static/registration/"))
            }
            manifest_js = json.dumps(route_assets, separators=(",", ":"), sort_keys=True)
            template = template.replace(MANIFEST_TABLE, f"const platformReleaseAssets = {manifest_js};")
            emit_asset(ROUTE_ASSET_LIST, template.encode("utf-8"))
            reference = json.dumps(assets[ROUTE_ASSET_LIST], separators=(",", ":"), sort_keys=True)
            loader = loader.replace(MANIFEST_REFERENCE, f"const builtRouteManifest = {reference};")
            emit_asset(LOADER, loader.encode("utf-8"))
        elif ROUTE_ASSET_LIST in inputs:
            raise ValueError("A release asset table without a platform loader cannot be used")

        def rewrite_tag(match: re.Match[str]) -> str:
            tag = match.group()
            found = ASSET_ATTRIBUTE.search(tag)
            if not found:
                return tag
            name = found[2]
            if name not in assets:
                raise ValueError(f"Undeclared script or stylesheet in HTML: {name}")
            asset = assets[name]
            attr = f'{found[1]}="{asset["path"]}" integrity="{asset["integrity"]}"'
            return tag[:found.start()] + attr + tag[found.end():]

        for name, path in sorted(inputs.items()):
            if name in assets:
                continue
            data = path.read_bytes()
            if path.suffix == ".html":
                html = STATIC_TAG.sub(rewrite_tag, data.decode("utf-8"))
                if name == "index.html" and public_origin:
                    html = html.replace('property="og:image" content="assets/hero-city.webp"',
                                        f'property="og:image" content="{platform_base(public_origin)}assets/hero-city.webp"')
                if name == "index.html" and pages and public_origin:
                    html = pages_landing(html, public_origin)
                data = html.encode("utf-8")
            emit(name, data)
        manifest: FrontendManifest = {"version": 1, "assets": assets}
        emit("asset-manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
        root.rename(destination)
    return manifest
