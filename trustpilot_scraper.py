"""
Trustpilot review scraper CLI.

Fetches the overall star rating and total review count for a business on
Trustpilot.

Usage:
  python trustpilot_scraper.py <domain-or-url>

Examples:
  python trustpilot_scraper.py example.com
  python trustpilot_scraper.py https://www.trustpilot.com/review/example.com
"""

import json
import logging
import sys

from app.scraper.trustpilot import scrape_trustpilot


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if len(argv) < 2:
        print("Usage: python trustpilot_scraper.py <domain-or-url>", file=sys.stderr)
        return 2

    result = scrape_trustpilot(argv[1])
    print(json.dumps({
        "ok": result.ok,
        "url": result.url,
        "domain": result.domain,
        "business_name": result.business_name,
        "rating": result.rating,
        "review_count": result.review_count,
        "status_code": result.status_code,
        "error": result.error,
    }, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
