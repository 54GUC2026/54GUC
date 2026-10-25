"""Main application window for the NGUI Front."""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from arch4.interfaces import LUC_VERSION

from ..architecture_contract_manifest import (
    ACTIVE_OPERATOR_PAGES,
)
from ..front_register import NGUIFrontRegister, PageTruthSnapshot
from ..edit_register import NGUIEditRegister
from ..interactions import ControlFamily, IntentKind, NGUIInteractionRegister
from ..page_owners import require_exact_page_truth
from ..types import PageId
from .charts import AtomicChartView
from .chrome import OperationalFooter, OperationalHeader, OperationalNavigation
from .confirm_button import NGUIConfirmationCoordinator
from .dashboard_page import DashboardPageRenderer
from .developer_pages import DeveloperControlPage
from .graphs_page import GraphsPageShell
from .intelligence_page import IntelligencePage
from .referee_page import RefereePage
from .interactions import bind_timer, publish_intent
from .pages import DevicePageShell
from .style import STYLESHEET, install_bundled_fonts


VISIBLE_PAGE_LABELS = {
    PageId.DEVICE: "DEVICE",
    PageId.GRAPHS: "GRAPHS",
    PageId.DASHBOARD: "DASHBOARD",
    PageId.DEVELOPER_CONTROL: "DEVELOPER CONTROL",
    PageId.REFEREE: "REFEREE",
    PageId.INTELLIGENCE: "BRAIN",
}
if tuple(VISIBLE_PAGE_LABELS) != ACTIVE_OPERATOR_PAGES:
    raise RuntimeError("NGUI visible page label catalog mismatch")

ACCEPTED_CLIENT_WIDTH = 1500
ACCEPTED_CLIENT_HEIGHT = 900
NGUI_WINDOW_TITLE = LUC_VERSION
_CHART_UNRENDERED = object()


def _page_render_meaning(truth: PageTruthSnapshot) -> tuple[object, ...]:
    """Return only state that the page can display."""

    command_identity = (
        (truth.matrix_identity,)
        # Device now submits SIM/network commands and invalidates drafts on a
        # session change. Its command binding is observable even if values match.
        if truth.page in {PageId.DEVICE, PageId.GRAPHS, PageId.DEVELOPER_CONTROL}
        else ()
    )
    # A repeated failure still acknowledges a new Connect attempt. Transport
    # frames alone must not repaint, but this canonical owner transition must.
    login_acknowledgement = (
        (truth.fields["profile.login_lifecycle"].source_sequence,)
        if truth.page is PageId.DEVICE else ()
    )
    return command_identity + login_acknowledgement + tuple(
        (
            field_id,
            field.value,
            field.text,
            field.state,
            field.freshness,
            field.reason,
        )
        for field_id, field in sorted(truth.fields.items())
    )


class StableClientPageStack(QStackedWidget):
    """Keep page-local hints from becoming window state."""

    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)

    def sizeHint(self):  # noqa: N802
        return QSize(0, 0)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, 0)


class NGUIWindow(QMainWindow):
    def __init__(
        self,
        front: NGUIFrontRegister,
        interactions: NGUIInteractionRegister,
        edits: NGUIEditRegister,
        *,
        report_render_error: Callable[[str], None],
    ) -> None:
        super().__init__()
        install_bundled_fonts()
        self._front = front
        self._interactions = interactions
        self._edits = edits
        if not callable(report_render_error):
            raise TypeError("NGUIWindow requires a render-fault reporter")
        self._report_render_error = report_render_error
        self._rendered_sequence = -1
        self._initial_render_complete = False
        self._page_rendered_sequences: dict[PageId, int] = {}
        self._surface_render_meanings: dict[str, tuple[object, ...]] = {}
        self._chart_rendered_sequence = -1
        self._chart_rendered_snapshot: object = _CHART_UNRENDERED
        self._render_faults: dict[str, str] = {}
        self._active_page_dirty = False
        self._exit_published = False
        self._pages: dict[PageId, QWidget] = {}
        self._page_indexes: dict[PageId, int] = {}
        self._header_labels: dict[str, QLabel] = {}
        self._footer_labels: dict[str, QLabel] = {}
        self._charts = AtomicChartView()
        self._confirmation = NGUIConfirmationCoordinator()
        self.setWindowTitle(NGUI_WINDOW_TITLE)
        self.setStyleSheet(STYLESHEET)
        self.setCentralWidget(self._build_root())
        self._footer_bar = OperationalFooter()
        self._footer_labels = self._footer_bar.labels
        self.setStatusBar(self._footer_bar)
        self._resize_to_accepted_client_area()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        bind_timer(self._timer, self.render_latest)
        self._timer.start()
        self.render_latest()

    def _resize_to_accepted_client_area(self) -> None:
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        width = ACCEPTED_CLIENT_WIDTH
        height = ACCEPTED_CLIENT_HEIGHT
        if available is not None:
            width = min(width, available.width())
            height = min(height, available.height())
        self.resize(width, height)

    def _build_root(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self._header_bar = OperationalHeader()
        self._header_labels = self._header_bar.labels
        layout.addWidget(self._header_bar)
        self._navigation_bar = OperationalNavigation(
            self._interactions,
            VISIBLE_PAGE_LABELS,
            self.show_page,
        )
        self._navigation_buttons = self._navigation_bar.navigation_buttons
        layout.addWidget(self._navigation_bar)
        self._stack = StableClientPageStack()
        for page in ACTIVE_OPERATOR_PAGES:
            shell = self._build_page(page)
            self._page_indexes[page] = self._stack.addWidget(shell)
            self._pages[page] = shell
        layout.addWidget(self._stack, 1)
        self._apply_page_chrome(PageId.DEVICE)
        self._navigation_bar.set_active_page(PageId.DEVICE)
        self._device_header.set_active_page(PageId.DEVICE)
        return root

    def _build_page(self, page: PageId) -> QWidget:
        if page is PageId.DEVICE:
            shell = DevicePageShell(
                self._interactions,
                VISIBLE_PAGE_LABELS,
                self.show_page,
                self._confirmation,
            )
            self._device_header = shell.device_header
            return shell
        if page is PageId.GRAPHS:
            return GraphsPageShell(
                self._interactions,
                self._edits,
                self._charts,
                self._confirmation,
            )
        if page is PageId.DASHBOARD:
            return DashboardPageRenderer(self._interactions)
        if page is PageId.DEVELOPER_CONTROL:
            return DeveloperControlPage(
                self._interactions,
                self._edits,
                self._confirmation,
            )
        if page is PageId.REFEREE:
            return RefereePage()
        if page is PageId.INTELLIGENCE:
            return IntelligencePage(self._interactions)
        raise ValueError(f"unsupported NGUI main page: {page.value}")

    def show_page(self, page: PageId) -> None:
        if page not in ACTIVE_OPERATOR_PAGES:
            raise ValueError(f"page is not wired into NGUI: {page.value}")
        self._stack.setCurrentIndex(self._page_indexes[page])
        self._apply_page_chrome(page)
        self._navigation_bar.set_active_page(page)
        self._device_header.set_active_page(page)
        self._active_page_dirty = True
        self.render_latest()

    @property
    def operator_pages(self) -> tuple[PageId, ...]:
        return ACTIVE_OPERATOR_PAGES

    @property
    def device_controls(self):
        """Return Device-page controls for the source UI test driver."""

        return self._pages[PageId.DEVICE].controls

    @property
    def current_page(self) -> PageId:
        return self._active_page()

    def _apply_page_chrome(self, page: PageId) -> None:
        device = page is PageId.DEVICE
        self._device_header.setVisible(device)
        self._header_bar.setVisible(not device)
        self._navigation_bar.setVisible(not device)
        chrome = None if device else getattr(self._pages[page], "navigation_chrome", None)
        self._navigation_bar.set_action_widget(chrome)

    def show_network_scan_result(self, text: str) -> None:
        """Route transient scan text without exposing page ownership to callers."""
        self._pages[PageId.DEVICE].utility_controls.show_scan_result(text)

    def show_connected(self, page: PageId = PageId.GRAPHS) -> None:
        if page in {PageId.ENTRY, PageId.HEADER, PageId.FOOTER}:
            raise ValueError("connected view must be a main page")
        self.show_page(page)

    def render_latest(self) -> None:
        frame = self._front.snapshot()
        if frame.sequence == 0:
            return
        is_new_generation = frame.sequence != self._rendered_sequence
        if not is_new_generation and not self._active_page_dirty:
            return
        render_ok = True
        if is_new_generation:
            for surface, page, renderer in (
                ("header", PageId.HEADER, self._header_bar.render),
                ("footer", PageId.FOOTER, self._footer_bar.render),
                ("device_header", PageId.DEVICE, self._device_header.render),
            ):
                render_ok = self._render_truth_surface(
                    surface,
                    frame.pages.get(page),
                    page,
                    renderer,
                ) and render_ok
        active_page = self._active_page()
        if not self._initial_render_complete:
            pages = tuple(
                page
                for page in self._pages
                if self._page_rendered_sequences.get(page) != frame.sequence
            )
        else:
            pages = (active_page,)
        for page in pages:
            shell = self._pages[page]
            surface = f"page:{page.value}"
            accepted = self._render_truth_surface(
                surface,
                frame.pages.get(page),
                page,
                shell.render,
                force=self._active_page_dirty and page is active_page,
            )
            render_ok = accepted and render_ok
            if accepted:
                self._page_rendered_sequences[page] = frame.sequence
        if (
            self._chart_rendered_sequence < 0
            or active_page is PageId.GRAPHS
        ):
            graphs = self._pages[PageId.GRAPHS]
            render_chart = getattr(graphs, "render_chart", None)
            if not callable(render_chart):
                raise RuntimeError("Graphs page does not expose NGUI chart renderer")
            chart_changed = (
                self._chart_rendered_snapshot is _CHART_UNRENDERED
                or self._chart_rendered_snapshot != frame.chart_snapshot
            )
            if chart_changed or self._active_page_dirty:
                accepted = self._render_surface(
                    "chart:graphs",
                    lambda: render_chart(frame.chart_snapshot),
                )
                render_ok = accepted and render_ok
                if accepted:
                    self._chart_rendered_snapshot = frame.chart_snapshot
                    self._chart_rendered_sequence = frame.sequence
            else:
                self._chart_rendered_sequence = frame.sequence
        self._initial_render_complete = (
            all(page in self._page_rendered_sequences for page in self._pages)
            and self._chart_rendered_sequence >= 0
        )
        active_accepted = self._page_rendered_sequences.get(active_page) == frame.sequence
        chart_accepted = (
            active_page is not PageId.GRAPHS
            or self._chart_rendered_sequence == frame.sequence
        )
        if active_accepted and chart_accepted:
            self._active_page_dirty = False
        if render_ok:
            self._rendered_sequence = frame.sequence

    def _render_truth_surface(
        self,
        surface: str,
        truth: PageTruthSnapshot | None,
        page: PageId,
        renderer: Callable[[PageTruthSnapshot], None],
        *,
        force: bool = False,
    ) -> bool:
        try:
            accepted_truth = require_exact_page_truth(truth, page)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if self._render_faults.get(surface) != detail:
                self._render_faults[surface] = detail
                self._report_render_error(f"{surface}: {detail}")
            return False
        meaning = _page_render_meaning(accepted_truth)
        if (
            not force
            and surface not in self._render_faults
            and self._surface_render_meanings.get(surface) == meaning
        ):
            return True
        accepted = self._render_surface(
            surface,
            lambda: renderer(accepted_truth),
        )
        if accepted:
            self._surface_render_meanings[surface] = meaning
        return accepted

    def _render_surface(self, surface: str, callback: Callable[[], None]) -> bool:
        try:
            callback()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if self._render_faults.get(surface) != detail:
                self._render_faults[surface] = detail
                self._report_render_error(f"{surface}: {detail}")
            return False
        self._render_faults.pop(surface, None)
        return True

    def _active_page(self) -> PageId:
        index = self._stack.currentIndex()
        for page, page_index in self._page_indexes.items():
            if page_index == index:
                return page
        raise RuntimeError(f"NGUI stack index {index} has no page owner")

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if not self._exit_published:
            publish_intent(
                self._interactions,
                IntentKind.EXIT_APPLICATION,
                family=ControlFamily.APPLICATION_EVENT,
            )
            self._exit_published = True
        super().closeEvent(event)

    def request_application_exit(self) -> None:
        """Close after an accepted shutdown without publishing EXIT again."""

        self._exit_published = True
        self._timer.stop()
        self.close()
