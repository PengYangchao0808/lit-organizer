#!/usr/bin/env python3
"""lit-organizer · Phase 1+3 RENAME — 元数据 → 归一化 → 关键词重命名（一条龙第二步）.

Metadata priority per PDF:
  1. sidecar `<stem>.meta.json` written by download.py (authoritative)
  2. PyPDF2 text extraction (author/year/journal/title)
  3. DOI embedded in temp filename → journal map (scan-quality PDFs)

Naming: {year}_{author}_{journal}_{title_keywords 3-6}.pdf
Idempotent: files already matching YYYY_x_y_z.pdf are skipped unless --force.

Usage:
  python3 rename.py --dir ./papers
  python3 rename.py --dir ./papers --dry-run     # preview only
  python3 rename.py --dir ./papers --force       # reprocess normalized files
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import PyPDF2
except ImportError:
    sys.exit("PyPDF2 required: pip install PyPDF2")

# ---------------- metadata extraction ----------------

JOURNAL_MAP = [  # (pattern, abbr) — ordered: specific first
    (r'Tetrahedron\s*Lett(?:ers)?', 'Tetrahedron Lett'),
    (r'Tetrahedron\s*Asymmetry', 'Tetrahedron Asymmetry'),
    (r'Tetrahedron(?!.*Lett)', 'Tetrahedron'),
    (r'J\.?\s*Am\.?\s*Chem\.?\s*Soc|JACS|Journal\s*of\s*the\s*American\s*Chemical', 'JACS'),
    (r'J\.?\s*Org\.?\s*Chem', 'J Org Chem'),
    (r'Angew\.?\s*Chem\.?\s*Int\.?\s*Ed', 'Angew Chem Int Ed'),
    (r'Organic\s*Letters|Org\.?\s*Lett', 'Org Lett'),
    (r'Accounts\s*of\s*Chemical\s*Research', 'Acc Chem Res'),
    (r'Chemical\s*Communications|Chem\.?\s*Commun', 'Chem Commun'),
    (r'Chemical\s*Science|Chem\.?\s*Sci(?!ence?\s*Commun)', 'Chem Sci'),
    (r'Organic\s*&\s*Biomolecular\s*Chemistry', 'Org Biomol Chem'),
    (r'Organic\s*Chemistry\s*Frontiers', 'Org Chem Front'),
    (r'Chemistry\s*Letters', 'Chem Lett'),
    (r'Dalton\s*Transactions', 'Dalton Trans'),
    (r'Phytochemistry', 'Phytochemistry'),
    (r'Fitoterapia', 'Fitoterapia'),
    (r'Nature', 'Nature'),
]

# longest-prefix-first when resolving DOI → journal
DOI_TO_JOURNAL = [
    ('10.1016/0040-4039', 'Tetrahedron Lett'),
    ('10.1016/0040-4027', 'Tetrahedron'),
    ('10.1016/0040-4028', 'Tetrahedron Asymmetry'),
    ('10.1021/jo', 'J Org Chem'),
    ('10.1021/ol', 'Org Lett'),
    ('10.1021/ja', 'JACS'),
    ('10.1002/anie', 'Angew Chem Int Ed'),
    ('10.1038/', 'Nature'),
    ('10.1016/', 'Elsevier'),
]

NORMALIZED_RE = re.compile(r'^\d{4}_[A-Za-z][A-Za-z0-9\-]*_.+_.+\.pdf$')
DOI_STEM_RE = re.compile(r'^(10\.\d{4,9})_(.+)$')


def extract_text(pdf_path, max_pages=3):
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            return "\n".join(
                (page.extract_text() or "")
                for i, page in enumerate(reader.pages) if i < max_pages)
    except Exception:
        return ""


def extract_author(text):
    """Star-marked corresponding author → email → 'Last, F.' byline → None."""
    for line in text.split('\n'):
        m = re.search(
            r'([A-Z][a-z]+(?:\s+[A-Z]\.?\s*)*(?:\s+[A-Z][a-z]+)*)\s*\d*,?\d*\s*\*$',
            line)
        if m:
            return m.group(1).strip()
    m = re.search(r'E-mail\s*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)', text)
    if m:
        user = m.group(1).split('@')[0]
        return re.sub(r'[^a-zA-Z]', '', user).capitalize()
    # old-style byline "Maruyama, K.; Yamamoto, Y." → first surname
    m = re.search(r'([A-Z][a-zA-Z\-]{1,25}),\s+[A-Z]\.?(?:\s+[A-Z]\.?)?\s*[;.]', text)
    if m:
        return m.group(1)
    # "Kazuhiro Maruyama and Yoshinori Yamamoto" → first author's last token
    m = re.search(r'\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\s+(?:and|&)\s+[A-Z]', text)
    if m:
        return m.group(2)
    return None


def extract_year(text):
    for p in (r'(\d{4})\s*©', r'©\s*(\d{4})', r'\((\d{4})\)',
              r'(?:Received|Published|Available online)\s*:?\s*\d{1,2}\s+\w+\s+(\d{4})',
              r'doi:.*?(\d{4})'):
        m = re.search(p, text)
        if m and 1900 <= int(m.group(1)) <= 2030:
            return m.group(1)
    return None


def match_journal(candidates):
    """candidates: strings to test against JOURNAL_MAP (first hit wins)."""
    for cand in candidates:
        if not cand:
            continue
        for pat, abbr in JOURNAL_MAP:
            if re.search(pat, cand, re.IGNORECASE):
                return abbr
    return None


def extract_title(text):
    junk_prefix = ('figure', 'fig.', 'table', 'scheme', 'chart', 'graph',
                   'photo', 'illustration')
    for line in text.split('\n'):
        line = line.strip()
        skip = ['received', 'published', 'doi:', 'copyright', 'abstract',
                'corresponding', 'e-mail', 'supporting', 'article info']
        if 15 < len(line) < 200 and not any(s in line.lower() for s in skip):
            if line.lower().startswith(junk_prefix):
                continue
            if line != line.upper() and line != line.lower():
                return re.sub(r'[^\w\s-]', '', line).strip()
    return None


# ---------------- normalization + keywords ----------------

def normalize_chemistry(text):
    trans = str.maketrans('₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹', '01234567890123456789')
    text = text.translate(trans)
    for k, v in {'α': 'alpha', 'β': 'beta', 'γ': 'gamma', 'δ': 'delta',
                 'ε': 'epsilon', 'μ': 'mu', 'Δ': 'Delta', 'Ω': 'Omega',
                 'σ': 'sigma'}.items():
        text = text.replace(k, v)
    return text.replace('·', ' ').replace('•', ' ')


STOPWORDS = {'a', 'an', 'the', 'of', 'in', 'on', 'to', 'towards', 'toward',
             'with', 'via', 'for', 'and', 'or', 'by', 'from', 'under',
             'into', 'involving', 'using', 'between', 'is', 'are', 'at', 'as',
             'very', 'remarkably', 'completely', 'efficiently', 'highly'}


def extract_keywords(title, max_words=6):
    """First clause → stopwords removed → keep 3-6 content words."""
    clause = title.split('. ')[0].rstrip('.,;:')
    clause = normalize_chemistry(clause)
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", clause)
    words = [t for t in tokens if t.lower() not in STOPWORDS] or tokens
    if len(words) < 3 and len(tokens) >= 3:   # stopwords ate too much
        words = tokens
    return words[:max_words]


def clean(s, max_len=80):
    if not s:
        return "Unknown"
    s = re.sub(r'[^\w\s-]', '', str(s))
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:max_len].rsplit('_', 1)[0] if len(s) > max_len else s


def kw_case(w):
    """Capitalize all-lowercase words only — never touch RCu/BF3 chemical case."""
    return w if any(c.isupper() for c in w) else w.capitalize()


def surname(full_name):
    """'Yoshinori Yamamoto' → 'Yamamoto'; 'van der Berg' → 'Berg'."""
    if not full_name:
        return None
    parts = re.split(r'\s+', full_name.strip())
    return parts[-1] if parts else None


# ---------------- per-file pipeline ----------------

def load_sidecar(pdf: Path):
    meta_path = pdf.with_suffix('.meta.json')
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding='utf-8')), meta_path
        except Exception:
            return None, None
    return None, None


def journal_from_stem(pdf: Path):
    m = DOI_STEM_RE.match(pdf.stem)
    if m:
        doi = f"{m.group(1)}/{m.group(2)}"
        for pfx, abbr in DOI_TO_JOURNAL:   # list ordered longest-prefix first
            if doi.lower().startswith(pfx.lower()):
                return abbr
    return None


def journal_from_name(stem):
    """Recover journal from an already-normalized filename ('_JACS_' etc.)."""
    hay = f'_{stem}_'
    abbrs = sorted({a for _, a in JOURNAL_MAP}, key=len, reverse=True)
    for abbr in abbrs:
        if f'_{clean(abbr)}_' in hay:
            return abbr
    return None


def fields_from_name(stem):
    """Recover (year, author) from normalized name 'YYYY_Author_...pdf stem'."""
    m = re.match(r'^(\d{4})_([A-Za-z][A-Za-z0-9\-]*)_(.+)$', stem)
    return (m.group(1), m.group(2)) if m else (None, None)


def resolve(pdf: Path):
    """→ (year, author, journal, title, origin) | 'keep' | None.

    Priority: sidecar > human-verified filename fields > PDF text extraction.
    'keep' = normalized file with no new title source — leave name untouched.
    """
    meta, _ = load_sidecar(pdf)
    normalized = bool(NORMALIZED_RE.match(pdf.name))
    ny, na = fields_from_name(pdf.stem) if normalized else (None, None)
    nj = journal_from_name(pdf.stem) if normalized else None

    text = extract_text(pdf)
    year = (str(meta['year']) if meta and meta.get('year') else None) \
        or ny or extract_year(text)
    author = (surname(meta.get('first_author')) if meta else None) \
        or na or extract_author(text)
    journal = (match_journal([meta.get('venue') if meta else None])
               or journal_from_stem(pdf) or nj
               or match_journal([text[:2000]]))
    title = (meta.get('title') if meta else None) or extract_title(text)

    if not title and normalized and all([year, author, journal]):
        print(f"KEEP {pdf.name}: no new title source, name unchanged")
        return 'keep'

    if not all([year, author, journal, title]):
        missing = [k for k, v in zip(
            ('year', 'author', 'journal', 'title'),
            (year, author, journal, title)) if not v]
        print(f"  WARN {pdf.name}: cannot resolve {missing} — needs manual fix")
        return None
    return year, author, journal, title, 'sidecar' if meta else 'pdf'


def rename_one(pdf: Path, dry_run: bool, force: bool):
    if NORMALIZED_RE.match(pdf.name) and not force:
        print(f"SKIP (normalized): {pdf.name}")
        return 'skipped'

    resolved = resolve(pdf)
    if resolved == 'keep':
        return 'kept'
    if not resolved:
        return 'failed'
    year, author, journal, title, origin = resolved

    kws = extract_keywords(title)
    title_part = '_'.join(clean(kw_case(w)) for w in kws) if kws else 'Unknown'
    new_name = f"{clean(year)}_{clean(author)}_{clean(journal)}_{title_part}.pdf"
    new_path = pdf.parent / new_name

    if new_path.exists() and new_path != pdf:
        base, c = new_path.stem, 1
        while new_path.exists():
            new_path = pdf.parent / f"{base}_{c}.pdf"
            c += 1

    if dry_run:
        print(f"DRY  {pdf.name}\n  -> {new_path.name}")
        return 'planned'

    sidecar = pdf.with_suffix('.meta.json')
    pdf.rename(new_path)
    if sidecar.exists():
        # keep sidecar alongside renamed file (enables --force reprocessing)
        sidecar.rename(new_path.with_suffix('.meta.json'))
    print(f"OK   {pdf.name}\n  -> {new_path.name}  [{origin}]")
    return 'renamed'


def main():
    ap = argparse.ArgumentParser(description="DOI-based PDFs → normalized keyword names")
    ap.add_argument('--dir', required=True, help='directory containing PDFs')
    ap.add_argument('--dry-run', action='store_true', help='preview only, no renames')
    ap.add_argument('--force', action='store_true',
                    help='reprocess files already matching YYYY_x_y_z.pdf')
    ap.add_argument('--log', default=None, help='rename log json path '
                    '(default: <dir>/rename_log.json)')
    args = ap.parse_args()

    target = Path(args.dir)
    pdfs = sorted(p for p in target.glob('*.pdf')
                  if not p.name.startswith('rename_log'))
    if not pdfs:
        print(f"no PDFs in {target}")
        return

    results = {'renamed': [], 'skipped': [], 'kept': [], 'planned': [],
               'failed': []}
    for pdf in pdfs:
        results[rename_one(pdf, args.dry_run, args.force)].append(pdf.name)

    print(f"\n=== renamed {len(results['renamed'])}, "
          f"skipped {len(results['skipped'])}, "
          f"kept {len(results['kept'])}, "
          f"failed {len(results['failed'])}, total {len(pdfs)} ===")
    if results['failed']:
        print("failed files (fix manually):")
        for name in results['failed']:
            print("  -", name)

    if not args.dry_run and (results['renamed'] or results['failed']):
        log_path = Path(args.log) if args.log else target / 'rename_log.json'
        log_path.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                            encoding='utf-8')
        print(f"log: {log_path}")

    sys.exit(1 if results['failed'] else 0)


if __name__ == "__main__":
    main()
