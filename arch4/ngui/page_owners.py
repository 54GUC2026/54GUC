"""Declare which presentation fields belong to each page."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)

from .front_register import PageTruthSnapshot, PresentationValue
from .registry import fields_for_page
from .types import PageId


def require_exact_page_truth(
    truth: PageTruthSnapshot | None,
    expected_page: PageId,
) -> PageTruthSnapshot:
    """Validate a complete page snapshot before rendering it."""

    if truth is None:
        raise ValueError(f"{expected_page.value} PageTruthSnapshot is missing")
    if truth.page is not expected_page:
        raise ValueError(
            f"{expected_page.value} renderer received {truth.page.value} page truth"
        )
    expected = {spec.field_id for spec in fields_for_page(expected_page)}
    actual = set(truth.fields)
    if actual != expected:
        raise ValueError(
            f"{expected_page.value} PageTruthSnapshot contract mismatch: "
            f"missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    for field_id, value in truth.fields.items():
        if value.field_id != field_id:
            raise ValueError(
                f"{expected_page.value} PageTruthSnapshot key/value mismatch: "
                f"{field_id} != {value.field_id}"
            )
    return truth


def build_page_truth(
    page: PageId,
    fields: Mapping[str, PresentationValue],
    *,
    matrix_identity: MatrixGenerationIdentity | None = None,
    previous: PageTruthSnapshot | None = None,
) -> PageTruthSnapshot:
    selected: dict[str, PresentationValue] = {}
    for spec in fields_for_page(page):
        try:
            value = fields[spec.field_id]
        except KeyError as exc:
            raise RuntimeError(f"page {page.value} missing presentation field {spec.field_id}") from exc
        if value.field_id != spec.field_id:
            raise RuntimeError(f"page {page.value} received mismatched field {value.field_id}")
        selected[spec.field_id] = value
    if (
        previous is not None
        and previous.page is page
        and previous.matrix_identity == matrix_identity
        and set(previous.fields) == set(selected)
        and all(
            previous.fields[field_id] is value
            for field_id, value in selected.items()
        )
    ):
        return previous
    return PageTruthSnapshot(
        page=page,
        fields=MappingProxyType(selected),
        matrix_identity=matrix_identity,
    )


def build_all_page_truth(
    fields: Mapping[str, PresentationValue],
    *,
    matrix_identity: MatrixGenerationIdentity | None = None,
    previous_pages: Mapping[PageId, PageTruthSnapshot] | None = None,
) -> dict[PageId, PageTruthSnapshot]:
    prior = previous_pages or {}
    return {
        page: build_page_truth(
            page,
            fields,
            matrix_identity=matrix_identity,
            previous=prior.get(page),
        )
        for page in PageId
    }
