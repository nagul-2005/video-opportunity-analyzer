"""
Video Opportunity Analyzer
==========================
A clean, production-ready CLI tool for B2B video marketing agencies.
Analyzes public websites, detects DOM video presence, generates 3 grounded
video ideas with rationales, and drafts a cold pitch email under 120 words.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Configure terminal encoding for cross-platform compatibility
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# =====================================================================
# 1. DATA MODELS (Pydantic v2)
# =====================================================================

class VideoSuggestion(BaseModel):
    """A grounded video opportunity recommendation."""
    video_type: str = Field(description="Format (e.g., 60-second Product Explainer, Customer Case Study)")
    placement: str = Field(description="Recommended location on site (e.g., Homepage hero, Pricing section)")
    rationale: str = Field(description="Strategic reason referencing observed site features")
    observed_site_quote: str = Field(description="Direct verbatim quote from the website supporting the idea")


class VideoOpportunityReport(BaseModel):
    """Complete analysis report."""
    status: str = "success"
    company_name: str
    website_url: str
    existing_videos_found: bool
    existing_video_summary: str
    suggestions: List[VideoSuggestion] = Field(..., min_length=3, max_length=3)
    outreach_email: str
    word_count: int

    @field_validator("outreach_email")
    @classmethod
    def validate_email_length(cls, v: str) -> str:
        words = v.strip().split()
        if len(words) >= 120:
            raise ValueError(f"Outreach email must be strictly under 120 words (got {len(words)} words).")
        if len(words) == 0:
            raise ValueError("Outreach email cannot be empty.")
        return v


class ErrorReport(BaseModel):
    """Graceful failure report when a site cannot be read."""
    status: str = "error"
    website_url: str
    error_message: str
    company_name: str = "Unknown"
    existing_videos_found: bool = False
    existing_video_summary: str = "Analysis aborted: site could not be accessed."
    suggestions: List[VideoSuggestion] = Field(default_factory=list)
    outreach_email: str = ""
    word_count: int = 0


# =====================================================================
# 2. SCRAPING & DOM VIDEO DETECTION
# =====================================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

VIDEO_IFRAME_PATTERN = re.compile(r"(youtube\.com|youtu\.be|vimeo\.com|wistia\.(com|net)|loom\.com|vidyard\.com)", re.I)
SUBPAGE_PATTERN = re.compile(r"/(about|product|products|features|pricing|case-studies|solutions)", re.I)


def detect_dom_videos(soup: BeautifulSoup, page_url: str) -> List[str]:
    """Detects video elements directly in the DOM before any HTML cleaning."""
    found = []
    # 1. HTML5 native <video> and <source> tags
    for v in soup.find_all("video"):
        src = v.get("src") or (v.find("source").get("src") if v.find("source") else "inline/blob")
        found.append(f"HTML5 video tag ({src})")

    # 2. Embedded video players (YouTube, Vimeo, Wistia, Loom, Vidyard)
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src") or iframe.get("data-src") or ""
        if VIDEO_IFRAME_PATTERN.search(src):
            platform = "YouTube" if "youtu" in src.lower() else "Vimeo" if "vimeo" in src.lower() else "Loom" if "loom" in src.lower() else "Wistia" if "wistia" in src.lower() else "Vidyard"
            found.append(f"Embedded {platform} iframe ({src[:60]}...)")

    # 3. Known embed containers (e.g. Wistia async)
    if soup.find_all(class_=re.compile(r"wistia_embed|wistia_async", re.I)):
        found.append("Wistia container element")

    return found


def clean_html_to_text(soup: BeautifulSoup) -> str:
    """Decomposes non-content tags and returns clean body text."""
    clone = BeautifulSoup(str(soup), "html.parser")
    for tag in clone(["script", "style", "nav", "footer", "svg", "noscript", "header", "form"]):
        tag.decompose()
    return re.sub(r"\s+", " ", clone.get_text(separator=" ", strip=True))


def discover_subpages(soup: BeautifulSoup, base_url: str, max_subpages: int = 3) -> List[str]:
    """Finds internal links matching keywords like /about, /product, /pricing."""
    base_netloc = urlparse(base_url).netloc
    found = []
    seen = {base_url.rstrip("/")}

    for a in soup.find_all("a", href=True):
        full_url = urljoin(base_url, a["href"].strip())
        parsed = urlparse(full_url)
        if parsed.netloc == base_netloc and SUBPAGE_PATTERN.search(parsed.path):
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            if clean_url not in seen and not clean_url.endswith((".pdf", ".png", ".jpg")):
                seen.add(clean_url)
                found.append(clean_url)
                if len(found) >= max_subpages:
                    break
    return found


def extract_company_name(soup: BeautifulSoup, url: str) -> str:
    """Extracts company name from page title or domain name."""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        for sep in [" | ", " - ", " : ", " • "]:
            if sep in title:
                return title.split(sep)[0].strip()
        if len(title) < 30:
            return title
    return urlparse(url).netloc.replace("www.", "").split(".")[0].capitalize()


def fetch_page(url: str, session: requests.Session, timeout: int = 10) -> Tuple[Optional[BeautifulSoup], Optional[str]]:
    """Safely fetches a webpage with error handling."""
    try:
        res = session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        res.raise_for_status()
        if "text/html" not in res.headers.get("Content-Type", ""):
            return None, "Not an HTML page"
        res.encoding = res.apparent_encoding or "utf-8"
        return BeautifulSoup(res.text, "html.parser"), None
    except requests.exceptions.RequestException as e:
        return None, f"Connection failed: {e}"


def scrape_site(target_url: str, max_subpages: int = 3, timeout: int = 10) -> Tuple[Optional[dict], Optional[str]]:
    """Scrapes homepage + subpages and detects all DOM video elements."""
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    session = requests.Session()
    home_soup, err = fetch_page(target_url, session, timeout)
    if err or not home_soup:
        return None, err or "Could not load homepage"

    home_text = clean_html_to_text(home_soup)
    if len(home_text) < 50:
        return None, "Homepage returned empty content"

    all_videos = detect_dom_videos(home_soup, target_url)
    context = [f"=== Homepage ({target_url}) ===\n{home_text[:6000]}"]

    # Discover and scrape internal subpages
    subpages = discover_subpages(home_soup, target_url, max_subpages)
    for sub_url in subpages:
        sub_soup, sub_err = fetch_page(sub_url, session, timeout)
        if sub_soup and not sub_err:
            all_videos.extend(detect_dom_videos(sub_soup, sub_url))
            sub_text = clean_html_to_text(sub_soup)
            if len(sub_text) >= 50:
                context.append(f"=== Subpage ({sub_url}) ===\n{sub_text[:4000]}")

    return {
        "company_name": extract_company_name(home_soup, target_url),
        "target_url": target_url,
        "context": "\n\n".join(context),
        "all_videos": all_videos,
        "has_videos": len(all_videos) > 0,
        "pages_scraped": 1 + len(subpages),
    }, None


# =====================================================================
# 3. LLM GROUNDING & PROMPTING
# =====================================================================

def ensure_email_word_count(email: str) -> Tuple[str, int]:
    """Guarantees the outreach email is strictly under 120 words."""
    words = email.strip().split()
    if len(words) < 120:
        return email.strip(), len(words)

    # Trim to 105 words at the last complete sentence
    trimmed = " ".join(words[:105])
    last_stop = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
    if last_stop > 40:
        trimmed = trimmed[:last_stop + 1] + "\n\nBest,\nAlex"
    return trimmed, len(trimmed.split())


def run_gemini_analysis(data: dict, api_key: str, model_name: str = "gemini-2.5-flash") -> VideoOpportunityReport:
    """Generates structured video recommendations using Gemini with schema enforcement."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    prompt = f"""You are an expert B2B video marketing strategist.
Analyze this company's website copy and DOM video presence:

COMPANY: {data['company_name']}
URL: {data['target_url']}
EXISTING VIDEOS DETECTED IN DOM: {data['has_videos']} (Details: {data['all_videos']})

WEBSITE TEXT:
{data['context']}

RULES:
1. STRICT GROUNDING: Never invent features. Every suggestion must cite a verbatim quote from the website in 'observed_site_quote'.
2. Exactly 3 high-impact video ideas where visual video is missing.
3. Outreach email MUST strictly be UNDER 120 words (aim for 80-100 words).
"""
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VideoOpportunityReport,
            temperature=0.2,
        ),
    )
    result = json.loads(response.text)
    email, count = ensure_email_word_count(result.get("outreach_email", ""))
    result["outreach_email"] = email
    result["word_count"] = count
    result["website_url"] = data["target_url"]
    result["existing_videos_found"] = data["has_videos"]
    result["existing_video_summary"] = (
        f"Detected {len(data['all_videos'])} video(s): {', '.join(data['all_videos'][:3])}"
        if data["has_videos"] else "No video elements found in DOM."
    )
    return VideoOpportunityReport(**result)


def run_mock_grounded_analysis(data: dict) -> VideoOpportunityReport:
    """Offline heuristic engine for testing without an API key."""
    sentences = [s.strip() for s in re.split(r"[.!?]", data["context"]) if 20 < len(s.strip()) < 120]
    q1 = sentences[0] if sentences else f"{data['company_name']} helps streamline business operations."
    q2 = sentences[len(sentences) // 2] if len(sentences) > 2 else "Explore key features and tools."
    q3 = sentences[-2] if len(sentences) > 3 else "Get started today."

    suggestions = [
        VideoSuggestion(
            video_type="60-second Product Explainer",
            placement="Homepage Hero Section",
            rationale="Clarifies core value proposition visually for high-intent visitors.",
            observed_site_quote=q1,
        ),
        VideoSuggestion(
            video_type="Interactive Feature Walkthrough",
            placement="Features / Product Section",
            rationale="Demonstrates UI workflows in action instead of static text.",
            observed_site_quote=q2,
        ),
        VideoSuggestion(
            video_type="Customer Success Story",
            placement="Pricing / Testimonials Section",
            rationale="Provides visual social proof to boost purchase confidence.",
            observed_site_quote=q3,
        ),
    ]

    email = (
        f"Hi {data['company_name']} team,\n\n"
        f"I came across your site and loved your positioning: \"{q1[:45]}...\".\n\n"
        "However, I noticed your pages rely entirely on static text. We mapped out 3 quick video opportunities:\n"
        "1. 60s Hero Explainer — hook visitors immediately\n"
        "2. Feature Walkthrough — showcase your UI in action\n"
        "3. Customer Success Story — lift pricing conversions\n\n"
        "Would you be open to a 5-minute chat this Thursday?\n\n"
        "Best,\nAlex | Video Strategist"
    )
    email, count = ensure_email_word_count(email)

    return VideoOpportunityReport(
        company_name=data["company_name"],
        website_url=data["target_url"],
        existing_videos_found=data["has_videos"],
        existing_video_summary=f"Detected {len(data['all_videos'])} video(s)" if data["has_videos"] else "No videos found in DOM.",
        suggestions=suggestions,
        outreach_email=email,
        word_count=count,
    )


# =====================================================================
# 4. ORCHESTRATION & CLI
# =====================================================================

def analyze_website(
    target_url: str,
    output_path: Optional[str] = None,
    output_format: str = "json",
    provider: str = "gemini",
    api_key: Optional[str] = None,
    model_name: str = "gemini-2.5-flash",
    max_subpages: int = 3,
    timeout: int = 10,
    quiet: bool = False,
) -> VideoOpportunityReport | ErrorReport:
    """Main function that connects scraping, video detection, LLM, and file saving."""
    if not quiet:
        print(f"\n[1/3] Scraping {target_url}...")

    data, err = scrape_site(target_url, max_subpages, timeout)
    if err or not data:
        if not quiet:
            print(f"[ERROR] Could not read site: {err}")
        err_report = ErrorReport(website_url=target_url, error_message=err or "Failed to scrape")
        if output_path:
            save_report(err_report, output_path, output_format)
        return err_report

    if not quiet:
        print(f"[2/3] Scraped {data['pages_scraped']} pages. Videos detected: {data['has_videos']} ({len(data['all_videos'])})")
        print(f"[3/3] Generating grounded analysis via {provider}...")

    key = api_key or os.getenv("GEMINI_API_KEY")
    if provider == "mock" or not key:
        report = run_mock_grounded_analysis(data)
    else:
        try:
            report = run_gemini_analysis(data, key, model_name)
        except Exception as e:
            if not quiet:
                print(f"[WARN] Gemini error ({e}). Using grounded mock fallback.")
            report = run_mock_grounded_analysis(data)

    if not quiet:
        print("\n" + "=" * 60)
        print(f"REPORT: {report.company_name} ({report.website_url})")
        for i, s in enumerate(report.suggestions, 1):
            print(f"\n[{i}] {s.video_type} -> {s.placement}")
            print(f"    Rationale: {s.rationale}")
            print(f"    Quote: \"{s.observed_site_quote}\"")
        print("\nOUTREACH EMAIL (Word Count:", report.word_count, "words):")
        print(report.outreach_email)
        print("=" * 60)

    if output_path:
        save_report(report, output_path, output_format)
        if not quiet:
            print(f"[OK] Saved to: {output_path}")

    return report


def save_report(report: VideoOpportunityReport | ErrorReport, path: str, fmt: str = "json"):
    """Saves the report as JSON or formatted text."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix == ".txt" or fmt == "text":
        with open(p, "w", encoding="utf-8") as f:
            if isinstance(report, ErrorReport):
                f.write(f"ERROR REPORT: {report.website_url}\n{report.error_message}\n")
            else:
                f.write(f"COMPANY: {report.company_name} ({report.website_url})\n\nSUGGESTIONS:\n")
                for i, s in enumerate(report.suggestions, 1):
                    f.write(f"{i}. {s.video_type} ({s.placement})\n   {s.rationale}\n   Quote: \"{s.observed_site_quote}\"\n\n")
                f.write(f"OUTREACH EMAIL ({report.word_count} words):\n{report.outreach_email}\n")
    else:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(report.model_dump(), f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="Video Opportunity Analyzer CLI")
    parser.add_argument("url", help="Company website URL")
    parser.add_argument("--out", "-o", help="Output file path (e.g., results/output.json)")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--provider", choices=["gemini", "mock"], default="gemini")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    report = analyze_website(
        target_url=args.url,
        output_path=args.out,
        output_format=args.format,
        provider=args.provider,
        max_subpages=args.max_pages,
        quiet=args.quiet,
    )
    if report.status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
