"""Connectable RAG sources for IVY.

Mirrors the shape of `mcp_connector.py`: a JSON config at
`~/.ivy/rag_sources.json` lists ChromaDB collections to open at startup.
Each source registers a namespaced tool (`rag_<name>_search`) that the
principal model can call. Embeddings are computed with Ollama's
`nomic-embed-text` (default) so nothing leaves the machine.

Config format (Claude-Desktop-shaped, like mcp_servers.json)::

    {
      "ragSources": {
        "insyth-docs": {
          "path": "~/data/insyth-rag",
          "collection": "default",
          "embedding_model": "nomic-embed-text"
        },
        "notes": {
          "path": "~/Documents/notes-rag"
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

try:
    import chromadb
    from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
    import ollama
    _RAG_AVAILABLE = True
except ImportError:
    chromadb = None  # type: ignore
    EmbeddingFunction = object  # type: ignore
    ollama = None  # type: ignore
    _RAG_AVAILABLE = False


CONFIG_PATH = Path.home() / ".ivy" / "rag_sources.json"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"


def _default_config() -> dict:
    return {"ragSources": {}}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return _default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_config()


def ensure_config_file():
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_default_config(), f, indent=2)


def _expand(p: str) -> str:
    return str(Path(os.path.expanduser(p)).resolve())


# ─────────────────────────────────────────────
# Ollama-backed embedding function for Chroma
# ─────────────────────────────────────────────

class _OllamaEmbeddings(EmbeddingFunction if _RAG_AVAILABLE else object):
    """Embeds documents via Ollama's local embedding API.
    Implements Chroma's EmbeddingFunction protocol."""

    def __init__(self, model: str = DEFAULT_EMBEDDING_MODEL):
        self.model = model
        self._lock = threading.Lock()

    def __call__(self, texts):
        if not _RAG_AVAILABLE:
            return []
        out = []
        with self._lock:
            for t in texts:
                try:
                    r = ollama.embeddings(model=self.model, prompt=t)
                    # Newer ollama clients return dict-like with "embedding" key
                    emb = r.get("embedding") if isinstance(r, dict) else getattr(r, "embedding", None)
                    out.append(list(emb) if emb else [])
                except Exception:
                    out.append([])
        return out

    def name(self) -> str:
        return f"ollama-{self.model}"


# ─────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────

class _RAGSource:
    """One ChromaDB collection with its embedding function."""

    def __init__(self, name: str, path: str, collection: str, embedding_model: str):
        self.name = name
        self.path = _expand(path)
        self.collection_name = collection
        self.embedding_model = embedding_model
        self._client = chromadb.PersistentClient(path=self.path)
        self._embed = _OllamaEmbeddings(embedding_model)
        self._collection = self._client.get_or_create_collection(
            name=collection,
            embedding_function=self._embed,
        )

    def search(self, query: str, top_k: int = 5) -> dict:
        result = self._collection.query(
            query_texts=[query],
            n_results=max(1, min(top_k, 50)),
        )
        # result has keys: ids, documents, metadatas, distances (all list-of-list)
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        ids = (result.get("ids") or [[]])[0]
        hits = []
        for i, d in enumerate(docs):
            hits.append({
                "id": ids[i] if i < len(ids) else None,
                "score": round(1.0 - float(dists[i]), 4) if i < len(dists) else None,
                "metadata": metas[i] if i < len(metas) else None,
                "text": d[:600] if d else "",  # cap each hit
            })
        return {"source": self.name, "query": query, "hits": hits, "count": len(hits)}

    def count(self) -> int:
        try:
            return self._collection.count()
        except Exception:
            return 0

    def add_text(self, text: str, metadata: dict | None = None, doc_id: str | None = None):
        import uuid
        doc_id = doc_id or str(uuid.uuid4())
        self._collection.add(
            documents=[text],
            metadatas=[metadata or {}],
            ids=[doc_id],
        )
        return doc_id

    def add_file(self, path: str, chunk_size: int = 1500, overlap: int = 200) -> dict:
        """Read a file, chunk it, and add each chunk as a doc.
        Returns {chunks_added, total_chars, file}."""
        import uuid
        full = Path(_expand(path))
        if not full.is_file():
            return {"error": f"not a file: {path}"}
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return {"error": f"read failed: {e}"}
        chunks = []
        i = 0
        while i < len(text):
            chunks.append(text[i:i + chunk_size])
            i += max(1, chunk_size - overlap)
        ids = [f"{full.name}-{uuid.uuid4().hex[:8]}-{n}" for n in range(len(chunks))]
        metas = [{"file": str(full), "chunk": n} for n in range(len(chunks))]
        if chunks:
            self._collection.add(documents=chunks, metadatas=metas, ids=ids)
        return {"chunks_added": len(chunks), "total_chars": len(text), "file": str(full)}


class RAGRegistry:
    SEP = "_"  # namespace: rag_<source>_search

    def __init__(self):
        self._sources: dict[str, _RAGSource] = {}
        self._tools: dict[str, dict] = {}
        self._server_for_tool: dict[str, str] = {}  # parity w/ MCP shape

    def connect_all(self) -> dict:
        if not _RAG_AVAILABLE:
            return {"_error": "chromadb or ollama not installed in the venv"}
        config = load_config()
        sources = config.get("ragSources", {})
        results: dict[str, dict] = {}
        for name, cfg in sources.items():
            try:
                src = _RAGSource(
                    name=name,
                    path=cfg.get("path", str(Path.home() / ".ivy" / "rag" / name)),
                    collection=cfg.get("collection", "default"),
                    embedding_model=cfg.get("embedding_model", DEFAULT_EMBEDDING_MODEL),
                )
                self._sources[name] = src
                self._register_tools(name)
                results[name] = {"connected": True, "docs": src.count()}
            except Exception as e:
                results[name] = {"connected": False, "error": str(e)[:200]}
        return results

    def _register_tools(self, source_name: str):
        """One search tool per source, namespaced as `rag_<source>_search`."""
        ns = f"rag{self.SEP}{source_name}{self.SEP}search"
        self._tools[ns] = {
            "type": "function",
            "function": {
                "name": ns,
                "description": (
                    f"Search the '{source_name}' RAG knowledge base via semantic "
                    "vector similarity. Returns the top matching documents with "
                    "scores and metadata. Use when the user's question might be "
                    "answered from previously-indexed local knowledge."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language query."},
                        "top_k": {"type": "integer", "description": "How many results (default 5, max 50).", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        }
        self._server_for_tool[ns] = source_name

    def disconnect_all(self):
        # Chroma persistent clients close cleanly when GC'd.
        self._sources.clear()
        self._tools.clear()
        self._server_for_tool.clear()

    def reload(self) -> dict:
        self.disconnect_all()
        return self.connect_all()

    # ── For the LLM ───────────────────────────────────────

    def tools_for_llm(self) -> list[dict]:
        return list(self._tools.values())

    def has_tool(self, namespaced_name: str) -> bool:
        return namespaced_name in self._tools

    def call_tool(self, namespaced_name: str, args: dict) -> Any:
        if namespaced_name not in self._tools:
            return {"error": f"unknown RAG tool: {namespaced_name}"}
        src_name = self._server_for_tool[namespaced_name]
        src = self._sources.get(src_name)
        if not src:
            return {"error": f"source '{src_name}' not connected"}
        if namespaced_name.endswith("_search"):
            return src.search(args.get("query", ""), args.get("top_k", 5))
        return {"error": f"unsupported tool: {namespaced_name}"}

    # ── For the CLI ───────────────────────────────────────

    def status(self) -> dict:
        cfg_sources = load_config().get("ragSources", {})
        out_sources = {}
        for name in cfg_sources:
            src = self._sources.get(name)
            out_sources[name] = {
                "connected": src is not None,
                "path": cfg_sources[name].get("path", "?"),
                "embedding_model": cfg_sources[name].get("embedding_model", DEFAULT_EMBEDDING_MODEL),
                "docs": src.count() if src else 0,
            }
        return {
            "config_path": str(CONFIG_PATH),
            "configured": len(cfg_sources),
            "connected": len(self._sources),
            "total_tools": len(self._tools),
            "sources": out_sources,
        }

    def search(self, source_name: str, query: str, top_k: int = 5) -> dict:
        src = self._sources.get(source_name)
        if not src:
            return {"error": f"source '{source_name}' not connected"}
        return src.search(query, top_k)

    def add_file(self, source_name: str, file_path: str, chunk_size: int = 1500) -> dict:
        src = self._sources.get(source_name)
        if not src:
            return {"error": f"source '{source_name}' not connected"}
        return src.add_file(file_path, chunk_size=chunk_size)


REGISTRY = RAGRegistry()
