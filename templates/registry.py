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
        ("menu_items", "services", "description", "opening_hours", "phone", "email", "whatsapp", "instagram_url", "address", "images"),
        ("navigation", "hero", "about", "menu", "offerings", "gallery", "location", "hours", "contact", "footer"),
        "Single-column content with responsive menu cards and touch-friendly primary actions.",
        ("semantic landmarks", "visible focus", "sufficient contrast", "meaningful image alt text"),
    ),
    TemplateDefinition(
        "clinic-healthcare-v1", "Clinic & Healthcare", ("clinic", "healthcare", "medical_practice", "dental_clinic", "optometry", "wellness_center", "pharmacy"),
        "Consultation and care-focused layout for clinics, healthcare practices, and wellness centres.",
        ("business_name", "category", "city"),
        ("services", "description", "opening_hours", "phone", "whatsapp", "address", "practitioners", "images"),
        ("navigation", "hero", "about", "services", "practitioners", "hours", "location", "contact", "footer"),
        "Single-column content with accessible service cards, clear consultation booking touch targets, and responsive layout.",
        ("semantic landmarks", "visible focus", "sufficient contrast", "meaningful image alt text"),
    ),
    TemplateDefinition(
        "legal-practice-v1", "Legal & Advisory Practice", ("legal", "law_firm", "solicitor", "barrister", "advocate", "accounting", "tax_advisory", "consulting_firm"),
        "Authority and trust-led layout for law firms, legal practitioners, and corporate advisory services.",
        ("business_name", "category", "city"),
        ("services", "practice_areas", "description", "opening_hours", "phone", "email", "address", "attorneys", "images"),
        ("navigation", "hero", "about", "practice_areas", "attorneys", "hours", "location", "contact", "footer"),
        "Single-column content with structured practice area cards, clear consultation booking touch targets, and accessible layout.",
        ("semantic landmarks", "visible focus", "sufficient contrast", "meaningful image alt text"),
    ),
    TemplateDefinition(
        "automotive-service-v1", "Automotive & Mobility Service", ("automotive", "auto_repair", "mechanic", "car_care", "tire_service", "auto_body", "vehicle_service"),
        "Fast-action and reliability-focused layout for auto repair workshops, mechanic garages, and vehicle care centers.",
        ("business_name", "category", "city"),
        ("services", "description", "opening_hours", "phone", "whatsapp", "address", "emergency_assistance", "images"),
        ("navigation", "hero", "about", "services", "guarantee", "hours", "location", "contact", "footer"),
        "Single-column content with touch-accessible breakdown call action, service cards, and workshop location guidance.",
        ("semantic landmarks", "visible focus", "sufficient contrast", "meaningful image alt text"),
    ),
    TemplateDefinition(
        "retail-ecommerce-v1", "Retail & E-commerce", ("retail", "shop", "store", "boutique", "fashion", "clothing", "electronics", "supermarket", "grocery", "market"),
        "Product showcase and shopping-focused layout for retail stores, boutiques, and e-commerce businesses.",
        ("business_name", "category", "city"),
        ("services", "products", "description", "opening_hours", "phone", "whatsapp", "email", "address", "images"),
        ("navigation", "hero", "about", "products", "services", "gallery", "hours", "location", "contact", "footer"),
        "Single-column content with product showcase cards, call-to-action emphasis, and responsive grid layout.",
        ("semantic landmarks", "visible focus", "sufficient contrast", "meaningful image alt text"),
    ),
    TemplateDefinition(
        "personal-portfolio-v1", "Personal Portfolio", ("portfolio", "personal_portfolio", "developer", "software_engineer", "web_developer", "designer", "creative_technologist", "digital_builder", "creator", "freelancer"),
        "Showcase-led personal portfolio layout with profile introduction, projects, skills, services, and verified contact links.",
        ("business_name", "category", "city"),
        ("services", "projects", "skills", "description", "opening_hours", "phone", "email", "whatsapp", "github_url", "linkedin_url", "twitter_url", "images"),
        ("navigation", "hero", "about", "projects", "skills", "services", "contact", "footer"),
        "Single-column content with responsive project showcase cards, skill chips, and contact actions.",
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
