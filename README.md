# TyperGOLE-OVERY

Przedmeczowy Telegram bot dla rynku **obie drużyny strzelą gola w 1. połowie — TAK**.

## Strategia
- Wyłącznie spotkania, które jeszcze się nie rozpoczęły.
- Maksymalnie 4 najlepsze typy w warszawskim dniu kalendarzowym; mniej, jeśli nie ma czterech kandydatów spełniających kryteria.
- Rzeczywisty, aktywny kurs na dokładny rynek BTTS 1. połowa pobierany z publicznej oferty Superbet. Kurs jest odświeżany tuż przed publikacją; bot nie szacuje kursów.
- Model prawdopodobieństwa wykorzystuje ostatnie zakończone mecze obu drużyn, potwierdzone wyniki do przerwy oraz gole zdobywane i tracone w pierwszych połowach. Wymagane jest minimum 5 meczów z danymi HT dla każdej drużyny.
- Procent w wiadomości jest estymacją modelu (nie gwarancją ani kursem bukmachera). Jeśli dane historyczne są niekompletne lub rynek/kurs nie jest jednoznaczny, mecz jest pomijany.
- Typ jest rozliczany po potwierdzeniu wyniku pierwszej połowy: wygrana tylko wtedy, gdy obie drużyny strzeliły co najmniej jednego gola przed przerwą.
- Historia typów i rozliczeń pozostaje w `data/state.json`.

## Harmonogram
GitHub Actions skanuje terminarz co 5 minut i w każdym uruchomieniu sprawdza również nierozliczone typy. Nie ma już skanowania LIVE ani starej progresji Over HT/FT.

## GitHub Secrets
Dodaj w tym repozytorium:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

SportScore nie wymaga klucza API na bezpłatnym poziomie z atrybucją. Wiadomości zachowują wymagany link „Powered by SportScore”.
