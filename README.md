# TyperGOLE-OVERY

Przedmeczowy Telegram bot dla rynku **BTTS — obie drużyny strzelą gola w całym meczu (FT), TAK**.

## Źródła danych
- Terminarz, zakończone wyniki i historia drużyn: **Open Matchday** — publiczne pliki CSV, bez klucza API i bez rejestracji.
- Kursy i aktywne rynki: **Superbet**. Bot nie zgaduje kursów; wysyła typ tylko po potwierdzeniu aktywnego kursu BTTS FT.
- Open Matchday publikuje dane raz dziennie. Czas w plikach CSV używa stałej konwencji CET (UTC+1); bot przelicza go na czas polski.

## Strategia
- Wyłącznie spotkania, które jeszcze się nie rozpoczęły.
- Tylko BTTS cały mecz — TAK. Bez BTTS 1. połowy i bez innych rynków.
- Maksymalnie 10 typów dziennie.
- Minimalny kurs Superbet: 1.65.
- Statystyki BTTS są liczone z bezpłatnych plików CSV Open Matchday: forma sezonowa, dom/wyjazd, ostatnie 5 spotkań, gole zdobywane i tracone, czyste konta oraz mecze bez zdobytej bramki. Gdy dostawca udostępnia dane, model dodatkowo wykorzystuje strzały, strzały celne i rożne jako ograniczone sygnały aktywności ofensywnej. Puste pola statystyczne są traktowane jako brak danych, nigdy jako zero.
- Model łączy wskaźniki BTTS z venue, formę ostatnich meczów i estymację Poissona opartą o gole zdobywane/tracone; ma korektę na wielkość próby. Typ przechodzi tylko przy estymacji co najmniej 60% i przewadze co najmniej 4 punktów procentowych nad prawdopodobieństwem wynikającym z kursu. Minimalna próba: 5 meczów ogółem na zespół i 3 na odpowiednim terenie.
- Zakres kursów BTTS FT: 1.55–2.40. Brak odpowiedniej próbki, brak potwierdzonego rynku lub brak przewagi nad kursem oznacza pominięcie meczu.
- Open Matchday nie publikuje xG ani składów; bot nie udaje, że ma te dane. Szczegółowe statystyki strzałów są dostępne tylko dla części lig/meczów, więc są używane wyłącznie wtedy, gdy istnieje wystarczająca próbka.
- Procent w wiadomości jest estymacją modelu, nie gwarancją trafienia.

## Rozliczanie
Wynik końcowy jest pobierany z plików wynikowych Open Matchday. Typ jest rozliczany tylko wtedy, gdy znaleziony rekord zgadza się z drużynami i datą spotkania. Ponieważ dane są odświeżane raz dziennie, rozliczenie może pojawić się z opóźnieniem do kolejnej aktualizacji zbioru.

## Harmonogram
GitHub Actions skanuje terminarz co 5 minut i sprawdza nierozliczone typy.

## GitHub Secrets
Wymagane są wyłącznie:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

**Open Matchday nie wymaga żadnego klucza API.**

## Atrybucja
Dane meczowe: [Open Matchday](https://github.com/oladeeayo/open-matchday). Zbiór jest udostępniony do użytku niekomercyjnego; projekt wykorzystuje go do prywatnego bota.
