"""Five-page website rendering with industry photography and shared navigation."""

from __future__ import annotations

from html import escape
from pathlib import Path
import re
from urllib.parse import quote, urlparse

from .components import action_links, is_fictional, safe_url
from .registry import get_template, select_template
from .renderer import _brand_css

CSS = (Path(__file__).parent / "multipage.css").read_text(encoding="utf-8")

# Unsplash-hosted stock photos are layout assets, not claims about the business.
# Demo galleries and real-business drafts disclose when stock photography is used.
STOCK_IMAGES = {
    "salon-service-v1": [
        ("photo-1560066984-138dadb4c035", "Modern salon styling stations"),
        ("photo-1522337360788-8b13dee7a37e", "Beauty and skincare products"),
        ("photo-1521590832167-7bcbfaa6381f", "Professional salon interior"),
        ("photo-1562322140-8baeececf3df", "Hair styling and beauty care"),
        ("photo-1516975080664-ed2fc6a32937", "Beauty treatment details"),
        ("photo-1512496015851-a90fb38ba796", "Beauty and personal care products"),
    ],
    "restaurant-menu-v1": [
        ("photo-1414235077428-338989a2e8c0", "Restaurant dining and food presentation"),
        ("photo-1517248135467-4c7edcad34c4", "Contemporary restaurant interior"),
        ("photo-1559339352-11d035aa65de", "Restaurant table setting"),
        ("photo-1555396273-367ea4eb4db5", "Dining room and hospitality"),
        ("photo-1547592180-85f173990554", "Freshly prepared food"),
        ("photo-1544148103-0773bf10d330", "Restaurant food and ingredients"),
    ],
    "clinic-healthcare-v1": [
        ("photo-1629909613654-28e377c37b09", "Modern healthcare consultation room"),
        ("photo-1576091160399-112ba8d25d1d", "Healthcare professional at work"),
        ("photo-1584982751601-97dcc096659c", "Clinical care setting"),
        ("photo-1579684385127-1ef15d508118", "Medical consultation"),
        ("photo-1559757148-5c350d0d3c56", "Healthcare team workspace"),
        ("photo-1584515933487-779824d29309", "Patient care and clinical setting"),
    ],
    "legal-practice-v1": [
        ("photo-1589829545856-d10d557cf95f", "Legal desk and law books"),
        ("photo-1505664194779-8beaceb93744", "Legal library and research"),
        ("photo-1450101499163-c8848c66ca85", "Professional documents and legal work"),
        ("photo-1521791136064-7986c2920216", "Professional consultation"),
        ("photo-1450101499163-c8848c66ca85", "Documents prepared for review"),
        ("photo-1507679799987-c73779587ccf", "Professional meeting"),
    ],
    "automotive-service-v1": [
        ("photo-1486262715619-67b85e0b08d3", "Vehicle maintenance workshop"),
        ("photo-1492144534655-ae79c964c9d7", "Modern vehicle detail"),
        ("photo-1486262715619-67b85e0b08d3", "Automotive inspection setting"),
        ("photo-1549317661-bd32c8ce0db2", "Car service and maintenance"),
        ("photo-1487754180451-c456f719a1fc", "Automotive tools and equipment"),
        ("photo-1503376780353-7e6692767b70", "Automobile exterior detail"),
    ],
    "retail-ecommerce-v1": [
        ("photo-1621261027519-a71ac66d5a68", "Minimal boutique clothing display"),
        ("photo-1483985988355-763728e1935b", "Fashion shopping and styling"),
        ("photo-1551232864-3f0890e580d9", "Contemporary fashion collection"),
        ("photo-1441986300917-64674bd600d8", "Modern retail interior"),
        ("photo-1490481651871-ab68de25d43d", "Fashion and clothing details"),
        ("photo-1529139574466-a303027c1d8b", "Editorial fashion styling"),
    ],
    "personal-portfolio-v1": [
        ("photo-1498050108023-c5249f4df085", "Developer workspace with laptop"),
        ("photo-1519389950473-47ba0277781c", "Creative team collaborating"),
        ("photo-1523726491678-bf852e717f6a", "Design studio and digital tools"),
        ("photo-1558655146-d09347e92766", "Creative project materials"),
        ("photo-1516321318423-f06f85e504b3", "Product design and computer work"),
        ("photo-1552664730-d307ca884978", "Creative planning session"),
    ],
}
PHOTO_NOTE = {
    "demo": "Stock photography shown for this fictional design demonstration.",
    "business": "Illustrative stock photography. Replace with approved business photography before publication.",
}
PAGE_LABELS = {
    "salon-service-v1": ("Our Story", "Services", "Gallery", "Contact"),
    "restaurant-menu-v1": ("Our Story", "Menu", "Gallery", "Visit Us"),
    "clinic-healthcare-v1": ("About the Clinic", "Care & Services", "Facilities", "Contact"),
    "legal-practice-v1": ("Our Practice", "Practice Areas", "Insights & Spaces", "Contact"),
    "automotive-service-v1": ("About the Workshop", "Services", "Workshop Gallery", "Contact"),
    "retail-ecommerce-v1": ("Our Story", "Collections", "Lookbook", "Visit Us"),
    "personal-portfolio-v1": ("About", "Selected Work", "Portfolio", "Contact"),
}
PAGE_HERO = {
    "about.html": ("A closer look", "The story and context behind the work."),
    "services.html": ("Made for your needs", "A considered overview of the offerings and information provided."),
    "gallery.html": ("A visual introduction", "Browse imagery selected for this website concept."),
    "contact.html": ("Start a conversation", "Find the available contact details and practical information."),
}


def render_site_pages(data: dict, template_id: str | None = None) -> dict[str, str]:
    """Return a complete five-page site bundle, keyed by safe relative file names."""
    template = get_template(template_id) if template_id else select_template(data.get("category", ""))
    missing = [field for field in template.required_data if not data.get(field)]
    if missing:
        raise ValueError("; ".join(f"Missing required field: {field}" for field in missing))

    photos = _photos(data, template.template_id)
    page_builders = {
        "index.html": ("home", "Home", _home(data, template.template_id, photos)),
        "about.html": ("about", PAGE_LABELS[template.template_id][0], _about(data, template.template_id, photos)),
        "services.html": ("services", PAGE_LABELS[template.template_id][1], _services(data, template.template_id, photos)),
        "gallery.html": ("gallery", PAGE_LABELS[template.template_id][2], _gallery(data, template.template_id, photos)),
        "contact.html": ("contact", PAGE_LABELS[template.template_id][3], _contact(data, template.template_id, photos)),
    }
    return {
        filename: _document(data, template.template_id, filename, page_id, nav_label, content, photos)
        for filename, (page_id, nav_label, content) in page_builders.items()
    }


def _photos(data: dict, template_id: str) -> list[dict]:
    supplied = []
    for item in data.get("images", []) if isinstance(data.get("images"), list) else []:
        if not isinstance(item, dict):
            continue
        url, alt = item.get("url"), item.get("alt")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed and parsed.scheme in {"https", "http"} and parsed.netloc and isinstance(alt, str) and alt.strip():
            supplied.append({"url": url, "alt": alt.strip(), "supplied": True})
    stock = [
        {
            "url": f"https://images.unsplash.com/{photo_id}?auto=format&fit=crop&w=1400&q=82",
            "alt": alt,
            "supplied": False,
        }
        for photo_id, alt in STOCK_IMAGES[template_id]
    ]
    # Approved/source-backed assets take priority. Stock images fill empty spaces and provide a usable design.
    ordered = supplied + stock
    return ordered[:8]


def _e(value: object) -> str:
    return escape(str(value)) if value is not None else ""


def _category_label(template_id: str) -> str:
    return {
        "salon-service-v1": "Beauty & wellness",
        "restaurant-menu-v1": "Food & hospitality",
        "clinic-healthcare-v1": "Healthcare",
        "legal-practice-v1": "Legal & advisory",
        "automotive-service-v1": "Automotive",
        "retail-ecommerce-v1": "Retail & fashion",
        "personal-portfolio-v1": "Portfolio",
    }[template_id]


def _entries(data: dict, template_id: str) -> list[dict]:
    if template_id == "restaurant-menu-v1" and data.get("menu_items"):
        raw = data["menu_items"]
    elif template_id == "retail-ecommerce-v1" and data.get("products"):
        raw = data["products"]
    elif template_id == "legal-practice-v1" and data.get("practice_areas"):
        raw = data["practice_areas"]
    elif template_id == "personal-portfolio-v1" and data.get("projects"):
        raw = data["projects"]
    else:
        raw = data.get("services", [])
    result = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            name, description = item.get("name"), item.get("description", "")
            price = item.get("price")
        else:
            name, description, price = item, "", None
        if name:
            result.append({"name": str(name), "description": str(description or ""), "price": price})
    return result


def _service_title(template_id: str) -> str:
    return {
        "salon-service-v1": "Services & treatments",
        "restaurant-menu-v1": "The menu",
        "clinic-healthcare-v1": "Care & services",
        "legal-practice-v1": "Practice areas",
        "automotive-service-v1": "Workshop services",
        "retail-ecommerce-v1": "Featured collection",
        "personal-portfolio-v1": "Selected work",
    }[template_id]


def _image(item: dict, class_name: str, loading: str = "lazy") -> str:
    fetch = ' fetchpriority="high"' if loading == "eager" else ""
    return f'<img class="{_e(class_name)}" src="{_e(item["url"])}" alt="{_e(item["alt"])}" loading="{loading}"{fetch} decoding="async">'


def _image_note(photos: list[dict], data: dict) -> str:
    if all(photo["supplied"] for photo in photos):
        return ""
    note = PHOTO_NOTE["demo"] if is_fictional(data) else PHOTO_NOTE["business"]
    return f'<p class="image-note">{_e(note)}</p>'


def _home(data: dict, template_id: str, photos: list[dict]) -> str:
    name = _e(data["business_name"])
    city = _e(data["city"])
    title = _e(data.get("content", {}).get("hero_title") or data["business_name"])
    description = _e(data.get("description") or data.get("content", {}).get("hero_subtitle") or f"{data['category'].replace('_', ' ')} in {data['city']}.")
    entries = _entries(data, template_id)
    cards = _entry_cards(entries, photos, template_id, data, limit=6, home=True)
    intro = f'<section class="section story-preview"><div><p class="eyebrow">A considered approach</p><h2>Meet {name}</h2></div><div class="story-copy"><p>{_e(data.get("description") or description)}</p><a class="text-link" href="about.html">Discover our story <span aria-hidden="true">↗</span></a></div><figure class="story-image">{_image(photos[1], "cover-image")}<figcaption>{_e(photos[1]["alt"])}</figcaption></figure></section>'
    offerings = ""
    if cards:
        offerings = f'<section class="section"><div class="section-heading"><div><p class="eyebrow">{_e(_category_label(template_id))}</p><h2>{_e(_service_title(template_id))}</h2></div><a class="text-link" href="services.html">Explore all <span aria-hidden="true">↗</span></a></div><div class="card-grid">{cards}</div></section>'
    else:
        offerings = '<section class="section"><div class="section-heading"><div><p class="eyebrow">What to expect</p><h2>Information, clearly presented.</h2></div></div><p>Verified details can be added to this section before publication.</p></section>'
    image_strip = f'<section class="photo-band"><div class="photo-band-image">{_image(photos[2], "cover-image")}</div><div class="photo-band-copy"><p class="eyebrow">The details matter</p><h2>Thoughtful design. Clear information.</h2><p>Explore the pages to learn more, review the available details, and find the right next step.</p><a class="button button-light" href="contact.html">Contact {name}</a></div></section>'
    facts = _facts(data, template_id)
    note = _image_note(photos, data)
    actions = "" if is_fictional(data) else f'<div class="verified-actions">{action_links(data)}</div>'
    contact_preview = f'<section id="contact" class="section home-contact-preview"><div><p class="eyebrow">Contact & practical details</p><h2>Make the next step clear.</h2><p>Find the available phone, address, email, and social links in one place.</p>{actions}</div><div class="home-contact-facts">{_contact_facts(data)}</div></section>'
    return f'<section class="home-hero"><div class="hero-copy"><p class="eyebrow">{_e(_category_label(template_id))} <span aria-hidden="true">/</span> {city}</p><h1>{title}</h1><p class="hero-lede">{description}</p><div class="hero-actions"><a class="button button-primary" href="services.html">{_e(_home_cta(template_id))}</a><a class="button button-outline" href="contact.html">Get in touch <span aria-hidden="true">↗</span></a></div><p class="hero-location">{city}{(" · " + _e(data["opening_hours"])) if data.get("opening_hours") else ""}</p></div><figure class="hero-visual">{_image(photos[0], "hero-image", "eager")}<figcaption>{_e(photos[0]["alt"])}</figcaption><span class="hero-stamp" aria-hidden="true">{_e(_stamp(template_id))}</span></figure></section>{facts}{intro}{offerings}{image_strip}<section class="section close-panel"><p class="eyebrow">Next step</p><h2>Find the information you need.</h2><p>Review the details available for {name} and choose the most relevant way to connect.</p><a class="button button-primary" href="contact.html">Contact & location <span aria-hidden="true">↗</span></a></section>{contact_preview}{note}'


def _home_cta(template_id: str) -> str:
    return {
        "salon-service-v1": "Explore treatments",
        "restaurant-menu-v1": "View the menu",
        "clinic-healthcare-v1": "Explore care",
        "legal-practice-v1": "View practice areas",
        "automotive-service-v1": "Explore services",
        "retail-ecommerce-v1": "Explore collections",
        "personal-portfolio-v1": "View selected work",
    }[template_id]


def _stamp(template_id: str) -> str:
    return {
        "salon-service-v1": "CARE\\n& CRAFT",
        "restaurant-menu-v1": "AT THE\\nTABLE",
        "clinic-healthcare-v1": "CARE\\nFIRST",
        "legal-practice-v1": "CLARITY\\n& COUNSEL",
        "automotive-service-v1": "READY\\nTO MOVE",
        "retail-ecommerce-v1": "THE\\nCOLLECTION",
        "personal-portfolio-v1": "SELECTED\\nWORK",
    }[template_id]


def _facts(data: dict, template_id: str) -> str:
    items = []
    if data.get("city"):
        items.append(f'<div><span class="fact-label">Based in</span><strong>{_e(data["city"])}</strong></div>')
    if data.get("opening_hours"):
        items.append(f'<div><span class="fact-label">Hours</span><strong>{_e(data["opening_hours"])}</strong></div>')
    if template_id == "personal-portfolio-v1" and data.get("skills"):
        skills = [str(item.get("name") if isinstance(item, dict) else item) for item in data["skills"][:3]]
        items.append(f'<div><span class="fact-label">Focus</span><strong>{_e(" · ".join(skills))}</strong></div>')
    if data.get("address"):
        items.append(f'<div><span class="fact-label">Address</span><strong>{_e(data["address"])}</strong></div>')
    return f'<section class="fact-strip" aria-label="Key details">{"".join(items)}</section>' if items else ""




def _contact_facts(data: dict) -> str:
    rows = []
    if data.get("address"):
        rows.append(f'<p><span>Address</span><strong>{_e(data["address"])}</strong></p>')
    if data.get("phone"):
        rows.append(f'<p><span>Telephone</span><strong>{_e(data["phone"])}</strong></p>')
    if data.get("email"):
        rows.append(f'<p><span>Email</span><strong>{_e(data["email"])}</strong></p>')
    if data.get("opening_hours"):
        rows.append(f'<p><span>Hours</span><strong>{_e(data["opening_hours"])}</strong></p>')
    return "".join(rows)


def _about(data: dict, template_id: str, photos: list[dict]) -> str:
    title, intro = PAGE_HERO["about.html"]
    description = data.get("description") or data.get("content", {}).get("about_text") or "Background and business information can be added here once verified."
    name = _e(data["business_name"])
    picture = photos[1]
    return f'<section class="page-hero"><div class="page-hero-copy"><p class="eyebrow">{_e(_category_label(template_id))}</p><h1>{_e(title)}: {name}</h1><p class="hero-lede">{_e(intro)}</p><p class="page-summary">{_e(description)}</p><a class="button button-primary" href="contact.html">Contact us <span aria-hidden="true">↗</span></a></div><figure class="page-hero-image">{_image(picture, "cover-image", "eager")}<figcaption>{_e(picture["alt"])}</figcaption></figure></section><section class="section story-detail"><div><p class="eyebrow">The background</p><h2>What matters to this work.</h2></div><div><p>{_e(description)}</p><p>Use this page to provide the verified context, approach, and details that help visitors understand the business. Only confirmed information should be published.</p></div></section><section class="section three-up"><article><span class="step-number">01</span><h2>Clarity</h2><p>Keep the essential information easy to find.</p></article><article><span class="step-number">02</span><h2>Care</h2><p>Present offerings with useful, specific descriptions.</p></article><article><span class="step-number">03</span><h2>Connection</h2><p>Make the next step straightforward and accessible.</p></article></section>{_image_note(photos, data)}'


def _services(data: dict, template_id: str, photos: list[dict]) -> str:
    title = _service_title(template_id)
    entries = _entries(data, template_id)
    if not entries:
        cards = '<p class="empty-state">Verified offering details have not been provided for this section yet.</p>'
    else:
        cards = _entry_cards(entries, photos, template_id, data, limit=len(entries), home=False)
    intro = {
        "salon-service-v1": "A clear look at available treatments and service options.",
        "restaurant-menu-v1": "Explore the menu information provided for this restaurant concept.",
        "clinic-healthcare-v1": "A helpful overview of the available care and service information.",
        "legal-practice-v1": "Review the areas of practice included in the supplied information.",
        "automotive-service-v1": "Review the service categories provided for this workshop concept.",
        "retail-ecommerce-v1": "Explore products and collections represented in the supplied catalogue.",
        "personal-portfolio-v1": "Selected work and examples represented in the supplied portfolio information.",
    }[template_id]
    return f'<section class="page-hero page-hero-compact"><div class="page-hero-copy"><p class="eyebrow">{_e(_category_label(template_id))}</p><h1>{_e(title)}</h1><p class="hero-lede">{_e(intro)}</p><p class="page-summary">Descriptions and prices appear only when supplied in the source data. Confirm all details before publication.</p></div><figure class="page-hero-image">{_image(photos[2], "cover-image", "eager")}<figcaption>{_e(photos[2]["alt"])}</figcaption></figure></section><section class="section"><div class="section-heading"><div><p class="eyebrow">Explore the details</p><h2>{_e(title)}</h2></div></div><div class="card-grid card-grid-large">{cards}</div></section><section class="section close-panel"><p class="eyebrow">Need more information?</p><h2>We’re here to help.</h2><p>Use the contact page to find the verified contact and location details available.</p><a class="button button-primary" href="contact.html">Contact & practical details <span aria-hidden="true">↗</span></a></section>{_image_note(photos, data)}'


def _entry_cards(entries: list[dict], photos: list[dict], template_id: str, data: dict, limit: int, home: bool) -> str:
    cards = []
    for idx, entry in enumerate(entries[:limit]):
        photo = photos[(idx + 1) % len(photos)]
        price = f'<p class="entry-price">{_e(entry["price"])}</p>' if entry.get("price") and not is_fictional(data) else ""
        cards.append(f'<article class="entry-card"><div class="entry-image">{_image(photo, "cover-image")}</div><div class="entry-content"><span class="entry-index">{idx + 1:02d}</span><h3>{_e(entry["name"])}</h3><p>{_e(entry["description"])}</p>{price}</div></article>')
    return "".join(cards)


def _gallery(data: dict, template_id: str, photos: list[dict]) -> str:
    cards = []
    for i, photo in enumerate(photos[:6]):
        label = photo["alt"]
        if photo["supplied"]:
            caption = label
        else:
            caption = f"Illustrative image {i + 1} · {label}"
        cards.append(f'<figure class="gallery-card">{_image(photo, "cover-image")}<figcaption><span>{_e(caption)}</span><span class="gallery-number">{i + 1:02d}</span></figcaption></figure>')
    title, intro = PAGE_HERO["gallery.html"]
    return f'<section class="page-hero page-hero-compact"><div class="page-hero-copy"><p class="eyebrow">{_e(_category_label(template_id))}</p><h1>{_e(title)}</h1><p class="hero-lede">{_e(intro)}</p><p class="page-summary">Approved business images appear first when provided. Any stock images are clearly presented as illustrative references.</p></div><figure class="page-hero-image">{_image(photos[3], "cover-image", "eager")}<figcaption>{_e(photos[3]["alt"])}</figcaption></figure></section><section class="section"><div class="gallery-grid">{"".join(cards)}</div></section>{_image_note(photos, data)}'


def _contact(data: dict, template_id: str, photos: list[dict]) -> str:
    title, intro = PAGE_HERO["contact.html"]
    fictional = is_fictional(data)
    details = []
    if data.get("address"):
        details.append(f'<div class="contact-row"><span class="contact-label">Address</span><p>{_e(data["address"])}</p></div>')
    if data.get("city"):
        details.append(f'<div class="contact-row"><span class="contact-label">City</span><p>{_e(data["city"])}</p></div>')
    if data.get("opening_hours"):
        details.append(f'<div class="contact-row"><span class="contact-label">Opening hours</span><p>{_e(data["opening_hours"])}</p></div>')
    if data.get("phone"):
        number = re.sub(r"[^+0-9 ()-]", "", str(data["phone"]))
        line = _e(data["phone"])
        if not fictional:
            line = f'<a href="tel:{_e(number)}">{line}</a>'
        details.append(f'<div class="contact-row"><span class="contact-label">Telephone</span><p>{line}</p></div>')
    if data.get("email"):
        details.append(f'<div class="contact-row"><span class="contact-label">Email</span><p>{_e(data["email"])}</p></div>')
    if data.get("whatsapp") and not fictional:
        digits = re.sub(r"\D", "", str(data["whatsapp"]))
        if digits:
            details.append(f'<div class="contact-row"><span class="contact-label">WhatsApp</span><p><a href="https://wa.me/{digits}">Message on WhatsApp ↗</a></p></div>')
    socials = []
    if not fictional:
        for label, key in (("Instagram", "instagram_url"), ("Facebook", "facebook_url"), ("GitHub", "github_url"), ("LinkedIn", "linkedin_url"), ("X / Twitter", "twitter_url")):
            url = safe_url(data.get(key))
            if url and url.startswith(("https://", "http://")):
                socials.append(f'<a href="{_e(url)}" rel="noopener noreferrer">{_e(label)} ↗</a>')
    map_link = ""
    if data.get("address") and not fictional:
        map_url = "https://www.google.com/maps/search/?api=1&query=" + quote(str(data["address"]))
        map_link = f'<a class="text-link" href="{_e(map_url)}" rel="noopener noreferrer">Open map directions ↗</a>'
    fiction = f'<p class="demo-note">{_e("Fictional demonstration only. Contact details and location are not actionable.")}</p>' if fictional else ""
    contact_details = "".join(details) if details else "<p>Verified contact details have not been supplied yet.</p>"
    social_html = '<div class="social-links">' + "".join(socials) + "</div>" if socials else ""
    return f'<section class="page-hero page-hero-compact"><div class="page-hero-copy"><p class="eyebrow">{_e(_category_label(template_id))}</p><h1>{_e(title)}</h1><p class="hero-lede">{_e(intro)}</p><p class="page-summary">The details below reflect information provided for this website draft.</p></div><figure class="page-hero-image">{_image(photos[4], "cover-image", "eager")}<figcaption>{_e(photos[4]["alt"])}</figcaption></figure></section><section class="section contact-layout"><div class="contact-intro"><p class="eyebrow">Contact & practical details</p><h2>How to find us.</h2><p>Use the contact information shown here to choose the next step that best fits your needs.</p>{map_link}{fiction}</div><div class="contact-card">{contact_details}{social_html}</div></section>{_image_note(photos, data)}'


def _document(data: dict, template_id: str, filename: str, page_id: str, nav_label: str, content: str, photos: list[dict]) -> str:
    labels = PAGE_LABELS[template_id]
    nav_items = [
        ("index.html", "Home", "home"),
        ("about.html", labels[0], "about"),
        ("services.html", labels[1], "services"),
        ("gallery.html", labels[2], "gallery"),
        ("contact.html", labels[3], "contact"),
    ]
    nav_links = []
    for path, label, current in nav_items:
        aria = ' aria-current="page"' if current == page_id else ""
        nav_links.append(f'<a href="{path}"{aria}>{_e(label)}</a>')
    nav = "".join(nav_links)
    name = _e(data["business_name"])
    title = name if page_id == "home" else f"{_e(nav_label)} | {name}"
    description = _e(data.get("description") or f"{data.get('category', 'business').replace('_', ' ')} in {data.get('city', '')}.")
    disclosure = ""
    if is_fictional(data):
        disclosure = '<p class="site-disclosure">Fictional demonstration website · Created for AI Website Agent testing.</p>'
    else:
        disclosure = f'<p class="site-disclosure">Website draft for internal evaluation · Not an authorized or official website of {name}.</p>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="description" content="{description}"><meta property="og:title" content="{_e(title)}">'
        f'<meta property="og:description" content="{description}"><title>{_e(title)}</title>'
        f'<style>{CSS}{_brand_css(data.get("brand", {}), template_id)}</style></head>'
        f'<body data-template="{_e(template_id)}" data-page="{_e(page_id)}">'
        '<a class="skip-link" href="#main-content">Skip to content</a>'
        f'<div class="utility-bar"><span>{_e(_category_label(template_id))}</span><span>{_e(data.get("city", ""))}</span></div>'
        f'<header class="site-header"><a class="brand" href="index.html"><span class="brand-mark" aria-hidden="true">{_e("".join(word[0] for word in re.findall(r"[A-Za-z0-9]+", str(data["business_name"]))[:2]).upper() or "B")}</span><span>{name}</span></a><nav aria-label="Main navigation">{nav}</nav></header>'
        f'<main id="main-content">{content}</main>'
        f'<footer class="site-footer"><div><a class="brand footer-brand" href="index.html">{name}</a><p>{_e(data.get("city", ""))} · {_e(_category_label(template_id))}</p></div><nav class="footer-nav" aria-label="Footer navigation"><a href="about.html">{_e(labels[0])}</a><a href="services.html">{_e(labels[1])}</a><a href="gallery.html">{_e(labels[2])}</a><a href="contact.html">{_e(labels[3])}</a></nav><div class="footer-disclosure">{disclosure}</div></footer>'
        '</body></html>'
    )
