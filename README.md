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
- Statystyki BTTS są liczone z bezpłatnych plików CSV Open Matchday (bez klucza API i opłat): osobno sezon/rozgrywki, mecze gospodarza u siebie, gościa na wyjeździe oraz ostatnie 5 spotkań. Wiadomość Telegram pokazuje procent razem z liczbą trafień i próbą (np. 70% = 7/10). - Model prawdopodobieństwa wykorzystuje gole zdobywane i tracone w całym meczu. Brak wystarczających danych lub niejednoznaczny kurs oznacza pominięcie spotkania.
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
