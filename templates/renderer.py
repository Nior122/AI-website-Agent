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
    tid = template.template_id
    if tid == "salon-service-v1":
        parts.append(("services", components.card_list("services", "Services", data.get("services", []))))
    elif tid == "restaurant-menu-v1":
        menu_items = data.get("menu_items")
        if menu_items:
            parts.append(("menu", components.card_list("menu", "Signature dishes", menu_items, price=True, fictional=components.is_fictional(data))))
        elif data.get("services"):
            parts.append(("offerings", components.card_list("offerings", "Offerings", data.get("services", []), price=False, fictional=components.is_fictional(data))))
    elif tid == "clinic-healthcare-v1":
        if data.get("services"):
            parts.append(("services", components.card_list("services", "Clinical Specialties & Care", data.get("services", []))))
        if data.get("practitioners"):
            parts.append(("practitioners", components.card_list("practitioners", "Specialist Practitioners", data.get("practitioners", []))))
    elif tid == "legal-practice-v1":
        areas = data.get("practice_areas") or data.get("services", [])
        if areas:
            parts.append(("practice_areas", components.card_list("practice_areas", "Practice Areas", areas)))
        if data.get("attorneys"):
            parts.append(("attorneys", components.card_list("attorneys", "Legal Practitioners", data.get("attorneys", []))))
    elif tid == "automotive-service-v1":
        if data.get("services"):
            parts.append(("services", components.card_list("services", "Workshop & Repair Services", data.get("services", []))))
        if data.get("guarantee"):
            parts.append(("guarantee", components.card_list("guarantee", "Service Guarantee", data.get("guarantee", []))))
    elif tid == "retail-ecommerce-v1":
        products = data.get("products") or data.get("services", [])
        if products:
            parts.append(("products", components.card_list("products", "Featured Products", products, price=True, fictional=components.is_fictional(data))))
        if data.get("services") and data.get("products"):
            parts.append(("services", components.card_list("services", "Our Services", data.get("services", []))))
    elif tid == "personal-portfolio-v1":
        projects = data.get("projects", [])
        if projects:
            parts.append(("projects", components.card_list("projects", "Selected Projects", projects, price=False, fictional=components.is_fictional(data))))
        skills = data.get("skills", [])
        if skills:
            parts.append(("skills", components.card_list("skills", "Core Capabilities & Stack", skills, price=False, fictional=components.is_fictional(data))))
        services = data.get("services", [])
        if services:
            parts.append(("services", components.card_list("services", "Services & Specializations", services, price=False, fictional=components.is_fictional(data))))
    else:
        if data.get("services"):
            parts.append(("services", components.card_list("services", "Services", data.get("services", []))))

    parts.extend([
        ("gallery", components.gallery(data.get("images", []))),
        ("location", components.location(data)),
        ("hours", components.hours(data)),
        ("contact", components.contact(data)),
    ])
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
