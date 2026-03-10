import io
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import pdfplumber
import psycopg2
import requests
import streamlit as st
from bs4 import BeautifulSoup
from psycopg2.extras import RealDictCursor

BASE_URL = "https://cfmotoparts.eu"
ORDERS_URL = f"{BASE_URL}/user/201/orders?order=created&sort=desc"


def get_connection():
    return psycopg2.connect(
        host=st.secrets["SUPABASE_DB_HOST"],
        dbname=st.secrets["SUPABASE_DB_NAME"],
        user=st.secrets["SUPABASE_DB_USER"],
        password=st.secrets["SUPABASE_DB_PASSWORD"],
        port=st.secrets["SUPABASE_DB_PORT"],
        sslmode="require",
        cursor_factory=RealDictCursor,
    )


def init_db(conn) -> None:
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS comenzi (
            id BIGSERIAL PRIMARY KEY,
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
            id BIGSERIAL PRIMARY KEY,
            comanda_id BIGINT NOT NULL REFERENCES comenzi(id) ON DELETE CASCADE,
            nume_piesa TEXT NOT NULL,
            cod TEXT,
            cantitate DOUBLE PRECISION NOT NULL,
            cantitate_primita DOUBLE PRECISION DEFAULT 0.0,
            data_primire TEXT,
            status TEXT DEFAULT 'asteptata',
            pret_unitar DOUBLE PRECISION,
            disponibilitate_plasare TEXT
        )
        """
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_comenzi_order_number ON comenzi(order_number)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_comenzi_tip ON comenzi(tip)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_piese_comanda_id ON piese(comanda_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_piese_cod ON piese(cod)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_piese_status ON piese(status)"
    )

    conn.commit()


def _extract_csrf_login_fields(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    form = soup.find("form", id="user-login") or soup.find(
        "form", attrs={"action": "/user/login"}
    )
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
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    if cookie_header.strip():
        session.headers.update({"Cookie": cookie_header.strip()})
    return session


def login_and_fetch_orders_html(
    username: str,
    password: str,
    orders_url: str = ORDERS_URL,
):
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
        match = re.search(pat, text)
        if match:
            dt = _parse_cfmoto_datetime(match.group(0))
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
    for entry in entries:
        if entry["link"] not in seen:
            deduped.append(entry)
            seen.add(entry["link"])
    return deduped


def extract_order_links(orders_html: str):
    return [e["link"] for e in extract_order_entries(orders_html)]


def _import_order_links_into_db(conn, session: requests.Session, order_entries, limit: int):
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
            number, added, state = parse_html_and_insert(
                conn, order_resp.text, forced_order_date=created_at
            )
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
    conn,
    username: str,
    password: str,
    orders_url: str = ORDERS_URL,
    limit: int = 20,
):
    session, _ = login_and_fetch_orders_html(username, password, orders_url)
    order_entries = _collect_order_links_from_pages(session, orders_url, limit=limit, max_pages=120)
    return _import_order_links_into_db(conn, session, order_entries, limit)


def import_orders_from_cookie(
    conn,
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
        match = re.search(r"not in stock[^\n]*", text, flags=re.I)
        return match.group(0).strip() if match else "not in stock"
    if "sufficient stock" in lowered:
        return "sufficient stock"
    return ""


def _extract_price_from_text(text: str):
    match = re.search(r"(\d+[\d.,]*)\s*€", text)
    if not match:
        match = re.search(r"€\s*(\d+[\d.,]*)", text)
    if not match:
        return None

    raw = match.group(1).replace(" ", "")
    if "," in raw and "." in raw:
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
    raw = " ".join(raw.replace(" ", " ").split())
    patterns = [
        "%A, %B %d, %Y - %H:%M",
        "%a, %m/%d/%Y - %H:%M",
    ]
    for fmt in patterns:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _extract_order_placed_date(soup: BeautifulSoup):
    invoice_label = soup.find(string=re.compile(r"invoice\s*date", re.I))
    if invoice_label:
        candidates = []
        node = invoice_label.parent
        if node:
            candidates.append(node.get_text(" ", strip=True))
            if node.next_sibling and hasattr(node.next_sibling, "get_text"):
                candidates.append(node.next_sibling.get_text(" ", strip=True))
        for candidate in candidates:
            candidate = re.sub(r"(?i)invoice\s*date\s*:?", "", candidate).strip()
            dt = _parse_cfmoto_datetime(candidate)
            if dt:
                return dt.strftime("%Y-%m-%d %H:%M")

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


def parse_html_and_insert(conn, html_text: str, forced_order_date: Optional[str] = None):
    cursor = conn.cursor()
    soup = BeautifulSoup(html_text, "html.parser")

    order_title = soup.find("h1", class_="page-header")
    if not order_title:
        raise ValueError("Nu am găsit numărul comenzii în HTML.")

    order_number = order_title.text.strip().replace("Order ", "")

    cursor.execute(
        "SELECT id, data_plasare FROM comenzi WHERE order_number = %s",
        (order_number,),
    )
    existing = cursor.fetchone()
    if existing:
        if forced_order_date and existing["data_plasare"] != forced_order_date:
            cursor.execute(
                "UPDATE comenzi SET data_plasare = %s WHERE order_number = %s",
                (forced_order_date, order_number),
            )
            conn.commit()
            return order_number, 0, "updated"
        return order_number, 0, "exists"

    data = forced_order_date or _extract_order_placed_date(soup)
    cursor.execute(
        """
        INSERT INTO comenzi (order_number, data_plasare, tip)
        VALUES (%s, %s, 'plasata')
        RETURNING id
        """,
        (order_number, data),
    )
    comanda_id = cursor.fetchone()["id"]

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
                    INSERT INTO piese (
                        comanda_id, nume_piesa, cod, cantitate, status, pret_unitar, disponibilitate_plasare
                    )
                    VALUES (%s, %s, %s, %s, 'asteptata', %s, %s)
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


def parse_pdf_and_insert(conn, file_bytes: bytes):
    cursor = conn.cursor()
    added = 0

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    order_number = _extract_invoice_number(text)
    if not order_number:
        raise ValueError("Nu am găsit numărul facturii în PDF.")

    cursor.execute(
        "SELECT id FROM comenzi WHERE order_number = %s",
        (order_number,),
    )
    if cursor.fetchone():
        return order_number, 0, "exists"

    data = _extract_invoice_date(text)

    cursor.execute(
        """
        INSERT INTO comenzi (order_number, data_plasare, tip, note)
        VALUES (%s, %s, 'viitoare', 'Din PDF')
        RETURNING id
        """,
        (order_number, data),
    )
    comanda_id = cursor.fetchone()["id"]

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
                            VALUES (%s, %s, %s, %s, 'in_tranzit')
                            """,
                            (comanda_id, nume, cod, cant),
                        )
                        added += 1

    conn.commit()
    return order_number, added, "created"


def get_comenzi(conn, tip: str):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.id, c.order_number, c.data_plasare,
               COALESCE(SUM(p.cantitate - p.cantitate_primita), 0) AS lipsa
        FROM comenzi c
        LEFT JOIN piese p ON c.id = p.comanda_id
        WHERE c.tip = %s
        GROUP BY c.id, c.order_number, c.data_plasare
        HAVING COALESCE(SUM(p.cantitate - p.cantitate_primita), 0) > 0
        ORDER BY c.data_plasare DESC
        """,
        (tip,),
    )
    return cursor.fetchall()


def get_comenzi_by_order_number(conn, tip: str, order_query: str):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.id, c.order_number, c.data_plasare,
               COALESCE(SUM(p.cantitate - p.cantitate_primita), 0) AS lipsa
        FROM comenzi c
        LEFT JOIN piese p ON c.id = p.comanda_id
        WHERE c.tip = %s AND c.order_number LIKE %s
        GROUP BY c.id, c.order_number, c.data_plasare
        ORDER BY c.data_plasare DESC
        """,
        (tip, f"%{order_query.strip()}%"),
    )
    return cursor.fetchall()


def get_comanda_id_by_order_number(conn, order_number: str):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM comenzi WHERE order_number = %s",
        (order_number.strip(),),
    )
    row = cursor.fetchone()
    return row["id"] if row else None


def get_piese_for_comanda(conn, comanda_id: int, query_text: str = ""):
    cursor = conn.cursor()
    query = """
        SELECT id, cod, nume_piesa, cantitate, cantitate_primita,
               (cantitate - cantitate_primita) AS lipsa, status, data_primire,
               pret_unitar, disponibilitate_plasare
        FROM piese
        WHERE comanda_id = %s
    """
    params = [comanda_id]

    if query_text.strip():
        query += " AND (cod LIKE %s OR nume_piesa LIKE %s)"
        pattern = f"%{query_text.strip()}%"
        params += [pattern, pattern]

    query += " ORDER BY status, nume_piesa"
    cursor.execute(query, tuple(params))
    return cursor.fetchall()


def _normalize_scanned_code(raw: str):
    text = (raw or "").strip()
    if not text:
        return "", 0.0

    match = re.match(r"^(.+?)\*(\d+)$", text)
    if match:
        code = match.group(1).strip()
        qty = float(match.group(2))
    else:
        code = text
        qty = 1.0
    return code, qty


def _update_piece_received(conn, piesa_id: int, qty: float):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT cantitate, cantitate_primita
        FROM piese
        WHERE id = %s
        """,
        (piesa_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError("Piesa nu există.")

    total = float(row["cantitate"])
    primita_curent = float(row["cantitate_primita"])
    primita_nou = min(total, primita_curent + max(0.0, qty))

    if primita_nou >= total:
        status = "primita"
    elif primita_nou > 0:
        status = "in_tranzit"
    else:
        status = "asteptata"

    data_primire = datetime.now().strftime("%Y-%m-%d %H:%M") if primita_nou > 0 else None
    cursor.execute(
        """
        UPDATE piese
        SET cantitate_primita = %s, status = %s, data_primire = %s
        WHERE id = %s
        """,
        (primita_nou, status, data_primire, piesa_id),
    )
    conn.commit()


def apply_received_by_code(
    conn,
    comanda_id: int,
    scanned: str,
    qty_override: Optional[float] = None,
):
    code, qty_from_scan = _normalize_scanned_code(scanned)
    if not code:
        raise ValueError("Cod scanat gol.")

    qty = qty_override if qty_override is not None else qty_from_scan
    if qty <= 0:
        raise ValueError("Cantitatea primită trebuie să fie > 0.")

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, cantitate, cantitate_primita
        FROM piese
        WHERE comanda_id = %s
          AND UPPER(TRIM(cod)) = UPPER(TRIM(%s))
        ORDER BY id
        """,
        (comanda_id, code),
    )
    rows = cursor.fetchall()
    if not rows:
        raise ValueError(f"Codul {code} nu există în comanda selectată.")

    remaining = float(qty)
    updated = 0
    for row in rows:
        total = float(row["cantitate"])
        got = float(row["cantitate_primita"])
        lipsa = max(0.0, total - got)
        if lipsa <= 0:
            continue
        add_now = min(lipsa, remaining)
        if add_now > 0:
            _update_piece_received(conn, int(row["id"]), add_now)
            updated += 1
            remaining -= add_now
        if remaining <= 0:
            break

    if updated == 0:
        raise ValueError(f"Codul {code} există, dar piesele sunt deja complet primite.")

    return {
        "code": code,
        "qty_requested": qty,
        "qty_unapplied": remaining,
        "lines_updated": updated,
    }


def mark_piece_as_fully_received(conn, piesa_id: int):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT cantitate
        FROM piese
        WHERE id = %s
        """,
        (piesa_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError("Piesa nu există.")

    total = float(row["cantitate"])
    _update_piece_received(conn, piesa_id, total)


def mark_selected_pieces_received(conn, piece_ids):
    if not piece_ids:
        return 0

    updated = 0
    for piece_id in piece_ids:
        mark_piece_as_fully_received(conn, int(piece_id))
        updated += 1
    return updated


def mark_order_remaining_as_received(conn, comanda_id: int):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id
        FROM piese
        WHERE comanda_id = %s
          AND cantitate_primita < cantitate
        """,
        (comanda_id,),
    )
    rows = cursor.fetchall()
    piece_ids = [int(row["id"]) for row in rows]
    return mark_selected_pieces_received(conn, piece_ids)


def render_reception_panel(conn, selected_id: int, detalii, key_prefix: str):
    st.markdown("**Recepție piese (manual / scanner barcode / bife pe comandă)**")
    c1, c2 = st.columns(2)
    with c1:
        manual_code = st.text_input("Cod piesă pentru recepție", key=f"recv_code_{key_prefix}")
        manual_qty = st.number_input(
            "Cantitate primită",
            min_value=0.0,
            value=1.0,
            step=1.0,
            key=f"recv_qty_{key_prefix}",
        )
        if st.button("Marchează ca primite", key=f"recv_btn_{key_prefix}", use_container_width=True):
            try:
                res = apply_received_by_code(conn, selected_id, manual_code, float(manual_qty))
                st.success(f"Actualizat cod {res['code']} pe {res['lines_updated']} poziții.")
                st.rerun()
            except Exception as exc:
                st.error(f"Eroare recepție: {exc}")

    with c2:
        scanned_raw = st.text_input(
            "Scan barcode (ex: 5BYV-041033-1000*1)",
            key=f"scan_raw_{key_prefix}",
            help="Suffix-ul *1/*2/... este interpretat ca număr de bucăți.",
        )
        if st.button("Aplică scan", key=f"scan_btn_{key_prefix}", use_container_width=True):
            try:
                res = apply_received_by_code(conn, selected_id, scanned_raw)
                msg = f"Scan aplicat pentru {res['code']}. Linii actualizate: {res['lines_updated']}."
                if res["qty_unapplied"] > 0:
                    msg += f" Rămas nealocat: {res['qty_unapplied']:.0f} buc."
                st.success(msg)
                st.rerun()
            except Exception as exc:
                st.error(f"Eroare scan: {exc}")

    st.markdown("**Bifează piesele venite separat**")
    pending_rows = [row for row in detalii if float(row.get("lipsa", 0) or 0) > 0]
    if not pending_rows:
        st.info("Toate piesele din această comandă sunt deja recepționate.")
        return

    for row in pending_rows:
        cod = row.get("cod") or "fără cod"
        lipsa = float(row.get("lipsa", 0) or 0)
        label = f"{row['nume_piesa']} ({cod}) — lipsă {lipsa:.0f} buc"
        st.checkbox(label, key=f"recv_piece_{key_prefix}_{int(row['id'])}")

    b1, b2 = st.columns(2)
    with b1:
        if st.button("Marchează piesele bifate ca venite", key=f"mark_checked_{key_prefix}", use_container_width=True):
            try:
                selected_piece_ids = [
                    int(row["id"])
                    for row in pending_rows
                    if st.session_state.get(f"recv_piece_{key_prefix}_{int(row['id'])}", False)
                ]
                updated = mark_selected_pieces_received(conn, selected_piece_ids)
                if updated == 0:
                    st.warning("Nu ai bifat nicio piesă.")
                else:
                    st.success(f"Au fost recepționate integral {updated} piese selectate.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Eroare la recepția pe bază de bife: {exc}")

    with b2:
        if st.button("Bulk: marchează toată comanda ca venită", key=f"mark_bulk_{key_prefix}", use_container_width=True):
            try:
                updated = mark_order_remaining_as_received(conn, selected_id)
                if updated == 0:
                    st.info("Nu mai există piese în așteptare pentru această comandă.")
                else:
                    st.success(f"Recepție bulk finalizată: {updated} piese au fost marcate ca venite.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Eroare la recepția bulk: {exc}")


def get_raport_asteptate(conn):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.order_number, c.data_plasare, p.nume_piesa, p.cod,
               (p.cantitate - p.cantitate_primita) AS lipsa, p.status
        FROM piese p
        JOIN comenzi c ON p.comanda_id = c.id
        WHERE p.cantitate > p.cantitate_primita
        ORDER BY c.data_plasare DESC, lipsa DESC
        """
    )
    return cursor.fetchall()


def search_piesa_in_comenzi(conn, cod_query: str):
    cursor = conn.cursor()
    pattern = f"%{cod_query.strip()}%"
    cursor.execute(
        """
        SELECT c.order_number, c.data_plasare, p.cod, p.nume_piesa, p.cantitate,
               p.pret_unitar, p.disponibilitate_plasare
        FROM piese p
        JOIN comenzi c ON c.id = p.comanda_id
        WHERE p.cod LIKE %s
        ORDER BY c.data_plasare DESC, c.order_number DESC
        """,
        (pattern,),
    )
    return cursor.fetchall()


def format_piese_rows(rows):
    data = []
    for row in rows:
        comandate = float(row.get("cantitate", 0) or 0)
        venite = float(row.get("cantitate_primita", 0) or 0)
        asteptate = float(row.get("lipsa", comandate - venite) or 0)
        pret = row.get("pret_unitar")
        data.append(
            {
                "Cod piesă": str(row.get("cod", "")),
                "Denumire": str(row.get("nume_piesa", "")),
                "Comandate": comandate,
                "Așteptate": asteptate,
                "Venite": venite,
                "Preț unitar": pret,
                "Disponibilitate la plasare": row.get("disponibilitate_plasare") or "-",
            }
        )
    return data


def main():
    st.set_page_config(page_title="Monitor Comenzi CFMoto Parts", layout="wide")
    st.title("Monitorizare Comenzi CFMOTO")

    try:
        conn = get_connection()
        init_db(conn)
    except Exception as exc:
        st.error(f"Eroare conectare la baza de date: {exc}")
        st.stop()

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
                    elif state == "updated":
                        st.success(f"Comanda {number} a fost actualizată.")
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
        sync_limit = st.number_input(
            "Număr maxim comenzi de importat",
            min_value=1,
            max_value=1000,
            value=200,
            step=1,
        )

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
                    f"Import gata. Noi: {result['imported']} | Actualizate: {result.get('updated', 0)} | "
                    f"Existente: {result['existing']} | Piese noi: {result['parts']} | "
                    f"Linkuri detectate: {result['total_links']}"
                )
                if result["errors"]:
                    st.warning("Unele comenzi nu au putut fi importate:")
                    for err in result["errors"][:10]:
                        st.write(f"- {err}")
            except Exception as exc:
                st.error(f"Eroare la sincronizare: {exc}")

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Comenzi plasate (HTML)", "Invoice viitoare (PDF)", "Raport așteptate", "Căutare cod piesă"]
    )

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

        selected_id_num = st.number_input(
            "Sau vezi detalii comandă (ID local)",
            min_value=0,
            step=1,
            value=0,
            key="det_plasata",
        )
        selected_id = selected_id or int(selected_id_num)
        if selected_id > 0:
            query_text = st.text_input("Caută piesă (cod / denumire)", key="q_plasata")
            detalii = get_piese_for_comanda(conn, selected_id, query_text)
            st.dataframe(format_piese_rows(detalii), use_container_width=True)

            render_reception_panel(conn, selected_id, detalii, "plasata")

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

        selected_order_no = st.text_input(
            "Vezi detalii după ID comandă/factură CFMoto",
            key="det_order_viitoare",
        )
        selected_id = 0
        if selected_order_no.strip():
            selected_id = get_comanda_id_by_order_number(conn, selected_order_no) or 0
            if selected_id == 0:
                st.warning("Nu am găsit comanda/factura cu acest ID CFMoto.")

        selected_id_num = st.number_input(
            "Sau vezi detalii factură (ID local)",
            min_value=0,
            step=1,
            value=0,
            key="det_viitoare",
        )
        selected_id = selected_id or int(selected_id_num)
        if selected_id > 0:
            query_text = st.text_input("Caută piesă (cod / denumire)", key="q_viitoare")
            detalii = get_piese_for_comanda(conn, selected_id, query_text)
            st.dataframe(format_piese_rows(detalii), use_container_width=True)

            render_reception_panel(conn, selected_id, detalii, "viitoare")

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
