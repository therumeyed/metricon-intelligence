"""Turn fetched HTML/PDF into denoised evidence text.

Template noise (navigation, footers, cookie banners, clocks, copyright years, session tokens, repeated carousel
slides) is removed *before* hashing, so a page that only changed its chrome produces the same content hash and
therefore no new offer version or competitor event (brief s6, s15 check 10).
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "canvas", "form", "select", "option", "button", "nav", "footer", "aside", "dialog"]
NOISE_ATTR = re.compile(r"(cookie|consent|gdpr|onetrust|modal|popup|newsletter|breadcrumb|skip-?link|mega-?menu|sub-?menu|main-?menu|menu-?(tab|container|links)|site-?nav|main-?nav|navbar|nav-?(item|bar|menu)|footer|chat-?widget|search-?bar|favourites|mobile-?menu)", re.I)
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "blockquote", "dt", "dd", "figcaption", "summary", "div", "span", "a"}
LEAF_BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "blockquote", "dt", "dd", "figcaption", "summary"}

# volatile fragments that change without the page meaning anything different
_VOLATILE = [
    re.compile(r"©\s*(?:copyright\s*)?(?:19|20)\d{2}[^\n]*", re.I),
    re.compile(r"\b(?:last\s+)?(?:updated|modified|refreshed)\s*[:\-]?\s*(?:on\s+)?\d{1,2}[\s/\-][A-Za-z0-9]+[\s/\-]\d{2,4}\b", re.I),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.I),
    re.compile(r"\b[A-Za-z0-9_\-]{28,}\b"),  # tokens / hashes / nonces
    re.compile(r"\bsession[\w\-]*\s*[:=]\s*\S+", re.I),
    re.compile(r"\b\d+\s+(?:people|others)\s+(?:are\s+)?(?:viewing|looking)[^\n]*", re.I),
]
_WS = re.compile(r"[ \t\r\f\v ​]+")


@dataclass
class CleanDoc:
    title: str | None
    blocks: list[str]
    links: list[dict] = field(default_factory=list)
    is_pdf: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)

    @property
    def content_hash(self) -> str:
        return text_hash(self.text)


def normalise_for_hash(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalise_for_hash(text).encode()).hexdigest()


def strip_volatile(s: str) -> str:
    for rx in _VOLATILE:
        s = rx.sub("", s)
    return _WS.sub(" ", s).strip()


def _is_noise(tag: Tag) -> bool:
    if tag.attrs is None:
        return False
    if tag.get("hidden") is not None or tag.get("aria-hidden") == "true":
        return True
    style = (tag.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    role = (tag.get("role") or "").lower()
    if role in {"navigation", "banner", "contentinfo", "dialog", "search", "alert"}:
        return True
    if tag.name in {"html", "body", "main", "article"}:
        return False
    ident = " ".join([tag.get("id") or "", " ".join(tag.get("class") or [])])
    if not (ident and NOISE_ATTR.search(ident)):
        return False
    # a wrapper whose class merely mentions 'menu'/'footer' but which holds the page's main content is not noise
    return not (tag.find("main") or tag.find("h1"))


def html_to_clean(html: str, base_url: str | None = None) -> CleanDoc:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    for t in soup(DROP_TAGS):
        t.decompose()
    for t in list(soup.find_all(True)):
        if t.decomposed if hasattr(t, "decomposed") else False:
            continue
        try:
            if _is_noise(t):
                t.decompose()
        except Exception:  # element already removed with a parent
            continue
    root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article") or soup.body or soup
    # keep links (with text) from the content region for source discovery
    links = []
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        links.append({"href": urljoin(base_url, href) if base_url else href, "text": _WS.sub(" ", a.get_text(" ", strip=True))[:200]})
    blocks: list[str] = []
    for el in root.find_all(LEAF_BLOCKS):
        # only leaf-most block elements so text is not repeated through nesting
        if el.find(LEAF_BLOCKS):
            continue
        txt = strip_volatile(_WS.sub(" ", el.get_text(" ", strip=True)))
        if len(txt) < 2:
            continue
        blocks.append(txt)
    # text living directly in divs/spans with no block children (common on JS-light CMS pages)
    if len(blocks) < 5:
        for el in root.find_all(["div", "span", "section"]):
            if el.find(list(BLOCK_TAGS - {"div", "span", "a"})) or el.find(["div", "section"]):
                continue
            txt = strip_volatile(_WS.sub(" ", el.get_text(" ", strip=True)))
            if len(txt) >= 3:
                blocks.append(txt)
    blocks = dedupe_blocks(blocks)
    return CleanDoc(title=title, blocks=blocks, links=dedupe_links(links))


def dedupe_blocks(blocks: list[str]) -> list[str]:
    """Drop exact repeats (carousels repeat slides; menus repeat items). Order of first appearance is kept."""
    seen, out = set(), []
    for b in blocks:
        k = b.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(b)
    return out


def dedupe_links(links: list[dict]) -> list[dict]:
    seen, out = set(), []
    for l in links:
        k = (l["href"].split("#")[0], l["text"])
        if k in seen:
            continue
        seen.add(k)
        out.append(l)
    return out[:800]


def pdf_to_clean(data: bytes) -> CleanDoc:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    blocks: list[str] = []
    for page in reader.pages[:60]:
        t = page.extract_text() or ""
        for para in re.split(r"\n{2,}|\n(?=[A-Z0-9•\-–])", t):
            para = strip_volatile(_WS.sub(" ", para.replace("\n", " ")))
            if len(para) >= 3:
                blocks.append(para)
    title = (reader.metadata.title if reader.metadata and reader.metadata.title else None)
    return CleanDoc(title=title, blocks=dedupe_blocks(blocks), links=[], is_pdf=True)


# --------------------------------------------------------------------------- promo-window selection
PROMO_RX = re.compile(
    r"\$\s?\d|\b(?:offer|offers|promotion|promotions|promo|save|saving|savings|discount|rebate|bonus|free|upgrade|upgrades|"
    r"incentive|price (?:lock|hold|drop|promise)|deposit|inclusions?|giveaway|cash ?back|grant|bundle|"
    r"terms|conditions|t&cs?|expires?|ends?|until|valid|limited time|fixed price|site costs?)\b", re.I)


def promo_window(blocks: list[str], context: int = 2, max_chars: int = 24000) -> list[str]:
    """Keep blocks near promotional language (plus neighbours) so the offer-bearing part of a long page is
    what we hash and send to the extractor. Everything else is irrelevant to change detection."""
    keep: set[int] = set()
    for i, b in enumerate(blocks):
        if PROMO_RX.search(b) and (len(b) >= 12 or "$" in b):
            keep.add(i)
            for j in range(max(0, i - context), min(len(blocks), i + context + 1)):
                if len(blocks[j]) >= 20 or j == i:  # neighbouring menu labels are not evidence
                    keep.add(j)
    out, total = [], 0
    for i in sorted(keep):
        b = blocks[i]
        if total + len(b) > max_chars:
            break
        out.append(b)
        total += len(b) + 1
    return out


def material_hash(blocks: list[str]) -> str:
    return text_hash("\n".join(promo_window(blocks)))


def same_site(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)
