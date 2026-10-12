"""Dependency-free local website validation and quality assurance."""

from .quality_validator import ValidationResult, validate_rendered_html

__all__ = ["ValidationResult", "validate_rendered_html"]
