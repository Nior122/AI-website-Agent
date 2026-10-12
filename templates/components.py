"""Small semantic HTML components shared by all templates."""

from __future__ import annotations

from html import escape
import re
from urllib.parse import quote, urlparse


def text(value: object) -> str:
    return escape(str(value)) if value is not None else ""


def is_fictional(data: dict) -> bool:
    """Keep fixture demonstrations visually complete without actionable contacts."""
    return bool(data.get("demo")) or data.get("data_classification") == "TEST_FIXTURE"


def safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"https", "http", "tel"} and parsed.netloc or parsed.scheme == "tel" else None


def action_links(data: dict) -> str:
    if is_fictional(data):
        primary_target = "#menu" if data.get("category") in {"restaurant", "food_business"} else "#services"
        primary_label = "Explore the menu" if primary_target == "#menu" else "Explore services"
        secondary_target = "#location" if data.get("address") else "#about"
        secondary_label = "Visit the test location" if secondary_target == "#location" else "Read the test story"
        return f'<a class="button button-primary" href="{primary_target}">{primary_label}</a><a class="button button-secondary" href="{secondary_target}">{secondary_label}</a>'
    links = []
    labels = data.get("content", {}).get("cta", {})
    phone = data.get("phone")
    if phone:
        links.append(f'<a class="button button-secondary" href="tel:{text(phone)}">{text(labels.get("phone") or "Call us")}</a>')
    whatsapp_digits = re.sub(r"\D", "", str(data.get("whatsapp") or ""))
    if whatsapp_digits:
        links.append(f'<a class="button button-primary" href="https://wa.me/{whatsapp_digits}">{text(labels.get("whatsapp") or "Message on WhatsApp")}</a>')
    instagram = safe_url(data.get("instagram_url"))
    if instagram:
        links.append(f'<a class="text-link" href="{text(instagram)}">Instagram</a>')
    facebook = safe_url(data.get("facebook_url"))
    if facebook:
        links.append(f'<a class="text-link" href="{text(facebook)}">Facebook</a>')
    return "".join(links)


def navigation(name: str, section_ids: list[str], restaurant: bool = False, fictional: bool = True) -> str:
    links = "".join(f'<a href="#{text(section)}">{text(section.title())}</a>' for section in section_ids)
    if restaurant:
        mark_text = "LF" if fictional else "".join(w[0].upper() for w in name.split() if w)[:2] or "PL"
        mark = f'<span class="brand-mark" aria-hidden="true">{text(mark_text)}</span>'
    else:
        mark = ""
    return f'<header class="site-header"><a class="brand" href="#top">{mark}<span>{text(name)}</span></a><nav aria-label="Main navigation">{links}</nav></header>'


def _restaurant_illustration() -> str:
    return '''<div class="hero-art" aria-label="Decorative illustration of a shared meal" role="img">
      <svg viewBox="0 0 520 520" aria-hidden="true" focusable="false">
        <circle class="art-sun" cx="382" cy="116" r="78"/>
        <path class="art-leaf" d="M76 102c83 2 119 38 121 107C114 209 78 173 76 102Z"/>
        <path class="art-leaf art-leaf-alt" d="M424 367c-89 5-134-32-141-103 88-6 133 29 141 103Z"/>
        <path class="art-platter" d="M83 337c41-101 109-151 201-151s160 50 201 151c-57 72-124 108-201 108S140 409 83 337Z"/>
        <ellipse class="art-bowl" cx="284" cy="302" rx="128" ry="59"/>
        <path class="art-rice" d="M183 292c16-62 55-93 102-93s86 31 103 93c-25 24-60 36-104 36-43 0-78-12-101-36Z"/>
        <circle class="art-garnish" cx="224" cy="259" r="17"/><circle class="art-garnish" cx="321" cy="247" r="14"/><circle class="art-garnish" cx="349" cy="285" r="13"/>
        <path class="art-steam" d="M244 176c-19-25 20-40 2-70M288 171c-19-25 20-40 2-70M331 178c-19-25 20-40 2-70"/>
      </svg>
      <span class="art-caption">Made for the table</span>
    </div>'''


def hero(data: dict) -> str:
    title = data.get("content", {}).get("hero_title") or data["business_name"]
    subtitle = data.get("content", {}).get("hero_subtitle") or data.get("description") or f"Local {data['category'].replace('_', ' ')} in {data['city']}."
    if data.get("category") in {"restaurant", "food_business"}:
        eyebrow = "Lagos table · fictional test kitchen" if is_fictional(data) else f"{text(data.get('city', 'Lagos'))} · uncommissioned preview draft"
        hero_art = _restaurant_illustration() if is_fictional(data) else ""
        art_class = " has-art" if hero_art else " no-art"
        return f'<section class="hero hero-restaurant{art_class}" id="top"><div class="hero-copy"><p class="eyebrow">{eyebrow}</p><h1>{text(title)}</h1><p class="hero-intro">{text(subtitle)}</p><div class="actions">{action_links(data)}</div></div>{hero_art}</section>'
    return f'<section class="hero" id="top"><p class="eyebrow">{text(data["city"])}</p><h1>{text(title)}</h1><p>{text(subtitle)}</p><div class="actions">{action_links(data)}</div></section>'


def about(data: dict) -> str:
    description = data.get("description") or data.get("content", {}).get("about_text")
    title = data.get("content", {}).get("about_title") or "About us"
    if not description:
        return ""
    if data.get("category") in {"restaurant", "food_business"}:
        return f'<section id="about" class="story-section"><div><p class="section-kicker">The idea</p><h2>{text(title)}</h2></div><p class="story-copy">{text(description)}</p></section>'
    return f'<section id="about"><h2>{text(title)}</h2><p>{text(description)}</p></section>'


def card_list(section_id: str, title: str, entries: list[dict], price: bool = False, fictional: bool = False) -> str:
    if not entries:
        return ""
    cards = []
    for index, entry in enumerate(entries, start=1):
        heading = text(entry.get("name"))
        detail = text(entry.get("description"))
        amount = f'<p class="price">{text(entry.get("price"))}</p>' if price and entry.get("price") and not fictional else ""
        number = f'<span class="menu-index" aria-hidden="true">0{index}</span>' if section_id in {"menu", "offerings", "services"} else ""
        cards.append(f'<article class="card">{number}<div><h3>{heading}</h3><p>{detail}</p>{amount}</div></article>')
    if section_id == "menu" and fictional:
        heading = f'<div class="section-heading"><div><p class="section-kicker">From the test kitchen</p><h2>{text(title)}</h2></div><p class="section-note">Fictional dishes for layout testing. Availability and prices are not represented.</p></div>'
    elif section_id in {"menu", "offerings", "services"}:
        kicker = "Signature dishes" if section_id == "menu" else "What we offer"
        heading = f'<div class="section-heading"><div><p class="section-kicker">{kicker}</p><h2>{text(title)}</h2></div></div>'
    else:
        heading = f'<h2>{text(title)}</h2>'
    class_name = "menu-section" if section_id in {"menu", "offerings"} else ""
    return f'<section id="{text(section_id)}" class="{class_name}">{heading}<div class="card-grid">{"".join(cards)}</div></section>'


def gallery(images: list[dict]) -> str:
    valid = [image for image in images if safe_url(image.get("url")) and image.get("alt")]
    if not valid:
        return ""
    items = "".join(f'<img src="{text(image["url"])}" alt="{text(image["alt"])}" loading="lazy">' for image in valid)
    return f'<section id="gallery"><h2>Gallery</h2><div class="gallery">{items}</div></section>'


def hours(data: dict) -> str:
    if not data.get("opening_hours"):
        return ""
    note = '<p class="demo-note">Fictional test schedule — not a real operating promise.</p>' if is_fictional(data) else ""
    return f'<section id="hours" class="hours-panel"><p class="section-kicker">When to gather</p><h2>Opening hours</h2><p class="hours-value">{text(data["opening_hours"])}</p>{note}</section>'


def location(data: dict) -> str:
    address = data.get("address")
    if not address:
        return ""
    if is_fictional(data):
        return f'<section id="location" class="location-panel"><p class="section-kicker">A fictional setting</p><h2>Find the test kitchen</h2><p>{text(address)}</p><p class="demo-note">This address is fictional. No map or directions link is provided.</p></section>'
    map_url = f"https://www.google.com/maps/search/?api=1&query={quote(str(address))}"
    return f'<section id="location"><h2>Find us</h2><p>{text(address)}</p><a class="text-link" href="{text(map_url)}">Open map</a></section>'


def contact(data: dict) -> str:
    if is_fictional(data):
        return '<section id="contact" class="contact-panel"><p class="section-kicker">Demonstration only</p><h2>A considered journey, safely contained.</h2><p>This fictional concept intentionally has no live phone, WhatsApp, social, booking, or message destination.</p></section>'
    actions = action_links(data)
    email = data.get("email")
    email_html = f'<p class="contact-detail">Email: {text(email)}</p>' if email and "@" in str(email) else ""
    if not actions and not email_html:
        return ""
    return f'<section id="contact"><h2>Get in touch</h2><div class="actions">{actions}</div>{email_html}</section>'


def footer(name: str, fictional: bool = False) -> str:
    disclosure = '<p class="fictional-disclosure">Fictional demonstration website · Created for AI Website Agent testing.</p>' if fictional else f'<p class="draft-disclosure">Uncommissioned preview draft prepared for internal evaluation · Not an authorized or official website of {text(name)}.</p>'
    return f'<footer><a class="brand footer-brand" href="#top">{text(name)}</a>{disclosure}</footer>'
