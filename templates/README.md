# Reusable Website Templates

This is a provider-neutral, dependency-free static template system. A template renders ordinary structured data; future AI may propose content or brand values but is not required to render a website.

## Initial templates

- `salon-service-v1` supports beauty salons, barbershops, fitness, and creative services.
- `restaurant-menu-v1` supports restaurants, food businesses, and short stays.

Two templates are intentional: they validate both service-led and menu/location-led layouts without prematurely creating fifteen near-duplicates. Add a category only when its information architecture genuinely differs.

## Architecture

```text
Business data + content data + brand config
                 ↓
          template registry selection
                 ↓
      semantic shared components + static CSS
                 ↓
             local static HTML
```

- `registry.py` is the metadata catalogue and deterministic category selector.
- `renderer.py` validates required fields and assembles a self-contained HTML document.
- `components.py` holds reusable semantic sections and optional CTA logic.
- `styles.css` is lightweight shared CSS embedded into output, with brand CSS variables.
- `demo-data/` holds clearly fictional, non-production examples.

## Data boundaries

Business data (`business_name`, city, contacts, services/menu, address) is separate from template structure. Optional `content` provides business-specific wording; optional `brand` supplies presentation variables. Image entries require a provided URL and meaningful alt text; absent images omit the gallery.

CTA links only render when their corresponding data exists. WhatsApp uses a sanitized `wa.me` link, phone uses `tel:`, and social links require HTTP(S) URLs. For clearly labeled `TEST_FIXTURE`/`demo` data, the renderer deliberately replaces all externally actionable contact/map/social targets with in-page navigation and a visible fictional disclosure. No contact form, tracking code, image download, or deployment behavior exists.

## Add a template

1. Add one `TemplateDefinition` to `registry.py`, including data requirements, sections, mobile behavior, and accessibility expectations.
2. Add a deliberate rendering branch in `renderer._sections` only if existing shared sections cannot express the layout.
3. Reuse/add semantic components in `components.py`; do not put business data in them.
4. Add fictional demo data and tests for required fields, absent optional values, long content, and the intended category mapping.
5. Update this document, `ARCHITECTURE.md`, `TASKS.md`, and `CHANGELOG.md`.

## Local demo

```powershell
python -m templates.cli --data templates/demo-data/demo_salon.json --output data/leads/demo-salon.html
```

The output is local and ignored by Git. It is not a deployment.
