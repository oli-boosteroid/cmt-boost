import logging
import re
from typing import Literal, Tuple

logger = logging.getLogger(__name__)


_EMOJI_RE = re.compile(
    '['
    '\U0001F300-\U0001F9FF'  # misc symbols, emoticons, transport, etc.
    '\U00002600-\U000027BF'  # misc symbols
    '\U0001FA00-\U0001FA6F'  # chess, symbols
    '\U0001FA70-\U0001FAFF'  # symbols and pictographs extended-A
    '\U00002702-\U000027B0'  # dingbats
    ']+',
    flags=re.UNICODE,
)


def html_to_pdf(html_content: str) -> bytes | None:
    """Convert an HTML string to PDF bytes using weasyprint. Returns None on failure."""
    import logging
    logging.getLogger('weasyprint').setLevel(logging.ERROR)
    logging.getLogger('fonttools').setLevel(logging.ERROR)
    try:
        from weasyprint import HTML as WeasyHTML
        clean_html = _EMOJI_RE.sub('', html_content)
        return WeasyHTML(string=clean_html).write_pdf()
    except Exception as e:
        logger.error(f'[PDF] Failed to convert HTML to PDF: {e}')
        return None

# Characters to strip when normalising game titles
_TRADEMARK_CHARS = str.maketrans('', '', '™®©℠')


def clean_title(name: str) -> str:
    """
    Strips trademark / copyright symbols (™ ® © ℠) and normalises
    surrounding whitespace.  Used when storing and querying game titles so
    that 'Game Name™' and 'Game Name' are treated as the same title.
    """
    return ' '.join(name.translate(_TRADEMARK_CHARS).split())


def extract_link(text: str) -> str:
    """
    Extracts the first URL from a string.
    """
    link_regex = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    try:
        return re.findall(link_regex, text)[0]
    except IndexError:
        return ""


def extract_steam_app_id(url: str) -> str:
    """
    Extracts the Steam app ID from a URL.
    Examples:
        https://store.steampowered.com/app/3241660/REPO/ -> 3241660
        https://store.steampowered.com/app/3241660/ -> 3241660
        https://store.steampowered.com/app/3241660 -> 3241660
    """
    pattern = r"https?://store\.steampowered\.com/(?:agecheck/)?app/(\d+)"
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    return ""


def extract_epic_game_slug(url: str) -> str:
    """
    Extracts the Epic Games store slug from a URL.
    Examples:
        https://store.epicgames.com/en-US/p/game-slug -> game-slug
        https://store.epicgames.com/p/game-slug -> game-slug
        https://www.epicgames.com/store/en-US/product/game-slug/home -> game-slug
    """
    patterns = [
        r"https?://store\.epicgames\.com/[^/]*/p/([^/?#\s]+)",
        r"https?://store\.epicgames\.com/p/([^/?#\s]+)",
        r"https?://(?:www\.)?epicgames\.com/store/[^/]*/product/([^/?#\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def strip_epic_slug_suffix(slug: str) -> str:
    """
    Strips the trailing 6-char hex offer-ID that Epic appends to some slugs.
    e.g. '007-first-light-182cea' -> '007-first-light'
    """
    return re.sub(r'-[0-9a-f]{6}$', '', slug)


def extract_xbox_product_id(url: str) -> Tuple[str, str]:
    """
    Extracts the Xbox/Microsoft Store product ID and slug from a URL.
    Supported formats:
        https://www.xbox.com/uk-UA/games/store/metro-exodus-gold-edition/9NP2B6V2KPXD
            -> ("9NP2B6V2KPXD", "metro-exodus-gold-edition")
        https://www.microsoft.com/store/productId/9NMPDV49K4TX
            -> ("9NMPDV49K4TX", "")
        https://www.microsoft.com/en-us/p/game-name/9NMPDV49K4TX
            -> ("9NMPDV49K4TX", "game-name")
    Returns ("", "") if not matched.
    """
    # xbox.com: /locale/games/store/slug/PRODUCT_ID
    xbox_pattern = r"https?://(?:www\.)?xbox\.com/[^/]+/games/store/([^/?#\s]+)/([A-Za-z0-9]+)"
    match = re.search(xbox_pattern, url)
    if match:
        return match.group(2), match.group(1)  # product_id, slug

    # microsoft.com/store/productId/PRODUCT_ID
    ms_product_pattern = r"https?://(?:www\.)?microsoft\.com(?:/[^/]+)?/store/productId/([A-Za-z0-9]+)"
    match = re.search(ms_product_pattern, url)
    if match:
        return match.group(1), ""  # product_id, no slug

    # microsoft.com/locale/p/game-slug/PRODUCT_ID  or  /store/apps/PRODUCT_ID/...
    ms_p_pattern = r"https?://(?:www\.)?microsoft\.com(?:/[^/]+)?/p/([^/?#\s]+)/([A-Za-z0-9]{10,20})"
    match = re.search(ms_p_pattern, url)
    if match:
        return match.group(2), match.group(1)  # product_id, slug

    return "", ""


def detect_store(url: str) -> Literal["steam", "epic", "xbox", ""]:
    """
    Detects which game store a URL belongs to.
    Returns "steam", "epic", "xbox", or "" if unknown.
    """
    if "store.steampowered.com" in url:
        return "steam"
    if "epicgames.com" in url:
        return "epic"
    if "xbox.com" in url or "microsoft.com/store" in url or "microsoft.com/p/" in url:
        return "xbox"
    return ""
