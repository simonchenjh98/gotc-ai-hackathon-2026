"""Auditable Moark/Gitee pricing. No network calls and no client supplied costs.

Catalog snapshots are safe to persist with asynchronous jobs. ``None`` cost means
unsettled, never free. All costs are integer micro-CNY; legacy gateway pricing is
deliberately outside this module. Only pass upstream responses/headers to quote.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re


class PricingError(ValueError):
    pass


_MILLION = Decimal(1000000)
_BEIJING = timezone(timedelta(hours=8))
_TOKEN_FIELDS = ("input_million_tokens_price", "output_million_tokens_price",
                 "cache_write_million_tokens_price", "cache_read_million_tokens_price")


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise PricingError("Missing or invalid billing number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PricingError("Invalid billing number") from exc
    if not result.is_finite() or result < 0:
        raise PricingError("Billing numbers must be finite and nonnegative")
    return result


def _micro(value):
    result = int((value * _MILLION).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if result > 9223372036854775807:
        raise PricingError("Billed amount exceeds the ledger range")
    return result


def _path(value):
    return "/" + str(value).lstrip("/").rstrip("/")


class Catalog:
    """Current public operations, including unavailable entries with a reason."""

    def __init__(self, path=None, data=None):
        self.data = deepcopy(data) if data is not None else json.loads(
            Path(path or Path(__file__).with_name("gateway_catalog.json")).read_text(encoding="utf-8"))
        self.models = self.data["models"]
        self._names = {name.casefold(): name for name in self.models}

    def _name(self, model):
        name = self._names.get(str(model).casefold())
        if name is None:
            name = self._names.get(str(model).rsplit("/", 1)[-1].casefold())
        if name is None:
            raise PricingError("Unknown model")
        return name

    def model_ids(self, include_unavailable=False):
        return sorted(name for name, model in self.models.items()
                      if include_unavailable or model.get("available"))

    def paths(self):
        return sorted({op["path"] for model in self.models.values() for op in model["operations"]})

    def model_info(self, model):
        name = self._name(model)
        return {"id": name, **deepcopy(self.models[name])}

    def resolve(self, model, path, body=None, headers=None):
        name = self._name(model)
        info = self.models[name]
        if not info.get("available"):
            raise PricingError(info.get("unavailable_reason", "Model pricing unavailable"))
        candidates = [o for o in info["operations"] if o["path"] == _path(path)]
        if not candidates:
            raise PricingError("Model does not support this endpoint")
        body = body if isinstance(body, dict) else {}
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        if headers.get("x-compute-id"):
            candidates = [o for o in candidates if str(o.get("compute")) == str(headers["x-compute-id"])]
        if headers.get("x-pipeline-tag"):
            candidates = [o for o in candidates if o.get("type") == headers["x-pipeline-tag"]]
        if not candidates:
            raise PricingError("No priced operation matches the selected compute or pipeline")

        # The public operation parameters distinguish image size, embedding batch
        # length, ControlNet and several speech/video variants. Never choose a
        # cheaper operation merely because it happens to appear first.
        known = {p["name"] for o in candidates for p in o.get("params", [])}
        routing = {"size", "resolution", "input", "inputs", "width", "height", "control_image", "mode", "task"}
        for field in routing & known:
            value = body.get(field)
            if value is None:
                if field == "control_image":
                    candidates = [o for o in candidates if not any(
                        p["name"] == field and p.get("required") for p in o.get("params", []))]
                continue
            narrowed = []
            for operation in candidates:
                param = next((p for p in operation.get("params", []) if p["name"] == field), None)
                if param is None:
                    if field != "control_image":
                        narrowed.append(operation)
                    continue
                options = param.get("options")
                if options and not any(str(value).casefold() == str(v).casefold() for v in options):
                    continue
                if isinstance(value, list) and ("min_length" in param or "max_length" in param):
                    length = len(value)
                    if not param.get("min_length", 0) <= length <= param.get("max_length", float("inf")):
                        continue
                narrowed.append(operation)
            candidates = narrowed
        if not candidates:
            raise PricingError("No priced operation matches the request parameters")
        return {"version": self.data["version"], "snapshot_date": self.data.get("snapshot_date"),
                "currency": self.data.get("currency", "CNY"), "timezone": self.data.get("timezone", "Asia/Shanghai"),
                "rate_mode": "specified" if headers.get("x-compute-id") else "average",
                "model": name, "path": _path(path), "operations": deepcopy(candidates),
                "resolution_price_factors": deepcopy(info.get("resolution_price_factors", {}))}


def _result(cost=None, estimated=False, reason=None, **details):
    if reason:
        details["reason"] = reason
    return {"cost_micro": cost, "currency": "CNY", "estimated": estimated,
            "pending": cost is None, "details": details}


def _usage(response, usage):
    if usage is not None:
        return usage
    for data in (response, response.get("output", {}), response.get("response", {})):
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            return data["usage"]
    return None


def _tokens(usage):
    """Return noncached input, output, cache write, cache read, long cache write."""
    if isinstance(usage, (list, tuple)) and len(usage) == 5:
        return [_decimal(value) for value in usage]
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    def first(*fields):
        for source, field in fields:
            if field in source:
                return _decimal(source[field])
        return Decimal(0)
    if not any(k in usage for k in ("prompt_tokens", "input_tokens", "total_tokens", "completion_tokens", "output_tokens")):
        return None
    cached = first((details, "cached_tokens"), (usage, "cache_read_input_tokens"),
                   (usage, "cache_read_tokens"), (usage, "cached_tokens"), (usage, "prompt_cache_hit_tokens"))
    written = first((details, "cache_write_tokens"), (usage, "cache_creation_input_tokens"))
    split = usage.get("cache_creation") or {}
    hour = _decimal(split.get("ephemeral_1h_input_tokens", 0)) if isinstance(split, dict) else Decimal(0)
    if isinstance(split, dict) and "ephemeral_5m_input_tokens" in split:
        written = _decimal(split["ephemeral_5m_input_tokens"]) + hour
    # prompt_tokens is OpenAI's inclusive count. Anthropic input_tokens excludes
    # cache creation/read tokens. Responses input_tokens is inclusive instead.
    anthropic = "prompt_tokens" not in usage and any(k in usage for k in ("cache_creation_input_tokens", "cache_read_input_tokens", "cache_creation"))
    prompt = first((usage, "prompt_tokens"), (usage, "input_tokens"), (usage, "total_tokens"))
    output = first((usage, "completion_tokens"), (usage, "output_tokens"))
    if not anthropic:
        if cached + written > prompt:
            raise PricingError("Cache tokens exceed the inclusive input token count")
        prompt -= cached + written
    if hour > written:
        raise PricingError("Cache write split exceeds its total")
    return [prompt, output, written - hour, cached, hour]


def _off_peak(policy, started_at):
    settings = policy.get("time_of_use") or {}
    if not settings.get("enabled"):
        return False
    if started_at is None:
        raise PricingError("Request start time is required for time-of-use pricing")
    if isinstance(started_at, datetime):
        at = started_at
    elif isinstance(started_at, (int, float)):
        at = datetime.fromtimestamp(started_at, timezone.utc)
    else:
        at = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise PricingError("Request start time must include a timezone")
    at = at.astimezone(_BEIJING)
    minute = at.hour * 60 + at.minute
    for period in settings.get("off_peak_periods", []):
        def minutes(value):
            hour, mins = map(int, value.split(":"))
            if not (0 <= hour <= 23 and 0 <= mins <= 59):
                raise PricingError("Invalid time-of-use interval")
            return hour * 60 + mins
        start, end = minutes(period["start"]), minutes(period["end"])
        if (start <= minute < end) if start < end else (minute >= start or minute < end):
            return True
    return False


def _token_quote(operation, usage, started_at, rate_mode="average"):
    tokens = _tokens(usage)
    policy = operation.get("billing_policy") or {}
    strategy = operation.get("billing_strategy", "standard")
    if strategy not in ("standard", "rule_based"):
        raise PricingError("Unsupported billing strategy")
    if strategy == "rule_based":
        if policy.get("version", 1) != 1:
            raise PricingError("Unsupported token policy version")
        if policy.get("type") not in ("prompt_cache_tokens", "tiered_prompt_cache_tokens"):
            raise PricingError("Unsupported token billing policy")
        if tokens is None:
            return _result(reason="Token usage was not reported")
        context = tokens[0] + tokens[2] + tokens[3] + tokens[4]
        selected = policy
        tier_range = None
        if policy["type"] == "tiered_prompt_cache_tokens":
            if policy.get("calculation_mode") != "whole_order":
                raise PricingError("Unsupported tier calculation mode")
            selected = None
            for tier in policy.get("tiers", []):
                lower = _decimal(tier.get("min_input_tokens", 0))
                upper = tier.get("max_input_tokens")
                if (context > lower or lower == 0) and (upper is None or context <= _decimal(upper)):
                    selected = tier
                    tier_range = [int(lower), upper]
                    break
            if selected is None:
                raise PricingError("No token price tier covers this request")
        off_peak = _off_peak(policy, started_at)
        rate_source = selected.get("off_peak", {}) if off_peak else selected
        rates = rate_source.get(rate_mode)
        if not rates or any(field not in rates for field in _TOKEN_FIELDS):
            raise PricingError("Incomplete token price policy")
        prices = [_decimal(rates[field]) for field in _TOKEN_FIELDS]
        prices.append(prices[2])  # Public Moark policies do not distinguish cache TTLs.
    else:
        suffix = "_average" if rate_mode == "average" else ""
        prices = [_decimal(operation.get("input_million_tokens_price" + suffix, operation.get("input_million_tokens_price", 0))),
                  _decimal(operation.get("output_million_tokens_price" + suffix, operation.get("output_million_tokens_price", 0)))]
        prices += [prices[0], prices[0], prices[0]]
        if operation.get("unit") == "token" and not any(prices):
            prices = [_decimal(operation.get("average_price" if rate_mode == "average" else "price", operation.get("price", 0))), Decimal(0)]
            prices += [prices[0], prices[0], prices[0]]
        off_peak, tier_range = False, None
    if not any(prices):
        return _result(0, basis="published_free_token_rate")
    if tokens is None:
        return _result(reason="Token usage was not reported")
    amount = sum(t * p for t, p in zip(tokens, prices))
    return _result(_micro(amount / _MILLION), basis="tokens",
                   tokens=[int(t) for t in tokens], rates=[str(p) for p in prices], tier=tier_range,
                   off_peak=off_peak, timezone="Asia/Shanghai")


def _number_from(sources, names):
    for label, source in sources:
        if not isinstance(source, dict):
            continue
        for name in names:
            if name in source and source[name] is not None:
                return _decimal(source[name]), label + "." + name
    return None, None


def _factor(snapshot, request, response, quantity):
    factors = snapshot.get("resolution_price_factors") or {}
    if not factors:
        return Decimal(1), []
    dimensions = {name: item for name, item in factors.items() if "width" in item}
    durations = {name: item for name, item in factors.items() if "start" in item}
    selected = []
    if dimensions:
        resolution = response.get("resolution", request.get("resolution"))
        match = next(((name, item) for name, item in dimensions.items()
                      if resolution is not None and name.casefold() == str(resolution).casefold()), None)
        if match is None:
            width = response.get("width", request.get("width"))
            height = response.get("height", request.get("height"))
            size = response.get("size", request.get("size"))
            if (width is None or height is None) and isinstance(size, str) and re.fullmatch(r"\d+[xX*]\d+", size):
                width, height = re.split(r"[xX*]", size)
            if width is None or height is None:
                raise PricingError("Resolution is required for the published price multiplier")
            width, height = sorted((_decimal(width), _decimal(height)), reverse=True)
            valid = [(name, item) for name, item in dimensions.items()
                     if width <= _decimal(item["width"]) and height <= _decimal(item["height"])]
            match = min(valid, key=lambda pair: pair[1]["width"] * pair[1]["height"]) if valid else None
        if match is None:
            raise PricingError("No published resolution multiplier matches the response")
        selected.append(match)
    if durations:
        # Public duration bands are whole seconds; fractional durations between
        # labelled bands are deliberately unsettled rather than rounded silently.
        match = next(((name, item) for name, item in durations.items()
                      if _decimal(item["start"]) <= quantity <= _decimal(item["end"])), None)
        if match is None:
            raise PricingError("No published duration multiplier matches the response")
        selected.append(match)
    factor = Decimal(1)
    for _, item in selected:
        factor *= _decimal(item["factor"])
    return factor, [name for name, _ in selected]


def _unit_quote(snapshot, operation, request, response, usage, trusted_quantities=None):
    unit = operation.get("unit")
    price = _decimal(operation.get("average_price" if snapshot.get("rate_mode", "average") == "average" else "price", operation.get("price")))
    if unit not in ("call", "image", "page", "item", "chars_10k", "seconds", "layers"):
        raise PricingError("Unknown billing unit")
    if price == 0:
        return _result(0, basis="published_free_unit_rate", unit=unit)
    output = response.get("output") if isinstance(response.get("output"), dict) else response
    sources = [("usage", usage), ("response", response), ("output", output)]
    estimated, basis = False, ""
    if unit == "call":
        quantity, basis = Decimal(1), "successful_call"
    elif unit == "image":
        quantity, basis = _number_from(sources, ("image_count", "images_count", "num_images"))
        if quantity is None:
            images = output.get("data", output.get("images"))
            if isinstance(images, list):
                quantity, basis = Decimal(len(images)), "response.images"
            else:
                quantity = _decimal(request.get("n", 1))
                basis, estimated = "requested_image_count", True
    elif unit == "page":
        quantity, basis = _number_from(sources, ("page_count", "pages_count", "total_pages", "num_pages"))
        if quantity is None and isinstance(output.get("pages"), list):
            quantity, basis = Decimal(len(output["pages"])), "response.pages"
        if quantity is None:
            quantity, basis = _number_from([("gateway_measured", trusted_quantities)], ("page_count",))
            estimated = quantity is not None
    elif unit == "item":
        quantity, basis = _number_from(sources, ("item_count", "items_count"))
        if quantity is None:
            quantity, basis = _number_from([("gateway_measured", trusted_quantities)], ("items",))
            estimated = quantity is not None
        if quantity is None:
            inputs = request.get("input", request.get("inputs"))
            if isinstance(inputs, (str, list)):
                quantity, basis = Decimal(len(inputs) if isinstance(inputs, list) else 1), "request.items"
    elif unit == "chars_10k":
        quantity, basis = _number_from(sources, ("characters", "character_count", "chars", "text_length"))
        if quantity is None:
            quantity, basis = _number_from([("gateway_measured", trusted_quantities)], ("characters",))
            estimated = quantity is not None
        if quantity is None:
            text = request.get("input", request.get("inputs", request.get("text")))
            if isinstance(text, str):
                quantity, basis, estimated = Decimal(len(text)), "request.unicode_codepoints", True
        if quantity is not None:
            quantity /= Decimal(10000)
    elif unit == "seconds":
        quantity, basis = _number_from(sources, ("duration", "duration_seconds", "audio_seconds", "video_seconds"))
        if quantity is None:
            quantity, basis = _number_from([("gateway_measured", trusted_quantities)], ("duration",))
            estimated = quantity is not None
        if quantity is None:
            measured = request.get("file") if operation.get("type") == "speech2text" else request.get("ref_audio")
            quantity, basis = _number_from([("request.measured_file", measured)], ("duration",))
            estimated = quantity is not None
        if quantity is None:
            quantity, basis = _number_from([("request", request)], ("duration", "duration_seconds"))
            estimated = quantity is not None
    else:
        quantity, basis = _number_from(sources, ("layer_count", "layers_count"))
    if quantity is None:
        return _result(reason="Billable quantity was not reported", unit=unit)
    if quantity == 0 and price:
        return _result(reason="Zero billable quantity needs reconciliation", unit=unit)
    factor, factor_names = _factor(snapshot, request, output, quantity)
    return _result(_micro(quantity * price * factor), estimated=estimated, basis=basis, unit=unit,
                   quantity=str(quantity), price=str(price), multiplier=str(factor), multiplier_bands=factor_names)


def quote(snapshot, request=None, response=None, usage=None, started_at=None, headers=None, trusted_quantities=None):
    """Quote trusted upstream evidence; return pending instead of inventing cost.

    Headers and response must originate at the upstream, never at the client.
    ``trusted_quantities`` is a separate server-measured metadata dictionary,
    never copied from client JSON. Async task IDs/status/prices must already have been checked for ownership by
    the caller. An upstream zero cost is valid; absence is not a zero cost.
    """
    request = request if isinstance(request, dict) else {}
    response = response if isinstance(response, dict) else {}
    headers = {str(k).lower(): v for k, v in (headers or response.get("_gateway_headers") or {}).items()}
    try:
        status = response.get("status")
        task = "task_id" in response and status in ("waiting", "in_progress", "success", "failure", "cancelled")
        if task and status in ("waiting", "in_progress"):
            return _result(reason="Asynchronous task has not reached a terminal state")
        reported_currencies = {str(value).strip().upper() for value in
                               (response.get("currency"), headers.get("inference-currency"),
                                headers.get("inference-cost-currency")) if value is not None and str(value).strip()}
        if len(reported_currencies) > 1:
            return _result(reason="Upstream reported conflicting currencies", upstream_currencies=sorted(reported_currencies))
        currency = next(iter(reported_currencies), snapshot.get("currency", "CNY"))
        if str(currency).upper() != "CNY":
            return _result(reason="Upstream currency differs from CNY", upstream_currency=str(currency))
        if "inference-cost" in headers:
            return _result(_micro(_decimal(headers["inference-cost"])), basis="upstream.inference-cost")
        if task and response.get("price") is not None:
            return _result(_micro(_decimal(response["price"])), basis="upstream.task.price")
        if task and status in ("failure", "cancelled"):
            return _result(reason="Terminal task has not reported its final cost")
        if response.get("error"):
            return _result(reason="Upstream error has not reported its final cost")
        if any(request.get(k) for k in ("lora_weights", "lora", "lora_id")):
            return _result(reason="LoRA adapter surcharge requires the upstream billed amount")
        usage = _usage(response, usage)
        results = []
        for operation in snapshot.get("operations", []):
            token = operation.get("unit") == "token" or operation.get("billing_strategy") == "rule_based" or any(
                _decimal(operation.get(field, 0)) > 0 for field in _TOKEN_FIELDS[:2])
            result = _token_quote(operation, usage, started_at, snapshot.get("rate_mode", "average")) if token else _unit_quote(snapshot, operation, request, response, usage, trusted_quantities)
            result["details"]["operation_id"] = operation["id"]
            results.append(result)
        if not results:
            return _result(reason="No priced operation in the saved snapshot")
        costs = {r["cost_micro"] for r in results}
        if len(costs) == 1 and None not in costs:
            result = results[0]
            result["estimated"] = any(r["estimated"] for r in results)
            result["details"]["operation_ids"] = [o["id"] for o in snapshot["operations"]]
            return result
        if len(results) == 1:
            return results[0]
        amounts = [r["cost_micro"] for r in results if r["cost_micro"] is not None]
        return _result(reason="Operation cost is ambiguous or incomplete", candidate_quotes=results,
                       upper_estimate_micro=max(amounts) if amounts else None)
    except (PricingError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return _result(reason=str(exc))
