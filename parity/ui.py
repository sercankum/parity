"""PySide6 window: scan, review what to bring back, export the restore kit."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt, QThread, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QTableView, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import exporter
from .demo import demo_result
from .models import App, ScanResult
from .scanner import scan

FILTERS = ["All programs", "Included", "Left out", "Reinstalls automatically", "Needs manual install",
           "Runtimes, drivers and updates"]
COLUMNS = ["Include", "Name", "Version", "Publisher", "Type", "Reinstall with"]


def announce(widget: QWidget, text: str) -> None:
    """Tell screen readers about status changes (Qt 6.8+; older Qt just updates the label)."""
    try:
        from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(widget, text))
    except Exception:
        pass


def _checked(value) -> bool:
    return int(getattr(value, "value", value)) == Qt.CheckState.Checked.value


class AppModel(QAbstractTableModel):
    selection_changed = Signal()

    def __init__(self):
        super().__init__()
        self.apps: list[App] = []

    def set_apps(self, apps: list[App]) -> None:
        self.beginResetModel()
        self.apps = apps
        self.endResetModel()
        self.selection_changed.emit()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.apps)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        a, col = self.apps[index.row()], index.column()
        if col == 0:
            if role == Qt.ItemDataRole.CheckStateRole:
                return Qt.CheckState.Checked if a.selected else Qt.CheckState.Unchecked
            if role in (Qt.ItemDataRole.AccessibleTextRole, Qt.ItemDataRole.ToolTipRole):
                return f"{'Included' if a.selected else 'Left out'}: {a.name}"
            if role == Qt.ItemDataRole.UserRole:      # sort key
                return int(a.selected)
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.UserRole):
            return ["", a.name, a.version, a.publisher, a.category, a.restore_label][col]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return f | Qt.ItemFlag.ItemIsUserCheckable if index.column() == 0 else f

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if index.column() != 0 or role != Qt.ItemDataRole.CheckStateRole:
            return False
        self.set_selected([index.row()], _checked(value))
        return True

    def set_selected(self, rows: list[int], on: bool) -> None:
        for r in rows:
            self.apps[r].selected = on
        if rows:
            self.dataChanged.emit(self.index(min(rows), 0), self.index(max(rows), len(COLUMNS) - 1))
            self.selection_changed.emit()


class AppFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text, self.mode = "", FILTERS[0]
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def update(self, text: str | None = None, mode: str | None = None) -> None:
        if text is not None:
            self.text = text.casefold().strip()
        if mode is not None:
            self.mode = mode
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, row, parent):
        a: App = self.sourceModel().apps[row]
        if self.text and self.text not in f"{a.name} {a.publisher} {a.pkg_id}".casefold():
            return False
        return {
            "Included": a.selected,
            "Left out": not a.selected,
            "Reinstalls automatically": bool(a.pkg_id),
            "Needs manual install": not a.pkg_id,
            "Runtimes, drivers and updates": a.category in ("Runtime", "Driver", "Update"),
        }.get(self.mode, True)

    def source_rows(self, proxy_indexes) -> list[int]:
        return sorted({self.mapToSource(i).row() for i in proxy_indexes})


class AppTable(QTableView):
    """Space toggles Include for every selected row, so keyboard users never need the mouse."""
    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Space and self.model() and self.selectionModel().selectedRows():
            proxy: AppFilter = self.model()
            src: AppModel = proxy.sourceModel()
            rows = proxy.source_rows(self.selectionModel().selectedRows())
            on = not src.apps[rows[0]].selected
            src.set_selected(rows, on)
            announce(self, f"{len(rows)} {'included' if on else 'left out'}" if len(rows) > 1
                     else f"{src.apps[rows[0]].name} {'included' if on else 'left out'}")
            return
        super().keyPressEvent(e)


class ScanWorker(QObject):
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, demo: bool):
        super().__init__()
        self.demo = demo

    @Slot()
    def run(self):
        try:
            self.done.emit(demo_result() if self.demo else scan(self.progress.emit))
        except Exception as e:     # anything from the OS tools ends up in the status line, not a crash
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.demo, self.result, self._thread = demo, None, None
        self.setWindowTitle("Parity" + (" (demo data)" if demo else ""))
        self.resize(1120, 740)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        title = QLabel("Parity")
        f = title.font()
        f.setPointSizeF(f.pointSizeF() * 1.8)
        f.setBold(True)
        title.setFont(f)
        title.setAccessibleName("Parity")
        sub = QLabel("Snapshot this PC's programs and hardware, then rebuild it after a reinstall.")
        sub.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(sub)

        bar = QHBoxLayout()
        self.scan_btn = QPushButton("&Scan this PC")
        self.scan_btn.setToolTip("Scan installed programs and hardware (F5)")
        self.export_btn = QPushButton("&Export restore kit…")
        self.export_btn.setToolTip("Save the report and restore script to a folder (Ctrl+E)")
        self.export_btn.setEnabled(False)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(140)
        self.busy.setAccessibleName("Scanning")
        self.busy.hide()
        self.status = QLabel("Not scanned yet. Press Scan this PC to start.")
        self.status.setAccessibleName("Status")
        self.status.setWordWrap(True)
        for w in (self.scan_btn, self.export_btn, self.busy):
            bar.addWidget(w)
        bar.addWidget(self.status, 1)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._software_tab(), "S&oftware")
        self.tabs.addTab(self._hardware_tab(), "&Hardware")
        root.addWidget(self.tabs, 1)

        self.scan_btn.clicked.connect(self.start_scan)
        self.export_btn.clicked.connect(self.export)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, self.start_scan)
        QShortcut(QKeySequence("Ctrl+E"), self, lambda: self.export_btn.isEnabled() and self.export())
        QShortcut(QKeySequence.StandardKey.Find, self, self._focus_search)

    # ------------------------------------------------------------ layout
    def _software_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        find_lbl = QLabel("&Find:")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Name, publisher or package")
        self.search.setClearButtonEnabled(True)
        find_lbl.setBuddy(self.search)
        show_lbl = QLabel("Sho&w:")
        self.filter_box = QComboBox()
        self.filter_box.addItems(FILTERS)
        show_lbl.setBuddy(self.filter_box)
        self.include_btn = QPushButton("&Include shown")
        self.exclude_btn = QPushButton("&Leave out shown")
        for x in (find_lbl, self.search, show_lbl, self.filter_box, self.include_btn, self.exclude_btn):
            row.addWidget(x)
        row.setStretch(1, 1)
        v.addLayout(row)

        self.model, self.proxy = AppModel(), AppFilter()
        self.proxy.setSourceModel(self.model)
        self.table = AppTable()
        self.table.setModel(self.proxy)
        self.table.setAccessibleName("Programs")
        self.table.setAccessibleDescription("Space includes or leaves out the selected programs.")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setTabKeyNavigation(False)     # Tab leaves the table instead of walking every cell
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 70), (2, 120), (3, 190), (4, 90), (5, 260)):
            self.table.setColumnWidth(col, width)
        v.addWidget(self.table, 1)

        self.counts = QLabel()
        self.counts.setAccessibleName("Totals")
        hint = QLabel("Space includes or leaves out the selected rows. Runtimes, drivers and updates start out "
                      "left out, because the system and driver installers bring them back.")
        hint.setWordWrap(True)
        v.addWidget(self.counts)
        v.addWidget(hint)

        self.search.textChanged.connect(lambda t: self.proxy.update(text=t))
        self.filter_box.currentTextChanged.connect(lambda m: self.proxy.update(mode=m))
        self.include_btn.clicked.connect(lambda: self._set_shown(True))
        self.exclude_btn.clicked.connect(lambda: self._set_shown(False))
        self.model.selection_changed.connect(self._update_counts)
        return w

    def _hardware_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.disk_note = QLabel()
        self.disk_note.setObjectName("callout")
        self.disk_note.setWordWrap(True)
        self.disk_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard | Qt.TextInteractionFlag.TextSelectableByMouse)
        self.disk_note.setStyleSheet("#callout{border:2px solid palette(highlight);border-radius:6px;padding:10px;}")
        self.disk_note.hide()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Component", "Details"])
        self.tree.setAccessibleName("Hardware")
        self.tree.setColumnWidth(0, 280)
        v.addWidget(self.disk_note)
        v.addWidget(self.tree, 1)
        return w

    # ------------------------------------------------------------ behaviour
    def _focus_search(self):
        self.tabs.setCurrentIndex(0)
        self.search.setFocus()
        self.search.selectAll()

    def _set_shown(self, on: bool):
        rows = self.proxy.source_rows(self.proxy.index(r, 0) for r in range(self.proxy.rowCount()))
        self.model.set_selected(rows, on)
        announce(self, f"{len(rows)} programs {'included' if on else 'left out'}")

    def _update_counts(self):
        winget, manual, _ = exporter.split_apps(self.model.apps)
        text = (f"{len(self.model.apps)} programs found · {len(winget) + len(manual)} included · "
                f"{len(winget)} reinstall automatically · {len(manual)} need a manual install")
        self.counts.setText(text)

    def _set_status(self, text: str):
        self.status.setText(text)
        announce(self.status, text)

    def start_scan(self):
        if self._thread:
            return
        self.scan_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.busy.show()
        self._set_status("Scanning… this takes a minute or two.")
        self._thread, self._worker = QThread(self), ScanWorker(self.demo)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._set_status)
        self._worker.done.connect(self._scan_done)
        self._worker.failed.connect(self._scan_failed)
        self._thread.start()

    def _finish_thread(self):
        self._thread.quit()
        self._thread.wait()
        self._thread = None
        self.busy.hide()
        self.scan_btn.setEnabled(True)

    def _scan_done(self, result: ScanResult):
        self._finish_thread()
        self.result = result
        self.model.set_apps(result.apps)
        self._fill_hardware(result)
        self.export_btn.setEnabled(True)
        msg = f"Scan finished: {len(result.apps)} programs. Review the list, then export the restore kit."
        if result.warnings:
            msg += " Note: " + " ".join(result.warnings)
        self._set_status(msg)

    def _scan_failed(self, err: str):
        self._finish_thread()
        self._set_status(f"Scan failed: {err}")

    def _fill_hardware(self, result: ScanResult):
        self.tree.clear()
        for s in result.hardware:
            if not s.items:
                continue
            top = QTreeWidgetItem([s.title, ""])
            self.tree.addTopLevelItem(top)
            for item in s.items:
                parent = top
                if len(s.items) > 1:
                    label = str(item.get("Name") or item.get("Model") or item.get("Slot") or item.get("Volume") or item.get("Disk", ""))
                    if "Disk" in item:
                        label = f"Disk {item['Disk']}: {item.get('Model', '')}"
                    parent = QTreeWidgetItem([label, str(item.get("Contains", ""))])
                    top.addChild(parent)
                for k, val in item.items():
                    parent.addChild(QTreeWidgetItem([k, str(val)]))
        self.tree.expandAll()
        note = exporter.disk_summary(result)
        self.disk_note.setText(note or "")
        self.disk_note.setVisible(bool(note))

    def export(self):
        if not self.result:
            return
        start = str(Path.home() / "Desktop") if (Path.home() / "Desktop").exists() else str(Path.home())
        base = QFileDialog.getExistingDirectory(self, "Choose where to save the restore kit", start)
        if not base:
            return
        name = re.sub(r'[\\/:*?"<>|]', "-", f"Parity {self.result.computer_name} {datetime.now():%Y-%m-%d %H%M}")
        folder = Path(base) / name
        try:
            exporter.export_kit(self.result, folder)
        except OSError as e:
            QMessageBox.critical(self, "Export failed", f"Couldn't write the restore kit:\n{e}")
            return
        self._set_status(f"Restore kit saved to {folder}")
        box = QMessageBox(QMessageBox.Icon.Information, "Restore kit saved",
                          f"Saved to:\n{folder}\n\nCopy this folder off the drive you're wiping before you reinstall.", parent=self)
        open_btn = box.addButton("&Open folder", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            if sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])


def run(demo: bool = False, smoke: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Parity")
    win = MainWindow(demo=demo or smoke)
    win.show()
    if smoke:     # CI check that the packaged app starts: demo scan, then exit 0 only if it loaded
        from PySide6.QtCore import QTimer
        win.start_scan()
        QTimer.singleShot(5000, lambda: app.exit(0 if win.result and win.result.apps else 1))
    return app.exec()
