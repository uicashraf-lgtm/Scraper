from urllib.parse import quote


def build_affiliate_link(base_url: str, affiliate_template: str | None) -> str:
    if not affiliate_template:
        return base_url
    # A template without the {url} placeholder cannot route to the actual
    # product page — returning it as-is would send clicks to the vendor's
    # home page (or an affiliate root URL). Fall back to the product URL.
    if "{url}" not in affiliate_template:
        return base_url or affiliate_template
    if not base_url:
        return affiliate_template
    return affiliate_template.replace("{url}", quote(base_url, safe=""))
