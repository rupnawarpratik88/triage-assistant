"""Retrieve relevant manual sections (BM25 over ## sections of kb/*.md).
Chunk id format: <manual>:<section number>, e.g. pump:2. Used later as citations."""
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

KB_DIR = Path(__file__).resolve().parent.parent / "kb"
HEADING = re.compile(r"^##\s*(\d+)\.\s*(.+?)\s*$", re.M)
STOP = {"the", "an", "and", "or", "of", "to", "is", "are", "in", "on", "at", "for",
        "with", "it", "this", "that", "was", "be", "by", "as", "from"}


class RetrievalError(Exception):
    """Raised when no usable manual evidence can be retrieved."""


def load_chunks(kb_dir=KB_DIR):
    kb_dir = Path(kb_dir)
    chunks = []
    if not kb_dir.is_dir():
        return chunks
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        matches = list(HEADING.finditer(text))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            chunks.append({
                "id": f"{path.stem}:{m.group(1)}",
                "manual": path.stem,
                "title": m.group(2),
                "text": text[m.end():end].strip(),
            })
    return chunks


def _tokens(s):
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1 and t not in STOP]


def build_query(description, events, observations):
    """Description + events + the text of critical/warning rule observations."""
    parts = [description or "", events or ""]
    parts += [o["text"] for o in observations if o["severity"] in ("critical", "warning")]
    return " ".join(parts)


def retrieve(equipment_type, description, events="", observations=(), k=4, kb_dir=KB_DIR):
    chunks = [c for c in load_chunks(kb_dir) if c["manual"] == equipment_type]
    if not chunks:
        raise RetrievalError(f"No manual found for equipment type '{equipment_type}'.")
    q = _tokens(build_query(description, events, observations))
    if not q:
        raise RetrievalError("The report has no searchable text.")
    bm25 = BM25Okapi([_tokens(c["title"] + " " + c["text"]) for c in chunks])
    scores = bm25.get_scores(q)
    ranked = sorted(zip(chunks, scores), key=lambda x: x[1], reverse=True)
    hits = [(c, s) for c, s in ranked if s > 0][:k]
    if not hits:
        raise RetrievalError("No relevant manual sections found for this report.")
    # always include the safety section so work orders can cite it
    if not any(c["title"].lower().startswith("safety") for c, _ in hits):
        for c, _ in ranked:
            if c["title"].lower().startswith("safety"):
                hits.append((c, 0.0))
                break
    return [{"id": c["id"], "title": c["title"], "text": c["text"], "score": round(float(s), 3)} for c, s in hits]
