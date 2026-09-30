"""Shared state: Upstash Redis over REST in production, an in-memory stand-in otherwise.

Serverless instances share nothing, so rate limits, circuit breakers, metrics and
the exact-match cache all live here. Commands are batched through Upstash's
/pipeline endpoint so a request pays one round trip, not five.
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any

import httpx


class Store:
    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        raise NotImplementedError

    async def cmd(self, *command: Any) -> Any:
        return (await self.pipeline([list(command)]))[0]

    async def aclose(self) -> None:
        pass


class UpstashStore(Store):
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(5.0),
            transport=transport,
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120),
        )

    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        if not commands:
            return []
        body = [[str(x) for x in c] for c in commands]
        r = await self._client.post(f"{self._url}/pipeline", json=body)
        r.raise_for_status()
        return [item.get("result") if "error" not in item else None for item in r.json()]

    async def aclose(self) -> None:
        await self._client.aclose()


class MemoryStore(Store):
    """Implements the handful of Redis commands Sluice uses. Single process only."""

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._exp: dict[str, float] = {}

    def _alive(self, key: str) -> bool:
        exp = self._exp.get(key)
        if exp is not None and exp <= time.time():
            self._data.pop(key, None)
            self._exp.pop(key, None)
            return False
        return key in self._data

    def _run(self, c: list[Any]) -> Any:
        op, args = str(c[0]).upper(), c[1:]
        key = str(args[0]) if args else ""
        if op == "GET":
            return self._data.get(key) if self._alive(key) else None
        if op == "MGET":
            return [self._data.get(str(k)) if self._alive(str(k)) else None for k in args]
        if op == "SET":
            self._data[key] = str(args[1])
            self._exp.pop(key, None)
            if len(args) >= 4 and str(args[2]).upper() == "EX":
                self._exp[key] = time.time() + int(args[3])
            return "OK"
        if op in ("INCR", "INCRBY"):
            self._alive(key)
            val = int(self._data.get(key, 0)) + (int(args[1]) if op == "INCRBY" else 1)
            self._data[key] = str(val)
            return val
        if op == "EXPIRE":
            if self._alive(key):
                self._exp[key] = time.time() + int(args[1])
                return 1
            return 0
        if op == "DEL":
            n = 0
            for k in args:
                n += int(self._data.pop(str(k), None) is not None)
                self._exp.pop(str(k), None)
            return n
        if op == "LPUSH":
            if not self._alive(key):
                self._data[key] = []
            lst = self._data[key]
            for v in args[1:]:
                lst.insert(0, str(v))
            return len(lst)
        if op == "LTRIM":
            lst = self._data.get(key, [])
            start, stop = int(args[1]), int(args[2])
            self._data[key] = lst[start : (stop + 1) if stop >= 0 else len(lst) + stop + 1]
            return "OK"
        if op == "LRANGE":
            lst = self._data.get(key, []) if self._alive(key) else []
            start, stop = int(args[1]), int(args[2])
            return lst[start : (stop + 1) if stop >= 0 else len(lst) + stop + 1]
        if op in ("HINCRBY", "HINCRBYFLOAT"):
            if not self._alive(key):
                self._data[key] = {}
            h = self._data[key]
            field = str(args[1])
            if op == "HINCRBY":
                h[field] = str(int(h.get(field, 0)) + int(args[2]))
            else:
                h[field] = str(float(h.get(field, 0)) + float(args[2]))
            return h[field]
        if op == "HSET":
            h = self._data.setdefault(key, {})
            for i in range(1, len(args) - 1, 2):
                h[str(args[i])] = str(args[i + 1])
            return 1
        if op == "HGETALL":
            h = self._data.get(key, {}) if self._alive(key) else {}
            out: list[str] = []
            for k, v in h.items():
                out += [k, v]
            return out
        if op == "KEYS":
            return [k for k in list(self._data) if self._alive(k) and fnmatch.fnmatch(k, key)]
        raise ValueError(f"MemoryStore does not implement {op}")

    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        return [self._run(c) for c in commands]


def hash_to_dict(flat: list[Any] | None) -> dict[str, str]:
    flat = flat or []
    return {str(flat[i]): str(flat[i + 1]) for i in range(0, len(flat) - 1, 2)}
