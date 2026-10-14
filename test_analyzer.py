"""
Unit and Integration Tests for Video Opportunity Analyzer
"""

import unittest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from analyzer import (
    VideoSuggestion,
    VideoOpportunityReport,
    ErrorReport,
    detect_dom_videos,
    clean_html_to_text,
    discover_subpages,
    extract_company_name,
    ensure_email_word_count,
    analyze_website,
)


class TestVideoDetectionEngine(unittest.TestCase):
    """Verifies DOM-level video detection without LLM guesswork."""

    def test_detect_youtube_iframe(self):
        html = """
        <html><body>
            <h1>Welcome</h1>
            <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" width="560" height="315"></iframe>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        detected = detect_dom_videos(soup, "https://example.com")
        self.assertEqual(len(detected), 1)
        self.assertIn("YouTube", detected[0])

    def test_detect_vimeo_iframe(self):
        html = """
        <html><body>
            <iframe src="https://player.vimeo.com/video/123456789"></iframe>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        detected = detect_dom_videos(soup, "https://example.com")
        self.assertEqual(len(detected), 1)
        self.assertIn("Vimeo", detected[0])

    def test_detect_loom_iframe(self):
        html = """
        <html><body>
            <iframe src="https://www.loom.com/embed/abcdef123456"></iframe>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        detected = detect_dom_videos(soup, "https://example.com")
        self.assertEqual(len(detected), 1)
        self.assertIn("Loom", detected[0])

    def test_detect_native_video_tag(self):
        html = """
        <html><body>
            <video width="320" height="240" controls>
                <source src="movie.mp4" type="video/mp4">
            </video>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        detected = detect_dom_videos(soup, "https://example.com")
        self.assertEqual(len(detected), 1)
        self.assertIn("HTML5", detected[0])
        self.assertIn("movie.mp4", detected[0])

    def test_detect_wistia_container(self):
        html = """
        <html><body>
            <div class="wistia_embed wistia_async_abc123" style="height:360px;width:640px">&nbsp;</div>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        detected = detect_dom_videos(soup, "https://example.com")
        self.assertEqual(len(detected), 1)
        self.assertIn("Wistia", detected[0])

    def test_no_video_elements(self):
        html = """
        <html><body>
            <h1>Pure Text Page</h1>
            <p>We build software for teams.</p>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        detected = detect_dom_videos(soup, "https://example.com")
        self.assertEqual(len(detected), 0)


class TestHTMLCleaningAndSubpages(unittest.TestCase):
    """Verifies content extraction and subpage discovery."""

    def test_clean_html_removes_scripts_and_nav(self):
        html = """
        <html>
            <head><script>alert('spam')</script><style>body { color: red; }</style></head>
            <body>
                <nav><a href="/home">Home</a><a href="/login">Login</a></nav>
                <main><h1>Hero Headline</h1><p>Our SaaS saves 20 hours a week.</p></main>
                <footer>Copyright 2026. All rights reserved.</footer>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        text = clean_html_to_text(soup)
        self.assertNotIn("alert", text)
        self.assertNotIn("body { color: red; }", text)
        self.assertNotIn("Login", text)
        self.assertNotIn("Copyright 2026", text)
        self.assertIn("Hero Headline", text)
        self.assertIn("saves 20 hours a week", text)

    def test_subpage_discovery_filters(self):
        html = """
        <html><body>
            <a href="/about">About Us</a>
            <a href="/pricing">Pricing Plans</a>
            <a href="/features/automation">Automation Feature</a>
            <a href="https://linkedin.com/company/acme">LinkedIn</a>
            <a href="/contact#form">Contact Anchor</a>
            <a href="/whitepaper.pdf">PDF Document</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        subpages = discover_subpages(soup, "https://example.com", max_subpages=3)
        self.assertIn("https://example.com/about", subpages)
        self.assertIn("https://example.com/pricing", subpages)
        self.assertIn("https://example.com/features/automation", subpages)
        self.assertNotIn("https://linkedin.com/company/acme", subpages)
        self.assertFalse(any("pdf" in p for p in subpages))
        self.assertLessEqual(len(subpages), 3)


class TestPydanticValidation(unittest.TestCase):
    """Verifies strict schema and word count constraints."""

    def setUp(self):
        self.valid_suggestions = [
            VideoSuggestion(
                video_type="Product Explainer",
                placement="Homepage Hero",
                rationale="Explains features clearly.",
                observed_site_quote="Saves 10 hours weekly.",
            ),
            VideoSuggestion(
                video_type="Feature Walkthrough",
                placement="Features Section",
                rationale="Shows UI workflow.",
                observed_site_quote="Automate repetitive tasks.",
            ),
            VideoSuggestion(
                video_type="Customer Story",
                placement="Pricing Page",
                rationale="Builds conversion trust.",
                observed_site_quote="Over 10,000 teams trust Acme.",
            ),
        ]

    def test_valid_report_passes(self):
        email = "Hi team, loved your platform. We recommend 3 video ideas to boost conversion. Let's chat!"
        report = VideoOpportunityReport(
            company_name="Acme",
            website_url="https://acme.com",
            existing_videos_found=False,
            existing_video_summary="No videos found.",
            suggestions=self.valid_suggestions,
            outreach_email=email,
            word_count=len(email.split()),
        )
        self.assertEqual(report.company_name, "Acme")
        self.assertEqual(len(report.suggestions), 3)
        self.assertLess(report.word_count, 120)

    def test_email_exceeding_120_words_raises_validation_error(self):
        long_email = "word " * 125
        with self.assertRaises(ValidationError):
            VideoOpportunityReport(
                company_name="Acme",
                website_url="https://acme.com",
                existing_videos_found=False,
                existing_video_summary="No videos found.",
                suggestions=self.valid_suggestions,
                outreach_email=long_email,
                word_count=125,
            )

    def test_less_than_3_suggestions_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            VideoOpportunityReport(
                company_name="Acme",
                website_url="https://acme.com",
                existing_videos_found=False,
                existing_video_summary="No videos found.",
                suggestions=self.valid_suggestions[:2],  # only 2
                outreach_email="Hi team, nice product.",
                word_count=4,
            )

    def test_ensure_email_word_count_enforcement(self):
        long_email = "This is a sentence. " * 30  # ~120 words
        trimmed, count = ensure_email_word_count(long_email)
        self.assertLess(count, 120)


class TestGracefulFailure(unittest.TestCase):
    """Verifies that non-existent or blocked sites fail gracefully without LLM calls."""

    def test_nonexistent_domain_returns_error_report(self):
        bad_url = "https://this-domain-does-not-exist-at-all-987654321.com"
        report = analyze_website(bad_url, quiet=True)
        self.assertEqual(report.status, "error")
        self.assertIsInstance(report, ErrorReport)
        self.assertIn("Connection failed", report.error_message)


if __name__ == "__main__":
    unittest.main()
