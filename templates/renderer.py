"""Render structured fictional or approved business data into static HTML."""

from __future__ import annotations

from html import escape
from pathlib import Path

from . import components
from .registry import TemplateDefinition, get_template, select_template


STYLE = (Path(__file__).parent / "styles.css").read_text(encoding="utf-8")


def validate_business_data(data: dict, template: TemplateDefinition) -> list[str]:
    return [f"Missing required field: {field}" for field in template.required_data if not data.get(field)]


def render_site(data: dict, template_id: str | None = None) -> str:
    template = get_template(template_id) if template_id else select_template(data.get("category", ""))
    errors = validate_business_data(data, template)
    if errors:
        raise ValueError("; ".join(errors))
    sections = _sections(data, template)
    brand = _brand_css(data.get("brand", {}))
    description = escape(data.get("description") or f"{data['business_name']} in {data['city']}.")
    title = escape(data["business_name"])
    restaurant = template.template_id == "restaurant-menu-v1"
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title><meta name="description" content="{description}"><meta property="og:title" content="{title}"><meta property="og:description" content="{description}"><style>{STYLE}{brand}</style></head><body>{components.navigation(data["business_name"], sections["nav"], restaurant=restaurant, fictional=components.is_fictional(data))}<main>{sections["html"]}</main>{components.footer(data["business_name"], fictional=components.is_fictional(data))}</body></html>'


def _sections(data: dict, template: TemplateDefinition) -> dict:
    parts = [("about", components.about(data))]
    if template.template_id == "salon-service-v1":
        parts.append(("services", components.card_list("services", "Services", data.get("services", []))))
    else:
        menu_items = data.get("menu_items")
        if menu_items:
            parts.append(("menu", components.card_list("menu", "Signature dishes", menu_items, price=True, fictional=components.is_fictional(data))))
        elif data.get("services"):
            parts.append(("offerings", components.card_list("offerings", "Offerings", data.get("services", []), price=False, fictional=components.is_fictional(data))))
    parts.extend([("gallery", components.gallery(data.get("images", []))), ("location", components.location(data)), ("hours", components.hours(data)), ("contact", components.contact(data))])
    rendered = [(section_id, html) for section_id, html in parts if html]
    return {"nav": [section_id for section_id, _ in rendered], "html": components.hero(data) + "".join(html for _, html in rendered)}


def _brand_css(brand: dict) -> str:
    allowed = {"primary_color": "--primary", "secondary_color": "--secondary", "accent_color": "--accent", "background_color": "--background", "text_color": "--text", "border_radius": "--radius", "spacing": "--space"}
    values = []
    for key, variable in allowed.items():
        value = brand.get(key)
        if isinstance(value, str) and all(character not in value for character in "{};<>"):
            values.append(f"{variable}:{value}")
    return f':root{{{";".join(values)}}}' if values else ""
