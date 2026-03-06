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
- În aplicație completezi user + parolă și URL-ul de listă comenzi (implicit: `https://cfmotoparts.eu/user/201/orders?order=created&sort=desc`).
- Apasă `Login și import comenzi`: aplicația intră în cont, citește comenzile și importă automat comenzile în baza locală (`comenzi.db`).
- Dacă site-ul activează verificări suplimentare (ex. CAPTCHA), login-ul automat poate eșua și trebuie făcut import manual din HTML.
