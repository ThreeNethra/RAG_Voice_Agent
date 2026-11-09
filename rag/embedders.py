"""Stage 3: EMBEDDINGS.

An embedder maps text -> a vector. Retrieval then becomes "find the nearest
vectors to the question's vector" (cosine similarity = dot product once vectors
are length-1).

Two implementations behind one interface:

  tfidf  Pure statistics, no model download. It only knows which WORDS overlap,
         so "how long does the battery last" will not match "lifetime". It is a
         useful baseline and it works offline.
  st     A real neural embedding model (sentence-transformers). Understands
         paraphrases and synonyms. Default: BAAI/bge-small-en-v1.5 (133 MB, CPU
         friendly, strong for its size).

Swapping tfidf for st and re-running the evaluation is the best way to *see*
what semantic embeddings buy you.
"""
from __future__ import annotations

import numpy as np

from .bm25 import tokenize


class TfidfEmbedder:
    name = "tfidf"
    corpus_dependent = True   # vocabulary and idf come from the whole corpus

    def __init__(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vec = TfidfVectorizer(tokenizer=tokenize, lowercase=False, token_pattern=None,
                                   ngram_range=(1, 2), sublinear_tf=True, min_df=1)

    def fit(self, texts: list[str]) -> None:
        self.vec.fit(texts)

    def _enc(self, texts: list[str]) -> np.ndarray:
        return self.vec.transform(texts).toarray().astype("float32")  # rows are L2-normalised

    encode_docs = _enc
    encode_query = lambda self, q: self._enc([q])[0]  # noqa: E731


class HashingEmbedder:
    """Bag-of-words hashed into a fixed number of buckets. Crude, offline, and DETERMINISTIC PER TEXT:
    the vector of a chunk never depends on the other chunks. That property (like a neural model's) is what
    makes incremental indexing possible; tf-idf lacks it because its vocabulary and idf weights are fitted
    on the whole corpus, so adding one document changes every vector."""
    name = "hash"
    corpus_dependent = False

    def __init__(self, dim: int = 1024):
        self.dim = dim

    def fit(self, texts: list[str]) -> None:
        pass

    def _enc(self, texts: list[str]) -> np.ndarray:
        import zlib
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for i, t in enumerate(texts):
            for tok in tokenize(t):
                out[i, zlib.crc32(tok.encode()) % self.dim] += 1.0
        n = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(n == 0, 1, n)

    encode_docs = _enc
    encode_query = lambda self, q: self._enc([q])[0]  # noqa: E731


class SentenceTransformerEmbedder:
    # bge models want this prefix on the QUERY side only (documents get none).
    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from sentence_transformers import SentenceTransformer  # lazy: heavy import
        self.name = "st:" + model
        self.corpus_dependent = False  # pretrained: a text always maps to the same vector
        self.model = SentenceTransformer(model)
        self.prefix = self.QUERY_PREFIX if "bge" in model.lower() else ""

    def fit(self, texts: list[str]) -> None:  # nothing to fit; pretrained
        pass

    def encode_docs(self, texts: list[str]) -> np.ndarray:
        return self.model.encode(texts, normalize_embeddings=True, batch_size=32,
                                 show_progress_bar=False).astype("float32")

    def encode_query(self, q: str) -> np.ndarray:
        return self.model.encode([self.prefix + q], normalize_embeddings=True)[0].astype("float32")


def get_embedder(spec: str):
    """'tfidf' | 'st' | 'st:<huggingface-model-name>'"""
    if spec == "tfidf":
        return TfidfEmbedder()
    if spec == "hash":
        return HashingEmbedder()
    if spec == "st":
        return SentenceTransformerEmbedder()
    if spec.startswith("st:"):
        return SentenceTransformerEmbedder(spec[3:])
    raise ValueError(f"unknown embedder {spec!r}")
