# Test użytkownika — QuickNMT 0.0.6 — duże obszary

Zainstaluj QuickNMT-0.0.6-duze-obszary.zip, zastąp poprzednią wersję i uruchom QGIS ponownie.
W oknie powinna być wersja 0.0.6. Zacznij od małego obszaru.

1. Wybierz NMPT, EVRF2007 oraz źródłową/automatyczną rozdzielczość 0,5 m / 1 m.
2. Wskaż obszar, plik GeoTIFF i kliknij „Pobierz i połącz”.
3. Jeżeli źródła mają różne rozdzielczości, pojawi się wybór siatki wyniku.
   Wybierz 1 m, a następnie „Zastosuj i połącz”. Przy jednakowych, zgodnych siatkach
   pytanie nie jest potrzebne. Sam wybór trybu automatycznego nie wymusza pytania.
4. Powtórz ten sam obszar z nową nazwą pliku i wybierz wynik 0,5 m.
   Sprawdź, czy źródła 1 m nadal są opisane jako 1 m w pliku .quicknmt.json.
5. Sprawdź rozdzielczość wyników w informacjach warstwy oraz grupę NMPT.
6. Powtórz próbę dla KRON86 na obszarze z dostępnymi źródłami tego produktu.
   Wtyczka nie przelicza wysokości z EVRF2007. Brak pokrycia jest możliwy.
7. Wybierz „0,5 m — produkt źródłowy”. W pobranych źródłach powinny być tylko
   arkusze 0,5 m. Nie powinny być zastępowane przeskalowanymi źródłami 1 m.
8. Anuluj okno wyboru mieszanej siatki. Wynik nie powinien powstać, a formularz
   powinien ponownie pozwolić uruchomić pobieranie.
9. Sprawdź ASC (wraz z PRJ), XYZ i TXT dla tego samego obszaru. Tekst zapisuje
   środki komórek i pomija NoData; nie jest automatycznie dodawany jako warstwa.
10. Wróć do NMT. Dodawane rastry nadal powinny trafiać do grupy NMT,
    także gdy grupa jest zagnieżdżona i zawiera wcześniejsze wyniki.

Zmniejszenie piksela nie poprawia szczegółowości źródła. Zwiększenie piksela
może pominąć drobne szczegóły. Najbliższy sąsiad wybiera istniejące wysokości
bez uśredniania. Luki pozostają jako NoData.

Pozostają etapy 7 — odporność i obsługa zadań; 8 — warunkowy WCS;
9 — końcowe testy, dokumentacja i wydanie.
W razie błędu podaj wersję QGIS, produkt, obszar i komunikat z dziennika QuickNMT.


## Test dużego obszaru — 0.0.6

1. Wybierz AOI szerszy niż 60 km lub o powierzchni większej niż wcześniejszy limit.
2. Kliknij **Pobierz i połącz**. W statusie powinien pojawić się komunikat `Duży obszar: dzielę wyszukiwanie na ... fragmentów`.
3. Sprawdź, że wyszukiwanie przechodzi kolejno przez `fragment X/Y` zamiast kończyć się błędem o zbyt dużym obszarze.
4. Podczas wolnej odpowiedzi serwera pobieranie nie powinno zostać przerwane przed 180 s bezczynności dla WFS lub 600 s bezczynności dla pliku źródłowego.
5. Po przerwaniu połączenia plik źródłowy powinien być ponawiany do 6 razy.
6. Po zakończeniu sprawdź ciągłość mozaiki na granicach fragmentów wyszukiwania.
