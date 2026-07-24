# src/ingestion/chunker.py
"""
Splits source documents into Chunk objects ready for embedding.

Design principle: metadata is extracted FROM document text, never hardcoded.
When a source document updates, re-run fetcher.py + ingestor.py.
Zero code changes needed to pick up rule changes.

Two chunking strategies:
  recursive — baseline splitter, respects natural text boundaries
  clause    — production splitter, one chunk per return code entry
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import re
from pathlib import Path
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ── Chunk dataclass ───────────────────────────────────────────────────────────
# Every piece of text that enters the database starts as one of these.
# Optional fields are None when the chunk isn't about a specific return code
# (e.g. preamble text, general Nacha rules sections).

@dataclass
class Chunk:
    content: str                          # text the LLM will read
    source_doc: str                       # which file this came from
    chunk_index: int                      # position within that document
    strategy: str                         # "recursive" or "clause"
    return_code: Optional[str] = None     # R01, R02 ... or None
    return_category: Optional[str] = None # nsf | unauthorized | account | stop_payment
    return_window: Optional[str] = None   # extracted phrase from document
    can_retry: Optional[bool] = None      # derived from document language
    max_retries: Optional[int] = None     # derived from document language


# ── Metadata extractors ───────────────────────────────────────────────────────
# Each function reads the chunk's own text and extracts one piece of metadata.
# The patterns cover all four of our source documents' different phrasings.
# Values come from the document — we only hardcode the shape of what to look for.

def extract_return_window(text: str) -> Optional[str]:
    """
    Extracts return window from chunk text.

    Handles multiple source formats:
      ramp:          "Timeframe: 60 calendar days"
      achforbusiness: "60 Calendar Days"  (table column)
      plaid:         "within two banking days of the settlement date"
      achq:          "2 banking days"
    """
    # Pattern 1 — explicit label like "Timeframe: X" or "Return window: X"
    labeled = re.search(
        r'(?:timeframe|return\s+window|time\s+frame)\s*[:\-]\s*([^\n.]{3,60})',
        text, re.IGNORECASE
    )
    if labeled:
        return labeled.group(1).strip()

    # Pattern 2 — standalone "60 calendar days" or "2 banking days"
    standalone = re.search(
        r'\b(\d+\s+(?:calendar|banking)\s+days?)\b',
        text, re.IGNORECASE
    )
    if standalone:
        return standalone.group(1).strip()

    # Pattern 3 — "within two banking days" (written out)
    written = re.search(
        r'within\s+(two|2)\s+banking\s+days?',
        text, re.IGNORECASE
    )
    if written:
        return "2 banking days"

    return None


def extract_can_retry(text: str) -> Optional[bool]:
    """
    Determines retry eligibility from chunk text.

    No-retry signals — explicit in ramp ("Retry eligible? No") and
    implicit in plaid/achq ("Do not retry", "Nacha violation").

    Yes-retry signals — "retry up to", "additional attempt", "re-initiate".
    """
    no_retry_patterns = [
    r'retry\s+eligible\s*[?:]\s*no',
    r'do\s+not\s+retry',
    r'may\s+not\s+be\s+re-(?:presented|initiated)',
    r'retrying.*(?:nacha\s+violation|violates\s+nacha)',
    r'not\s+allowed.*retry',
    r'cannot\s+be\s+resubmitted',
    r'no\s+retry\s+should\s+be\s+made',   # ← new
    r'should\s+not\s+retry',               # ← new
    r'not\s+simply\s+retry',               # ← new
    r'do\s+not\s+retry\s+without',         # ← new
]
    for pattern in no_retry_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return False

    yes_retry_patterns = [
        r'retry\s+eligible\s*[?:]\s*yes',
        r'retry\s+up\s+to',
        r'may\s+retry',
        r're-initiate',
        r'additional\s+attempt',
        r'resubmit\s+after',
        r'wait.*then\s+retry',
    ]
    for pattern in yes_retry_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return None  # not determinable from this chunk alone


def extract_max_retries(text: str) -> Optional[int]:
    """
    Extracts maximum retry count from chunk text.
    Returns 0 for unauthorized codes where retry is prohibited.
    Returns None when not mentioned in this chunk.
    """
    # Explicit prohibition — 0 retries
    if re.search(
        r'do\s+not\s+retry|may\s+not\s+be\s+re-presented|retrying.*violation',
        text, re.IGNORECASE
    ):
        return 0

    # "retry up to 2 additional times"
    match = re.search(
        r'(?:retry|re-present|re-initiate)\s+up\s+to\s+(\d+)',
        text, re.IGNORECASE
    )
    if match:
        return int(match.group(1))

    # "maximum 2 times" or "2 additional attempts"
    match = re.search(
        r'(?:maximum|no\s+more\s+than)\s+(\d+)\s+(?:additional|times|attempts)',
        text, re.IGNORECASE
    )
    if match:
        return int(match.group(1))

    # "2 additional times within..."
    match = re.search(
        r'(\d+)\s+additional\s+(?:times?|attempts?)',
        text, re.IGNORECASE
    )
    if match:
        return int(match.group(1))

    return None


def extract_category(text: str) -> Optional[str]:
    """
    Classifies return code into operational categories.
    Category determines business logic: retry rules, threshold tracking, etc.

    Nacha tracks unauthorized returns separately (0.5% threshold)
    from administrative returns (3% threshold) and overall (15%).
    """
    category_signals: dict[str, list[str]] = {
        "unauthorized": [
            r'unauthorized',
            r'not\s+authorized',
            r'authorization\s+revoked',
            r'revoked\s+authorization',
            r'consumer.*not\s+authorize',
        ],
        "nsf": [
            r'insufficient\s+funds',
            r'uncollected\s+funds',
            r'non.sufficient',
            r'available\s+balance.*not\s+sufficient',
        ],
        "account": [
            r'account\s+closed',
            r'no\s+account',
            r'invalid\s+account\s+number',
            r'unable\s+to\s+locate\s+account',
            r'account\s+number\s+structure',
        ],
        "stop_payment": [
            r'stop\s+payment',
            r'payment\s+stopped',
        ],
        "administrative": [
            r'odfi\s+request',
            r'incorrect.*routing',
            r'duplicate\s+entry',
            r'erroneous',
        ],
    }

    for category, patterns in category_signals.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return category

    return None


def extract_metadata(text: str) -> dict:
    """
    Runs all extractors on a chunk's text.
    Called once per chunk — returns all metadata as a single dict.
    """
    return {
        "return_window":    extract_return_window(text),
        "can_retry":        extract_can_retry(text),
        "max_retries":      extract_max_retries(text),
        "return_category":  extract_category(text),
    }


# ── Return code detection ─────────────────────────────────────────────────────
# Matches R01–R99 as a whole word.
# \b prevents matching R01 inside words like "TR01" or "R016".

RETURN_CODE_RE = re.compile(r'\b(R\d{2})\b')


def detect_return_code(text: str, strict: bool = False) -> Optional[str]:
    """
    strict=True  — only matches R## at the very start of the text.
                   Used by clause_split since each clause starts with its code.
    strict=False — searches anywhere in text.
                   Used by recursive_split where code may appear mid-chunk.
    """
    if strict:
        match = re.match(r'^(R\d{2})\b', text.strip())
    else:
        match = RETURN_CODE_RE.search(text)
    return match.group(1) if match else None


# ── Strategy 1: Recursive character splitting (baseline) ─────────────────────

def recursive_split(
    text: str,
    source_doc: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> list[Chunk]:
    """
    Baseline strategy. No awareness of return code boundaries.
    Splits on: paragraph → sentence → word → character (in priority order).

    chunk_size=512    — max characters per chunk
    chunk_overlap=64  — repeated characters at chunk boundaries
                        prevents cutting a sentence mid-thought

    We still run metadata extraction on each chunk — even a dumb chunker
    can pick up partial metadata if the text pattern happens to be there.
    This baseline is deliberately imperfect so RAGAS scores show improvement.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    raw_chunks = splitter.split_text(text)
    chunks = []

    for i, content in enumerate(raw_chunks):
        content = content.strip()
        if len(content) < 20:   # skip noise — very short fragments
            continue

        code = detect_return_code(content)
        meta = extract_metadata(content)

        chunks.append(Chunk(
            content=content,
            source_doc=source_doc,
            chunk_index=i,
            strategy="recursive",
            return_code=code,
            **meta,
        ))

    return chunks

def _is_section_index(content: str) -> bool:
    """
    Returns True if chunk looks like a table-of-contents entry.
    These contain multiple return codes but no detailed explanation.

    Signals:
      - Contains "to R" pattern (e.g. "R31 to R40")
      - Very short content with multiple codes and no explanation
      - Contains table headers like "Code Description"
    """
    if re.search(r'R\d{2}\s+to\s+R\d{2}', content):
        return True
    if re.search(r'\bCode\s+Description\b', content, re.IGNORECASE):
        return True
    return False

# ── Strategy 2: Clause-level splitting (production) ──────────────────────────

def clause_split(text: str, source_doc: str) -> list[Chunk]:
    """
    Production strategy. Splits at return code boundaries.

    How the split works:
      re.split(r'\n(?=R\d{2}\b)', text)

    \n          — consume the newline before each return code
    (?=R\d{2})  — lookahead: next thing must be R## at word boundary
                  lookahead doesn't consume — R## stays at start of next chunk

    Result:
      chunk 0 → preamble (general rules, thresholds, intro text)
      chunk 1 → "R01\nInsufficient Funds\n..."
      chunk 2 → "R02\nAccount Closed\n..."
      ...

    Why preamble matters:
      General rules like "unauthorized returns must stay below 0.5%"
      apply to ALL return codes. Keeping preamble as its own retrievable
      chunk means a question like "what are Nacha's return thresholds?"
      finds it directly, without needing to be inside any one code's chunk.
    """
    sections = re.split(r'\n(?=R\d{2}\b)', text)
    chunks = []

    for i, section in enumerate(sections):
        section = section.strip()
        if len(section) < 20:   # skip empty sections
            continue
        if _is_section_index(section):   # ← add this
            continue
        
        code = detect_return_code(section, strict=True)  # ← add strict=True
        meta = extract_metadata(section)

        chunks.append(Chunk(
            content=section,
            source_doc=source_doc,
            chunk_index=i,
            strategy="clause",
            return_code=code,
            **meta,
        ))

    return chunks


# ── Main entry point ──────────────────────────────────────────────────────────

def chunk_document(
    source: str,
    source_doc: str,
    strategy: str = "clause",
    is_file: bool = False,
) -> list[Chunk]:
    """
    Main function called by ingestor.py.

    Args:
        source:     File path (if is_file=True) or raw text string
        source_doc: Human-readable name tagged on every chunk — used
                    to filter by source during retrieval if needed
        strategy:   "recursive" or "clause"
        is_file:    Whether source is a path to a .txt file
    """
    if is_file:
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = source

    if strategy == "recursive":
        return recursive_split(text, source_doc)
    elif strategy == "clause":
        return clause_split(text, source_doc)
    else:
        raise ValueError(
            f"Unknown strategy: '{strategy}'. Use 'recursive' or 'clause'."
        )