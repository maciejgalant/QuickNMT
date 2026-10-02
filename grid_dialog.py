"""Explicit choice of an output grid for mixed source resolutions.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from qgis.PyQt.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout


class GridChoiceDialog(QDialog):
    def __init__(self, plan, family, parent=None):
        super().__init__(parent)
        self.setWindowTitle('QuickNMT — rozdzielczość wyniku')
        self.setMinimumWidth(480)
        self.resize(550, 360)
        layout = QVBoxLayout(self)

        def paragraph(text):
            label = QLabel(text)
            label.setWordWrap(True)
            layout.addWidget(label)

        paragraph(f'{family.data_type} · {family.vertical_datum}')
        paragraph(plan['reason'])
        paragraph('Wybierz wielkość piksela wspólnego rastra wynikowego:')
        self.resolution = QComboBox()
        self.resolution.setObjectName('outputResolutionChoice')
        self.resolution.addItem('Wybierz rozdzielczość…', None)
        for value in sorted(plan['source_resolutions'], reverse=True):
            self.resolution.addItem(f'{value:g} m'.replace('.', ','), value)
        layout.addWidget(self.resolution)
        self.effect = QLabel('Wybór dotyczy wyniku, nie produktu źródłowego.')
        self.effect.setWordWrap(True)
        layout.addWidget(self.effect)
        paragraph('Łączenie użyje najbliższego sąsiada. Metoda wybiera istniejące wysokości '
                  'bez ich uśredniania; przy większym pikselu część szczegółów zostanie pominięta. '
                  'Braki danych pozostaną jako NoData.')
        paragraph('Układ wysokościowy pozostaje bez zmian. Rozdzielczości źródeł, wybór siatki '
                  'wyniku i metoda przetwarzania zostaną zapisane w opisie JSON.')
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Zastosuj i połącz')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Anuluj')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.resolution.currentIndexChanged.connect(self._changed)
        self._changed()

    def _changed(self):
        value = self.resolution.currentData()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(value is not None)
        if value is not None:
            self.effect.setText('Mniejszy piksel nie dodaje szczegółów do danych źródłowych o większym pikselu. '
                                'Wynik zachowa informację o mieszanych źródłach.')

    def accept(self):
        if self.resolution.currentData() is not None:
            super().accept()
