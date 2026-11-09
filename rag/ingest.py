"""Stage 1: INGESTION.

Turn files on disk into Document objects with clean text and useful metadata.
Metadata is not decoration: later it lets us filter out stale documents, enforce access control
and show citations. Real systems extract it from the source system (SharePoint, Confluence, S3
paths); here we derive it from file names, content and an acl.json file.

Formats: .md .txt  (as is)     .docx  (headings, lists, tables)     .html/.htm  (headings, lists,
tables, boilerplate removed)     .pdf  (text layer only: see the limits below).

Everything is converted to markdown-style text, because the structure-aware chunker splits on
`#` headings and keeps tables whole. WHAT PARSING CANNOT GIVE YOU: PDFs have no real heading or
table structure (we guess headings from numbering and ALL-CAPS lines; tables come out as loose
text), and scanned PDFs have no text at all (they need OCR, which we report instead of silently
indexing nothing). Parsing quality is a retrieval-quality ceiling: inspect what your parser
produced before blaming the embedder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .access import acl_for, load_acl_rules

SUPPORTED = (".md", ".txt", ".docx", ".html", ".htm", ".pdf")


@dataclass
class Document:
    doc_id: str      # "03b"
    source: str      # file name, used for citations
    title: str       # first H1
    text: str
    status: str      # "current" or "deprecated"
    acl: list = field(default_factory=lambda: ["*"])  # groups allowed to retrieve it ("*" = anyone)


_ARCHIVED = re.compile(r"status:\s*archived|do not quote", re.IGNORECASE)


# ---------------------------------------------------------------- format parsers (-> markdown text)
def _md_table(rows: list[list[str]]) -> str:
    rows = [[re.sub(r"\s+", " ", c).strip().replace("|", "/") for c in r] for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join(["---"] * width) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def parse_docx(path: Path) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = docx.Document(str(path))
    out: list[str] = []
    for el in d.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, d)
            t = p.text.strip()
            if not t:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = re.match(r"Heading (\d)", style)
            if style == "Title":
                out.append(f"# {t}")
            elif m:
                out.append(f"{'#' * min(int(m.group(1)), 6)} {t}")
            elif style.startswith("List"):
                out.append(f"- {t}")
            else:
                out.append(t)
        elif tag == "tbl":
            out.append(_md_table([[c.text for c in row.cells] for row in Table(el, d).rows]))
    return "\n\n".join(x for x in out if x)


def parse_html(path: Path) -> str:
    from bs4 import BeautifulSoup, NavigableString
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "aside", "form", "svg"]):
        t.decompose()
    out: list[str] = []

    def walk(node):
        for ch in node.children:
            if isinstance(ch, NavigableString):
                s = re.sub(r"\s+", " ", str(ch)).strip()
                if s and ch.parent.name in ("body", "div", "section", "article", "main"):
                    out.append(s)
                continue
            n = ch.name
            if n and re.fullmatch(r"h[1-6]", n):
                out.append(f"{'#' * int(n[1])} {ch.get_text(' ', strip=True)}")
            elif n == "p":
                out.append(re.sub(r"\s+", " ", ch.get_text(" ", strip=True)))
            elif n in ("ul", "ol"):
                out.append("\n".join(f"- {re.sub(chr(10) + '+', ' ', li.get_text(' ', strip=True))}" for li in ch.find_all("li")))
            elif n == "table":
                out.append(_md_table([[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in ch.find_all("tr")]))
            elif n == "pre":
                out.append("```\n" + ch.get_text() + "\n```")
            elif n:
                walk(ch)

    walk(soup.body or soup)
    text = "\n\n".join(x for x in out if x.strip())
    if not re.search(r"^#\s", text, re.M) and soup.title and soup.title.string:
        text = f"# {soup.title.string.strip()}\n\n{text}"
    return text


_NUMBERED = re.compile(r"^(\d+(?:\.\d+){0,3})[.)]?\s+([A-Z][^.!?]{2,70})$")


def parse_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages = [(p.extract_text() or "") for p in reader.pages]
    if sum(len(t.strip()) for t in pages) < 20 * max(len(pages), 1):
        return ""  # no text layer: a scanned PDF. The caller reports it.
    blocks: list[tuple[int, str]] = []   # (page_no, markdown block)
    found_heading = False
    for n, raw in enumerate(pages, 1):
        para: list[str] = []

        def flush():
            if para:
                blocks.append((n, " ".join(para)))
                para.clear()
        for line in raw.splitlines():
            s = line.strip()
            m = _NUMBERED.match(s)
            if not s:
                flush()
            elif m and len(s) < 80:
                flush()
                blocks.append((n, f"{'#' * min(m.group(1).count('.') + 2, 6)} {s}"))
                found_heading = True
            elif s.isupper() and 3 <= len(s) <= 60 and any(c.isalpha() for c in s):
                flush()
                blocks.append((n, f"## {s.title()}"))
                found_heading = True
            else:
                para.append(s)
        flush()
    out, last_page = [], 0
    for n, b in blocks:
        if not found_heading and n != last_page:
            out.append(f"## Page {n}")  # no structure to recover: at least cite by page
            last_page = n
        out.append(b)
    meta_title = (reader.metadata.title or "").strip() if reader.metadata else ""
    text = "\n\n".join(out)
    if not re.search(r"^#\s", text, re.M):
        text = f"# {meta_title or path.stem.replace('_', ' ')}\n\n{text}"
    return text


def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return parse_docx(path)
    if ext in (".html", ".htm"):
        return parse_html(path)
    if ext == ".pdf":
        return parse_pdf(path)
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def load_corpus(corpus_dir: str | Path, report: list | None = None) -> list[Document]:
    """Parse every supported file. Problems (scanned PDF, unreadable file) are appended to `report`
    and the file is skipped: a silent empty document would just be an invisible hole in recall."""
    corpus_dir = Path(corpus_dir)
    rules = load_acl_rules(corpus_dir)
    docs: list[Document] = []
    paths = sorted(p for p in corpus_dir.rglob("*") if p.suffix.lower() in SUPPORTED)
    for path in paths:
        source = path.relative_to(corpus_dir).as_posix()
        try:
            text = read_text(path)
        except Exception as e:
            if report is not None:
                report.append(f"{source}: could not be parsed ({type(e).__name__}: {str(e)[:80]}), skipped")
            continue
        if not text.strip():
            if report is not None:
                report.append(f"{source}: no extractable text (scanned PDF? it needs OCR), skipped")
            continue
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = m.group(1).strip() if m else path.stem
        deprecated = "DEPRECATED" in path.name.upper() or bool(_ARCHIVED.search(text[:600]))
        docs.append(
            Document(
                doc_id=path.stem.split("_")[0],
                source=source,
                title=title,
                text=text,
                status="deprecated" if deprecated else "current",
                acl=acl_for(source, rules),
            )
        )
    return docs
