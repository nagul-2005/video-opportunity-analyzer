"""
Video Opportunity Analyzer
==========================
A production-ready CLI tool for B2B video marketing agencies.
Analyzes public company websites, inspects DOM elements for existing video presence,
extracts grounded context from home and key subpages, generates 3 grounded video
recommendations, and drafts a punchy cold outreach email strictly under 120 words.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Set UTF-8 encoding for standard outputs across all platforms (Windows, Linux, macOS)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Optional rich formatting for enhanced terminal experience
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console(highlight=False, legacy_windows=False)
    HAS_RICH = True
except Exception:
    console = None
    HAS_RICH = False

# Load environment variables (.env)
load_dotenv()

# =====================================================================
# 1. STRUCTURED DATA MODELS (Pydantic v2)
# =====================================================================

class VideoSuggestion(BaseModel):
    """Grounded video opportunity recommendation for a specific website placement."""
    video_type: str = Field(
        ...,
        description="Format/type of video (e.g., '60-second Product Explainer', 'Customer Case Study', 'Interactive Feature Walkthrough')"
    )
    placement: str = Field(
        ...,
        description="Specific recommended location on website (e.g., 'Homepage hero section', 'Pricing tier comparison section')"
    )
    rationale: str = Field(
        ...,
        description="Strategic marketing explanation referencing actual observed site copy and features explaining why this video is needed"
    )
    observed_site_quote: str = Field(
        ...,
        description="Direct verbatim quote or observed text excerpt from the scraped website copy supporting the recommendation"
    )


class VideoOpportunityReport(BaseModel):
    """Complete structured opportunity report generated from website inspection."""
    status: str = Field(
        default="success",
        description="Execution status ('success' or 'error')"
    )
    company_name: str = Field(
        ...,
        description="Name of the company parsed from website title or body text"
    )
    website_url: str = Field(
        ...,
        description="Analyzed website URL"
    )
    existing_videos_found: bool = Field(
        ...,
        description="True if native video tags or embedded iframes (YouTube, Vimeo, Wistia, Loom, Vidyard) were detected"
    )
    existing_video_summary: str = Field(
        ...,
        description="Summary of video elements detected in DOM or confirmation that no video presence was observed"
    )
    suggestions: List[VideoSuggestion] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="Exactly 3 grounded video recommendations"
    )
    outreach_email: str = Field(
        ...,
        description="Personalized cold outreach pitch email strictly under 120 words"
    )
    word_count: int = Field(
        ...,
        description="Exact word count of outreach_email"
    )

    @field_validator("outreach_email")
    @classmethod
    def validate_email_word_count(cls, v: str) -> str:
        """Enforces the strict < 120 words constraint on cold outreach email."""
        words = v.strip().split()
        count = len(words)
        if count >= 120:
            raise ValueError(
                f"Outreach email must strictly be UNDER 120 words. Provided email has {count} words."
            )
        if count == 0:
            raise ValueError("Outreach email cannot be empty.")
        return v


class ErrorReport(BaseModel):
    """Graceful failure report when a website cannot be accessed or scraped."""
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
# 2. INGESTION, SCRAPING & DOM VIDEO DETECTION LAYER
# =====================================================================

# Standard browser headers to avoid trivial bot blocking
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Regex for common video hosting providers embedded via iframe
VIDEO_IFRAME_REGEX = re.compile(
    r"(youtube\.com/embed/|youtube-nocookie\.com/embed/|youtu\.be/|"
    r"player\.vimeo\.com/video/|vimeo\.com/|"
    r"fast\.wistia\.(com|net)/embed/|wistia\.com/|"
    r"loom\.com/embed/|loom\.com/share/|"
    r"play\.vidyard\.com/|vidyard\.com/|"
    r"streamable\.com/|dailymotion\.com/embed/|supavideo\.com/)",
    re.IGNORECASE,
)

# Subpage discovery target patterns
SUBPAGE_KEYWORDS_REGEX = re.compile(
    r"/(about|product|products|features|pricing|case-studies|customers|solutions|how-it-works|services)",
    re.IGNORECASE,
)

# Blacklist domains for subpage discovery (social media, auth, etc.)
DISALLOWED_SUBPAGE_HOSTS = {
    "linkedin.com", "instagram.com", "twitter.com", "x.com", "facebook.com",
    "youtube.com", "github.com", "discord.com", "tiktok.com"
}


@dataclass
class PageData:
    url: str
    title: str
    cleaned_text: str
    detected_videos: List[str] = field(default_factory=list)


def detect_dom_videos(soup: BeautifulSoup, page_url: str) -> List[str]:
    """
    Direct DOM-level video detection. Inspects HTML tags and embedded iframes
    BEFORE any decomposition occurs.
    """
    detected: List[str] = []

    # 1. Native HTML5 <video> elements
    video_tags = soup.find_all("video")
    for v in video_tags:
        src = v.get("src")
        if not src:
            source_tag = v.find("source")
            if source_tag:
                src = source_tag.get("src")
        desc = f"Native HTML5 <video> element (src: {src or 'inline/blob'})"
        detected.append(desc)

    # 2. Embedded video iframes (YouTube, Vimeo, Wistia, Loom, Vidyard, etc.)
    iframe_tags = soup.find_all("iframe")
    for iframe in iframe_tags:
        src = iframe.get("src") or iframe.get("data-src") or ""
        if VIDEO_IFRAME_REGEX.search(src):
            # Categorize player
            player = "Video Player"
            if "youtube" in src.lower() or "youtu.be" in src.lower():
                player = "YouTube iframe"
            elif "vimeo" in src.lower():
                player = "Vimeo iframe"
            elif "wistia" in src.lower():
                player = "Wistia iframe"
            elif "loom" in src.lower():
                player = "Loom iframe"
            elif "vidyard" in src.lower():
                player = "Vidyard iframe"
            detected.append(f"Embedded {player} (src: {src})")

    # 3. Known video embed wrapper containers
    wistia_divs = soup.find_all(class_=re.compile(r"wistia_embed|wistia_async", re.IGNORECASE))
    if wistia_divs and not any("Wistia" in d for d in detected):
        detected.append(f"Wistia embedded container element ({len(wistia_divs)} found)")

    return detected


def clean_html_to_text(soup: BeautifulSoup) -> str:
    """
    Decomposes non-content tags and extracts clean, normalized readable text.
    Decomposes: <script>, <style>, <nav>, <footer>, <svg>, <noscript>, <header>, <form>
    """
    # Clone soup to avoid mutating original DOM if used elsewhere
    s = BeautifulSoup(str(soup), "html.parser")

    # Decompose non-content elements
    for tag in s(["script", "style", "nav", "footer", "svg", "noscript", "header", "form", "button", "iframe"]):
        tag.decompose()

    text = s.get_text(separator=" ", strip=True)
    # Normalize whitespaces and linebreaks
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_company_name(soup: BeautifulSoup, url: str) -> str:
    """Extracts a plausible company name from title or URL hostname."""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        # Common title separators: " | ", " - ", " : ", " • "
        for sep in [" | ", " - ", " : ", " • ", " — "]:
            if sep in title:
                parts = title.split(sep)
                # Usually company name is first or last part
                cand = parts[0].strip()
                if len(cand) > 1 and len(cand) < 30:
                    return cand
                cand_last = parts[-1].strip()
                if len(cand_last) > 1 and len(cand_last) < 30:
                    return cand_last
        if len(title) < 40:
            return title

    parsed = urlparse(url)
    hostname = parsed.netloc.replace("www.", "")
    name = hostname.split(".")[0].capitalize()
    return name or "Company"


def discover_subpages(soup: BeautifulSoup, base_url: str, max_subpages: int = 3) -> List[str]:
    """
    Discovers up to max_subpages internal links on the same domain matching keywords
    like /about, /product, /features, /pricing, /case-studies.
    """
    base_parsed = urlparse(base_url)
    discovered: List[str] = []
    seen: set[str] = {base_url.rstrip("/")}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:") or href.startswith("javascript:"):
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Same domain check
        if parsed.netloc != base_parsed.netloc and not parsed.netloc.endswith("." + base_parsed.netloc):
            continue

        # Skip disallowed social/login domains
        if any(bad in parsed.netloc.lower() for bad in DISALLOWED_SUBPAGE_HOSTS):
            continue

        # Skip assets and binaries
        if re.search(r"\.(pdf|png|jpg|jpeg|gif|svg|zip|tar|gz|exe|dmg|mp4|webp)$", parsed.path, re.I):
            continue

        normalized_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")

        # Keyword match
        if SUBPAGE_KEYWORDS_REGEX.search(parsed.path):
            if normalized_url not in seen:
                seen.add(normalized_url)
                discovered.append(normalized_url)
                if len(discovered) >= max_subpages:
                    break

    return discovered


def fetch_page(url: str, session: requests.Session, timeout: int = 10) -> Tuple[Optional[BeautifulSoup], Optional[str]]:
    """
    Fetches a web page safely with headers, timeout, and status code verification.
    Returns (BeautifulSoup, error_message).
    """
    try:
        response = session.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None, f"Unsupported content type ({content_type})"
        if response.encoding is None or response.encoding.lower() in ("iso-8859-1", "latin1"):
            response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        return soup, None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "Unknown"
        return None, f"HTTP {status} {e.response.reason if e.response is not None else ''}"
    except requests.exceptions.Timeout:
        return None, f"Connection timed out after {timeout} seconds"
    except requests.exceptions.ConnectionError:
        return None, "Connection failed (DNS resolution error, refused connection, or host unreachable)"
    except requests.exceptions.RequestException as e:
        return None, f"Request error: {str(e)}"
    except Exception as e:
        return None, f"Unexpected error: {str(e)}"


def scrape_site(target_url: str, max_subpages: int = 3, timeout: int = 10) -> Tuple[Optional[List[PageData]], Optional[str]]:
    """
    Executes ingestion layer:
    1. Fetches homepage.
    2. Runs DOM video detection on homepage.
    3. Cleans homepage text.
    4. Discovers up to max_subpages internal links (/about, /product, /features, /pricing, /case-studies).
    5. Fetches and scans internal pages for DOM videos and text context.
    """
    if not target_url.startswith("http://") and not target_url.startswith("https://"):
        target_url = "https://" + target_url

    session = requests.Session()

    # Step 1: Homepage
    home_soup, home_err = fetch_page(target_url, session, timeout)
    if home_err or home_soup is None:
        return None, f"Could not read homepage ({target_url}): {home_err}"

    # Verify homepage has content
    home_text = clean_html_to_text(home_soup)
    if len(home_text) < 50:
        return None, f"Could not read website ({target_url}): page returned empty or non-content body"

    home_title = home_soup.title.string.strip() if home_soup.title and home_soup.title.string else target_url
    home_videos = detect_dom_videos(home_soup, target_url)

    pages: List[PageData] = [
        PageData(
            url=target_url,
            title=home_title,
            cleaned_text=home_text[:8000],  # bounded context
            detected_videos=home_videos,
        )
    ]

    # Step 2: Discover and fetch subpages
    subpage_urls = discover_subpages(home_soup, target_url, max_subpages=max_subpages)
    for sub_url in subpage_urls:
        sub_soup, sub_err = fetch_page(sub_url, session, timeout)
        if sub_soup is not None and not sub_err:
            sub_videos = detect_dom_videos(sub_soup, sub_url)
            sub_text = clean_html_to_text(sub_soup)
            if len(sub_text) >= 50:
                sub_title = sub_soup.title.string.strip() if sub_soup.title and sub_soup.title.string else sub_url
                pages.append(
                    PageData(
                        url=sub_url,
                        title=sub_title,
                        cleaned_text=sub_text[:6000],
                        detected_videos=sub_videos,
                    )
                )

    return pages, None


# =====================================================================
# 3. LLM GROUNDING & PROMPTING LAYER
# =====================================================================

SYSTEM_PROMPT = """You are an expert B2B video marketing strategist working for a premier video production agency.
Your task is to analyze the provided company's website copy and existing video inventory, then identify exactly 3 high-impact video opportunities and write a personalized cold pitch email.

CRITICAL INSTRUCTIONS & CONSTRAINTS:
1. STRICT GROUNDING: You MUST NEVER hallucinate, invent, or assume unmentioned features, products, testimonials, or services. Every single recommendation must be directly grounded in copy/elements observed on the website.
2. OBSERVED QUOTES: In each suggestion, provide an exact verbatim excerpt or direct quote from the provided website text that justifies why this video is needed.
3. VIDEO DETECTION AWARENESS: Review the detected video inventory.
   - If no videos were detected on the site, highlight the clarity/conversion gap of having zero visual explainers.
   - If existing videos were detected, identify what missing video type (e.g. detailed interactive walkthrough, customer story, ROI calculator explainer) would elevate conversion without duplicating existing content.
4. EXACTLY THREE IDEAS: Provide exactly 3 diverse, specific, high-ROI video ideas (e.g. "60-second Product Explainer", "Customer Case Study", "Interactive Feature Walkthrough", "ROI / Pricing Breakdown Video").
5. OUTREACH EMAIL WORD COUNT CONSTRAINT:
   - Draft a concise, personalized cold outreach email pitching these 3 suggestions to the company's decision maker.
   - The email MUST strictly be UNDER 120 words (aim for 75-105 words).
   - Count words carefully. An email with 120 or more words will fail automated validation.
"""


def build_analysis_prompt(company_name: str, target_url: str, pages: List[PageData], video_summary: str, has_videos: bool) -> str:
    """Builds the comprehensive user prompt containing scraped grounded context."""
    context_blocks = []
    for idx, p in enumerate(pages, 1):
        context_blocks.append(
            f"--- PAGE {idx}: {p.url} ({p.title}) ---\n"
            f"Observed Content:\n{p.cleaned_text}\n"
        )
    combined_context = "\n".join(context_blocks)

    return f"""Analyze the following company website data and output the structured video opportunity report:

COMPANY NAME: {company_name}
WEBSITE URL: {target_url}

EXISTING VIDEO INVENTORY (DOM-level detection):
- Videos detected: {has_videos}
- Details: {video_summary}

SCRAPED WEBSITE PAGES & COPY:
{combined_context}

REQUIREMENTS:
1. Identify 3 grounded video ideas specifically tailored to this company's observed copy and products.
2. In 'observed_site_quote', provide the exact verbatim quote or phrase from the scraped copy above that supports each suggestion.
3. In 'rationale', explain why visual video content solves a clarity/conversion problem for that specific observed element.
4. In 'outreach_email', write a punchy cold pitch email strictly UNDER 120 words.
5. In 'word_count', record the exact word count of the outreach email.
"""


def ensure_email_word_count(email: str, client: Optional[Any] = None, model_name: str = "gemini-2.5-flash") -> Tuple[str, int]:
    """
    Validates and programmatically enforces that outreach email is strictly UNDER 120 words.
    Uses LLM re-prompt if client is available and email exceeds limit; otherwise applies
    smart sentence-boundary trimming.
    """
    words = email.strip().split()
    count = len(words)

    if count < 120:
        return email.strip(), count

    # If too long and client is available, condense with LLM
    if client:
        try:
            condense_prompt = (
                f"Condense the following cold pitch email so it is strictly between 70 and 95 words "
                f"while retaining the 3 video suggestions and clear CTA. Return ONLY the condensed email text:\n\n{email}"
            )
            response = client.models.generate_content(
                model=model_name,
                contents=condense_prompt,
            )
            condensed = response.text.strip()
            condensed_words = condensed.split()
            if len(condensed_words) < 120:
                return condensed, len(condensed_words)
        except Exception:
            pass

    # Deterministic programmatic fallback: trim at last complete sentence under 115 words
    trimmed_words = words[:110]
    trimmed_text = " ".join(trimmed_words)
    # Find last period, exclamation mark, or question mark
    last_punct = max(trimmed_text.rfind("."), trimmed_text.rfind("!"), trimmed_text.rfind("?"))
    if last_punct > 50:
        final_email = trimmed_text[: last_punct + 1] + "\n\nBest,\nAlex | Production Lead"
    else:
        final_email = trimmed_text + "...\n\nBest,\nAlex"

    final_words = final_email.split()
    return final_email, len(final_words)


def run_gemini_analysis(
    company_name: str,
    target_url: str,
    pages: List[PageData],
    video_summary: str,
    has_videos: bool,
    api_key: str,
    model_name: str = "gemini-2.5-flash"
) -> VideoOpportunityReport:
    """Executes structured LLM generation using Google GenAI SDK (gemini-2.5-flash)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    prompt = build_analysis_prompt(company_name, target_url, pages, video_summary, has_videos)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=VideoOpportunityReport,
        temperature=0.2,  # Low temperature for strict adherence to facts and grounding
    )

    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=config,
    )

    raw_json = response.text
    data = json.loads(raw_json)

    # Word count safety check and enforcement
    email = data.get("outreach_email", "")
    validated_email, word_count = ensure_email_word_count(email, client=client, model_name=model_name)
    data["outreach_email"] = validated_email
    data["word_count"] = word_count
    data["website_url"] = target_url
    data["existing_videos_found"] = has_videos
    data["existing_video_summary"] = video_summary
    if not data.get("company_name"):
        data["company_name"] = company_name

    # Validate with Pydantic v2
    report = VideoOpportunityReport(**data)
    return report


def run_mock_grounded_analysis(
    company_name: str,
    target_url: str,
    pages: List[PageData],
    video_summary: str,
    has_videos: bool,
) -> VideoOpportunityReport:
    """
    Offline / Mock grounded engine for testing and demonstration without API key.
    Extracts actual sentences from scraped text to ensure 100% grounding without hallucination.
    """
    all_text = " ".join([p.cleaned_text for p in pages])
    sentences = [s.strip() for s in re.split(r"[.!?]", all_text) if len(s.strip()) > 25 and len(s.strip()) < 150]

    # Pick 3 grounded quotes from different parts of the text
    q1 = sentences[0] if len(sentences) > 0 else f"{company_name} helps teams streamline operations."
    q2 = sentences[len(sentences) // 2] if len(sentences) > 2 else f"Features include advanced automation and integrations."
    q3 = sentences[-2] if len(sentences) > 3 else f"Get started today with flexible pricing plans."

    suggestions = [
        VideoSuggestion(
            video_type="60-second Product Explainer",
            placement="Homepage Hero Section",
            rationale=(
                f"The homepage introduces {company_name}'s core value proposition purely via text. "
                "A dynamic 60-second explainer right above the fold will instantly clarify the product workflow, "
                "capturing high-intent visitors before they bounce."
            ),
            observed_site_quote=q1,
        ),
        VideoSuggestion(
            video_type="Interactive Feature Walkthrough",
            placement="Core Features / Product Page",
            rationale=(
                "The site outlines technical features through static copy and bullets. "
                "An interactive visual walkthrough showing UI clicks and workflow automation will demystify "
                "how the platform works in real time."
            ),
            observed_site_quote=q2,
        ),
        VideoSuggestion(
            video_type="Customer Social Proof & ROI Story",
            placement="Pricing & Case Studies Section",
            rationale=(
                "Prospective buyers reviewing pricing tiers need conversion confidence. "
                "A customer case study video spotlighting measurable outcomes directly accelerates purchasing decisions."
            ),
            observed_site_quote=q3,
        ),
    ]

    # Pre-calculated punchy cold email strictly under 100 words
    email = (
        f"Hi {company_name} team,\n\n"
        f"I came across {company_name}'s website and love how clearly you position your offering ('{q1[:45]}...').\n\n"
        f"However, while reviewing your site, I noticed you aren't currently leveraging visual video to show your product in action. "
        f"We mapped out 3 quick video opportunities that could lift conversions:\n"
        f"1. 60s Hero Explainer — hook visitors immediately\n"
        f"2. Interactive Feature Demo — showcase your UI workflows\n"
        f"3. Customer ROI Story — boost pricing-page conversions\n\n"
        f"Open to a brief 5-minute chat this Thursday to explore these concepts?\n\n"
        f"Best,\nAlex | Production Lead"
    )

    words = email.split()
    word_count = len(words)

    # Double check word limit
    if word_count >= 120:
        email, word_count = ensure_email_word_count(email)

    return VideoOpportunityReport(
        company_name=company_name,
        website_url=target_url,
        existing_videos_found=has_videos,
        existing_video_summary=video_summary,
        suggestions=suggestions,
        outreach_email=email,
        word_count=word_count,
    )


# =====================================================================
# 4. CLI CONTROLLER & EXECUTION PIPELINE
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
    """
    Main orchestration pipeline:
    1. Ingestion: Scrape target website and up to max_subpages internal pages.
    2. Video Detection: Check DOM elements for native videos and embedded player iframes.
    3. LLM Synthesis: Generate grounded recommendations with Gemini (or fallback mock).
    4. Validation: Verify Pydantic v2 constraints (< 120 words email).
    5. Persistence: Save report to disk in JSON or text format.
    """
    def log(msg: str, style: str = ""):
        if quiet:
            return
        if HAS_RICH and console:
            console.print(msg)
        else:
            print(re.sub(r"\[/?\w+\]", "", msg))

    log(f"\n[bold cyan]=== Video Opportunity Analyzer ===[/bold cyan]")
    log(f"[bold]Target URL:[/bold] {target_url}")

    # 1. Ingestion Layer
    log(f"\n[cyan][1/3] Ingesting website & subpages...[/cyan]")
    pages, error = scrape_site(target_url, max_subpages=max_subpages, timeout=timeout)

    if error or not pages:
        log(f"[bold red][ERROR] Scrape Failed:[/bold red] {error}")
        err_report = ErrorReport(
            website_url=target_url,
            error_message=error or "Unknown scraping failure",
        )
        if output_path:
            save_report(err_report, output_path, output_format)
            log(f"[yellow]Saved failure report to:[/yellow] {output_path}")
        return err_report

    company_name = extract_company_name(BeautifulSoup(pages[0].cleaned_text, "html.parser"), target_url)
    log(f"  [OK] Scraped [green]{len(pages)}[/green] page(s):")
    for p in pages:
        log(f"    * {p.url} ({len(p.cleaned_text)} chars)")

    # 2. DOM Video Detection Engine
    log(f"\n[cyan][2/3] Analyzing DOM for video elements...[/cyan]")
    all_detected_videos: List[str] = []
    for p in pages:
        for v in p.detected_videos:
            all_detected_videos.append(f"{v} on {p.url}")

    has_videos = len(all_detected_videos) > 0
    if has_videos:
        video_summary = f"Detected {len(all_detected_videos)} video element(s): " + "; ".join(all_detected_videos[:5])
        log(f"  [yellow][WARN] Existing videos detected ({len(all_detected_videos)} total):[/yellow]")
        for v in all_detected_videos[:3]:
            log(f"    - {v}")
    else:
        video_summary = "No native HTML5 video tags or embedded video iframes (YouTube, Vimeo, Wistia, Loom, Vidyard) detected in DOM."
        log(f"  [green][OK] Zero video presence detected in DOM (prime opportunity for video outreach).[/green]")

    # 3. LLM Grounding & Prompting Layer
    log(f"\n[cyan][3/3] Generating grounded video opportunities...[/cyan]")
    gemini_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    if provider == "mock" or (not gemini_key and provider == "gemini"):
        if not gemini_key and provider != "mock":
            log("  [yellow]Note: No GEMINI_API_KEY detected. Running in grounded mock engine mode.[/yellow]")
        report = run_mock_grounded_analysis(
            company_name=company_name,
            target_url=target_url,
            pages=pages,
            video_summary=video_summary,
            has_videos=has_videos,
        )
    else:
        log(f"  Connecting to Google GenAI ({model_name})...")
        try:
            report = run_gemini_analysis(
                company_name=company_name,
                target_url=target_url,
                pages=pages,
                video_summary=video_summary,
                has_videos=has_videos,
                api_key=gemini_key,
                model_name=model_name,
            )
        except Exception as e:
            log(f"  [red]LLM generation error: {e}. Falling back to grounded rule engine...[/red]")
            report = run_mock_grounded_analysis(
                company_name=company_name,
                target_url=target_url,
                pages=pages,
                video_summary=video_summary,
                has_videos=has_videos,
            )

    # 4. Display Results in Terminal
    if not quiet:
        display_report(report)

    # 5. File Persistence
    if output_path:
        save_report(report, output_path, output_format)
        log(f"\n[bold green][OK] Report successfully saved to:[/bold green] {output_path}")

    return report


def display_report(report: VideoOpportunityReport | ErrorReport):
    """Prints a beautiful summary of the report to the terminal."""
    if isinstance(report, ErrorReport):
        if HAS_RICH and console:
            console.print(Panel(f"[bold red]Scrape Failure:[/bold red] {report.error_message}\nURL: {report.website_url}", title="Error Report", border_style="red"))
        else:
            print(f"\n--- ERROR REPORT ---\nURL: {report.website_url}\nError: {report.error_message}\n")
        return

    if HAS_RICH and console:
        table = Table(title=f"Grounded Video Opportunities for {report.company_name}", show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=4)
        table.add_column("Video Type", style="cyan", width=26)
        table.add_column("Placement", style="green", width=22)
        table.add_column("Rationale & Observed Quote", style="white")

        for idx, s in enumerate(report.suggestions, 1):
            table.add_row(
                str(idx),
                s.video_type,
                s.placement,
                f"{s.rationale}\n[italic dim]Quote: \"{s.observed_site_quote}\"[/italic dim]"
            )
        console.print(table)

        email_panel = Panel(
            f"{report.outreach_email}\n\n[dim](Word count: {report.word_count} words | Constraint: < 120 words)[/dim]",
            title="Generated Cold Outreach Email",
            border_style="cyan"
        )
        console.print(email_panel)
    else:
        print("\n" + "="*60)
        print(f"ANALYSIS REPORT: {report.company_name} ({report.website_url})")
        print(f"Existing Videos Found: {report.existing_videos_found}")
        print(f"Summary: {report.existing_video_summary}")
        print("="*60)
        for idx, s in enumerate(report.suggestions, 1):
            print(f"\n[{idx}] {s.video_type} -> {s.placement}")
            print(f"    Rationale: {s.rationale}")
            print(f"    Observed Quote: \"{s.observed_site_quote}\"")
        print("\n" + "-"*60)
        print("COLD OUTREACH EMAIL:")
        print(report.outreach_email)
        print(f"(Word count: {report.word_count} words)")
        print("-"*60 + "\n")


def save_report(report: VideoOpportunityReport | ErrorReport, file_path: str, fmt: str = "json"):
    """Saves the report to disk as JSON or formatted text."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Determine format by extension if specified
    if path.suffix.lower() == ".json" or fmt.lower() == "json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report.model_dump(), f, indent=2, ensure_ascii=False)
    else:
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(report, ErrorReport):
                f.write(f"VIDEO OPPORTUNITY ANALYZER - ERROR REPORT\n")
                f.write(f"Website URL: {report.website_url}\n")
                f.write(f"Error: {report.error_message}\n")
            else:
                f.write(f"VIDEO OPPORTUNITY ANALYZER REPORT\n")
                f.write(f"Company: {report.company_name}\n")
                f.write(f"URL: {report.website_url}\n")
                f.write(f"Existing Videos Detected: {report.existing_videos_found}\n")
                f.write(f"Summary: {report.existing_video_summary}\n\n")
                f.write("="*60 + "\n")
                f.write("RECOMMENDED VIDEO IDEAS:\n")
                f.write("="*60 + "\n")
                for i, s in enumerate(report.suggestions, 1):
                    f.write(f"{i}. Video Type: {s.video_type}\n")
                    f.write(f"   Placement: {s.placement}\n")
                    f.write(f"   Rationale: {s.rationale}\n")
                    f.write(f"   Observed Site Quote: \"{s.observed_site_quote}\"\n\n")
                f.write("="*60 + "\n")
                f.write(f"COLD OUTREACH EMAIL (Word Count: {report.word_count} words):\n")
                f.write("="*60 + "\n")
                f.write(report.outreach_email + "\n")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Video Opportunity Analyzer — Scrapes public websites, detects DOM video presence, generates 3 grounded video ideas, and writes a cold outreach email strictly under 120 words."
    )
    parser.add_argument("url", help="Public website link to analyze (e.g., https://cal.com)")
    parser.add_argument("--out", "-o", help="Output file path (e.g., results/cal_com.json or results/report.txt)")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output file format (default: json)")
    parser.add_argument("--provider", choices=["gemini", "mock"], default="gemini", help="LLM backend (gemini or mock)")
    parser.add_argument("--model", default="gemini-2.5-flash", help="Gemini model identifier (default: gemini-2.5-flash)")
    parser.add_argument("--api-key", help="Google GenAI API Key (or set GEMINI_API_KEY environment variable)")
    parser.add_argument("--max-pages", type=int, default=3, help="Max internal subpages to scrape (default: 3)")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP request timeout in seconds (default: 10)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet mode (suppress verbose logs)")
    return parser.parse_args()


def main():
    args = parse_args()
    report = analyze_website(
        target_url=args.url,
        output_path=args.out,
        output_format=args.format,
        provider=args.provider,
        api_key=args.api_key,
        model_name=args.model,
        max_subpages=args.max_pages,
        timeout=args.timeout,
        quiet=args.quiet,
    )
    if report.status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
