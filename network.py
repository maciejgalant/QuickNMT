"""Small asynchronous Geoportal requests using QGIS network/proxy settings.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from urllib.parse import urlsplit, urljoin, urlencode

from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import QgsNetworkAccessManager

from .geoportal_services import ALLOWED_HOSTS, USER_AGENT

REQUEST_TIMEOUT_MS = 180000
REQUEST_MAX_ATTEMPTS = 5


def validate_url(url):
    parsed = urlsplit(str(url))
    if (parsed.scheme.lower() != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("Adres nie jest dozwolonym adresem HTTPS Geoportalu.")
    return parsed.geturl()


def service_url(endpoint, **parameters):
    validate_url(endpoint)
    if urlsplit(endpoint).query:
        raise ValueError("Adres bazowy usługi nie może zawierać parametrów.")
    return endpoint + "?" + urlencode(parameters)


def set_reply_timeout(reply, milliseconds):
    """Set QGIS' own inactivity timer for this reply, without global settings.

    QgsNetworkAccessManager in QGIS 3.36 and 4.2 creates a child QTimer named
    timeoutTimer. Its default 60s aborts a transfer independently of Qt's
    QNetworkRequest.setTransferTimeout. The named child is an implementation
    detail: guard its presence, keep our own timer, and do not change NAM's
    global settings. QGIS restarts this timer on progress, retaining its interval.
    """
    timer = reply.findChild(QTimer, "timeoutTimer")
    if timer is not None:
        previous = timer.interval()
        timer.setInterval(milliseconds)
        return previous
    return None


class RequestJob(QObject):
    succeeded = pyqtSignal(bytes)
    failed = pyqtSignal(str)

    def __init__(self, url, parent=None, max_bytes=8 * 1024 * 1024,
                 timeout_ms=REQUEST_TIMEOUT_MS, max_attempts=REQUEST_MAX_ATTEMPTS):
        super().__init__(parent)
        self.url = validate_url(url)
        self.max_bytes = max_bytes
        self.timeout_ms = int(timeout_ms)
        self.max_attempts = int(max_attempts)
        self.reply = None
        self.data = bytearray()
        self.cancelled = False
        self.attempt = 0
        self.redirects = 0
        self.failure = ""
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self.start)

    def start(self):
        if self.cancelled:
            return
        self.attempt += 1
        self.data.clear()
        self.failure = ""
        request = QNetworkRequest(QUrl(validate_url(self.url)))
        request.setRawHeader(b"User-Agent", USER_AGENT.encode())
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setTransferTimeout(self.timeout_ms)
        self.reply = QgsNetworkAccessManager.instance().get(request)
        set_reply_timeout(self.reply, self.timeout_ms)
        self.reply.readyRead.connect(self._read)
        self.reply.finished.connect(self._finished)
        self.timer.start(self.timeout_ms)

    def _read(self):
        if self.reply is None or self.cancelled:
            return
        block = bytes(self.reply.readAll())
        if block:
            self.data.extend(block)
            # This is an inactivity timeout, not a total-operation timeout.
            self.timer.start(self.timeout_ms)
        if len(self.data) > self.max_bytes:
            self.failure = "Odpowiedź Geoportalu przekracza dopuszczalny rozmiar. Przybliż mapę."
            self.reply.abort()

    def _timeout(self):
        if self.reply is not None:
            self.failure = f"Geoportal nie przesłał danych przez {self.timeout_ms // 1000} s."
            self.reply.abort()

    def _release(self):
        self.timer.stop()
        if self.reply is not None:
            self.reply.deleteLater()
            self.reply = None

    def _finished(self):
        if self.cancelled or self.reply is None:
            return
        self._read()
        status = self.reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        redirect = self.reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
        error = self.reply.error()
        error_text = self.reply.errorString()
        self._release()
        if redirect and not self.failure:
            try:
                self.url = validate_url(urljoin(self.url, redirect.toString()))
                self.redirects += 1
                if self.redirects > 4:
                    raise ValueError("Zbyt wiele przekierowań Geoportalu.")
            except ValueError as exc:
                self.failed.emit(str(exc))
                self.deleteLater()
                return
            self.attempt -= 1
            self.start()
            return
        if not self.failure and error == QNetworkReply.NetworkError.NoError and status == 200:
            self.succeeded.emit(bytes(self.data))
            self.deleteLater()
            return
        transient = status in (None, 408, 429, 500, 502, 503, 504)
        if transient and self.attempt < self.max_attempts and len(self.data) <= self.max_bytes:
            self.retry_timer.start(1000 * (2 ** (self.attempt - 1)))
            return
        message = self.failure or f"Błąd Geoportalu: HTTP {status or '—'}; {error_text}"
        self.failed.emit(message)
        self.deleteLater()

    def cancel(self):
        self.cancelled = True
        self.timer.stop()
        self.retry_timer.stop()
        if self.reply is not None:
            self.reply.abort()
            self._release()
        self.deleteLater()


def request_bytes(url, parent, success, failure, max_bytes=8 * 1024 * 1024,
                  timeout_ms=REQUEST_TIMEOUT_MS, max_attempts=REQUEST_MAX_ATTEMPTS):
    job = RequestJob(url, parent, max_bytes, timeout_ms, max_attempts)
    job.succeeded.connect(success)
    job.failed.connect(failure)
    job.start()
    return job
