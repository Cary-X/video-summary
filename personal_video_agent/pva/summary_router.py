"""Per-job summary routes. Provider credentials are kept in process memory."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlsplit


_keys: dict[tuple[str, str], str] = {}


def _api_endpoint(value: str) -> tuple[str, bool]:
    base_url = value.strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        parsed.port
    except ValueError:
        raise ValueError("Enter a valid summary API base URL.") from None
    local = host in {"localhost", "127.0.0.1", "::1"}
    if (not host or parsed.username is not None or parsed.query or parsed.fragment
            or parsed.scheme not in ({"https", "http"} if local else {"https"})
            or any(ch.isspace() for ch in base_url)
            or base_url.endswith(("/chat/completions", "/models"))):
        raise ValueError("Enter the API base URL without credentials, query, or endpoint suffix.")
    return base_url, local


@dataclass(frozen=True)
class SummaryRoute:
    backend: str = "local"
    method: str = "auto"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)

    def register(self) -> dict[str, str]:
        if self.backend not in {"local", "llama.cpp", "api"}:
            raise ValueError("Choose llama.cpp, Local Ollama, or a summary API.")
        if self.method not in {"auto", "direct", "batched"}:
            raise ValueError("Choose Auto, Single pass, or Batched.")
        base_url = model = ""
        if self.backend == "api":
            base_url, local = _api_endpoint(self.base_url)
            model = self.model.strip()
            if not model:
                raise ValueError("Enter the summary API model name.")
            key = self.api_key.strip()
            if key:
                if "\r" in key or "\n" in key:
                    raise ValueError("Enter the summary API key on one line.")
                _keys[base_url, model] = key
            elif not local and (base_url, model) not in _keys:
                raise ValueError("Enter the summary API key in Settings, then retry.")
        return {"backend": self.backend, "method": self.method, "base_url": base_url,
                "model": model}


current: ContextVar[SummaryRoute] = ContextVar("summary_route", default=SummaryRoute())


@contextmanager
def use(route: SummaryRoute):
    token = current.set(SummaryRoute(**route.register()))
    try:
        yield
    finally:
        current.reset(token)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def list_models(base_url: str, api_key: str = "", *, saved_model: str = "") -> list[str]:
    base_url, _ = _api_endpoint(base_url)
    key = api_key.strip() or _keys.get((base_url, saved_model.strip()), "")
    if "\r" in key or "\n" in key:
        raise ValueError("Enter the summary API key on one line.")
    headers = {"Authorization": "Bearer " + key} if key else {}
    request = urllib.request.Request(base_url + "/models", headers=headers)
    try:
        with _opener.open(request, timeout=5) as response:
            payload = json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("Enter an API key with permission to list models.") from None
        raise RuntimeError("This API did not provide a model list.") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise RuntimeError("This API did not provide a model list.") from None
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("This API did not provide a model list.")
    return sorted({item["id"].strip() for item in items if isinstance(item, dict)
                   and isinstance(item.get("id"), str) and item["id"].strip()})


def complete(route: SummaryRoute, messages: list[dict]) -> dict:
    key = _keys.get((route.base_url, route.model), "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(
        route.base_url + "/chat/completions",
        data=json.dumps({"model": route.model, "messages": messages}, ensure_ascii=False).encode("utf-8"),
        headers=headers)
    try:
        with _opener.open(request, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Summary API returned HTTP {exc.code}. Check the endpoint, model, key, and account.") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RuntimeError("The summary API did not return a valid response. Check the endpoint and retry.") from None
