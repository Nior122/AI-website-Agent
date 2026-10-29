# Reusable Website Template System

The AI Website Agent contains seven industry-specific, image-led website templates. Each generated website now contains **five separate HTML pages** with file-to-file navigation: Home, About, Services/Menu, Gallery, and Contact. The generated page bundle is reviewed as a unit before the builder writes it to disk.

## Template collection

| Template ID | Best fit | Art direction |
| --- | --- | --- |
| `salon-service-v1` | Salons, barbershops, beauty, wellness | Editorial luxury: plum, rose, warm parchment and full-bleed beauty photography |
| `restaurant-menu-v1` | Restaurants, cafes, food businesses | Botanical hospitality: forest green, ivory, menu-led hierarchy and food imagery |
| `clinic-healthcare-v1` | Clinics, dental, healthcare and wellness | Calm and reassuring: teal, mist, clear care pathways and clinical imagery |
| `legal-practice-v1` | Law, accounting, tax, advisory and consulting | Restrained authority: navy, parchment, brass and considered editorial layouts |
| `automotive-service-v1` | Garages, mechanics, auto repair and vehicle care | Technical contrast: charcoal, white, workshop photography and clear service cards |
| `retail-ecommerce-v1` | Boutiques, fashion, stores and specialty retail | Warm merchandising: neutral surfaces, terracotta and image-led collections |
| `personal-portfolio-v1` | Developers, designers, creators and digital builders | Dark editorial: ink, soft white, electric lime and project photography |

## Generated site structure

Every built site writes the following files to the same output directory:

- `index.html` — image-led home page with a distinct hero, key facts, introduction, featured cards, and next steps.
- `about.html` — dedicated background/story page.
- `services.html` — offerings, menu, practice areas, products, or portfolio work, depending on the selected template.
- `gallery.html` — responsive image gallery with meaningful alternative text.
- `contact.html` — contact details, location, hours, and allowed social/contact links.
- `build.json` — the existing build record written by the builder.

Every page has the same brand treatment and five-link navigation. Links are real relative URLs, not section anchors. The builder checks the complete set for the five required filenames, relative-link targets, semantic main landmarks, viewport metadata, and images with alt text before writing a successful bundle.

## Architecture

```text
Verified business facts + approved assets + optional brand tokens
                         ↓
                 templates/registry.py
                         ↓
            templates/multipage.py + components.py
                         ↓
          templates/multipage.css (seven art directions)
                         ↓
   index.html · about.html · services.html · gallery.html · contact.html
                         ↓
             bundle validation → local build output
```

- `registry.py` maps supported categories to a registered template; unknown categories fail explicitly.
- `multipage.py` renders the five-page site bundle. The existing `renderer.render_site()` remains available for backwards compatibility.
- `multipage.css` supplies shared responsive components and seven scoped brand palettes. The generated documents embed the CSS so they can be opened locally without a stylesheet runtime.
- `components.py` remains the source for safe contact action links and fictional-data handling.
- `builder/website_builder.py` writes every page only after validation passes; it does not publish or deploy the site.
- `previews/index.html` is the image-led catalogue. `previews/multipage/<template-id>/` contains five directly browsable demo pages for each template.

## Images and data boundaries

Approved/source-backed images supplied in the business data take priority. Where there are not enough supplied images, the renderer uses category-matched Unsplash-hosted stock photographs as design placeholders. Those images are explicitly disclosed as illustrative and should be replaced by approved business photography before a real site goes live. Remote stock image URLs require an internet connection in the preview browser.

All preview brands and business details are fictional. Fixture contact links are inert; no page sends forms, messages, bookings, or outreach. Real-business builds continue to use verified business facts and their existing safety gates. Do not invent credentials, team biographies, testimonials, awards, operating hours, or business claims.

## Render a five-page demo locally (PowerShell)

```powershell
py -3 -m templates.cli --data templates/demo-data/demo_salon.json --site-output output/demo-salon --template-id salon-service-v1
```

This writes the five HTML pages to `output/demo-salon/`. To render the legacy single-document preview, keep using `--output <path-to-file>`. Rendering is local only; it does not deploy sites or activate integrations.

## Add or refine a template

1. Register the category mapping and metadata in `templates/registry.py`.
2. Add the industry photo set and page copy/layout in `templates/multipage.py`.
3. Refine the template's scoped art direction in `templates/multipage.css`.
4. Add fictional fixture data and regression tests for the bundle, links, images, disclosure, and contact safety.
5. Regenerate static previews in `templates/previews/multipage/`; update this README, `CHANGELOG.md`, and `TASKS.md`.
