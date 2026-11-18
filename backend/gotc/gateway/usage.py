"""Token usage evidence and completion markers for OpenAI and Anthropic responses."""

USAGE_FIELDS = 5  # [input, output, cache write 5m, cache read, cache write 1h] tokens, in price order


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def read_usage(upstream, data, usage, call=None):
    """Update usage from one response object or stream event, merging like sub2api.

    Anthropic: message_start sets input and cache counts (even 0) and ignores its placeholder output; message_delta
    and final objects only override counts > 0. The cache-write total is split into 5-minute and 1-hour parts when the
    upstream reports cache_creation.ephemeral_5m/1h_input_tokens; without a split it is billed at the 5-minute price.
    OpenAI-compatible: cached prompt tokens are read from the first field present (providers name it differently),
    cache writes from prompt_tokens_details.cache_write_tokens, and input is prompt minus both, floored at zero.
    """
    if not isinstance(data, dict):
        return
    if upstream == "openai":
        u = data.get("usage")
        if not isinstance(u, dict) and isinstance(data.get("response"), dict):
            u = data["response"].get("usage")  # Responses API: response.completed carries the usage
        if not isinstance(u, dict):
            return
        if call is not None and call.get("pricing_snapshot"):
            if not u and call.get("pricing_usage"):
                return  # a later empty SSE placeholder cannot erase known usage
            # Catalog pricing must distinguish a missing counter from a reported
            # zero and reject inconsistent cache totals before lossy normalization.
            call["pricing_usage"] = u
        details = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
        alt = u.get("input_tokens_details") if isinstance(u.get("input_tokens_details"), dict) else {}
        cached = next((_count(src[k]) for src, k in ((details, "cached_tokens"), (alt, "cached_tokens"), (u, "cache_read_input_tokens"),
                                                     (u, "cache_read_tokens"), (u, "cached_tokens"), (u, "prompt_cache_hit_tokens"))
                       if k in src), 0)
        written = _count(details.get("cache_write_tokens") or u.get("cache_creation_input_tokens"))
        prompt = _count(u.get("prompt_tokens") if "prompt_tokens" in u else u.get("input_tokens"))
        output = _count(u.get("completion_tokens") if "completion_tokens" in u else u.get("output_tokens"))
        usage[:] = [max(0, prompt - cached - written), output, written, cached, 0]
        return
    start = data.get("type") == "message_start"
    u = (data.get("message") or {}).get("usage") if start else data.get("usage")
    if not isinstance(u, dict):
        return
    if call is not None and call.get("pricing_snapshot"):
        # Anthropic's input excludes caches. A nonstandard total cannot recover
        # a missing input counter as though this were OpenAI's inclusive total.
        evidence = {k: v for k, v in u.items() if k != "total_tokens"}
        if start:
            # message_start's output is a protocol placeholder, not final usage.
            call["pricing_usage"] = {k: v for k, v in evidence.items() if k != "output_tokens"}
        elif data.get("type") == "message_delta":
            # Anthropic splits the real input/cache counters and output count
            # across events. Merge evidence without inventing absent counters;
            # zero placeholders must not erase a previously positive counter.
            def merge(previous, incoming):
                result = dict(previous)
                for key, value in incoming.items():
                    prior = result.get(key)
                    if isinstance(value, dict) and isinstance(prior, dict):
                        result[key] = merge(prior, value)
                    elif type(value) in (int, float) and value == 0 and type(prior) in (int, float) and prior > 0:
                        continue
                    else:
                        result[key] = value
                return result
            call["pricing_usage"] = merge(call.get("pricing_usage", {}), evidence)
        else:
            call["pricing_usage"] = evidence
    split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    written = _count(u.get("cache_creation_input_tokens"))
    one_hour = _count(split.get("ephemeral_1h_input_tokens")) if split else 0
    five_min = _count(split.get("ephemeral_5m_input_tokens")) if split else 0
    if split and not (five_min or one_hour):
        five_min = written  # a total without a split is billed at the 5-minute price
    elif not split:
        five_min = written
    values = {0: _count(u.get("input_tokens")), 1: _count(u.get("output_tokens")), 2: five_min, 3: _count(u.get("cache_read_input_tokens")),
              4: one_hour}
    for index, value in values.items():
        if start:
            if index != 1:
                usage[index] = value
        elif value > 0:
            usage[index] = value


def stream_progress(upstream, data, call):
    """Track whether the stream reached its terminal event and how much reply text it carried (for estimates)."""
    if not isinstance(data, dict):
        return
    if upstream == "anthropic":
        if data.get("type") == "message_stop":
            call["done"] = True
        delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
        texts = [delta.get("text"), delta.get("thinking"), delta.get("partial_json")]
    elif str(data.get("type", "")).startswith("response."):  # Responses API events
        if data["type"] in ("response.completed", "response.incomplete"):
            call["done"] = True
        texts = [data.get("delta")] if data["type"].endswith(".delta") else []
    else:
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        # Usage may be reported mid-stream; only a terminal marker proves the reply completed.
        if any(isinstance(c, dict) and c.get("finish_reason") for c in choices):
            call["done"] = True
        texts = []
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) and isinstance(choice.get("delta"), dict) else {}
            texts += [delta.get("content"), delta.get("reasoning_content")]
            calls = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else []
            texts += [c.get("function", {}).get("arguments") for c in calls if isinstance(c, dict) and isinstance(c.get("function"), dict)]
    call["reply_bytes"] += sum(len(t.encode()) for t in texts if isinstance(t, str))
