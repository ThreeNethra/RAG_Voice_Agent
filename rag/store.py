"""A tiny vector store: a matrix of vectors plus the chunk list.

For 100 chunks a numpy matrix multiply is instant, and seeing it in 10 lines
shows what a "vector database" fundamentally does. Dedicated stores (Chroma,
Qdrant, pgvector, FAISS) add: approximate nearest-neighbour indexes (HNSW) so
search stays fast at millions of vectors, metadata filtering, persistence,
concurrent writes. We'll discuss when to switch in the production lesson.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
from dataclasses import asdict
from pathlib import Path

import numpy as np

from .access import acl_fingerprint
from .bm25 import TOKENIZER_VERSION
from .chunking import Chunk, chunk_corpus
from .embedders import get_embedder
from .ingest import load_corpus

CHUNKER_VERSION = "1"  # bump when chunking logic changes: old vectors then no longer match their chunks


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def index_dir(root: str | Path, data: str | Path, strategy: str, embedder: str) -> Path:
    """index/<dataset-name>/<strategy>__<embedder>, so many datasets can coexist."""
    safe = embedder.replace(":", "_").replace("/", "_")
    return Path(root) / "index" / Path(data).name / f"{strategy}__{safe}"


def _recover(out: Path) -> None:
    """A crash between the two renames of a swap leaves <dir>.old and no <dir>: restore it."""
    old = out.with_name(out.name + ".old")
    if old.exists() and not out.exists():
        os.rename(old, out)
    elif old.exists():
        shutil.rmtree(old, ignore_errors=True)


def _write_atomic(out: Path, chunks, vectors, meta: dict, emb_obj=None) -> None:
    """Write the whole index to <dir>.tmp, then swap it in. A reader (or a crash) never sees a half-written
    index: it sees the old one or the new one."""
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    np.save(tmp / "vectors.npy", vectors)
    (tmp / "chunks.json").write_text(json.dumps([asdict(c) for c in chunks], ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if emb_obj is not None and getattr(emb_obj, "corpus_dependent", False):  # the fitted vocabulary is part of the index
        with open(tmp / "tfidf.pkl", "wb") as f:
            pickle.dump(emb_obj, f)
    old = out.with_name(out.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        os.rename(out, old)
    os.rename(tmp, out)
    shutil.rmtree(old, ignore_errors=True)


def _meta(embedder_spec: str, chunks, docs, corpus_dir=None) -> dict:
    return {"embedder": embedder_spec, "chunker": CHUNKER_VERSION, "tokenizer": TOKENIZER_VERSION,
            "chunk_hashes": [_h(c.embed_text) for c in chunks],
            "doc_hashes": {d.source: _h(d.text) for d in docs},
            "doc_meta": {d.source: {"acl": list(d.acl), "status": d.status} for d in docs},
            "acl_fingerprint": acl_fingerprint(corpus_dir) if corpus_dir else None}


def build_index(chunks: list[Chunk], embedder_spec: str, out_dir: str | Path, docs=None, corpus_dir=None) -> None:
    """Full build. Prefer sync_index(): it does the same job but only re-embeds what changed."""
    out = Path(out_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    _recover(out)
    if not chunks:
        raise RuntimeError("no chunks to index: the corpus is empty or contains no extractable text")
    emb = get_embedder(embedder_spec)
    texts = [c.embed_text for c in chunks]
    emb.fit(texts)
    vectors = emb.encode_docs(texts)
    _write_atomic(out, chunks, vectors, _meta(embedder_spec, chunks, docs or [], corpus_dir), emb)


def sync_index(root, data, strategy: str, embedder_spec: str) -> dict:
    """INCREMENTAL INDEXING. Make the index match the corpus on disk, doing as little work as is correct.

    Every chunk is identified by a hash of the exact text we embed. Vectors for chunks whose hash is
    unchanged are REUSED; only new texts are embedded. Documents are compared by hash, so added /
    changed / removed files are reported. Permission or status changes need no embedding at all, but
    the chunk metadata is always rewritten so they take effect.

    The exception, and a lesson: tf-idf is fitted on the whole corpus, so ANY text change alters every
    vector and forces a full rebuild. Pretrained embedders (st, hash) map a text to the same vector
    regardless of its neighbours, which is what makes incremental updates possible at all.
    """
    data = Path(data)
    corpus = data / "corpus"
    ingest_warnings: list[str] = []
    docs = load_corpus(corpus, ingest_warnings)
    chunks = chunk_corpus(docs, strategy)
    if not chunks:
        # Never replace a working index with an empty one, and never serve one: it almost always means a wrong path or a failed export.
        why = f" ({len(ingest_warnings)} file(s) skipped: {'; '.join(ingest_warnings[:3])})" if ingest_warnings else ""
        raise RuntimeError(f"no indexable text found in {corpus}{why}. The existing index, if any, was left untouched.")
    texts = [c.embed_text for c in chunks]
    meta = _meta(embedder_spec, chunks, docs, corpus)
    hashes = meta["chunk_hashes"]
    out = index_dir(root, data, strategy, embedder_spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    _recover(out)
    emb = get_embedder(embedder_spec)
    rep = {"added": [], "changed": [], "removed": [], "acl_changed": [], "status_changed": [], "unchanged": 0,
           "chunks_total": len(chunks), "chunks_embedded": 0, "chunks_reused": 0, "full_rebuild": False,
           "reason": "", "ingest_warnings": ingest_warnings}
    old = None
    if (out / "meta.json").exists():
        om = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        if om.get("embedder") == embedder_spec and om.get("chunker") == CHUNKER_VERSION and om.get("tokenizer") == TOKENIZER_VERSION and "chunk_hashes" in om:
            old = om
    if old is None:
        rep["full_rebuild"], rep["reason"] = True, "no compatible index yet"
        rep["added"] = sorted(meta["doc_hashes"])
    else:
        od, nd = old["doc_hashes"], meta["doc_hashes"]
        rep["added"] = sorted(set(nd) - set(od))
        rep["removed"] = sorted(set(od) - set(nd))
        rep["changed"] = sorted(s for s in nd if s in od and nd[s] != od[s])
        rep["unchanged"] = sum(1 for s in nd if s in od and nd[s] == od[s])
        om_meta, nm_meta = old.get("doc_meta", {}), meta["doc_meta"]
        rep["acl_changed"] = sorted(s for s in nm_meta if s in om_meta and nm_meta[s]["acl"] != om_meta[s]["acl"])
        rep["status_changed"] = sorted(s for s in nm_meta if s in om_meta and nm_meta[s]["status"] != om_meta[s]["status"])
    if old is not None:
        old_vecs = np.load(out / "vectors.npy")
        if emb.corpus_dependent:
            if hashes == old["chunk_hashes"]:
                with open(out / "tfidf.pkl", "rb") as f:
                    emb = pickle.load(f)
                vectors, rep["chunks_reused"] = old_vecs, len(chunks)
            else:
                old = None
                rep["full_rebuild"] = True
                rep["reason"] = f"{embedder_spec} is fitted on the whole corpus: any text change alters every vector"
        else:
            have = {h: old_vecs[i] for i, h in enumerate(old["chunk_hashes"])}
            need = {}
            for t, h in zip(texts, hashes):
                if h not in have:
                    need.setdefault(h, t)
            if need:
                for h, v in zip(need, emb.encode_docs(list(need.values()))):
                    have[h] = v
            vectors = np.stack([have[h] for h in hashes]) if hashes else np.zeros((0, old_vecs.shape[1]), "float32")
            rep["chunks_embedded"], rep["chunks_reused"] = len(need), len(chunks) - len(need)
    if old is None:
        emb.fit(texts)
        vectors = emb.encode_docs(texts)
        rep["chunks_embedded"] = len(chunks)
    _write_atomic(out, chunks, vectors, meta, emb)
    return rep


def load_index(index_dir: str | Path):
    d = Path(index_dir)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    chunks = [Chunk(**c) for c in json.loads((d / "chunks.json").read_text(encoding="utf-8"))]
    vectors = np.load(d / "vectors.npy")
    if meta["embedder"] == "tfidf":
        with open(d / "tfidf.pkl", "rb") as f:
            emb = pickle.load(f)
    else:
        emb = get_embedder(meta["embedder"])
    return chunks, vectors, emb
