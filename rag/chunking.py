"""Stage 2: CHUNKING.

Why chunk? Embedding a whole document produces one blurry vector, and the LLM
prompt has limited room. We want pieces that each express ONE idea and that
still make sense when read alone.

Two strategies, so you can measure the difference:

  naive_chunks      fixed-size character windows. Simple, and it happily cuts
                    tables and sentences in half.
  structure_chunks  split on markdown headings, never split a table or code
                    block, prefix each chunk with "Document > Section".
                    This "contextual header" trick is cheap and very effective.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ingest import Document


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    source: str
    title: str
    status: str
    section: str
    text: str
    strategy: str
    acl: list = field(default_factory=lambda: ["*"])  # groups allowed to retrieve it; [] = nobody

    @property
    def embed_text(self) -> str:
        """What we actually embed / keyword-index. Naive chunks get raw text;
        structure chunks get a breadcrumb header so 'Growth plan price' can
        match a table whose own rows never say 'pricing'."""
        if self.strategy == "naive":
            return self.text
        head = self.title if self.section in ("", self.title) else f"{self.title} > {self.section}"
        return f"{head}\n{self.text}"


# --------------------------------------------------------------------------- naive
def naive_chunks(doc: Document, size: int = 800, overlap: int = 100) -> list[Chunk]:
    out: list[Chunk] = []
    step = size - overlap
    for i, start in enumerate(range(0, max(len(doc.text), 1), step)):
        piece = doc.text[start:start + size].strip()
        if not piece:
            continue
        out.append(Chunk(f"{doc.doc_id}#{i}", doc.doc_id, doc.source, doc.title,
                         doc.status, "", piece, "naive"))
        if start + size >= len(doc.text):
            break
    return out


# --------------------------------------------------------------------------- structure
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _blocks(lines: list[str]) -> list[str]:
    """Group lines into atomic blocks: paragraph/list, table, or code fence."""
    blocks: list[str] = []
    cur: list[str] = []
    kind = None
    in_code = False

    def flush():
        nonlocal cur, kind
        if cur:
            blocks.append("\n".join(cur))
        cur, kind = [], None

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                cur.append(line)
                in_code = False
                flush()
            else:
                flush()
                in_code, kind = True, "code"
                cur = [line]
            continue
        if in_code:
            cur.append(line)
            continue
        if not line.strip() or re.fullmatch(r"-{3,}", line.strip()):
            flush()
            continue
        k = "table" if line.lstrip().startswith("|") else "text"
        if kind and k != kind:
            flush()
        kind = k
        cur.append(line)
    flush()
    return blocks


def _split_block(block: str, max_chars: int) -> list[str]:
    """Only oversized blocks get split. Tables are split by row with the
    header repeated, so every piece is still a valid, self-describing table."""
    if len(block) <= max_chars:
        return [block]
    lines = block.split("\n")
    header = lines[:2] if block.lstrip().startswith("|") else []
    body = lines[len(header):]
    pieces, cur = [], list(header)
    for ln in body:
        if len("\n".join(cur + [ln])) > max_chars and len(cur) > len(header):
            pieces.append("\n".join(cur))
            cur = list(header)
        cur.append(ln)
    pieces.append("\n".join(cur))
    return pieces


def structure_chunks(doc: Document, max_chars: int = 1400, min_chars: int = 250) -> list[Chunk]:
    # 1) walk the document, tracking the heading path
    stack: dict[int, str] = {}
    sections: list[tuple[str, list[str]]] = []
    buf: list[str] = []
    path = ""

    def close():
        if any(l.strip() for l in buf):
            sections.append((path, list(buf)))
        buf.clear()

    for line in doc.text.split("\n"):
        m = _HEADING.match(line)
        if m and not line.startswith("```"):
            close()
            level = len(m.group(1))
            stack = {k: v for k, v in stack.items() if k < level}
            stack[level] = m.group(2)
            # the document title is already prepended in embed_text; don't repeat it
            path = " > ".join(v for k, v in sorted(stack.items()) if not (k == 1 and v == doc.title))
            continue
        buf.append(line)
    close()

    # 2) pack each section's blocks into chunks <= max_chars
    raw: list[list] = []  # [section, body]
    for sec, lines in sections:
        group, size = [], 0
        for blk in _blocks(lines):
            for piece in _split_block(blk, max_chars):
                if group and size + len(piece) + 2 > max_chars:
                    raw.append([sec, "\n\n".join(group)])
                    group, size = [], 0
                group.append(piece)
                size += len(piece) + 2
        if group:
            raw.append([sec, "\n\n".join(group)])

    # 3) merge tiny neighbours so we don't index 40-character fragments
    merged: list[list] = []
    for sec, body in raw:
        if merged and (len(body) < min_chars or len(merged[-1][1]) < min_chars) \
                and len(merged[-1][1]) + len(body) + 2 <= max_chars:
            if sec != merged[-1][0]:
                tail = sec.split(" > ")[-1]
                merged[-1][0] = f"{merged[-1][0]} + {tail}" if merged[-1][0] else tail
            merged[-1][1] += "\n\n" + body
        else:
            merged.append([sec, body])

    return [Chunk(f"{doc.doc_id}#{i}", doc.doc_id, doc.source, doc.title, doc.status,
                  sec, body, "structure")
            for i, (sec, body) in enumerate(merged)]


def chunk_corpus(docs: list[Document], strategy: str = "structure", **kw) -> list[Chunk]:
    fn = {"naive": naive_chunks, "structure": structure_chunks}[strategy]
    out = []
    for d in docs:
        for c in fn(d, **kw):
            c.acl = list(d.acl)  # every chunk carries its document's ACL: the retriever checks chunks, not documents
            out.append(c)
    return out
