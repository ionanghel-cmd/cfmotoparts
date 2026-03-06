import io
import re
import sqlite3
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import pdfplumber
import requests
import streamlit as st
from bs4 import BeautifulSoup

DB_PATH = "comenzi.db"
BASE_URL = "https://cfmotoparts.eu"
ORDERS_URL = f"{BASE_URL}/user/201/orders?order=created&sort=desc"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS comenzi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE,
            data_plasare TEXT NOT NULL,
            note TEXT,
            tip TEXT DEFAULT 'plasata'
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS piese (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comanda_id INTEGER NOT NULL,
            nume_piesa TEXT NOT NULL,
            cod TEXT,
            cantitate REAL NOT NULL,
            cantitate_primita REAL DEFAULT 0.0,
            data_primire TEXT,
            status TEXT DEFAULT 'asteptata',
            pret_unitar REAL,
            disponibilitate_plasare TEXT,
            FOREIGN KEY (comanda_id) REFERENCES comenzi(id)
        )
        """
    )

    cursor.execute("PRAGMA table_info(piese)")
    cols = {row[1] for row in cursor.fetchall()}
    if "pret_unitar" not in cols:
        cursor.execute("ALTER TABLE piese ADD COLUMN pret_unitar REAL")
    if "disponibilitate_plasare" not in cols:
        cursor.execute("ALTER TABLE piese ADD COLUMN disponibilitate_plasare TEXT")

    conn.commit()


def _extract_csrf_login_fields(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    form = soup.find("form", id="user-login") or soup.find("form", attrs={"action": "/user/login"})
    if not form:
        raise ValueError("Nu am găsit formularul de login pe cfmotoparts.eu")

    payload = {}
    for inp in form.find_all("input"):
        name = (inp.get("name") or "").strip()
        if name:
            payload[name] = inp.get("value") or ""
    return payload


def _page_has_captcha(html_text: str) -> bool:
    txt = html_text.lower()
    return "captcha" in txt or "g-recaptcha" in txt or "i'm not a robot" in txt


def _build_session(cookie_header: str = ""):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    )
    if cookie_header.strip():
        session.headers.update({"Cookie": cookie_header.strip()})
    return session


def login_and_fetch_orders_html(username: str, password: str, orders_url: str = ORDERS_URL):
    session = _build_session()

    login_url = f"{BASE_URL}/user/login"
    login_page = session.get(login_url, timeout=30)
    login_page.raise_for_status()

    if _page_has_captcha(login_page.text):
        raise ValueError(
            "Pagina de login cere CAPTCHA. Nu pot automatiza rezolvarea CAPTCHA. "
            "Folosește metoda cu cookie de sesiune sau import HTML după login manual."
        )

    payload = _extract_csrf_login_fields(login_page.text)
    payload["name"] = username
    payload["pass"] = password

    response = session.post(login_url, data=payload, timeout=30, allow_redirects=True)
    response.raise_for_status()

    orders_response = session.get(orders_url, timeout=30)
    orders_response.raise_for_status()

    if "/user/login" in orders_response.url or "edit-name" in orders_response.text:
        raise ValueError(
            "Autentificarea a eșuat. Verifică user/parolă sau folosește metoda cu cookie dacă login-ul are CAPTCHA."
        )

    return session, orders_response.text


def fetch_orders_html_with_cookie(orders_url: str, cookie_header: str):
    if not cookie_header.strip():
        raise ValueError("Cookie-ul de sesiune este gol.")

    session = _build_session(cookie_header)
    response = session.get(orders_url, timeout=30)
    response.raise_for_status()

    if "/user/login" in response.url or "Log in" in response.text[:2000]:
        raise ValueError("Cookie invalid/expirat. Copiază din nou cookie-ul de sesiune după login manual.")

    return session, response.text


def _extract_created_date_from_row_text(text: str):
    text = " ".join(text.split())
    patterns = [
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*\d{2}/\d{2}/\d{4}\s*-\s*\d{1,2}:\d{2}",
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\s*-\s*\d{1,2}:\d{2}",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            dt = _parse_cfmoto_datetime(m.group(0))
            if dt:
                return dt.strftime("%Y-%m-%d %H:%M")
    return None


def extract_order_entries(orders_html: str):
    soup = BeautifulSoup(orders_html, "html.parser")
    entries = []

    order_patterns = [
        r"/user/\d+/orders/\d+",
        r"/orders/\d+",
        r"/order/\d+",
    ]

    for row in soup.select("table tr"):
        row_text = row.get_text(" ", strip=True)
        created_at = _extract_created_date_from_row_text(row_text)
        for a in row.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True)
            if any(re.search(pattern, href) for pattern in order_patterns) or re.match(r"^\d{4}-\d+", text):
                entries.append({"link": urljoin(BASE_URL, href), "created_at": created_at})

    if not entries:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True)
            if any(re.search(pattern, href) for pattern in order_patterns) or re.match(r"^\d{4}-\d+", text):
                entries.append({"link": urljoin(BASE_URL, href), "created_at": None})

    seen = set()
    deduped = []
    for e in entries:
        if e["link"] not in seen:
            deduped.append(e)
            seen.add(e["link"])
    return deduped


def extract_order_links(orders_html: str):
    return [e["link"] for e in extract_order_entries(orders_html)]


def _import_order_links_into_db(conn: sqlite3.Connection, session: requests.Session, order_entries, limit: int):
    if not order_entries:
        raise ValueError("Nu am găsit linkuri de comenzi în pagina de orders.")

    created_orders = 0
    existing_orders = 0
    updated_orders = 0
    total_parts = 0
    errors = []

    for entry in order_entries[:limit]:
        link = entry["link"] if isinstance(entry, dict) else str(entry)
        created_at = entry.get("created_at") if isinstance(entry, dict) else None
        try:
            order_resp = session.get(link, timeout=30)
            order_resp.raise_for_status()
            number, added, state = parse_html_and_insert(conn, order_resp.text, forced_order_date=created_at)
            if state == "exists":
                existing_orders += 1
            elif state == "updated":
                updated_orders += 1
            else:
                created_orders += 1
                total_parts += added
        except Exception as exc:
            errors.append(f"{link} -> {exc}")

    return {
        "total_links": len(order_entries),
        "imported": created_orders,
        "existing": existing_orders,
        "updated": updated_orders,
        "parts": total_parts,
        "errors": errors,
    }


def import_orders_from_account(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    orders_url: str = ORDERS_URL,
    limit: int = 20,
):
    session, _ = login_and_fetch_orders_html(username, password, orders_url)
    order_entries = _collect_order_links_from_pages(session, orders_url, limit=limit, max_pages=120)
    return _import_order_links_into_db(conn, session, order_entries, limit)


def import_orders_from_cookie(
    conn: sqlite3.Connection,
    cookie_header: str,
    orders_url: str = ORDERS_URL,
    limit: int = 20,
):
    session, _ = fetch_orders_html_with_cookie(orders_url, cookie_header)
    order_entries = _collect_order_links_from_pages(session, orders_url, limit=limit, max_pages=120)
    return _import_order_links_into_db(conn, session, order_entries, limit)


def _normalize_order_page_url(base_orders_url: str, page: int):
    parsed = urlparse(base_orders_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page)]
    new_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def _collect_order_links_from_pages(session: requests.Session, orders_url: str, limit: int, max_pages: int = 50):
    all_entries = []
    seen = set()

    pages_to_scan = max(1, min(max_pages, (limit // 20) + 2))
    for page in range(pages_to_scan):
        page_url = _normalize_order_page_url(orders_url, page)
        resp = session.get(page_url, timeout=30)
        resp.raise_for_status()
        page_entries = extract_order_entries(resp.text)

        if not page_entries and page > 0:
            break

        added_this_page = 0
        for entry in page_entries:
            link = entry["link"]
            if link not in seen:
                seen.add(link)
                all_entries.append(entry)
                added_this_page += 1
                if len(all_entries) >= limit:
                    return all_entries

        if page > 0 and added_this_page == 0:
            break

    return all_entries


def _extract_availability(text: str):
    lowered = text.lower()
    if "not in stock" in lowered:
        m = re.search(r"not in stock[^\n]*", text, flags=re.I)
        return m.group(0).strip() if m else "not in stock"
    if "sufficient stock" in lowered:
        return "sufficient stock"
    return ""


def _extract_price_from_text(text: str):
    m = re.search(r"(\d+[\d.,]*)\s*€", text)
    if not m:
        m = re.search(r"€\s*(\d+[\d.,]*)", text)
    if not m:
        return None

    raw = m.group(1).replace(" ", "")
    if "," in raw and "." in raw:
        # keep the last separator as decimal marker
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")

    try:
        return float(raw)
    except ValueError:
        return None


def _parse_cfmoto_datetime(raw: str):
    raw = " ".join(raw.replace(" ", " ").split())
    patterns = [
        "%A, %B %d, %Y - %H:%M",   # Thursday, March 5, 2026 - 12:20
        "%a, %m/%d/%Y - %H:%M",    # Thu, 03/05/2026 - 12:20
    ]
    for fmt in patterns:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _extract_order_placed_date(soup: BeautifulSoup):
    # 1) try explicit Invoice date label context
    invoice_label = soup.find(string=re.compile(r"invoice\s*date", re.I))
    if invoice_label:
        candidates = []
        node = invoice_label.parent
        if node:
            candidates.append(node.get_text(" ", strip=True))
            if node.next_sibling and hasattr(node.next_sibling, "get_text"):
                candidates.append(node.next_sibling.get_text(" ", strip=True))
        for c in candidates:
            c = re.sub(r"(?i)invoice\s*date\s*:?", "", c).strip()
            dt = _parse_cfmoto_datetime(c)
            if dt:
                return dt.strftime("%Y-%m-%d %H:%M")

    # 2) fallback from full page text
    page_text = soup.get_text("\n", strip=True)
    patterns = [
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\s*-\s*\d{1,2}:\d{2}",
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*\d{2}/\d{2}/\d{4}\s*-\s*\d{1,2}:\d{2}",
    ]
    for pat in patterns:
        match = re.search(pat, page_text)
        if match:
            dt = _parse_cfmoto_datetime(match.group(0))
            if dt:
                return dt.strftime("%Y-%m-%d %H:%M")

    return datetime.now().strftime("%Y-%m-%d")


def parse_html_and_insert(conn: sqlite3.Connection, html_text: str, forced_order_date=None):
    cursor = conn.cursor()
    soup = BeautifulSoup(html_text, "html.parser")

    order_title = soup.find("h1", class_="page-header")
    if not order_title:
        raise ValueError("Nu am găsit numărul comenzii în HTML.")

    order_number = order_title.text.strip().replace("Order ", "")

    cursor.execute("SELECT id, data_plasare FROM comenzi WHERE order_number = ?", (order_number,))
    existing = cursor.fetchone()
    if existing:
        if forced_order_date and existing["data_plasare"] != forced_order_date:
            cursor.execute("UPDATE comenzi SET data_plasare = ? WHERE order_number = ?", (forced_order_date, order_number))
            conn.commit()
            return order_number, 0, "updated"
        return order_number, 0, "exists"

    data = forced_order_date or _extract_order_placed_date(soup)
    cursor.execute(
        "INSERT INTO comenzi (order_number, data_plasare, tip) VALUES (?, ?, 'plasata')",
        (order_number, data),
    )
    comanda_id = cursor.lastrowid

    table = soup.find("table", class_="views-table")
    added = 0
    if table and table.find("tbody"):
        for row in table.find("tbody").find_all("tr"):
            cols = row.find_all("td")
            if len(cols) >= 4:
                raw_desc = cols[1].get_text(" ", strip=True)
                disponibilitate = _extract_availability(raw_desc)
                nume = raw_desc.replace("sufficient stock", "").strip()
                cod_match = re.search(r"\(([\w-]+)\)", nume)
                cod = cod_match.group(1) if cod_match else ""

                qty_cell = cols[3].get_text(" ", strip=True) if len(cols) > 3 else "1"
                try:
                    cant = float(re.sub(r"[^0-9.,-]", "", qty_cell).replace(",", ".") or "1")
                except (ValueError, TypeError):
                    cant = 1.0

                price_text = " ".join(c.get_text(" ", strip=True) for c in cols)
                pret = _extract_price_from_text(price_text)

                cursor.execute(
                    """
                    INSERT INTO piese (comanda_id, nume_piesa, cod, cantitate, status, pret_unitar, disponibilitate_plasare)
                    VALUES (?, ?, ?, ?, 'asteptata', ?, ?)
                    """,
                    (comanda_id, nume, cod, cant, pret, disponibilitate),
                )
                added += 1

    conn.commit()
    return order_number, added, "created"


def _extract_invoice_number(text: str):
    patterns = [
        r"Serie/Numar:\s*([A-Z]{1,6}\s?[A-Z]{0,6}/\d+)",
        r"Nr\.?\s*factura[:\s]*([A-Z0-9\-/]+)",
        r"Invoice\s*(?:No\.?|#|Number)[:\s]*([A-Z0-9\-/]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    return None


def _extract_invoice_date(text: str):
    patterns = [
        r"Data:\s*(\d{2}\.\d{2}\.\d{4})",
        r"Date:\s*(\d{2}[./-]\d{2}[./-]\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).replace("/", ".").replace("-", ".")
    return datetime.now().strftime("%d.%m.%Y")


def parse_pdf_and_insert(conn: sqlite3.Connection, file_bytes: bytes):
    cursor = conn.cursor()
    added = 0

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    order_number = _extract_invoice_number(text)
    if not order_number:
        raise ValueError("Nu am găsit numărul facturii în PDF.")

    cursor.execute("SELECT id FROM comenzi WHERE order_number = ?", (order_number,))
    if cursor.fetchone():
        return order_number, 0, "exists"

    data = _extract_invoice_date(text)

    cursor.execute(
        "INSERT INTO comenzi (order_number, data_plasare, tip, note) VALUES (?, ?, 'viitoare', 'Din PDF')",
        (order_number, data),
    )
    comanda_id = cursor.lastrowid

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                header_skipped = False
                for row in table:
                    if not row:
                        continue
                    if not header_skipped and "Nr" in str(row[0]):
                        header_skipped = True
                        continue

                    if len(row) < 5:
                        continue

                    if not row[0] or not re.match(r"^\d+$", str(row[0]).strip()):
                        continue

                    cod = str(row[1] or "").strip()
                    den = str(row[2] or "").strip().replace("...nedefinita...", "").strip()
                    nume = f"{cod} {den}".strip()

                    cant_idx = 4 if len(row) > 4 else 3
                    cant_str = str(row[cant_idx] or "0").replace(",", ".")

                    try:
                        cant = float(re.sub(r"[^0-9.\-]", "", cant_str) or "0")
                    except (ValueError, TypeError):
                        cant = 1.0

                    if cant > 0 and nume:
                        cursor.execute(
                            """
                            INSERT INTO piese (comanda_id, nume_piesa, cod, cantitate, status)
                            VALUES (?, ?, ?, ?, 'in_tranzit')
                            """,
                            (comanda_id, nume, cod, cant),
                        )
                        added += 1

    conn.commit()
    return order_number, added, "created"


def get_comenzi(conn: sqlite3.Connection, tip: str):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.id, c.order_number, c.data_plasare,
               COALESCE(SUM(p.cantitate - p.cantitate_primita), 0) AS lipsa
        FROM comenzi c LEFT JOIN piese p ON c.id = p.comanda_id
        WHERE c.tip = ?
        GROUP BY c.id
        HAVING lipsa > 0
        ORDER BY c.data_plasare DESC
        """,
        (tip,),
    )
    return cursor.fetchall()


def get_comenzi_by_order_number(conn: sqlite3.Connection, tip: str, order_query: str):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.id, c.order_number, c.data_plasare,
               COALESCE(SUM(p.cantitate - p.cantitate_primita), 0) AS lipsa
        FROM comenzi c LEFT JOIN piese p ON c.id = p.comanda_id
        WHERE c.tip = ? AND c.order_number LIKE ?
        GROUP BY c.id
        ORDER BY c.data_plasare DESC
        """,
        (tip, f"%{order_query.strip()}%"),
    )
    return cursor.fetchall()


def get_comanda_id_by_order_number(conn: sqlite3.Connection, order_number: str):
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM comenzi WHERE order_number = ?", (order_number.strip(),))
    row = cursor.fetchone()
    return row["id"] if row else None


def get_piese_for_comanda(conn: sqlite3.Connection, comanda_id: int, query_text: str = ""):
    cursor = conn.cursor()
    query = """
        SELECT id, cod, nume_piesa, cantitate, cantitate_primita,
               (cantitate - cantitate_primita) AS lipsa, status, data_primire,
               pret_unitar, disponibilitate_plasare
        FROM piese
        WHERE comanda_id = ?
    """
    params = [comanda_id]

    if query_text.strip():
        query += " AND (cod LIKE ? OR nume_piesa LIKE ?)"
        pattern = f"%{query_text.strip()}%"
        params += [pattern, pattern]

    query += " ORDER BY status, nume_piesa"
    cursor.execute(query, params)
    return cursor.fetchall()


def get_raport_asteptate(conn: sqlite3.Connection):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.order_number, c.data_plasare, p.nume_piesa, p.cod,
               (p.cantitate - p.cantitate_primita) AS lipsa, p.status
        FROM piese p JOIN comenzi c ON p.comanda_id = c.id
        WHERE p.cantitate > p.cantitate_primita
        ORDER BY c.data_plasare DESC, lipsa DESC
        """
    )
    return cursor.fetchall()




def search_piesa_in_comenzi(conn: sqlite3.Connection, cod_query: str):
    cursor = conn.cursor()
    pattern = f"%{cod_query.strip()}%"
    cursor.execute(
        """
        SELECT c.order_number, c.data_plasare, p.cod, p.nume_piesa, p.cantitate,
               p.pret_unitar, p.disponibilitate_plasare
        FROM piese p
        JOIN comenzi c ON c.id = p.comanda_id
        WHERE p.cod LIKE ?
        ORDER BY c.data_plasare DESC, c.order_number DESC
        """,
        (pattern,),
    )
    return cursor.fetchall()


def format_piese_rows(rows):
    def row_get(row, key, idx, default=None):
        if isinstance(row, sqlite3.Row):
            value = row[key]
        elif isinstance(row, dict):
            value = row.get(key, default)
        else:
            value = row[idx] if len(row) > idx else default
        return default if value is None else value

    data = []
    for row in rows:
        comandate = float(row_get(row, "cantitate", 3, 0))
        venite = float(row_get(row, "cantitate_primita", 4, 0))
        asteptate = float(row_get(row, "lipsa", 5, comandate - venite))
        pret = row_get(row, "pret_unitar", 8, None)
        data.append(
            {
                "Cod piesă": str(row_get(row, "cod", 1, "")),
                "Denumire": str(row_get(row, "nume_piesa", 2, "")),
                "Comandate": comandate,
                "Așteptate": asteptate,
                "Venite": venite,
                "Preț unitar": pret,
                "Disponibilitate la plasare": row_get(row, "disponibilitate_plasare", 9, "") or "-",
            }
        )
    return data


def main():
    st.set_page_config(page_title="Monitor Comenzi CFMoto Parts", layout="wide")
    st.title("Monitorizare Comenzi CFMOTO")

    conn = get_connection()
    init_db(conn)

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Încarcă comandă plasată (HTML)")
        html_file = st.file_uploader("Fișier HTML", type=["html"], key="html")
        if st.button("Importă HTML", use_container_width=True):
            if not html_file:
                st.warning("Alege un fișier HTML.")
            else:
                try:
                    html_file.seek(0)
                    html_text = html_file.read().decode("utf-8", errors="ignore")
                    number, added, state = parse_html_and_insert(conn, html_text)
                    if state == "exists":
                        st.info(f"{number} există deja.")
                    else:
                        st.success(f"Comanda {number} a fost adăugată cu {added} piese.")
                except Exception as exc:
                    st.error(f"Eroare la import HTML: {exc}")

    with col2:
        st.subheader("Încarcă invoice viitoare (PDF)")
        pdf_file = st.file_uploader("Fișier PDF", type=["pdf"], key="pdf")
        if st.button("Importă PDF", use_container_width=True):
            if not pdf_file:
                st.warning("Alege un fișier PDF.")
            else:
                try:
                    pdf_file.seek(0)
                    file_bytes = pdf_file.read()
                    number, added, state = parse_pdf_and_insert(conn, file_bytes)
                    if state == "exists":
                        st.info(f"{number} există deja.")
                    else:
                        st.success(f"Factura {number} a fost adăugată cu {added} piese viitoare.")
                except Exception as exc:
                    st.error(f"Eroare la import PDF: {exc}")

    st.subheader("Sincronizare comenzi din cfmotoparts.eu")
    st.info(
        "Se face login la https://cfmotoparts.eu/user/login, apoi se citește lista din orders și fiecare link din Order number. "
        "Dacă apare CAPTCHA, folosește metoda cu cookie de sesiune (login manual în browser)."
    )

    sync_mode = st.radio(
        "Metodă sincronizare",
        ["Login direct (fără CAPTCHA)", "Cookie de sesiune (compatibil CAPTCHA)"],
        horizontal=True,
    )

    with st.form("cfmoto_sync_form"):
        sync_orders_url = st.text_input("URL listă comenzi", value=ORDERS_URL)
        sync_limit = st.number_input("Număr maxim comenzi de importat", min_value=1, max_value=1000, value=200, step=1)

        if sync_mode == "Login direct (fără CAPTCHA)":
            sync_user = st.text_input("User / Email cfmotoparts.eu")
            sync_pass = st.text_input("Parolă", type="password")
            sync_cookie = ""
        else:
            sync_user = ""
            sync_pass = ""
            sync_cookie = st.text_area(
                "Cookie header din browser",
                placeholder="Ex: SESSxxxx=...; has_js=1; ...",
                height=110,
            )

        sync_submit = st.form_submit_button("Import comenzi")

    if sync_submit:
        with st.spinner("Import în curs..."):
            try:
                if sync_mode == "Login direct (fără CAPTCHA)":
                    if not sync_user or not sync_pass:
                        raise ValueError("Completează user și parolă.")
                    result = import_orders_from_account(
                        conn,
                        sync_user,
                        sync_pass,
                        orders_url=sync_orders_url.strip() or ORDERS_URL,
                        limit=int(sync_limit),
                    )
                else:
                    result = import_orders_from_cookie(
                        conn,
                        sync_cookie,
                        orders_url=sync_orders_url.strip() or ORDERS_URL,
                        limit=int(sync_limit),
                    )

                st.success(
                    f"Import gata. Noi: {result['imported']} | Actualizate: {result.get('updated', 0)} | Existente: {result['existing']} | "
                    f"Piese noi: {result['parts']} | Linkuri detectate: {result['total_links']}"
                )
                if result["errors"]:
                    st.warning("Unele comenzi nu au putut fi importate:")
                    for err in result["errors"][:10]:
                        st.write(f"- {err}")
            except Exception as exc:
                st.error(f"Eroare la sincronizare: {exc}")

    tab1, tab2, tab3, tab4 = st.tabs(["Comenzi plasate (HTML)", "Invoice viitoare (PDF)", "Raport așteptate", "Căutare cod piesă"])

    with tab1:
        order_q1 = st.text_input("Caută după ID comandă CFMoto (ex: 2026-543)", key="order_q_plasata")
        rows = get_comenzi_by_order_number(conn, "plasata", order_q1) if order_q1.strip() else get_comenzi(conn, "plasata")
        st.dataframe(
            [
                {
                    "ID": row["id"],
                    "Comandă": row["order_number"],
                    "Data": row["data_plasare"],
                    "Piese lipsă (buc)": int(row["lipsa"]),
                }
                for row in rows
            ],
            use_container_width=True,
        )

        selected_order_no = st.text_input("Vezi detalii după ID comandă CFMoto", key="det_order_plasata")
        selected_id = 0
        if selected_order_no.strip():
            selected_id = get_comanda_id_by_order_number(conn, selected_order_no) or 0
            if selected_id == 0:
                st.warning("Nu am găsit comanda cu acest ID CFMoto.")

        selected_id_num = st.number_input("Sau vezi detalii comandă (ID local)", min_value=0, step=1, value=0, key="det_plasata")
        selected_id = selected_id or int(selected_id_num)
        if selected_id > 0:
            query_text = st.text_input("Caută piesă (cod / denumire)", key="q_plasata")
            detalii = get_piese_for_comanda(conn, selected_id, query_text)
            st.dataframe(format_piese_rows(detalii), use_container_width=True)

    with tab2:
        order_q2 = st.text_input("Caută după ID comandă/factură CFMoto", key="order_q_viitoare")
        rows = get_comenzi_by_order_number(conn, "viitoare", order_q2) if order_q2.strip() else get_comenzi(conn, "viitoare")
        st.dataframe(
            [
                {
                    "ID": row["id"],
                    "Factură": row["order_number"],
                    "Data": row["data_plasare"],
                    "Piese lipsă (buc)": int(row["lipsa"]),
                }
                for row in rows
            ],
            use_container_width=True,
        )

        selected_order_no = st.text_input("Vezi detalii după ID comandă/factură CFMoto", key="det_order_viitoare")
        selected_id = 0
        if selected_order_no.strip():
            selected_id = get_comanda_id_by_order_number(conn, selected_order_no) or 0
            if selected_id == 0:
                st.warning("Nu am găsit comanda/factura cu acest ID CFMoto.")

        selected_id_num = st.number_input("Sau vezi detalii factură (ID local)", min_value=0, step=1, value=0, key="det_viitoare")
        selected_id = selected_id or int(selected_id_num)
        if selected_id > 0:
            query_text = st.text_input("Caută piesă (cod / denumire)", key="q_viitoare")
            detalii = get_piese_for_comanda(conn, selected_id, query_text)
            st.dataframe(format_piese_rows(detalii), use_container_width=True)

    with tab3:
        rows = get_raport_asteptate(conn)
        if not rows:
            st.info("Nimic în așteptare.")
        else:
            lines = []
            current_com = None
            for r in rows:
                if r["order_number"] != current_com:
                    lines.append(f"\n### {r['order_number']} ({r['data_plasare']})")
                    current_com = r["order_number"]
                status_color = "🔴" if r["status"] == "asteptata" else "🟡" if r["status"] == "in_tranzit" else "🟢"
                cod = r["cod"] or "fără cod"
                lines.append(f"- {status_color} {r['nume_piesa']} ({cod}) — lipsă {r['lipsa']:.0f} buc")
            st.markdown("\n".join(lines))


    with tab4:
        st.subheader("Caută cod piesă în toate comenzile")
        cod_query = st.text_input("Cod piesă căutat", key="global_cod_query")
        if cod_query.strip():
            results = search_piesa_in_comenzi(conn, cod_query)
            if not results:
                st.info("Nu am găsit codul în comenzile importate.")
            else:
                st.dataframe(
                    [
                        {
                            "Nr comandă": r["order_number"],
                            "Data comandă": r["data_plasare"],
                            "Cod piesă": r["cod"],
                            "Denumire": r["nume_piesa"],
                            "Unități comandate": float(r["cantitate"]),
                            "Preț unitar": r["pret_unitar"],
                            "Disponibilitate la plasare": r["disponibilitate_plasare"] or "-",
                        }
                        for r in results
                    ],
                    use_container_width=True,
                )

    st.caption(
        "Pentru import masiv (până la 1000), aplicația parcurge și paginile următoare din orders (?page=1,2,3...). "
        "Pentru conturi cu CAPTCHA, fă login manual în browser și folosește importul cu cookie de sesiune."
    )

    conn.close()


if __name__ == "__main__":
    main()
