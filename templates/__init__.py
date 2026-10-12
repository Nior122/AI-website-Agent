"""Dependency-free, data-driven website template foundation."""

from .registry import get_template, select_template
from .renderer import render_site, validate_business_data

__all__ = ["get_template", "select_template", "render_site", "validate_business_data"]
