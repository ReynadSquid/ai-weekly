"""HTTP на стандартной библиотеке: GET с ретраями, без внешних зависимостей."""
from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from common import USER_AGENT, mask


@dataclass
class Response:
    url: str          # итоговый URL после редиректов
    status: int
    body: bytes
    headers: dict

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class FetchError(Exception):
    pass


def http_get(url: str, *, timeout: float = 20, headers: dict | None = None,
             method: str = "GET", max_bytes: int = 5_000_000) -> Response:
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.8,ru;q=0.6",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes) if method != "HEAD" else b""
            if body[:2] == b"\x1f\x8b":  # некоторые сайты отдают gzip без запроса
                body = gzip.decompress(body)
            return Response(resp.geturl(), resp.status, body, dict(resp.headers))
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(200_000)
        except Exception:
            pass
        return Response(e.geturl() or url, e.code, body, dict(e.headers or {}))


def get_with_retries(url: str, *, retries: int = 3, pause: float = 5, timeout: float = 20,
                     headers: dict | None = None, sleep=time.sleep) -> Response:
    """3 попытки с паузой; 5xx/429/сетевые ошибки повторяются, 4xx — нет."""
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            resp = http_get(url, timeout=timeout, headers=headers)
            if resp.status < 400:
                return resp
            last_err = f"HTTP {resp.status}"
            if resp.status < 500 and resp.status != 429:
                break
        except Exception as e:  # сетевые ошибки, таймауты, SSL
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            sleep(pause * attempt)
    raise FetchError(mask(f"{url}: {last_err}"))
