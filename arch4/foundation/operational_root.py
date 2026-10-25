"""The only Foundation composition owner for pre-Connect desktop services."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from arch4.foundation.operational_service_contracts import OperationalServicePorts
from arch4.foundation.operational_services import (
    DiscoveryService,
    ExternalActionService,
    LicenseService,
    OperatorConfigService,
    ReportService,
    SupportService,
)
from arch4.foundation.site_report_job import SiteReportContext
from arch4.core_contracts.site_report_history import (
    SiteReportHistoryProjection,
)
from arch4.support_bundle_context import SupportBundleContext


FOUNDATION_OPERATIONAL_OWNER = "54guc.foundation.operational_root"


class FoundationOperationalRoot:
    """Construct each desktop/pre-Connect Foundation owner exactly once."""

    def __init__(
        self,
        *,
        app_root: str | Path,
        storage_root: str | Path,
        support_context: SupportBundleContext,
        current_version: str,
        site_report_history_projector: (
            Callable[[float], SiteReportHistoryProjection] | None
        ) = None,
    ) -> None:
        if type(support_context) is not SupportBundleContext:
            raise TypeError("foundation_operational_support_context_required")
        application = Path(app_root).resolve()
        storage = Path(storage_root).resolve()
        if support_context.app_root != application:
            raise RuntimeError("foundation_operational_app_root_mismatch")
        if support_context.runtime_dir != storage:
            raise RuntimeError("foundation_operational_storage_root_mismatch")
        storage.mkdir(parents=True, exist_ok=True)
        self.app_root = application
        self.storage_root = storage
        self.ports = OperationalServicePorts(
            report=ReportService(
                SiteReportContext.from_support_context(support_context),
                history_projector=site_report_history_projector,
            ),
            support=SupportService(support_context),
            license=LicenseService(storage),
            operator_config=OperatorConfigService(storage),
            discovery=DiscoveryService(),
            external=ExternalActionService(
                application,
                current_version=str(current_version or ""),
            ),
        )


__all__ = (
    "FOUNDATION_OPERATIONAL_OWNER",
    "FoundationOperationalRoot",
)
