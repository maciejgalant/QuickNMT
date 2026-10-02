"""Stage 3: choose and download exactly one original source sheet.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from pathlib import Path
from qgis.PyQt.QtCore import Qt, QTimer, QUrl, QStandardPaths
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)
from qgis.core import Qgis, QgsApplication, QgsCoordinateTransformContext, QgsMessageLog, QgsProject
from .sheet_selection import AreaSheetLookup
from .download_manager import SourceDownload
from .project_layers import add_result_layer


class SourceSheetDialog(QDialog):
    def __init__(self, family, choice, parent=None, geometry=None, records=(), folder="", add_to_project=True):
        super().__init__(parent)
        self.family, self.choice, self.geometry = family, choice, geometry
        self.records = list(records)
        self.lookup = self.job = None
        self.closed = False
        self.result_folder = ""
        self.context = QgsCoordinateTransformContext(QgsProject.instance().transformContext())
        self.setWindowTitle("QuickNMT — pobierz jeden arkusz źródłowy")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(800, 590)
        self.setMinimumSize(600, 430)
        layout = QVBoxLayout(self)
        note = QLabel("Wybierz jeden arkusz z listy. Zapiszemy cały oryginalny raster wraz z opisem źródła. "
                      "Aby połączyć arkusze i przyciąć je do obszaru z buforem, użyj „Pobierz i połącz” w głównym oknie.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel(f"{family.data_type} · {family.vertical_datum} · {choice.label}"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Arkusz", "Rok", "Rozdzielczość", "Format źródłowy"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Folder zapisu"))
        self.folder = QLineEdit(folder or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation))
        row.addWidget(self.folder, 1)
        self.browse_btn = QPushButton("Przeglądaj…")
        self.browse_btn.clicked.connect(self._browse)
        row.addWidget(self.browse_btn)
        layout.addLayout(row)
        notice = QLabel("Każde pobranie tworzy nowy podfolder. Istniejące pliki pozostają zachowane.")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.add_check = QCheckBox("Dodaj pobrany raster do projektu QGIS")
        self.add_check.setChecked(add_to_project)
        layout.addWidget(self.add_check)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.open_btn = QPushButton("Otwórz folder wyniku")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.result_folder)))
        footer.addWidget(self.open_btn)
        footer.addStretch()
        self.cancel_btn = QPushButton("Zamknij")
        self.cancel_btn.clicked.connect(self._cancel_or_close)
        footer.addWidget(self.cancel_btn)
        self.download_btn = QPushButton("Pobierz wskazany arkusz")
        self.download_btn.clicked.connect(self.download_selected)
        footer.addWidget(self.download_btn)
        layout.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.finished.connect(self._shutdown)
        self._update_buttons()
        QTimer.singleShot(0, self._load)

    def _load(self):
        if self.closed:
            return
        if self.records:
            self._records_ready(self.records)
            return
        if self.geometry is None:
            self._error("Brak obszaru wyszukiwania. Wybierz arkusz na mapie lub sprawdź obszar.")
            return
        self.lookup = AreaSheetLookup(self.family, self.choice, self.geometry, self)
        self.lookup.ready.connect(self._records_ready)
        self.lookup.failed.connect(self._error)
        self.lookup.status.connect(self.status.setText)
        self.progress.setRange(0, 0)
        self.cancel_btn.setText("Anuluj wyszukiwanie")
        self.lookup.start()

    def _records_ready(self, records):
        if self.closed:
            return
        if self.lookup is not None:
            self.lookup.deleteLater()
            self.lookup = None
        self.records = sorted(records, key=lambda record: (record.sheet_id, -record.year))
        self.table.setRowCount(len(self.records))
        for row, record in enumerate(self.records):
            for col, text in enumerate((record.sheet_id, str(record.year), f"{record.resolution:g} m", record.attributes.get('format', ''))):
                item = QTableWidgetItem(text)
                item.setToolTip(record.download_url)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        if self.records:
            self.table.selectRow(0)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(f"Znaleziono arkuszy: {len(self.records)}. Wybierz jeden do pobrania." if records
                            else "Brak zgodnych arkuszy przecinających wskazany obszar.")
        self.cancel_btn.setText("Zamknij")
        self._update_buttons()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder zapisu arkusza źródłowego", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def _update_buttons(self):
        busy = self.job is not None or self.lookup is not None
        self.download_btn.setEnabled(not busy and bool(self.records) and self.table.currentRow() >= 0)
        for widget in (self.table, self.folder, self.browse_btn, self.add_check):
            widget.setEnabled(not busy)

    def download_selected(self):
        row = self.table.currentRow()
        if self.job is not None or not 0 <= row < len(self.records):
            return
        if not Path(self.folder.text().strip()).is_dir():
            self.status.setText("Wybierz istniejący folder zapisu.")
            return
        try:
            self.job = SourceDownload(self.records[row], self.family, self.choice,
                                      self.folder.text().strip(), self.context, QgsApplication.instance())
            self.job.ready.connect(self._download_ready)
            self.job.failed.connect(self._error)
            self.job.cancelled.connect(self._cancelled)
            self.job.status.connect(self.status.setText)
            self.job.progress.connect(self.progress.setValue)
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.cancel_btn.setText("Anuluj pobieranie")
            self._update_buttons()
            self.job.start()
        except Exception as exc:
            self.job = None
            self._error(str(exc))

    def _download_ready(self, result):
        self.job = None
        self.result_folder = result['folder']
        self.progress.setValue(100)
        self.open_btn.setEnabled(True)
        self.cancel_btn.setText("Zamknij")
        self.status.setText("Zapisano oryginalny arkusz: " + result['raster'])
        QgsMessageLog.logMessage("Zapisano: " + result['raster'], "QuickNMT", Qgis.MessageLevel.Info)
        if self.add_check.isChecked() and not self.closed:
            try:
                add_result_layer(result['raster'], self.family)
            except Exception as exc:
                self.status.setText(str(exc))
        self._update_buttons()

    def _error(self, message):
        self.job = None
        if self.lookup is not None:
            self.lookup.deleteLater()
            self.lookup = None
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(message)
        self.cancel_btn.setText("Zamknij")
        self._update_buttons()

    def _cancelled(self):
        self.job = None
        self.progress.setValue(0)
        self.status.setText("Pobieranie anulowane. Pliki tymczasowe usunięte.")
        self.cancel_btn.setText("Zamknij")
        self._update_buttons()

    def _cancel_or_close(self):
        if self.job is not None:
            self.status.setText("Anuluję pobieranie…")
            self.job.cancel()
        elif self.lookup is not None:
            self.lookup.cancel()
            self.lookup.deleteLater()
            self.lookup = None
            self._error("Wyszukiwanie anulowane.")
        else:
            self.reject()

    def _shutdown(self):
        self.closed = True
        if self.lookup is not None:
            self.lookup.cancel()
            self.lookup.deleteLater()
            self.lookup = None
        if self.job is not None:
            self.job.cancel()
