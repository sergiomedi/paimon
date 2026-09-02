"""An in-process stand-in for Azure AI Search.

Implements enough of the document and search APIs for the VectorStore contract to
run against the real adapter: key handling, tenant and document filters, vector
and lexical search, deletion, and per-document failures.

What this proves and what it does not. It exercises the adapter's own logic —
request shapes, key encoding, filter construction, hit mapping, mismatch guards —
end to end through the same assertions the pgvector adapter passes. It does not
prove the adapter works against the real service, because it is a model of Azure
written by the same person who wrote the adapter. The contract class points at
this by default and at a real search service when one is configured; only the
second run settles the question.
"""

import json
import math
import re
from typing import Any

import httpx

_TOKEN = re.compile(r"[a-z0-9]+")
_TENANT = re.compile(r"tenant_id eq '([^']*)'")
_DOCUMENTS = re.compile(r"search\.in\(document_id, '([^']*)', ','\)")
_DOCUMENT_EQ = re.compile(r"document_id eq '([^']*)'")


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


class FakeAzureSearchService:
    """Holds documents and answers the two endpoints the adapter uses."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.index_definition: dict[str, Any] | None = None
        self.reject_keys: set[str] = set()
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        """A transport that routes to this service."""
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}

        if path.endswith("/docs/index"):
            return self._index(payload)
        if path.endswith("/docs/search"):
            return self._search(payload)
        if request.method == "POST" and "/indexes/" in path:
            self.index_definition = payload
            return httpx.Response(201, json=payload)
        return httpx.Response(404, json={"error": {"code": "NotFound"}})

    def _index(self, payload: dict[str, Any]) -> httpx.Response:
        results = []
        for action in payload.get("value", []):
            key = action["id"]
            if key in self.reject_keys:
                results.append({"key": key, "status": False, "errorMessage": "rejected by test"})
                continue
            if action["@search.action"] == "delete":
                self.documents.pop(key, None)
            else:
                self.documents[key] = {k: v for k, v in action.items() if k != "@search.action"}
            results.append({"key": key, "status": True})
        return httpx.Response(200, json={"value": results})

    def _visible(self, odata: str | None) -> list[dict[str, Any]]:
        documents = list(self.documents.values())
        if not odata:
            return documents

        tenant = _TENANT.search(odata)
        if tenant:
            documents = [d for d in documents if d.get("tenant_id") == tenant.group(1)]

        listed = _DOCUMENTS.search(odata)
        if listed:
            wanted = set(listed.group(1).split(","))
            documents = [d for d in documents if d.get("document_id") in wanted]

        single = _DOCUMENT_EQ.search(odata)
        if single:
            documents = [d for d in documents if d.get("document_id") == single.group(1)]

        return documents

    def _search(self, payload: dict[str, Any]) -> httpx.Response:
        documents = self._visible(payload.get("filter"))
        top = int(payload.get("top") or 50)
        query = payload.get("search")
        vectors = payload.get("vectorQueries") or []

        scored: list[tuple[float, dict[str, Any]]] = []
        if vectors:
            wanted = vectors[0]["vector"]
            scored = [(_cosine(wanted, d["embedding"]), d) for d in documents]
        elif query and query != "*":
            terms = set(_TOKEN.findall(query.lower()))
            for document in documents:
                tokens = _TOKEN.findall(str(document.get("text", "")).lower())
                tokens += _TOKEN.findall(" ".join(document.get("heading_path") or []).lower())
                overlap = sum(1 for token in tokens if token in terms)
                if overlap:
                    scored.append((overlap / max(len(tokens), 1), document))
        else:
            scored = [(1.0, document) for document in documents]

        scored.sort(key=lambda pair: pair[0], reverse=True)
        selected = payload.get("select")
        value = []
        for score, document in scored[:top]:
            item = {"id": document["id"]} if selected == "id" else dict(document)
            item["@search.score"] = score
            value.append(item)
        return httpx.Response(200, json={"value": value})
