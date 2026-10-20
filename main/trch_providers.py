"""HTTP adapters for hosted model APIs and an Ollama server.

The module has no third-party dependencies.  ``chat`` performs a single
blocking request; ``chat_stream`` yields text chunks as they arrive.  Both use
the same provider resolution rules so the CLI and the legacy interface behave
identically.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


PROVIDERS = {
    "openai": {"model": "gpt-4o-mini", "key": "OPENAI_API_KEY", "url": "https://api.openai.com/v1"},
    "anthropic": {"model": "claude-3-5-haiku-latest", "key": "ANTHROPIC_API_KEY"},
    "gemini": {"model": "gemini-2.0-flash", "key": "GEMINI_API_KEY"},
    "deepseek": {"model": "deepseek-chat", "key": "DEEPSEEK_API_KEY", "url": "https://api.deepseek.com"},
    "mistral": {"model": "mistral-small-latest", "key": "MISTRAL_API_KEY", "url": "https://api.mistral.ai/v1"},
    "kimi": {"model": "moonshot-v1-8k", "key": "MOONSHOT_API_KEY", "url": "https://api.moonshot.ai/v1"},
    "ollama": {"model": "llama3.2:3b"},
    "openai_compatible": {"model": ""},
}

ALIASES = {
    "chatgpt": "openai",
    "gpt": "openai",
    "claude": "anthropic",
    "kimi/moonshot": "kimi",
    "moonshot": "kimi",
    "local": "ollama",
    "compatible": "openai_compatible",
    "openai-compatible": "openai_compatible",
    "openai compatible": "openai_compatible",
    "custom": "openai_compatible",
}

# Models offered by ``trch models`` as suggestions.  Any model ID accepted by
# the provider can still be supplied with --model.
MODEL_CHOICES = {
    "openai": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o3", "o4-mini"],
    "anthropic": [
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-3-7-sonnet-latest",
        "claude-3-5-haiku-latest",
    ],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "mistral": ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest", "codestral-latest"],
    "kimi": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "ollama": ["llama3.2:3b", "llama3.2:1b", "llama3.1:8b", "qwen2.5-coder:3b"],
    "openai_compatible": [],
}

CLOUD_PROVIDERS = ("openai", "anthropic", "gemini", "deepseek", "mistral", "kimi", "openai_compatible")


class ProviderError(RuntimeError):
    """Raised when a provider cannot return a valid chat response."""


def normalize_provider(provider):
    name = (provider or "").strip().lower()
    return ALIASES.get(name, name)


def provider_env_var(provider):
    """Return the environment variable that holds a provider's API key."""
    provider = normalize_provider(provider)
    if provider == "openai_compatible":
        return "TRCH_OPENAI_COMPAT_API_KEY"
    return PROVIDERS.get(provider, {}).get("key")


def ollama_host(environ=None):
    env = os.environ if environ is None else environ
    host = (env.get("OLLAMA_HOST") or env.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
    if "://" not in host:
        host = f"http://{host}"
    return host


def configured_providers(environ=None):
    """Return provider names whose API key (or Ollama server) is available."""
    env = os.environ if environ is None else environ
    available = []
    for name in CLOUD_PROVIDERS:
        key = provider_env_var(name)
        base = env.get("TRCH_OPENAI_COMPAT_BASE_URL")
        if key and env.get(key) and (name != "openai_compatible" or base):
            available.append(name)
    if ollama_reachable(environ=env):
        available.append("ollama")
    return available


def ollama_reachable(environ=None, timeout=1.0):
    """Cheap health check for a local Ollama server."""
    try:
        with urllib.request.urlopen(f"{ollama_host(environ)}/api/tags", timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def list_ollama_models(environ=None, timeout=3.0):
    """Return installed Ollama model names, or an empty list when unreachable."""
    try:
        with urllib.request.urlopen(f"{ollama_host(environ)}/api/tags", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return sorted(item.get("name", "") for item in data.get("models", []) if item.get("name"))
    except Exception:
        return []


def _resolve(provider, model, environ):
    provider = normalize_provider(provider)
    if provider not in PROVIDERS:
        raise ProviderError(f"Unsupported provider: {provider or '(empty)'}")
    settings = PROVIDERS[provider]
    model = model or settings["model"]
    return provider, settings, model


def _request_json(url, payload, headers, timeout):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise ProviderError(f"{error.code} {error.reason}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ProviderError(str(error)) from error


def _open_stream(url, payload, headers, timeout):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream", **headers},
        method="POST",
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise ProviderError(f"{error.code} {error.reason}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ProviderError(str(error)) from error


def _sse_json(response):
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _ollama_messages(messages):
    return messages


def chat(provider, model, messages, timeout=120, environ=None):
    """Send messages to one configured provider and return assistant text."""
    env = os.environ if environ is None else environ
    provider, settings, model = _resolve(provider, model, env)

    if provider == "ollama":
        data = _request_json(
            f"{ollama_host(env)}/api/chat",
            {"model": model, "messages": _ollama_messages(messages), "stream": False},
            {},
            timeout,
        )
        return data["message"]["content"]

    if provider == "anthropic":
        api_key = env.get(settings["key"])
        if not api_key:
            raise ProviderError(f"Claude needs {settings['key']} set in the environment.")
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        user_messages = [item for item in messages if item["role"] != "system"]
        data = _request_json(
            "https://api.anthropic.com/v1/messages",
            {"model": model, "max_tokens": 4096, "system": system, "messages": user_messages},
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout,
        )
        return "".join(item.get("text", "") for item in data.get("content", []) if item.get("type") == "text")

    if provider == "gemini":
        api_key = env.get(settings["key"])
        if not api_key:
            raise ProviderError(f"Gemini needs {settings['key']} set in the environment.")
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        user_messages = [item for item in messages if item["role"] != "system"]
        contents = [
            {"role": "user" if item["role"] == "user" else "model", "parts": [{"text": item["content"]}]}
            for item in user_messages
        ]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model, safe='.-_')}:generateContent?key={urllib.parse.quote(api_key)}"
        )
        payload = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = _request_json(url, payload, {}, timeout)
        return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])

    base_url = settings.get("url") or env.get("TRCH_OPENAI_COMPAT_BASE_URL", "").rstrip("/")
    api_key = env.get(settings.get("key", "")) or env.get("TRCH_OPENAI_COMPAT_API_KEY", "")
    if not base_url:
        raise ProviderError("Compatible providers need TRCH_OPENAI_COMPAT_BASE_URL set.")
    if provider == "openai_compatible" and not api_key:
        raise ProviderError("Compatible providers need TRCH_OPENAI_COMPAT_API_KEY set.")
    if provider != "openai_compatible" and not api_key:
        raise ProviderError(f"{provider.title()} needs {settings['key']} set in the environment.")

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = _request_json(f"{base_url}/chat/completions", {"model": model, "messages": messages}, headers, timeout)
    return data["choices"][0]["message"]["content"]


def chat_stream(provider, model, messages, timeout=120, environ=None):
    """Yield assistant text chunks from a provider as they arrive.

    The first request is opened lazily, so calling this function does not make
    a network request until iteration starts.
    """
    env = os.environ if environ is None else environ
    provider, settings, model = _resolve(provider, model, env)

    if provider == "ollama":
        response = _open_stream(
            f"{ollama_host(env)}/api/chat",
            {"model": model, "messages": _ollama_messages(messages), "stream": True},
            {},
            timeout,
        )
        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunk = event.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if event.get("done"):
                    return

    if provider == "anthropic":
        api_key = env.get(settings["key"])
        if not api_key:
            raise ProviderError(f"Claude needs {settings['key']} set in the environment.")
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        user_messages = [item for item in messages if item["role"] != "system"]
        response = _open_stream(
            "https://api.anthropic.com/v1/messages",
            {"model": model, "max_tokens": 4096, "system": system, "messages": user_messages, "stream": True},
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout,
        )
        with response:
            for event in _sse_json(response):
                if event.get("type") == "content_block_delta":
                    chunk = event.get("delta", {}).get("text", "")
                    if chunk:
                        yield chunk
        return

    if provider == "gemini":
        api_key = env.get(settings["key"])
        if not api_key:
            raise ProviderError(f"Gemini needs {settings['key']} set in the environment.")
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        user_messages = [item for item in messages if item["role"] != "system"]
        contents = [
            {"role": "user" if item["role"] == "user" else "model", "parts": [{"text": item["content"]}]}
            for item in user_messages
        ]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model, safe='.-_')}:streamGenerateContent?alt=sse&key={urllib.parse.quote(api_key)}"
        )
        payload = {"contents": contents, "stream": True}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        response = _open_stream(url, payload, {}, timeout)
        with response:
            for event in _sse_json(response):
                for candidate in event.get("candidates", []):
                    for part in candidate.get("content", {}).get("parts", []):
                        chunk = part.get("text", "")
                        if chunk:
                            yield chunk
        return

    # OpenAI and OpenAI-compatible providers (DeepSeek, Mistral, Kimi, custom).
    base_url = settings.get("url") or env.get("TRCH_OPENAI_COMPAT_BASE_URL", "").rstrip("/")
    api_key = env.get(settings.get("key", "")) or env.get("TRCH_OPENAI_COMPAT_API_KEY", "")
    if not base_url:
        raise ProviderError("Compatible providers need TRCH_OPENAI_COMPAT_BASE_URL set.")
    if provider == "openai_compatible" and not api_key:
        raise ProviderError("Compatible providers need TRCH_OPENAI_COMPAT_API_KEY set.")
    if provider != "openai_compatible" and not api_key:
        raise ProviderError(f"{provider.title()} needs {settings['key']} set in the environment.")

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = _open_stream(
        f"{base_url}/chat/completions",
        {"model": model, "messages": messages, "stream": True},
        headers,
        timeout,
    )
    with response:
        for event in _sse_json(response):
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                chunk = delta.get("content") or ""
                if chunk:
                    yield chunk
