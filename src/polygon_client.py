"""Thin wrapper around the Polygon.io REST API.

Scope is intentionally narrow: the pieces we need for post-trade
enrichment are minute / second aggregates for an option contract,
contract reference metadata, and the underlying equity's aggregates
for regime context. Every other endpoint is deferred until a caller
actually needs it.

Design notes:
  * The API key is read from the ``POLYGON_API_KEY`` env var. It is
    never accepted as a positional arg so it cannot accidentally be
    logged or committed.
  * Retries use tenacity with exponential backoff on 429 and 5xx.
  * Polygon Options Starter is documented as unlimited requests but a
    polite ~100ms floor between calls keeps us well clear of any
    unadvertised throttle.
  * Aggregate responses above the page limit return a ``next_url``
    cursor; ``_paginate`` follows it until exhausted.
"""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any, Iterator, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


BASE_URL = "https://api.polygon.io"
# Polygon Options Starter ($29/mo) enforces a per-minute rate limit
# despite the marketing copy saying "unlimited". Empirically, ~0.7s
# between calls keeps us just inside it. Tune via the constructor.
_MIN_INTERVAL_S_DEFAULT = 0.7


class PolygonError(RuntimeError):
    """Polygon returned a non-success status we should not retry."""


class PolygonRetryable(RuntimeError):
    """Transient Polygon error (429 / 5xx / network) — retryable."""


class PolygonClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        min_interval_s: float = _MIN_INTERVAL_S_DEFAULT,
        cache: bool = True,
    ):
        key = api_key or os.environ.get("POLYGON_API_KEY")
        if not key:
            raise RuntimeError(
                "POLYGON_API_KEY is not set. Export it locally or configure it "
                "as a GitHub Actions secret before running."
            )
        self._key = key
        self._session = session or requests.Session()
        self._last_call_ts = 0.0
        self._min_interval_s = min_interval_s
        self._cache_enabled = cache
        self._agg_cache: dict[tuple, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    def get_aggregates(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: date,
        to_date: date,
        adjusted: bool = True,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV aggregates for a ticker (option OCC or equity symbol).

        ``timespan`` is one of ``second``, ``minute``, ``hour``, ``day``.
        Returns a flat list of bars across all pages, each a dict with keys
        ``t`` (ms epoch), ``o``, ``h``, ``l``, ``c``, ``v``, ``vw``, ``n``.

        In-memory cache is keyed on (ticker, multiplier, timespan, from_date,
        to_date, adjusted) — deduplicates the common reference-backfill pattern
        where many trades on the same day share the same underlying window.
        """
        cache_key = (ticker, multiplier, timespan, from_date, to_date, adjusted)
        if self._cache_enabled and cache_key in self._agg_cache:
            return self._agg_cache[cache_key]

        path = (
            f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}"
            f"/{from_date:%Y-%m-%d}/{to_date:%Y-%m-%d}"
        )
        params = {
            "adjusted": "true" if adjusted else "false",
            "sort": "asc",
            "limit": limit,
        }
        bars = list(self._paginate(path, params, results_key="results"))
        if self._cache_enabled:
            self._agg_cache[cache_key] = bars
        return bars

    def get_option_contract(self, occ_symbol: str, as_of: Optional[date] = None) -> dict[str, Any]:
        """Reference metadata for a single option contract.

        Useful to verify a contract existed on a given date before firing
        a bar request that would otherwise return an empty response.
        """
        path = f"/v3/reference/options/contracts/{occ_symbol}"
        params: dict[str, Any] = {}
        if as_of is not None:
            params["as_of"] = f"{as_of:%Y-%m-%d}"
        payload = self._get(path, params)
        return payload.get("results", {})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _paginate(self, path: str, params: dict[str, Any], results_key: str) -> Iterator[dict[str, Any]]:
        payload = self._get(path, params)
        for row in payload.get(results_key, []) or []:
            yield row
        next_url = payload.get("next_url")
        while next_url:
            payload = self._get_absolute(next_url)
            for row in payload.get(results_key, []) or []:
                yield row
            next_url = payload.get("next_url")

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._get_absolute(f"{BASE_URL}{path}", params)

    @retry(
        retry=retry_if_exception_type(PolygonRetryable),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(8),
        reraise=True,
    )
    def _get_absolute(self, url: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._throttle()
        merged = dict(params or {})
        # Polygon accepts the key as a query param on every endpoint; next_url
        # comes back without it, so we always add it here.
        merged["apiKey"] = self._key
        try:
            resp = self._session.get(url, params=merged, timeout=30)
        except requests.RequestException as exc:
            raise PolygonRetryable(f"network error contacting polygon: {exc}") from exc

        if resp.status_code == 429:
            # Honor Retry-After header if Polygon sends one. Otherwise the
            # tenacity backoff loop handles the wait.
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    time.sleep(float(retry_after))
                except ValueError:
                    pass
            raise PolygonRetryable(f"polygon 429 rate limited")
        if 500 <= resp.status_code < 600:
            raise PolygonRetryable(f"polygon {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise PolygonError(f"polygon {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)
        self._last_call_ts = time.monotonic()
