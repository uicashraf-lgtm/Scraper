from urllib.parse import quote


def build_affiliate_link(base_url: str, affiliate_template: str | None) -> str:
    if not affiliate_template:
        return base_url
    return affiliate_template.replace("{url}", quote(base_url, safe=""))
