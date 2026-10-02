"""Streaming source download, followed by validation in a cancellable QgsTask.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time
from urllib.parse import urljoin

from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import QgsApplication, QgsNetworkAccessManager, QgsMessageLog, Qgis
from .network import validate_url, set_reply_timeout
from .geoportal_services import USER_AGENT
from .raster_source import SourceValidationTask

MAX_DOWNLOAD_BYTES = 2 * 1024 ** 3
TIMEOUT_MS = 600000
MAX_ATTEMPTS = 6


def cleanup_directory(path):
    """Only called with directories allocated for this job by tempfile."""
    if path is not None and Path(path).exists():
        try:
            shutil.rmtree(path)
        except OSError as exc:
            QgsMessageLog.logMessage(f"Nie udało się usunąć plików tymczasowych {path}: {exc}",
                                     "QuickNMT", Qgis.MessageLevel.Warning)


class SourceDownload(QObject):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    status = pyqtSignal(str)
    progress = pyqtSignal(int)

    def __init__(self, record, family, choice, output_folder, context, parent=None):
        super().__init__(parent)
        self.record, self.family, self.choice = record, family, choice
        self.output_folder, self.context = str(output_folder), context
        self.url = validate_url(record.download_url)
        self.reply = self.stream = self.task = None
        self.temp_dir = self.part = self.complete_file = None
        self.attempt = self.redirects = self.received = 0
        self.error = ""
        self.fatal = False
        self.stopped = self.cancel_requested = False
        self.digest = None
        self.attempt_started = self.last_data_at = 0.0
        self.waiting_timer = QTimer(self)
        self.waiting_timer.setInterval(1000)
        self.waiting_timer.timeout.connect(self._waiting_status)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(self._start_request)

    def start(self):
        try:
            if not Path(self.output_folder).is_dir():
                raise ValueError("Wybierz istniejący folder zapisu.")
            base = Path(tempfile.gettempdir()) / "QuickNMT"
            base.mkdir(exist_ok=True)
            self.temp_dir = Path(tempfile.mkdtemp(prefix="source-", dir=str(base)))
            basename = hashlib.sha256(self.record.download_url.encode()).hexdigest()
            self.part = self.temp_dir / (basename + ".part")
            self.complete_file = self.temp_dir / (basename + ".download")
            self._start_request()
        except Exception as exc:
            self._fail(str(exc))

    def _start_request(self):
        if self.stopped or self.cancel_requested:
            return
        try:
            self.attempt += 1
            self.error, self.received = "", 0
            self.fatal = False
            self.digest = hashlib.sha256()
            self.attempt_started = self.last_data_at = time.monotonic()
            self.stream = self.part.open("wb")
            request = QNetworkRequest(QUrl(validate_url(self.url)))
            request.setRawHeader(b"User-Agent", USER_AGENT.encode())
            request.setRawHeader(b"Accept-Encoding", b"identity")
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
            request.setTransferTimeout(TIMEOUT_MS)
            self.reply = QgsNetworkAccessManager.instance().get(request)
            previous_timeout = set_reply_timeout(self.reply, TIMEOUT_MS)
            self.reply.setReadBufferSize(2 * 1024 * 1024)
            self.reply.readyRead.connect(self._read)
            self.reply.finished.connect(self._finished)
            self.timer.start(TIMEOUT_MS)
            self.waiting_timer.start()
            QgsMessageLog.logMessage(
                f"Pobieranie arkusza {self.record.sheet_id}, próba {self.attempt}/{MAX_ATTEMPTS}; "
                f"limit bezczynności {TIMEOUT_MS} ms; poprzedni zegar odpowiedzi QGIS: {previous_timeout}.\nURL: {self.url}",
                "QuickNMT", Qgis.MessageLevel.Info)
            self.status.emit(f"Pobieram arkusz {self.record.sheet_id} — próba {self.attempt}/{MAX_ATTEMPTS}…")
            self.progress.emit(0)
        except Exception as exc:
            self._release()
            self._fail(str(exc))

    def _read(self):
        if self.reply is None or self.stopped or self.cancel_requested or self.error:
            return
        try:
            total = self.reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
            if total is not None and int(total) > MAX_DOWNLOAD_BYTES:
                raise ValueError("Plik przekracza limit 2 GiB dla pojedynczego arkusza.")
            while self.reply.bytesAvailable():
                block = bytes(self.reply.read(1024 * 1024))
                self.received += len(block)
                if self.received > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Plik przekracza limit 2 GiB dla pojedynczego arkusza.")
                self.stream.write(block)
                self.digest.update(block)
                self.last_data_at = time.monotonic()
            self.timer.start(TIMEOUT_MS)
            self.progress.emit(min(75, int(self.received * 75 / int(total))) if total and int(total) > 0 else 0)
            self.status.emit(f"Pobieram {self.record.sheet_id}: {self.received / 1024 ** 2:.1f} MiB"
                             + (f" / {int(total) / 1024 ** 2:.1f} MiB" if total else ""))
        except Exception as exc:
            self.error = str(exc)
            self.fatal = True
            # Queue abort to avoid a reentrant finished signal while processing bytes.
            QTimer.singleShot(0, self._abort_error)

    def _abort_error(self):
        if self.reply is not None:
            self.reply.abort()

    def _timeout(self):
        self.error = f"Geoportal nie przesłał danych przez {TIMEOUT_MS // 1000} sekund."
        self._abort_error()

    def _waiting_status(self):
        if self.reply is None or self.stopped or self.cancel_requested:
            return
        elapsed = int(time.monotonic() - self.last_data_at)
        if self.received == 0 or elapsed >= 5:
            self.status.emit(f"Czekam na dane arkusza {self.record.sheet_id}: {elapsed} s "
                             f"z {TIMEOUT_MS // 1000} s; próba {self.attempt}/{MAX_ATTEMPTS}. "
                             f"Pobrano {self.received / 1024 ** 2:.1f} MiB.")

    def _release(self):
        self.timer.stop()
        self.waiting_timer.stop()
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError as exc:
                self.error = "Nie udało się zapisać pliku tymczasowego: " + str(exc)
                self.fatal = True
            finally:
                self.stream = None
        if self.reply is not None:
            self.reply.deleteLater()
            self.reply = None

    def _finished(self):
        if self.reply is None or self.stopped or self.cancel_requested:
            return
        self._read()
        if self.reply is None or self.cancel_requested or self.stopped:
            return
        status = self.reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        redirect = self.reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
        total = self.reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        content_type = bytes(self.reply.rawHeader(b"Content-Type")).decode("ascii", "replace").lower()
        network_error = self.reply.error()
        detail = self.reply.errorString()
        self._release()
        if redirect and not self.error:
            try:
                self.url = validate_url(urljoin(self.url, redirect.toString()))
                self.redirects += 1
                if self.redirects > 4:
                    raise ValueError("Zbyt wiele przekierowań pobierania.")
            except ValueError as exc:
                self._fail(str(exc))
                return
            self.attempt -= 1
            self._start_request()
            return
        if not self.error and network_error == QNetworkReply.NetworkError.NoError and status == 200:
            if total is not None and self.received != int(total):
                self.error = "Pobrany plik ma niepełny rozmiar."
            elif self.received == 0 or "text/html" in content_type or "xml" in content_type:
                self._fail("Serwer zwrócił pusty plik lub komunikat zamiast rastra.")
                return
            else:
                try:
                    os.replace(self.part, self.complete_file)
                    self.task = SourceValidationTask(self.complete_file, self.record, self.family, self.choice,
                                                     self.output_folder, self.context, self.digest.hexdigest())
                    self.task.ready.connect(self._validated)
                    self.task.failed.connect(self._fail)
                    self.task.cancelled.connect(self._cancelled_task)
                    self.task.progressChanged.connect(lambda value: self.progress.emit(75 + round(value * .25)))
                    self.status.emit("Sprawdzam raster i zapisuję oryginalny arkusz…")
                    QgsApplication.taskManager().addTask(self.task)
                    return
                except Exception as exc:
                    self._fail(str(exc))
                    return
        transient = status in (None, 408, 429, 500, 502, 503, 504) or (
            status == 200 and (network_error != QNetworkReply.NetworkError.NoError or self.error == "Pobrany plik ma niepełny rozmiar."))
        QgsMessageLog.logMessage(
            f"Próba {self.attempt}/{MAX_ATTEMPTS} nieudana; arkusz {self.record.sheet_id}; "
            f"HTTP {status}; błąd Qt {network_error}; otrzymano {self.received} B; "
            f"czas {time.monotonic() - self.attempt_started:.1f} s; {self.error or detail}\nURL: {self.url}",
            "QuickNMT", Qgis.MessageLevel.Warning)
        if transient and self.attempt < MAX_ATTEMPTS and not self.fatal:
            self.status.emit(f"Połączenie przerwane. Ponawiam za {2 ** (self.attempt - 1)} s…")
            self.retry.start(1000 * 2 ** (self.attempt - 1))
        else:
            self._fail((self.error or f"Błąd pobierania: HTTP {status or '—'}; {detail}")
                       + f" Arkusz: {self.record.sheet_id}; wykonano prób: {self.attempt}. "
                       "Szczegóły połączenia: Dziennik komunikatów → QuickNMT.")

    def _validated(self, result):
        self.task = None
        # Publication is the commit point. A very late cancel must not hide a saved result.
        self.stopped = True
        cleanup_directory(self.temp_dir)
        self.progress.emit(100)
        self.ready.emit(result)
        self.deleteLater()

    def _fail(self, message):
        self.task = None
        if self.stopped:
            return
        self.stopped = True
        self.retry.stop()
        self._release()
        cleanup_directory(self.temp_dir)
        QgsMessageLog.logMessage(f"{message}\nArkusz: {self.record.sheet_id}\nURL: {self.url}", "QuickNMT", Qgis.MessageLevel.Warning)
        self.failed.emit(message)
        self.deleteLater()

    def cancel(self):
        if self.stopped or self.cancel_requested:
            return
        self.cancel_requested = True
        self.retry.stop()
        self.timer.stop()
        self.waiting_timer.stop()
        if self.task is not None:
            self.task.cancel()
            return
        if self.reply is not None:
            self.reply.abort()
        self._release()
        self._cancelled_task()

    def _cancelled_task(self):
        self.task = None
        self.stopped = True
        cleanup_directory(self.temp_dir)
        self.cancelled.emit()
        self.deleteLater()
