"""Upstream configuration validation, model aliases and organizer settings."""

import ipaddress
import json
from urllib.parse import urlsplit


DEFAULTS = {"keys.maxKeys": 5, "keys.expiresAt": None,
            "keys.logContent": False, "keys.teamRpm": 120, "keys.announcement": "", "keys.blockedWords": [],
            "keys.logRetentionHours": 72, "keys.logMaxMB": 2048}  # request content is bounded (sub2api keeps no bodies at all)


def check_url(value):
    parts = urlsplit(value if isinstance(value, str) else "")
    try:
        loopback = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment or parts.username \
            or (parts.scheme == "http" and not loopback):
        raise ValueError("upstream url must be https, or http on loopback")
    return value.rstrip("/")


def load_config(path):
    """Optional private gateway.json, used only to seed the admin panel's upstream settings on first start."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    parse_config(data)
    return data


def full_price(p):
    """[input, output, cache write 5m, cache read, cache write 1h]; missing cache prices use Anthropic's ratios to input."""
    p = list(p)
    if len(p) == 2:
        p += [round(p[0] * 1.25, 6), round(p[0] * 0.1, 6)]
    if len(p) == 4:
        p.append(round(p[0] * 2, 6))
    return p


def valid_price(p, lengths):
    return isinstance(p, list) and len(p) in lengths and all(type(x) in (int, float) and 0 <= x <= 10000 for x in p)


def parse_config(data):
    """Validate {"openai"|"anthropic": {"url", "key", "models": {name: price}, "aliases"?: {name|prefix*: model}, "defaultPrice"?: price}}.

    price is [input, output] or [input, output, cacheWrite, cacheRead] USD per million tokens;
    the short form bills cache writes at 1.25x and cache reads at 0.1x the input price.
    A model with price [] uses the upstream's defaultPrice, or bills $0 without one (as sub2api does for unpriced models).
    Returns the runtime form, or None when no upstream is configured.
    """
    if data in (None, {}):
        return None
    if not isinstance(data, dict) or not set(data).issubset({"openai", "anthropic"}):
        raise ValueError("gateway.json supports openai and anthropic upstreams")
    result = {}
    for name, item in data.items():
        if not isinstance(item, dict) or not {"url", "key", "models"} <= set(item) <= {"name", "url", "key", "models", "aliases", "defaultPrice"} \
                or not isinstance(item["key"], str) or not item["key"]:
            raise ValueError(f"{name} upstream needs url, key and models")
        models, default = item["models"], item.get("defaultPrice", [0, 0])
        if not isinstance(item.get("name", ""), str) or len(item.get("name", "")) > 60:
            raise ValueError(f"{name} name must be at most 60 characters")
        if not valid_price(default, (2, 4, 5)):
            raise ValueError(f"{name} defaultPrice must be [input, output(, cacheWrite5m, cacheRead(, cacheWrite1h))] USD per million tokens")
        if not isinstance(models, dict) or not models or len(models) > 500 or not all(
                isinstance(m, str) and 0 < len(m) <= 100 and valid_price(p, (0, 2, 4, 5)) for m, p in models.items()):
            raise ValueError(f"{name} models must map a model name to [] (default price) or [input, output(, cacheWrite5m, cacheRead(, cacheWrite1h))] USD per million tokens")
        aliases = item.get("aliases", {})
        if not isinstance(aliases, dict) or not all(isinstance(a, str) and a and target in models for a, target in aliases.items()):
            raise ValueError(f"{name} aliases must map a name (or prefix ending in *) to one of its models")
        result[name] = {"url": check_url(item["url"]), "key": item["key"], "aliases": aliases,
                        "models": {m: full_price(p or default) for m, p in models.items()}}
    return result


def resolve(upstream, model):
    """Allowed model name for a requested one: exact, then alias, then the longest matching prefix* alias."""
    if not isinstance(model, str):
        return None
    if model in upstream["models"]:
        return model
    aliases = upstream["aliases"]
    if model in aliases:
        return aliases[model]
    prefixes = [a for a in aliases if a.endswith("*") and model.startswith(a[:-1])]
    return aliases[max(prefixes, key=len)] if prefixes else None


def settings(db):
    return {r["key"][5:]: json.loads(r["value"])
            for r in db.execute("SELECT key,value FROM settings WHERE key LIKE 'keys.%'") if r["key"] in DEFAULTS}
