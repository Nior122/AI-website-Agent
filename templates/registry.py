"""Template metadata and deterministic category selection."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateDefinition:
    template_id: str
    name: str
    categories: tuple[str, ...]
    description: str
    required_data: tuple[str, ...]
    optional_data: tuple[str, ...]
    sections: tuple[str, ...]
    mobile_behavior: str
    accessibility_expectations: tuple[str, ...]


TEMPLATES = (
    TemplateDefinition(
        "salon-service-v1", "Service Business", ("beauty_salon", "barbershop", "fitness", "creative_services", "professional_service", "professional_services"),
        "Service-led local-business layout with booking/contact emphasis.",
        ("business_name", "category", "city"),
        ("services", "description", "opening_hours", "phone", "whatsapp", "instagram_url", "images"),
        ("navigation", "hero", "about", "services", "gallery", "hours", "contact", "footer"),
        "Single-column content with responsive service cards and a collapsible-safe navigation layout.",
        ("semantic landmarks", "visible focus", "sufficient contrast", "meaningful image alt text"),
    ),
    TemplateDefinition(
        "restaurant-menu-v1", "Restaurant Menu", ("restaurant", "food_business", "hotel_short_stay"),
        "Menu and location-led layout for food-focused local businesses.",
        ("business_name", "category", "city"),
        ("menu_items", "description", "opening_hours", "phone", "whatsapp", "address", "images"),
        ("navigation", "hero", "about", "menu", "gallery", "location", "hours", "contact", "footer"),
        "Single-column content with responsive menu cards and touch-friendly primary actions.",
        ("semantic landmarks", "visible focus", "sufficient contrast", "meaningful image alt text"),
    ),
)


def get_template(template_id: str) -> TemplateDefinition:
    for template in TEMPLATES:
        if template.template_id == template_id:
            return template
    raise ValueError(f"Unknown template: {template_id}")


def select_template(category: str) -> TemplateDefinition:
    normalized = category.strip().casefold().replace(" ", "_")
    for template in TEMPLATES:
        if normalized in template.categories:
            return template
    raise ValueError(f"No implemented template supports category: {category}")
