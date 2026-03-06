# Monitor Comenzi CFMoto Parts (Web)

Aplicație web (Streamlit) pentru monitorizarea comenzilor de piese CFMoto, import din:
- HTML pentru comenzi plasate
- PDF pentru invoice-uri viitoare

## Funcționalități
- Stocare locală în SQLite (`comenzi.db`)
- Separare pe tab-uri pentru comenzi plasate și invoice-uri viitoare
- Detalii piese pe comandă + filtrare după cod/denumire
- Raport piese în așteptare
- Parser PDF mai tolerant pentru număr și dată factură

## Rulare locală
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy online corect
### Varianta recomandată: Streamlit Community Cloud
1. Urcă proiectul pe GitHub.
2. Intră pe https://share.streamlit.io
3. Selectează repository-ul și fișierul `app.py`.
4. Deploy.

### Important despre GitHub Pages
`github.io` (GitHub Pages) este hosting static și **nu poate rula aplicații Python/Streamlit**.
Dacă publici doar acolo (ex: `ionanghel-cmd.github.io/cfmotoparts`), upload-ul nu va funcționa pentru această aplicație.

> Notă: baza de date SQLite e locală instanței de deploy. Pentru persistență reală multi-user recomand Postgres/Supabase.


## Sincronizare directă din contul cfmotoparts.eu
- Fluxul de sincronizare merge astfel: login la `https://cfmotoparts.eu/user/login` → acces listă comenzi (`https://cfmotoparts.eu/user/201/orders?order=created&sort=desc`) → acces fiecare link din **Order number** și import piese.
- În aplicație ai două moduri:
  1. `Login direct (fără CAPTCHA)` – pentru conturi/sesiuni unde loginul nu cere CAPTCHA.
  2. `Cookie de sesiune (compatibil CAPTCHA)` – faci login manual în browser, apoi copiezi `Cookie` header în aplicație pentru import.
- Rezolvarea automată CAPTCHA **nu este suportată**.
- Importul poate scana automat paginile următoare de orders (`?page=1`, `?page=2`, etc.) și permite import masiv până la 1000 comenzi din UI.
- Există tab nou `Căutare cod piesă` care caută codul în toate comenzile și arată: nr comandă, data comenzii, preț unitar, disponibilitate la plasare (`sufficient stock` / `not in stock; delivery ...`) și unitățile comandate.
- Data comenzii este preluată din pagina fiecărei comenzi (ex. `Invoice date`), nu data importului.
- În detalii comandă se afișează și `Preț unitar` + `Disponibilitate la plasare`.
- Poți căuta comenzile după ID-ul de comandă CFMoto (ex. `2026-543`) direct în tab-urile de comenzi.
- În detalii comandă poți marca piese ca primite manual sau prin scanare barcode (ex: `5BYV-041033-1000*1`). Suffix-ul `*1/*2/...` este tratat ca număr de bucăți, iar codul folosit la căutare este doar codul piesei.
