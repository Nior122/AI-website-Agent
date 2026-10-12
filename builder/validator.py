"""Compatibility adapter from the Builder to the Phase 6 QA validator."""

from validation.quality_validator import validate_rendered_html


def validate_html(html: str, business_name: str, real_build: bool, **kwargs) -> dict:
    result = validate_rendered_html(html, business_name, real_build=real_build, **kwargs)
    data = result.to_dict()
    data["passed"] = result.status == "PASS"
    return data
