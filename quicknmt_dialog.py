"""Stage 6 GUI: AOI, sources, explicit mixed grids and four output formats.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from pathlib import Path
import traceback

from qgis.PyQt.QtCore import Qt, QStandardPaths
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QTextBrowser, QVBoxLayout, QWidget,
)
from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsCoordinateTransformContext, QgsMapLayerProxyModel, QgsMessageLog,
    QgsProject, QgsVectorLayerFeatureSource,
)
from qgis.gui import QgsMapLayerComboBox, QgsRubberBand

from . import __version__
from .grid_dialog import GridChoiceDialog
from .geoportal_services import (
    AUDIT_DATE, KRON86, automatic_buffer, family_for, resolution_choices, vertical_datums,
)
from .settings import SettingsStore
from .aoi import extent_geometry, geometry_from_wkb, transform_polygon
from .area_selector import AreaSelectorDialog
from .tasks import AoiTask
from .source_dialog import SourceSheetDialog
from .mosaic_job import MosaicJob
from .mosaic import output_snapshot
from .project_layers import add_result_layer
from .export_formats import EXTENSIONS, TEXT_FORMATS

ROOT = Path(__file__).parent
FORMATS = (
    ("GeoTIFF", "GeoTIFF (*.tif)", ".tif"),
    ("AAIGrid", "Arc/Info ASCII Grid (*.asc)", ".asc"),
    ("XYZ", "XYZ (*.xyz)", ".xyz"),
    ("TXT", "TXT (*.txt)", ".txt"),
)


class QuickNMTDialog(QDialog):
    def __init__(self, iface, parent=None, settings=None):
        super().__init__(parent)
        self.iface = iface
        self.store = settings if settings is not None else SettingsStore()
        self.last_folder = self.store.get("last_folder")
        self.manual_buffer_m = self.store.get("manual_buffer_m")
        self.raster_add_preference = self.store.get("add_to_project")
        self.help_dialog = None
        self.area_selector = None
        self.source_dialog = None
        self.download_after_aoi = False
        self.mosaic_after_aoi = False
        self.mosaic_job = None
        self.run_options = None
        self.map_selection = None
        self.aoi_task = None
        self.aoi_result = None
        self.preview_bands = []
        self.setObjectName("QuickNMTDialog")
        self.setWindowTitle(f"QuickNMT {__version__} — etap 6")
        self.setWindowIcon(QIcon(str(ROOT / "icons" / "quicknmt_icon.png")))
        self.setMinimumSize(520, 480)
        self.resize(660, 810)
        self._build_ui()
        self._restore()
        self._connect()
        self._update_area()
        self._update_buffer()
        self._update_format()

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _set_choice(combo, data):
        index = combo.findData(data)
        combo.setCurrentIndex(max(0, index))

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QIcon(str(ROOT / "icons" / "quicknmt_logo.png")).pixmap(84, 84))
        logo.setFixedSize(88, 88)
        logo.setAccessibleName("Logo QuickNMT")
        header.addWidget(logo)
        titles = QVBoxLayout()
        title = QLabel("QuickNMT")
        font = title.font()
        font.setPointSize(23)
        font.setBold(True)
        title.setFont(font)
        titles.addWidget(title)
        titles.addWidget(self._label("Pobieranie NMT i NMPT bezpośrednio z PZGiK"))
        titles.addWidget(QLabel(f"{__version__}  •  Wersja testowa — etap 6"))
        header.addLayout(titles, 1)
        self.help_btn = QPushButton("?")
        self.help_btn.setFixedSize(32, 32)
        self.help_btn.setToolTip("Instrukcja QuickNMT")
        self.help_btn.setAccessibleName("Otwórz instrukcję QuickNMT")
        header.addWidget(self.help_btn, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        self.scope_label = self._label(
            "Wybierz obszar i plik wynikowy. QuickNMT pobierze potrzebne arkusze, połączy je "
            "i przytnie do obszaru. Wyniki NMT trafią do grupy „NMT” w projekcie."
        )
        outer.addWidget(self.scope_label)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(10)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area, 1)

        data_group = QGroupBox("Dane")
        data_layout = QVBoxLayout(data_group)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.data_combo = QComboBox()
        self.data_combo.addItem("NMT — Numeryczny Model Terenu", "NMT")
        self.data_combo.addItem("NMPT — Numeryczny Model Pokrycia Terenu", "NMPT")
        self.datum_combo = QComboBox()
        self.resolution_combo = QComboBox()
        form.addRow("Rodzaj danych", self.data_combo)
        form.addRow("Układ wysokościowy", self.datum_combo)
        form.addRow("Rozdzielczość / produkt", self.resolution_combo)
        data_layout.addLayout(form)
        data_layout.addWidget(self._label(
            "Pobierany będzie produkt w wybranym układzie wysokościowym. "
            "QuickNMT nie przelicza wysokości między EVRF2007 i KRON86."
        ))
        self.product_note = self._label("")
        data_layout.addWidget(self.product_note)
        body.addWidget(data_group)

        area_group = QGroupBox("Obszar")
        area = QVBoxLayout(area_group)
        self.area_radios = {}
        for key, title in (
            ("current_extent", "Bieżący widok mapy QGIS"),
            ("geoportal", "Wybór na mapie Geoportalu"),
            ("polygon_layer", "Warstwa poligonowa"),
        ):
            radio = QRadioButton(title)
            self.area_radios[key] = radio
            area.addWidget(radio)
            if key == "geoportal":
                self.select_area_btn = QPushButton("Wybierz obszar")
                self.select_area_btn.setEnabled(False)
                self.select_area_btn.setToolTip("Otwórz mapę Geoportalu: arkusze, prostokąt lub poligon.")
                area.addWidget(self.select_area_btn)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.setToolTip("Tylko warstwy Polygon / MultiPolygon z bieżącego projektu")
        self.layer_combo.setAccessibleName("Warstwa poligonowa obszaru")
        area.addWidget(self.layer_combo)
        self.selected_only = QCheckBox("Użyj tylko zaznaczonych obiektów")
        area.addWidget(self.selected_only)
        self.area_note = self._label("")
        area.addWidget(self.area_note)
        area_actions = QHBoxLayout()
        self.check_area_btn = QPushButton("Sprawdź obszar")
        self.check_area_btn.setToolTip("Przygotuj AOI i bufor; pokaż granice na mapie QGIS bez dodawania warstw.")
        area_actions.addWidget(self.check_area_btn)
        self.clear_preview_btn = QPushButton("Usuń podgląd")
        self.clear_preview_btn.clicked.connect(self.clear_preview)
        area_actions.addWidget(self.clear_preview_btn)
        area.addLayout(area_actions)
        self.aoi_details = self._label("Obszar nie został jeszcze sprawdzony.")
        self.aoi_details.setTextFormat(Qt.TextFormat.PlainText)
        area.addWidget(self.aoi_details)
        body.addWidget(area_group)

        buffer_group = QGroupBox("Bufor techniczny")
        buffer_layout = QVBoxLayout(buffer_group)
        buffer_row = QHBoxLayout()
        self.auto_buffer = QCheckBox("Automatyczny")
        buffer_row.addWidget(self.auto_buffer)
        buffer_row.addStretch()
        self.buffer_spin = QDoubleSpinBox()
        self.buffer_spin.setRange(0.0, 500.0)
        self.buffer_spin.setDecimals(1)
        self.buffer_spin.setSuffix(" m")
        self.buffer_spin.setSingleStep(5.0)
        self.buffer_spin.setAccessibleName("Wielkość bufora w metrach")
        buffer_row.addWidget(self.buffer_spin)
        buffer_layout.addLayout(buffer_row)
        buffer_layout.addWidget(self._label("Automatycznie: max(10 m, 5 × rozdzielczość rastra)."))
        self.keep_buffer = QCheckBox("Zachowaj bufor w pliku wynikowym")
        self.keep_buffer.setToolTip("Usunięcie bufora może powodować niepełne warstwice na granicy rastra.")
        buffer_layout.addWidget(self.keep_buffer)
        body.addWidget(buffer_group)

        output_group = QGroupBox("Plik wynikowy")
        output_layout = QVBoxLayout(output_group)
        self.format_combo = QComboBox()
        for key, label, _ in FORMATS:
            self.format_combo.addItem(label, key)
        format_row = QFormLayout()
        format_row.addRow("Format pliku", self.format_combo)
        output_layout.addLayout(format_row)
        path_row = QHBoxLayout()
        self.output_path = QLineEdit()
        self.output_path.setPlaceholderText("Wskaż plik wynikowy…")
        self.output_path.setAccessibleName("Ścieżka pliku wynikowego")
        self.output_path.setToolTip("Połączony i przycięty wynik w wybranym formacie. Opis źródeł zostanie zapisany obok pliku.")
        path_row.addWidget(self.output_path, 1)
        self.browse_btn = QPushButton("Przeglądaj…")
        path_row.addWidget(self.browse_btn)
        output_layout.addLayout(path_row)
        self.add_to_project = QCheckBox("Dodaj wynik do projektu QGIS")
        self.add_to_project.setToolTip("Automatyczne dodawanie dotyczy formatów rastrowych TIF/ASC.")
        output_layout.addWidget(self.add_to_project)
        self.format_note = self._label("")
        output_layout.addWidget(self.format_note)
        body.addWidget(output_group)
        body.addStretch()

        self.summary = self._label("")
        outer.addWidget(self.summary)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setEnabled(False)
        self.progress.setToolTip("Postęp przygotowania obszaru, pobierania i łączenia arkuszy.")
        outer.addWidget(self.progress)
        self.status = self._label("Wybierz obszar, wskaż plik i kliknij „Pobierz i połącz”.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.status)
        footer = QHBoxLayout()
        self.source_btn = QPushButton("Pojedynczy arkusz…")
        self.source_btn.setToolTip("Pobierz jeden pełny plik źródłowy bez łączenia i przycinania.")
        footer.addWidget(self.source_btn)
        footer.addStretch()
        self.close_btn = QPushButton("Zamknij")
        self.download_btn = QPushButton("Pobierz i połącz")
        self.download_btn.setToolTip("Pobierz potrzebne arkusze, połącz, przytnij i zapisz w wybranym formacie.")
        footer.addWidget(self.close_btn)
        footer.addWidget(self.download_btn)
        outer.addLayout(footer)
        for button in (self.help_btn, self.select_area_btn, self.check_area_btn, self.clear_preview_btn,
                       self.browse_btn, self.close_btn, self.download_btn, self.source_btn):
            button.setAutoDefault(False)

    def _restore(self):
        self._set_choice(self.data_combo, self.store.get("data_type"))
        self._populate_datums(self.store.get("vertical_datum"))
        self._populate_resolutions(self.store.get("resolution"))
        self._set_choice(self.format_combo, self.store.get('output_format'))
        mode = self.store.get("aoi_mode")
        self.area_radios.get(mode, self.area_radios["current_extent"]).setChecked(True)
        self.auto_buffer.setChecked(self.store.get("auto_buffer"))
        self.keep_buffer.setChecked(self.store.get("keep_buffer"))
        self.selected_only.setChecked(self.store.get("selected_only"))
        self.add_to_project.setChecked(self.raster_add_preference)

    def _connect(self):
        self.data_combo.currentIndexChanged.connect(self._data_changed)
        self.datum_combo.currentIndexChanged.connect(self._datum_changed)
        self.resolution_combo.currentIndexChanged.connect(self._resolution_changed)
        for radio in self.area_radios.values():
            radio.toggled.connect(self._update_area)
        self.auto_buffer.toggled.connect(self._update_buffer)
        self.buffer_spin.valueChanged.connect(self._manual_buffer_changed)
        self.keep_buffer.toggled.connect(self._update_summary)
        self.format_combo.currentIndexChanged.connect(self._update_format)
        self.add_to_project.toggled.connect(self._add_preference_changed)
        self.browse_btn.clicked.connect(self._choose_path)
        self.output_path.editingFinished.connect(self._normalize_path)
        self.help_btn.clicked.connect(self.show_help)
        self.close_btn.clicked.connect(self.close)
        self.select_area_btn.clicked.connect(self.open_area_selector)
        self.check_area_btn.clicked.connect(self.check_area)
        self.download_btn.clicked.connect(self.prepare_mosaic)
        self.source_btn.clicked.connect(self.prepare_source_download)
        self.selected_only.toggled.connect(self.invalidate_aoi)
        self.layer_combo.layerChanged.connect(self.invalidate_aoi)

    def _populate_datums(self, preferred):
        self.datum_combo.blockSignals(True)
        self.datum_combo.clear()
        for datum in vertical_datums(self.data_combo.currentData()):
            self.datum_combo.addItem(datum, datum)
        self._set_choice(self.datum_combo, preferred)
        self.datum_combo.blockSignals(False)

    def _populate_resolutions(self, preferred):
        self.resolution_combo.blockSignals(True)
        self.resolution_combo.clear()
        for choice in resolution_choices(self.data_combo.currentData(), self.datum_combo.currentData()):
            self.resolution_combo.addItem(choice.label, choice.key)
        self._set_choice(self.resolution_combo, preferred)
        self.resolution_combo.blockSignals(False)

    def _data_changed(self):
        self._populate_datums(self.datum_combo.currentData())
        self._populate_resolutions(self.resolution_combo.currentData())
        self._resolution_changed()

    def _datum_changed(self):
        self._populate_resolutions(self.resolution_combo.currentData())
        self._resolution_changed()

    def _resolution_changed(self):
        if self.map_selection is not None and self.map_selection.mode == "sheets":
            family = family_for(self.data_combo.currentData(), self.datum_combo.currentData(), self.resolution_combo.currentData())
            if self.map_selection.product_key != (family.key, self.current_resolution().key):
                self.map_selection = None
                self.status.setText("Zmieniono produkt — wybierz arkusze ponownie.")
        self.invalidate_aoi()
        self._update_buffer()
        self._update_area()

    def current_resolution(self):
        return next(choice for choice in resolution_choices(
            self.data_combo.currentData(), self.datum_combo.currentData())
            if choice.key == self.resolution_combo.currentData())

    def _update_buffer(self):
        self.invalidate_aoi()
        auto = self.auto_buffer.isChecked()
        self.buffer_spin.blockSignals(True)
        self.buffer_spin.setValue(automatic_buffer(self.current_resolution().buffer_resolution_m)
                                  if auto else self.manual_buffer_m)
        self.buffer_spin.setEnabled(not auto)
        self.buffer_spin.blockSignals(False)
        self._update_product_note()
        self._update_summary()

    def _manual_buffer_changed(self, value):
        if not self.auto_buffer.isChecked():
            self.manual_buffer_m = value
            self.invalidate_aoi()
        self._update_summary()

    def _update_product_note(self):
        if self.data_combo.currentData() == "NMPT":
            text = ("NMPT obejmuje teren wraz z zabudową i roślinnością. Automat zachowuje informację "
                    "o źródłach 0,5 m / 1 m. Mieszane rozdzielczości wymagają wyboru siatki wyniku. "
                    "Wybór produktu 0,5 m lub 1 m pobiera wyłącznie taką rozdzielczość źródłową.")
        elif self.datum_combo.currentData() == KRON86:
            text = ("NMT opisuje powierzchnię terenu. Źródłowego produktu GRID 5 m w KRON86 "
                    "nie potwierdzono w audycie, dlatego nie ma go na liście.")
        else:
            text = "NMT opisuje powierzchnię terenu. Siatka 5 m oznacza osobny produkt źródłowy PZGiK."
        self.product_note.setText(text)
        self.product_note.setToolTip(f"Katalog produktów sprawdzono {AUDIT_DATE}. Mapa odczytuje aktualne warstwy z Geoportalu; pokrycie rastra wymaga osobnego sprawdzenia.")

    def aoi_mode(self):
        return next(key for key, radio in self.area_radios.items() if radio.isChecked())

    def _update_area(self):
        self.invalidate_aoi()
        vector = self.area_radios["polygon_layer"].isChecked()
        self.layer_combo.setEnabled(vector)
        self.selected_only.setEnabled(vector)
        self.select_area_btn.setEnabled(self.area_radios["geoportal"].isChecked())
        notes = {
            "current_extent": "Obszar zostanie odczytany z aktualnego widoku, w CRS mapy. Po zmianie zasięgu sprawdź go ponownie.",
            "geoportal": (f"Wybrano arkuszy: {len(self.map_selection.sheets)}." if self.map_selection and self.map_selection.mode == "sheets"
                          else "Obszar narysowany na mapie Geoportalu." if self.map_selection else "Otwórz mapę i wybierz arkusze lub narysuj obszar."),
            "polygon_layer": "Obiekty zostaną połączone; błędne geometrie naprawione. Po zmianie zaznaczenia sprawdź obszar ponownie.",
        }
        self.area_note.setText(notes[self.aoi_mode()])

    def invalidate_aoi(self, *args):
        if self.mosaic_after_aoi:
            self._set_busy(False)
        self.download_after_aoi = False
        self.mosaic_after_aoi = False
        if self.aoi_task is not None:
            self.aoi_task.cancel()
            self.aoi_task = None
        self.aoi_result = None
        self.clear_preview()
        self.check_area_btn.setText("Sprawdź obszar")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setEnabled(False)
        self.aoi_details.setText("Obszar nie został jeszcze sprawdzony dla bieżących ustawień.")

    def clear_preview(self):
        for band in self.preview_bands:
            band.reset(Qgis.GeometryType.Polygon)
            band.scene().removeItem(band)
        self.preview_bands.clear()

    def open_area_selector(self):
        if self.area_selector is not None and self.area_selector.isVisible():
            self.area_selector.raise_()
            return
        if self.area_selector is not None:
            self.area_selector.deleteLater()
        family = family_for(self.data_combo.currentData(), self.datum_combo.currentData(), self.resolution_combo.currentData())
        self.area_selector = AreaSelectorDialog(self.iface, family, self.current_resolution(), self, self.map_selection)
        self.area_selector.accepted.connect(self._area_selected)
        self.area_selector.open()

    def _area_selected(self):
        self.map_selection = self.area_selector.result_selection
        self._update_area()
        self.check_area()

    def check_area(self):
        if self.aoi_task is not None:
            self.invalidate_aoi()
            self.status.setText("Anulowano przygotowanie obszaru.")
            return
        self.invalidate_aoi()
        try:
            mode = self.aoi_mode()
            context = QgsCoordinateTransformContext(QgsProject.instance().transformContext())
            options = dict(context=context, buffer_m=self.buffer_spin.value(), mode=mode)
            if mode == "current_extent":
                canvas = self.iface.mapCanvas()
                options.update(source_crs=canvas.mapSettings().destinationCrs().toWkt(),
                               geometry_wkb=bytes(extent_geometry(canvas.extent()).asWkb()))
            elif mode == "polygon_layer":
                layer = self.layer_combo.currentLayer()
                if layer is None or not layer.isValid() or not layer.crs().isValid():
                    raise ValueError("Wybierz poprawną warstwę poligonową z określonym CRS.")
                ids = list(layer.selectedFeatureIds()) if self.selected_only.isChecked() else None
                if ids == []:
                    raise ValueError("W warstwie nie ma zaznaczonych obiektów. Zaznacz obiekty albo wyłącz ograniczenie.")
                options.update(source_crs=layer.crs().toWkt(), feature_source=QgsVectorLayerFeatureSource(layer), selected_ids=ids)
            else:
                if self.map_selection is None:
                    raise ValueError("Najpierw wybierz lub narysuj obszar na mapie Geoportalu.")
                options.update(source_crs=self.map_selection.crs, geometry_wkb=self.map_selection.geometry_wkb)
            task = AoiTask(**options)
            if mode == "polygon_layer":
                task.setDependentLayers([layer])
            self.aoi_task = task
            task.ready.connect(self._aoi_ready)
            task.failed.connect(self._aoi_failed)
            task.cancelled.connect(self._aoi_cancelled)
            task.progressChanged.connect(self._aoi_progress)
            self.check_area_btn.setText("Anuluj sprawdzanie")
            self.progress.setEnabled(True)
            self.status.setText("Przygotowuję obszar i bufor w tle…")
            QgsApplication.taskManager().addTask(task)
        except Exception as exc:
            QgsMessageLog.logMessage(traceback.format_exc(), "QuickNMT", Qgis.MessageLevel.Warning)
            self.status.setText(str(exc))
            self.aoi_details.setText("Obszar nie jest gotowy: " + str(exc))
            self.download_after_aoi = False

    def _aoi_progress(self, value):
        if self.sender() is self.aoi_task:
            self.progress.setValue(round(value))

    def _aoi_ready(self, result):
        if self.sender() is not self.aoi_task:
            return
        self.aoi_task = None
        self.aoi_result = result
        self.check_area_btn.setText("Sprawdź obszar")
        self.progress.setValue(100)
        area = f"{result.area_m2 / 1_000_000:.4f}".replace(".", ",")
        self.aoi_details.setText(
            f"AOI: {area} km²; obiektów: {result.source_count}; bufor: {result.buffer_m:g} m. "
            "CRS roboczy: EPSG:2180. " + " ".join(result.warnings))
        try:
            self._show_aoi_preview(result)
            self.status.setText("Obszar gotowy. Zielony: AOI; pomarańczowy: bufor. Podgląd jest migawką z chwili sprawdzenia.")
        except Exception:
            QgsMessageLog.logMessage(traceback.format_exc(), "QuickNMT", Qgis.MessageLevel.Warning)
            self.clear_preview()
            self.status.setText("Obszar gotowy, ale nie udało się pokazać podglądu. Sprawdź dziennik QuickNMT.")
        if self.download_after_aoi:
            self.download_after_aoi = False
            self._open_source_dialog(result)
        elif self.mosaic_after_aoi:
            self.mosaic_after_aoi = False
            self._start_mosaic(result)

    def _set_busy(self, busy):
        self.scroll_area.setEnabled(not busy)
        self.source_btn.setEnabled(not busy)
        self.download_btn.setText('Anuluj' if busy else 'Pobierz i połącz')
        self.progress.setEnabled(busy)

    def prepare_mosaic(self):
        if self.mosaic_job is not None:
            self.status.setText('Anuluję i usuwam pliki tymczasowe…')
            self.mosaic_job.cancel()
            return
        if self.mosaic_after_aoi:
            self.invalidate_aoi()
            self._set_busy(False)
            self.status.setText('Przygotowanie wyniku anulowane.')
            return
        try:
            output_format = self.format_combo.currentData()
            if output_format not in EXTENSIONS:
                raise ValueError('Wybierz format wyniku.')
            self._normalize_path()
            if not self.output_path.text().strip():
                self._choose_path()
            if not self.output_path.text().strip():
                return
            path = Path(self.output_path.text().strip())
            if not path.is_absolute() or not path.parent.is_dir():
                raise ValueError('Wskaż pełną ścieżkę w istniejącym folderze zapisu.')
            path = path.resolve()
            for layer in QgsProject.instance().mapLayers().values():
                if layer.providerType() == 'gdal' and Path(layer.source().split('|')[0]).resolve() == path:
                    raise ValueError('Ten raster jest już otwarty w projekcie. Wskaż nową nazwę wyniku.')
            for suffix in ('.aux.xml', '.ovr', '.msk'):
                if Path(str(path) + suffix).exists():
                    raise ValueError('Obok wyniku istnieją dodatkowe pliki rastra. Wskaż nową nazwę wyniku.')
            snapshot = output_snapshot(path, output_format)
            if any(value is not None for value in snapshot.values()):
                answer = QMessageBox.question(self, 'Zastąpić wynik?',
                    'Po ukończeniu pobierania zapiszę następujące pliki (istniejące zostaną zastąpione):\n'
                    + '\n'.join(snapshot) + '\n'
                    'Dotychczasowy wynik pozostanie zachowany, jeśli przetwarzanie zostanie przerwane.',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.run_options = {'target': str(path), 'snapshot': snapshot,
                                'output_format': output_format,
                                'add_to_project': self.add_to_project.isChecked() and output_format not in TEXT_FORMATS}
            self.last_folder = str(path.parent)
            if self.aoi_task is not None:
                self.invalidate_aoi()
            self.check_area()
            self.mosaic_after_aoi = self.aoi_task is not None
            self._set_busy(self.mosaic_after_aoi)
        except Exception as exc:
            self.status.setText(str(exc))

    def _start_mosaic(self, result):
        try:
            family = family_for(self.data_combo.currentData(), self.datum_combo.currentData(), self.resolution_combo.currentData())
            self.run_options['family'] = family
            self.mosaic_job = MosaicJob(result, family, self.current_resolution(),
                self.run_options['target'], self.keep_buffer.isChecked(),
                QgsCoordinateTransformContext(QgsProject.instance().transformContext()),
                self.run_options['snapshot'], QgsApplication.instance(), output_format=self.run_options['output_format'])
            self.mosaic_job.ready.connect(self._mosaic_ready)
            self.mosaic_job.failed.connect(self._mosaic_failed)
            self.mosaic_job.cancelled.connect(self._mosaic_cancelled)
            self.mosaic_job.status.connect(self._mosaic_status)
            self.mosaic_job.progress.connect(self._mosaic_progress)
            self.mosaic_job.confirmation_required.connect(self._confirm_grid)
            self.mosaic_job.export_confirmation_required.connect(self._confirm_large_export)
            self.mosaic_job.start()
        except Exception as exc:
            if self.mosaic_job is not None:
                self.mosaic_job.cancel()
            self.mosaic_job = None
            self._set_busy(False)
            self.status.setText(str(exc))

    def _mosaic_status(self, message):
        if self.sender() is self.mosaic_job:
            self.status.setText(message)

    def _mosaic_progress(self, value):
        if self.sender() is self.mosaic_job:
            self.progress.setValue(value)

    def _confirm_grid(self, plan):
        job = self.mosaic_job
        if self.sender() is not job:
            return
        if plan['mixed_resolution']:
            dialog = GridChoiceDialog(plan, job.family, self)
            answer = dialog.exec()
            resolution = dialog.resolution.currentData()
            dialog.deleteLater()
            if job is self.mosaic_job:
                if answer == QDialog.DialogCode.Accepted:
                    job.continue_mosaic(True, resolution)
                else:
                    job.cancel()
            return
        answer = QMessageBox.question(self, 'Wspólna siatka wyniku', plan['reason'] +
            f'\nRozdzielczość wyniku: {plan["resolution"]:g} m. Zastosować metodę najbliższego sąsiada? '
            'Wysokości nie będą przeliczane między układami pionowymi. Informacja o zmianie siatki trafi do opisu wyniku.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if job is self.mosaic_job:
            if answer == QMessageBox.StandardButton.Yes:
                job.continue_mosaic(True)
            else:
                job.cancel()

    def _mosaic_ready(self, result):
        if self.sender() is not self.mosaic_job:
            return
        self.mosaic_job = None
        self._set_busy(False)
        self.progress.setValue(100)
        message = 'Zapisano: ' + result['raster']
        if self.run_options['add_to_project'] and result.get('is_raster', True):
            try:
                add_result_layer(result['raster'], self.run_options['family'])
            except Exception as exc:
                message += '\n' + str(exc)
        if result['warnings']:
            message += '\n' + '\n'.join(result['warnings'])
        self.status.setText(message)
        QgsMessageLog.logMessage(message, 'QuickNMT', Qgis.MessageLevel.Info)

    def _confirm_large_export(self, estimate):
        job = self.mosaic_job
        if self.sender() is not job:
            return
        cells = f'{estimate["cells"]:,}'.replace(',', ' ')
        size = estimate['estimated_bytes'] / 1024**3
        answer = QMessageBox.question(self, 'Duży eksport tekstowy',
            f'Prostokąt obejmujący wynik może zawierać do {cells} komórek. '
            f'Orientacyjny rozmiar pliku przy pełnym pokryciu: {size:.2f} GiB.\n'
            'Punkty bez danych i poza obszarem zostaną pominięte. Zapis może potrwać dłużej. Kontynuować?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if job is self.mosaic_job:
            job.confirm_export(answer == QMessageBox.StandardButton.Yes)

    def _mosaic_failed(self, message):
        if self.sender() is self.mosaic_job:
            self.mosaic_job = None
            self._set_busy(False)
            self.status.setText('Nie zapisano wyniku. ' + message)

    def _mosaic_cancelled(self):
        if self.sender() is self.mosaic_job:
            self.mosaic_job = None
            self._set_busy(False)
            self.progress.setValue(0)
            self.status.setText('Anulowano. Pliki tymczasowe usunięte; dotychczasowy wynik zachowany.')

    def prepare_source_download(self):
        if self.source_dialog is not None and self.source_dialog.isVisible():
            self.source_dialog.raise_()
            return
        if self.aoi_mode() == 'geoportal' and self.map_selection is not None and self.map_selection.sheets:
            self._open_source_dialog(records=self.map_selection.sheets)
            return
        # Always refresh extent, layer geometry and selected IDs when starting a download.
        if self.aoi_task is not None:
            self.invalidate_aoi()
        self.check_area()
        self.download_after_aoi = self.aoi_task is not None

    def _open_source_dialog(self, result=None, records=()):
        try:
            family = family_for(self.data_combo.currentData(), self.datum_combo.currentData(), self.resolution_combo.currentData())
            geometry = None
            if result is not None:
                geometry, _ = transform_polygon(geometry_from_wkb(result.buffered_wkb), result.crs, 'EPSG:3857',
                                                 QgsProject.instance().transformContext())
            if self.source_dialog is not None:
                self.source_dialog.deleteLater()
            self.source_dialog = SourceSheetDialog(family, self.current_resolution(), self, geometry, records,
                                                   self.last_folder, self.raster_add_preference)
            self.source_dialog.finished.connect(self._source_closed)
            self.source_dialog.open()
        except Exception as exc:
            QgsMessageLog.logMessage(traceback.format_exc(), 'QuickNMT', Qgis.MessageLevel.Warning)
            self.status.setText(str(exc))

    def _source_closed(self):
        if self.source_dialog is not None:
            folder = self.source_dialog.folder.text().strip()
            if Path(folder).is_dir():
                self.last_folder = folder

    def _show_aoi_preview(self, result):
        self.clear_preview()
        canvas = self.iface.mapCanvas()
        transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(result.crs),
            canvas.mapSettings().destinationCrs(), QgsProject.instance().transformContext())
        for wkb, color, z_order in ((result.buffered_wkb, QColor(225, 125, 20), 80),
                                    (result.original_wkb, QColor(0, 155, 65), 81)):
            geometry = geometry_from_wkb(wkb)
            geometry.transform(transform)
            band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
            band.setFillColor(QColor(0, 0, 0, 0))
            band.setStrokeColor(color)
            band.setWidth(2)
            band.setZValue(z_order)
            band.setToGeometry(geometry, None)
            self.preview_bands.append(band)

    def _aoi_failed(self, message):
        if self.sender() is not self.aoi_task:
            return
        self.aoi_task = None
        self.download_after_aoi = False
        self.mosaic_after_aoi = False
        self._set_busy(False)
        self.check_area_btn.setText("Sprawdź obszar")
        self.progress.setValue(0)
        self.aoi_details.setText("Obszar nie jest gotowy: " + message)
        self.status.setText("Nie udało się przygotować obszaru. " + message)

    def _aoi_cancelled(self):
        if self.sender() is self.aoi_task:
            self.invalidate_aoi()
            self._set_busy(False)
            self.status.setText("Przygotowanie obszaru anulowane.")

    def _add_preference_changed(self, checked):
        if self.format_combo.currentData() in ("GeoTIFF", "AAIGrid"):
            self.raster_add_preference = checked

    def _update_format(self):
        raster = self.format_combo.currentData() in ("GeoTIFF", "AAIGrid")
        self.add_to_project.blockSignals(True)
        self.add_to_project.setEnabled(raster)
        self.add_to_project.setChecked(self.raster_add_preference if raster else False)
        self.add_to_project.blockSignals(False)
        notes = {
            "GeoTIFF": "GeoTIFF zachowuje raster, układ poziomy i NoData. Obok zapiszemy opis źródeł .quicknmt.json.",
            "AAIGrid": "ASCII Grid: raster tekstowy; plik .prj będzie zapisany obok wyniku.",
            "XYZ": "XYZ: X Y Z, spacje, bez nagłówka, środki komórek; bez NoData. Duże obszary tworzą duże pliki.",
            "TXT": "TXT: nagłówek X/Y/Z, tabulatory, środki komórek; bez NoData. Duże obszary tworzą duże pliki.",
        }
        self.format_note.setText(notes[self.format_combo.currentData()])
        self._normalize_path()
        self._update_summary()

    def _extension(self):
        return next(ext for key, _, ext in FORMATS if key == self.format_combo.currentData())

    def _normalize_path(self):
        text = self.output_path.text().strip()
        if not text:
            return
        path = Path(text)
        if path.name and path.suffix.lower() != self._extension():
            self.output_path.setText(str(path.with_suffix(self._extension())))

    def _choose_path(self):
        self._normalize_path()
        folder = self.last_folder or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        suggested = self.output_path.text() or str(Path(folder) / (self.data_combo.currentData().lower() + self._extension()))
        path, _ = QFileDialog.getSaveFileName(self, "Plik wynikowy", suggested,
            self.format_combo.currentText(), options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.output_path.setText(path)
            self._normalize_path()
            self.last_folder = str(Path(self.output_path.text()).parent)

    def _update_summary(self):
        buffer = f"{self.buffer_spin.value():g}".replace(".", ",")
        self.summary.setText(
            f"{self.data_combo.currentData()}  •  {self.datum_combo.currentData()}  •  "
            f"bufor {buffer} m  •  {self.format_combo.currentData()}"
        )

    def show_help(self):
        if self.help_dialog is None:
            self.help_dialog = QDialog(self)
            self.help_dialog.setWindowTitle("QuickNMT — instrukcja")
            self.help_dialog.resize(580, 640)
            layout = QVBoxLayout(self.help_dialog)
            browser = QTextBrowser()
            browser.setOpenExternalLinks(False)
            try:
                browser.setHtml((ROOT / "docs" / "help.html").read_text(encoding="utf-8"))
            except OSError:
                QgsMessageLog.logMessage(traceback.format_exc(), "QuickNMT", Qgis.MessageLevel.Warning)
                browser.setPlainText("Nie udało się odczytać instrukcji. Sprawdź README.md w katalogu QuickNMT.")
            layout.addWidget(browser)
            close = QPushButton("Zamknij")
            close.clicked.connect(self.help_dialog.close)
            layout.addWidget(close)
        self.help_dialog.show()
        self.help_dialog.raise_()
        self.help_dialog.activateWindow()

    def save_settings(self):
        self._normalize_path()
        path = self.output_path.text().strip()
        if path and Path(path).is_absolute():
            self.last_folder = str(Path(path).parent)
        try:
            self.store.save({
                "last_folder": self.last_folder,
                "data_type": self.data_combo.currentData(),
                "vertical_datum": self.datum_combo.currentData(),
                "resolution": self.resolution_combo.currentData(),
                "output_format": self.format_combo.currentData(),
                "aoi_mode": self.aoi_mode(),
                "manual_buffer_m": self.manual_buffer_m,
                "auto_buffer": self.auto_buffer.isChecked(),
                "keep_buffer": self.keep_buffer.isChecked(),
                "add_to_project": self.raster_add_preference,
                "selected_only": self.selected_only.isChecked(),
            })
        except OSError:
            QgsMessageLog.logMessage(traceback.format_exc(), "QuickNMT", Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning("QuickNMT", "Nie udało się zapisać ustawień. Sprawdź dziennik QuickNMT.")

    def closeEvent(self, event):
        self._close_aoi_tools()
        self.save_settings()
        if self.help_dialog is not None:
            self.help_dialog.close()
        super().closeEvent(event)

    def reject(self):
        # Escape also saves preferences, without destroying the reusable dialog.
        self._close_aoi_tools()
        self.save_settings()
        if self.help_dialog is not None:
            self.help_dialog.close()
        super().reject()

    def _close_aoi_tools(self):
        if self.mosaic_job is not None:
            job = self.mosaic_job
            self.mosaic_job = None
            job.cancel()
        self._set_busy(False)
        self.invalidate_aoi()
        if self.area_selector is not None and self.area_selector.isVisible():
            self.area_selector.reject()
        if self.source_dialog is not None and self.source_dialog.isVisible():
            self.source_dialog.reject()
