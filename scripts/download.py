#!/usr/bin/env python3
"""lit-organizer · Phase 0 DOWNLOAD — DOI → PDF（一条龙第一步）.

Strategy chain per DOI (Sci-Hub first, OA fallback):
  1. Sci-Hub mirrors (verified order .ee/.vg/...) → <iframe id="pdf"> → direct
     fetch from unprotected host (sci.bban.top)
  2. Semantic Scholar OA lookup → PMC PDF (SHA-256 PoW solved programmatically)
     or publisher open-access URL
  3. Give up with per-mirror reason log

Outputs into --out:
  10.1021_ja00466a059.pdf        ← temp name (DOI-based, slash → underscore)
  10.1021_ja00466a059.meta.json  ← metadata sidecar consumed by rename.py

Usage:
  python3 download.py 10.1021/ja00527a032 --out ./papers
  python3 download.py --file dois.txt --out ./papers
"""

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

# Verified 2026-10 from datacenter IP: .ee/.vg serve article pages w/o captcha,
# PDFs live on unprotected sci.bban.top; .st=403, .se=DNS-dead, .box/.wf=DDoS-Guard.
MIRRORS = [
    "https://sci-hub.ee",
    "https://sci-hub.vg",
    "https://sci-hub.ren",
    "https://sci-hub.ru",       # ALTCHA PoW interstitial, retry after cooldown
    "https://sci-hub.shop",
    "https://sci-hub.tf",
    "https://sci-hub.box",
    "https://sci-hub.st",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

CHALLENGE_MARKERS = [
    "ddos-guard", "DDoS-Guard", "h-captcha", "hcaptcha.com",
    "/captcha/challenge/", "are you are robot", "проверка на робота",
    "turnstile", "verify you are human", "Checking your browser",
]


# ---------------- generic HTTP ----------------

def http_get(url, timeout=30, jar=None):
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar)) if jar else urllib.request.build_opener()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def is_pdf(data: bytes) -> bool:
    return isinstance(data, bytes) and len(data) > 10 * 1024 and data[:5] == b"%PDF-"


def is_challenge_page(html: str) -> bool:
    head = html[:4000]
    return any(m in head for m in CHALLENGE_MARKERS)


# ---------------- Sci-Hub link ----------------

def extract_pdf_url(html: str, base_url: str):
    """PDF URL from a Sci-Hub article page (iframe#pdf / embed / downloads)."""
    m = re.search(r'<iframe[^>]+id=["\']pdf["\'][^>]*src=["\']([^"\']+)', html, re.I)
    if not m:
        m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\'][^>]*id=["\']pdf["\']', html, re.I)
    if not m:
        m = re.search(r'<(?:embed|object)[^>]+(?:src|data)=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)
    if not m:
        m = re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'["\'](/downloads/[^"\']+\.pdf[^"\']*)["\']', html, re.I)
    if not m:
        m = re.search(r'["\']((?:https?://[^"\']+|/[^"\']+)\.pdf(?:\?[^"\']*)?)["\']', html, re.I)
    return urljoin(base_url, m.group(1)) if m else None


def scihub_fetch(doi):
    """Try Sci-Hub mirrors. Returns (pdf_bytes, source) or (None, reasons)."""
    reasons = []
    for mirror in MIRRORS:
        try:
            body, _ = http_get(f"{mirror}/{doi}", timeout=25)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            reasons.append(f"{mirror}:{type(e).__name__}")
            continue
        html = body.decode("utf-8", errors="replace")
        if is_challenge_page(html):
            reasons.append(f"{mirror}:challenge")
            continue
        pdf_url = extract_pdf_url(html, mirror)
        if not pdf_url:
            reasons.append(f"{mirror}:no-link")
            continue
        try:
            pdf_body, ctype = http_get(pdf_url, timeout=60)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            reasons.append(f"{mirror}:pdf-fail:{type(e).__name__}")
            continue
        if is_pdf(pdf_body):
            return pdf_body, f"{mirror}→{urlparse(pdf_url).netloc}"
        reasons.append(f"{mirror}:not-pdf({ctype})")
    return None, "; ".join(reasons)


# ---------------- OA fallback (Semantic Scholar → PMC PoW) ----------------

def s2_lookup(doi):
    """Semantic Scholar metadata + OA location. Returns dict or None."""
    url = ("https://api.semanticscholar.org/graph/v1/paper/"
           f"DOI:{doi}?fields=title,year,venue,authors.name,openAccessPdf,externalIds")
    try:
        body, _ = http_get(url, timeout=20)
        d = json.loads(body)
        if "error" in d:
            return None
        return d
    except Exception:
        return None


def pmc_pow_get(pdf_url):
    """Fetch a PMC PDF through the SHA-256 proof-of-work interstitial."""
    jar = http.cookiejar.CookieJar()
    body, _ = http_get(pdf_url, timeout=60, jar=jar)
    if is_pdf(body):
        return body
    html = body.decode("utf-8", errors="replace")
    m_ch = re.search(r'POW_CHALLENGE = "([^"]+)"', html)
    m_df = re.search(r'POW_DIFFICULTY = "(\d+)"', html)
    if not m_ch:
        return None  # reCAPTCHA escalation or page gone — needs cooldown
    challenge, difficulty = m_ch.group(1), int(m_df.group(1) or 4)
    target = "0" * difficulty
    nonce = 0
    while True:
        h = hashlib.sha256((challenge + str(nonce)).encode()).hexdigest()
        if h.startswith(target):
            break
        nonce += 1
    cookie = http.cookiejar.Cookie(
        version=0, name="cloudpmc-viewer-pow", value=f"{challenge},{nonce}",
        port=None, port_specified=False, domain=".ncbi.nlm.nih.gov",
        domain_specified=True, domain_initial_dot=True, path="/",
        path_specified=True, secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={})
    jar.set_cookie(cookie)
    body, _ = http_get(pdf_url, timeout=60, jar=jar)
    return body if is_pdf(body) else None


def oa_fetch(doi, meta):
    """OA fallback. Returns (pdf_bytes, source) or (None, reason)."""
    if not meta:
        return None, "no-OA-metadata"
    pmc = (meta.get("externalIds") or {}).get("PubMedCentral")
    if pmc:
        pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc}/pdf/"
        data = pmc_pow_get(pdf_url)
        if data:
            return data, f"PMC{pmc}(PoW)"
        return None, f"PMC{pmc}:pow-fail(recaptcha?cooldown 1-2min)"
    oa = meta.get("openAccessPdf") or {}
    oa_url = oa.get("url")
    if oa_url:
        try:
            body, ctype = http_get(oa_url, timeout=60)
            if is_pdf(body):
                return body, f"OA:{urlparse(oa_url).netloc}"
            return None, f"OA:{urlparse(oa_url).netloc}:not-pdf({ctype})"
        except Exception as e:
            return None, f"OA:{type(e).__name__}"
    return None, "not-OA"


# ---------------- main ----------------

def doi_to_stem(doi: str) -> str:
    return doi.strip().replace("/", "_")


def download_one(doi, out_dir, sleep=1.0):
    stem = doi_to_stem(doi)
    pdf_path = os.path.join(out_dir, stem + ".pdf")
    meta_path = os.path.join(out_dir, stem + ".meta.json")

    if os.path.exists(pdf_path):
        print(f"[SKIP] already have: {doi}")
        return True

    print(f"[GET ] {doi}")
    meta = s2_lookup(doi)          # metadata sidecar (best-effort, keyless)
    data, info = scihub_fetch(doi)
    if not data:
        print(f"       sci-hub miss ({info}); trying OA fallback...")
        data, info = oa_fetch(doi, meta)
    if not data:
        print(f"[FAIL] {doi}: {info}")
        return False

    with open(pdf_path, "wb") as f:
        f.write(data)
    print(f"[OK  ] {stem}.pdf ({len(data)//1024} KB, via {info})")

    if meta:
        slim = {
            "doi": doi,
            "title": meta.get("title"),
            "year": meta.get("year"),
            "venue": meta.get("venue"),
            "first_author": ((meta.get("authors") or [{}])[0]).get("name"),
            "source": info,
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)
        print(f"       sidecar: {stem}.meta.json")
    time.sleep(sleep)
    return True


def main():
    ap = argparse.ArgumentParser(description="DOI → PDF via Sci-Hub + OA fallback")
    ap.add_argument("dois", nargs="*", help="DOI(s), e.g. 10.1021/ja00527a032")
    ap.add_argument("--file", help="text file, one DOI per line (# comments ok)")
    ap.add_argument("--out", default=".", help="output directory (default: cwd)")
    ap.add_argument("--sleep", type=float, default=1.5, help="seconds between DOIs")
    args = ap.parse_args()

    dois = list(args.dois)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    dois.append(line)
    if not dois:
        ap.error("no DOIs given (positional or --file)")

    os.makedirs(args.out, exist_ok=True)
    ok = 0
    for doi in dois:
        if download_one(doi, args.out, args.sleep):
            ok += 1
    print(f"\n=== {ok}/{len(dois)} downloaded → {args.out} ===")
    print("Next: rename.py --dir", args.out)
    sys.exit(0 if ok == len(dois) else 1)


if __name__ == "__main__":
    main()
