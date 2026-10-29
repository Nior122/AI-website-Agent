# Reusable Website Template System

The AI Website Agent uses seven reusable static website templates. Each template shares safe semantic components but has its own visual direction, palette, typography, hero treatment, card styling, and responsive behavior. This helps the agent produce sites that suit their industry instead of reusing one look for every customer.

## The seven templates

| Template ID | Best fit | Visual direction |
| --- | --- | --- |
| `salon-service-v1` | Salons, barbershops, beauty, wellness, fitness, creative services | Editorial luxury: plum, rose, warm parchment and sculptural details |
| `restaurant-menu-v1` | Restaurants, cafes, food businesses, short stays | Botanical hospitality: forest green, ivory and menu-led hierarchy |
| `clinic-healthcare-v1` | Clinics, dental practices, healthcare, pharmacy, wellness | Calm and reassuring: teal, mist and clear care pathways |
| `legal-practice-v1` | Law, accounting, tax, advisory and consulting | Restrained authority: navy, parchment and antique brass |
| `automotive-service-v1` | Garages, mechanics, auto repair, tyres and vehicle care | High-contrast workshop style: charcoal, white and safety orange |
| `retail-ecommerce-v1` | Boutiques, fashion, stores, electronics, grocery and retail | Curated merchandising: warm neutrals, tactile cards and terracotta |
| `personal-portfolio-v1` | Developers, designers, creators, freelancers and digital builders | Dark editorial showcase: ink, soft white and electric lime |

## Architecture

```text
Verified business facts + generated copy + optional brand tokens
                         ↓
                 templates/registry.py
                         ↓
            templates/renderer.py + components.py
                         ↓
 styles.css (shared primitives) + themes.css (art direction)
                         ↓
                  standalone HTML
```

- `registry.py` maps supported categories to a registered template; unsupported categories fail explicitly.
- `renderer.py` checks required fields, scopes brand variables to the selected theme, and renders a standalone HTML document tagged with `body[data-template]`.
- `components.py` provides semantic navigation, hero, about, cards, hours, location, contact and footer sections.
- `styles.css` owns common layout rules. `themes.css` owns the seven visual systems.
- Generated sites embed their CSS. They do not require a JavaScript runtime, CSS CDN, remote font, image service or tracking code.
- Optional approved brand values are scoped to the selected template; one customer's colors cannot leak into another template.
- `demo-data/` contains fictional sample records. `previews/index.html` is the catalogue for local template demonstrations.

## Data and safety boundaries

Business facts, content, optional images, brand tokens and template structure stay separate. Calls to action render only when corresponding contact information is present. Phone uses `tel:`, WhatsApp uses a sanitized `wa.me` link, and social links require valid HTTP(S) URLs. A gallery needs supplied image URLs and meaningful alt text; missing images are omitted, not invented.

For `TEST_FIXTURE` and `demo` records, external actions are replaced by in-page navigation and a visible fictional disclosure. Rendering does not publish sites, deploy to hosting, activate integrations, submit contact forms, or contact businesses.

## Add or refine a template

1. Add a `TemplateDefinition` in `registry.py` with category aliases, required data, sections and accessibility expectations.
2. Reuse the semantic components in `components.py`; only create a component when existing sections cannot express the layout.
3. Add the theme rules in `themes.css` under a `body[data-template="..."]` selector.
4. Add fictional demo data and regression tests for selection, missing optional values, escaped content, disclosures and responsive rules.
5. Regenerate that template's HTML preview with `templates.cli`, refresh `previews/index.html`, and update `CHANGELOG.md` and `TASKS.md`.

## Render a demo locally (PowerShell)

```powershell
py -3 -m templates.cli --data templates/demo-data/demo_salon.json --output templates/previews/salon-service-v1.html --template-id salon-service-v1
py -3 -m templates.cli --data templates/demo-data/demo_restaurant.json --output templates/previews/restaurant-menu-v1.html --template-id restaurant-menu-v1
py -3 -m templates.cli --data templates/demo-data/demo_clinic.json --output templates/previews/clinic-healthcare-v1.html --template-id clinic-healthcare-v1
py -3 -m templates.cli --data templates/demo-data/demo_legal.json --output templates/previews/legal-practice-v1.html --template-id legal-practice-v1
py -3 -m templates.cli --data templates/demo-data/demo_automotive.json --output templates/previews/automotive-service-v1.html --template-id automotive-service-v1
py -3 -m templates.cli --data templates/demo-data/demo_retail.json --output templates/previews/retail-ecommerce-v1.html --template-id retail-ecommerce-v1
py -3 -m templates.cli --data templates/demo-data/demo_portfolio.json --output templates/previews/personal-portfolio-v1.html --template-id personal-portfolio-v1
```

Open `templates/previews/index.html` in a browser to browse the saved examples. These are local design demonstrations, not deployments.
