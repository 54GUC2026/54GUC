"""54GUC NGUI package."""

from .registry import NGUI_FIELD_REGISTRY, field_spec, fields_for_page
from .types import FieldKind, FieldSpec, PageId, PresentationPolicy, SourceOwner

__all__ = [
    "FieldKind",
    "FieldSpec",
    "NGUI_FIELD_REGISTRY",
    "PageId",
    "PresentationPolicy",
    "SourceOwner",
    "field_spec",
    "fields_for_page",
]
