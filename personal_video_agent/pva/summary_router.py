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
            base_url = self.base_url.strip().rstrip("/")
            model = self.model.strip()
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
                    or base_url.endswith("/chat/completions")):
                raise ValueError("Enter an HTTPS API base URL, without credentials, query, or /chat/completions.")
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
