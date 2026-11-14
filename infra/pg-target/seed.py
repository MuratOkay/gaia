"""Create and populate Gaia's PostgreSQL development target (decision D25).

    python infra/pg-target/seed.py                 # scale 1 (~3 M rows), seed 42
    python infra/pg-target/seed.py --scale 0.1     # quick, ~0.3 M rows
    make pg-target-seed ARGS="--scale 5"

Environment (see .env.example):

    GAIA_PG_TARGET_ADMIN_URL   superuser/owner DSN of the *target* database,
                               e.g. postgresql://postgres:pw@127.0.0.1:5434/gaia_target_pg
    GAIA_PG_TARGET_USER        read-only role to provision (default gaia_ro)
    GAIA_PG_TARGET_PASSWORD    its password

What it does, in order: create the database if missing; apply schema.sql
(drops and recreates the fixture schemas); generate and COPY the data; apply
the deliberate imperfections (a NOT VALID foreign key with orphans, a NOT VALID
check with violations, disabled FK enforcement triggers on finance.invoices
followed by orphan rows); refresh the materialized view; ANALYZE; provision the read-only role
from readonly_role.sql.

Everything is deterministic for a given ``--seed`` and ``--scale``: no wall
clock, no unseeded randomness. Every personal datum is synthetic — e-mail
domains are the reserved example.com/.net/.org, national IDs and IBANs are
random but checksum-valid, and cards are stored as BIN + last four only.

This is a fixture tool, not part of the Gaia application: it lives under
infra/ and is never imported by the backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

HERE = Path(__file__).resolve().parent
try:  # same .env as the backend; real environment variables win
    from dotenv import load_dotenv

    load_dotenv(HERE.parents[1] / ".env", override=False)
except ImportError:  # pragma: no cover - python-dotenv ships with gaia-env
    pass
SCHEMA_SQL = HERE / "schema.sql"
ROLE_SQL = HERE / "readonly_role.sql"

START = date(2023, 1, 1)
DAYS = 1096  # 2023-01-01 .. 2025-12-31
CENT = Decimal("0.01")

FIRST_F = ["Ayse", "Fatma", "Zeynep", "Elif", "Emine", "Merve", "Esra", "Ozge", "Selin", "Deniz",
           "Ebru", "Gul", "Seda", "Derya", "Buse", "Irem", "Melis", "Nazli", "Pinar", "Tugba",
           "Anna", "Maria", "Sofia", "Laura", "Emma"]
FIRST_M = ["Mehmet", "Mustafa", "Ahmet", "Ali", "Huseyin", "Hasan", "Ibrahim", "Murat", "Emre",
           "Burak", "Can", "Kerem", "Onur", "Serkan", "Tolga", "Umut", "Volkan", "Yusuf", "Baris",
           "Cem", "John", "David", "Lukas", "Marco", "Peter"]
LAST = ["Yilmaz", "Kaya", "Demir", "Sahin", "Celik", "Yildiz", "Yildirim", "Ozturk", "Aydin",
        "Ozdemir", "Arslan", "Dogan", "Kilic", "Aslan", "Cetin", "Kara", "Koc", "Kurt", "Ozkan",
        "Simsek", "Polat", "Erdogan", "Acar", "Tekin", "Aksoy", "Schmidt", "Rossi", "Smith",
        "Muller", "Novak"]
CITIES = {
    "Istanbul": ["Kadikoy", "Besiktas", "Uskudar", "Sisli", "Bakirkoy", "Atasehir"],
    "Ankara": ["Cankaya", "Kecioren", "Yenimahalle", "Etimesgut"],
    "Izmir": ["Konak", "Karsiyaka", "Bornova", "Buca"],
    "Bursa": ["Osmangazi", "Nilufer", "Yildirim"],
    "Antalya": ["Muratpasa", "Konyaalti", "Kepez"],
    "Adana": ["Seyhan", "Cukurova"],
    "Konya": ["Selcuklu", "Meram"],
    "Gaziantep": ["Sahinbey", "Sehitkamil"],
    "Kayseri": ["Melikgazi", "Kocasinan"],
    "Eskisehir": ["Odunpazari", "Tepebasi"],
}
STREETS = ["Ataturk Cd.", "Cumhuriyet Cd.", "Istiklal Cd.", "Bagdat Cd.", "Gazi Blv.",
           "Inonu Cd.", "Fevzi Cakmak Sk.", "Mimar Sinan Sk.", "Lale Sk.", "Papatya Sk."]
DEPARTMENTS = [
    (1, "MGT", "Management", "CC-100"), (2, "SAL", "Sales", "CC-200"),
    (3, "MKT", "Marketing", "CC-210"), (4, "FIN", "Finance", "CC-300"),
    (5, "HRS", "Human Resources", "CC-310"), (6, "ITS", "Information Technology", "CC-400"),
    (7, "LOG", "Logistics", "CC-500"), (8, "CSV", "Customer Service", "CC-220"),
    (9, "PUR", "Purchasing", "CC-510"), (10, "STR", "Store Operations", "CC-230"),
    (11, "LEG", "Legal", "CC-320"), (12, "ANL", "Analytics", "CC-410"),
]
TITLES = {
    "MGT": ["Director", "Vice President"], "SAL": ["Sales Associate", "Sales Manager"],
    "MKT": ["Marketing Specialist", "Brand Manager"], "FIN": ["Accountant", "Financial Analyst"],
    "HRS": ["HR Specialist", "Recruiter"], "ITS": ["Software Engineer", "System Administrator"],
    "LOG": ["Warehouse Operator", "Logistics Planner"], "CSV": ["Support Agent", "Team Lead"],
    "PUR": ["Buyer", "Category Manager"], "STR": ["Store Manager", "Cashier", "Shelf Operator"],
    "LEG": ["Legal Counsel"], "ANL": ["Data Analyst", "Data Scientist"],
}
CATEGORIES = {
    "Electronics": ("EL", ["Phones", "Laptops", "Audio", "Cameras", "Accessories"], 20),
    "Home": ("HM", ["Kitchen", "Furniture", "Lighting", "Textiles", "Decor"], 20),
    "Fashion": ("FA", ["Women", "Men", "Kids", "Shoes", "Bags"], 10),
    "Beauty": ("BE", ["Skincare", "Makeup", "Haircare", "Fragrance", "Personal Care"], 20),
    "Sports": ("SP", ["Fitness", "Outdoor", "Cycling", "Team Sports", "Swimming"], 20),
    "Toys": ("TY", ["Baby", "Puzzles", "Outdoor Toys", "Dolls", "Building Sets"], 20),
    "Books": ("BK", ["Fiction", "Non-fiction", "Children", "Education", "Comics"], 0),
    "Grocery": ("GR", ["Snacks", "Beverages", "Breakfast", "Pantry", "Organic"], 1),
}
ADJ = ["Classic", "Smart", "Eco", "Pro", "Mini", "Ultra", "Soft", "Urban", "Premium", "Basic"]
NOUN = ["Speaker", "Lamp", "Jacket", "Bottle", "Backpack", "Kettle", "Chair", "Headset", "Mat",
        "Watch", "Towel", "Notebook", "Blender", "Sneaker", "Serum", "Puzzle", "Helmet", "Mug"]
SUPPLIER_WORDS = ["Anadolu", "Marmara", "Ege", "Toros", "Kapadokya", "Bosphorus", "Atlas",
                  "Delta", "Nova", "Orion", "Vega", "Zenith"]
SUPPLIER_SUFFIX = ["Tekstil", "Elektronik", "Gida", "Lojistik", "Ticaret", "Kozmetik", "Spor"]
EMAIL_DOMAINS = ["example.com", "example.net", "example.org"]
NOTE_TEMPLATES = [
    "Customer asked about delivery time for order {n}.",
    "Requested invoice copy by e-mail.",
    "Complained about packaging damage; coupon offered.",
    "Prefers evening deliveries.",
    "Address updated after phone call.",
    "Loyalty points manually adjusted ({n} points).",
]


# -- checksum-valid synthetic identifiers ------------------------------------------


def tckn(rng: random.Random) -> str:
    """Turkish national ID: 11 digits, first non-zero, two check digits."""
    d = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(8)]
    d10 = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    d11 = (sum(d) + d10) % 10
    return "".join(map(str, d + [d10, d11]))


def iban(rng: random.Random, country: str = "TR") -> str:
    """Checksum-valid IBAN (ISO 13616, mod 97) with a random BBAN."""
    if country == "TR":
        bban = f"{rng.randint(0, 99999):05d}0{rng.randint(0, 10**16 - 1):016d}"
    else:  # DE
        bban = f"{rng.randint(10**7, 10**8 - 1)}{rng.randint(0, 10**10 - 1):010d}"
    rearranged = bban + country + "00"
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    check = 98 - int(digits) % 97
    return f"{country}{check:02d}{bban}"


def ascii_lower(text: str) -> str:
    return text.lower().replace(" ", "")


# -- helpers ---------------------------------------------------------------------------


def day(offset: int) -> date:
    return START + timedelta(days=offset)


def ts(d: date, rng: random.Random, lo_hour: int = 8, hi_hour: int = 22) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(
        hours=rng.randint(lo_hour, hi_hour - 1), minutes=rng.randint(0, 59),
        seconds=rng.randint(0, 59)
    )


def money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def weighted(rng: random.Random, options: list[tuple[object, float]]):
    r = rng.random() * sum(w for _, w in options)
    for value, w in options:
        r -= w
        if r <= 0:
            return value
    return options[-1][0]


class Loader:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.counts: dict[str, int] = {}

    def copy(self, table: str, columns: list[str], rows) -> int:
        stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier(*table.split(".")),
            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        )
        n = 0
        with self.conn.cursor() as cur, cur.copy(stmt) as cp:
            for row in rows:
                cp.write_row(row)
                n += 1
        self.counts[table] = self.counts.get(table, 0) + n
        return n


# -- generation ---------------------------------------------------------------------------


def generate(conn: psycopg.Connection, scale: float, seed: int) -> dict[str, int]:
    rng = random.Random(seed)  # noqa: S311 - reproducible fixture data, not security
    L = Loader(conn)
    n_customers = max(500, int(25_000 * scale))
    n_orders = max(2_000, int(250_000 * scale))
    n_legacy_trx = max(1_000, int(200_000 * scale))

    # hr.departments
    L.copy("hr.departments", ["dept_id", "dept_code", "name", "cost_center"], DEPARTMENTS)

    # hr.employees (manager hierarchy: first employee per department manages it)
    employees = []
    by_dept: dict[str, list[int]] = {code: [] for _, code, _, _ in DEPARTMENTS}
    dept_ids = {code: did for did, code, _, _ in DEPARTMENTS}
    for eid in range(1, 601):
        code = DEPARTMENTS[0][1] if eid <= 3 else rng.choice(
            [c for _, c, _, _ in DEPARTMENTS for _ in range(8 if c in ("STR", "SAL") else 1)]
        )
        female = rng.random() < 0.5
        first = rng.choice(FIRST_F if female else FIRST_M)
        last = rng.choice(LAST)
        email = f"{ascii_lower(first)}.{ascii_lower(last)}{eid}@example.com"
        if rng.random() < 0.03:  # malformed: detector match rate will be < 100 %
            email = rng.choice([email.replace("@", " at "), email.split("@")[0], email + " "])
        hire = day(-rng.randint(0, 15 * 365))
        term = hire + timedelta(days=rng.randint(60, 3000)) if rng.random() < 0.08 else None
        if term and term > day(DAYS - 1):
            term = None
        title = rng.choice(TITLES[code])
        salary = Decimal(rng.randint(25_000, 160_000)) if "Manager" in title or code == "MGT" \
            else Decimal(rng.randint(17_000, 70_000))
        manager = (by_dept[code][0] if by_dept[code] else (1 if eid > 1 else None))
        is_mgr = not by_dept[code] or "Manager" in title or code == "MGT"
        by_dept[code].append(eid)
        employees.append([eid, f"E{eid:05d}", first, last, email, hire, term, dept_ids[code],
                          manager, title, salary, "EUR" if rng.random() < 0.02 else "TRY",
                          is_mgr])
    for e in rng.sample(employees[20:], 3):  # interns; violate the later NOT VALID check
        e[9], e[10] = "Intern", Decimal(0)
    L.copy("hr.employees",
           ["employee_id", "emp_no", "first_name", "last_name", "email", "hire_date",
            "termination_date", "dept_id", "manager_id", "job_title", "salary",
            "salary_currency", "is_manager"],
           ([*e[:10], money(e[10]), *e[11:]] for e in employees))
    store_staff = by_dept["STR"] or [1]
    sales_staff = by_dept["SAL"] + by_dept["STR"]
    service_staff = by_dept["CSV"] or [1]

    # store.stores
    stores = []
    city_list = list(CITIES)
    for sid in range(1, 41):
        city = city_list[(sid - 1) % len(city_list)] if sid <= 20 else rng.choice(city_list)
        district = rng.choice(CITIES[city])
        stores.append((sid, f"S{sid:03d}", f"{city} {district} Store", city, district,
                       day(-rng.randint(365, 17 * 365)), rng.choice(store_staff),
                       f"{rng.randint(3000, 30000) / 10:.1f}"))
    L.copy("store.stores", ["store_id", "store_code", "name", "city", "district", "opened_on",
                            "manager_id", "sqm"], stores)
    store_codes = [s[1] for s in stores]

    # product.categories
    cats, leaf_cats = [], []
    cid = 0
    for parent_name, (prefix, children, vat) in CATEGORIES.items():
        cid += 1
        parent_id = cid
        cats.append((parent_id, None, parent_name))
        for child in children:
            cid += 1
            cats.append((cid, parent_id, child))
            leaf_cats.append((cid, prefix, vat, parent_name))
    L.copy("product.categories", ["category_id", "parent_id", "name"], cats)

    # product.suppliers
    suppliers, tax_nos = [], set()
    for sup in range(1, 151):
        country = weighted(rng, [("TR", 80), ("DE", 6), ("CN", 8), ("IT", 3), ("US", 3)])
        while (tax := f"{rng.randint(10**9, 10**10 - 1)}") in tax_nos:
            pass
        tax_nos.add(tax)
        acct = iban(rng, "DE" if country == "DE" else "TR") if rng.random() < 0.7 else None
        suppliers.append((sup, f"{rng.choice(SUPPLIER_WORDS)} {rng.choice(SUPPLIER_SUFFIX)} "
                               f"{rng.choice(['A.S.', 'Ltd. Sti.', 'GmbH', 'Co.'])}",
                          tax, country, acct, rng.choice([0, 30, 30, 45, 60, 90])))
    L.copy("product.suppliers", ["supplier_id", "name", "tax_no", "country_code", "iban",
                                 "payment_terms_days"], suppliers)

    # product.products
    products = []  # (id, list_price Decimal, vat Decimal)
    rows = []
    for pid in range(1, 3001):
        leaf, prefix, vat, parent = rng.choice(leaf_cats)
        cost = Decimal(rng.randint(500, 900_000)) / 100 if parent == "Electronics" \
            else Decimal(rng.randint(300, 150_000)) / 100
        price = (cost * Decimal(rng.uniform(1.2, 2.5))).quantize(CENT, rounding=ROUND_HALF_UP)
        vat_d = Decimal(10) if parent == "Grocery" and rng.random() < 0.5 else Decimal(vat)
        attrs = None
        if parent == "Fashion":
            attrs = {"color": rng.choice(["black", "white", "red", "blue", "beige"]),
                     "size": rng.choice(["XS", "S", "M", "L", "XL"])}
        elif parent == "Electronics":
            attrs = {"warranty_months": rng.choice([12, 24, 36]), "brand": rng.choice(ADJ)}
        products.append((pid, price, vat_d))
        rows.append((pid, f"{prefix}-{pid:06d}", f"{rng.choice(ADJ)} {rng.choice(NOUN)} "
                     f"{rng.randint(100, 999)}", leaf, rng.randint(1, 150), money(cost),
                     money(price), money(vat_d), rng.randint(50, 25_000) if rng.random() < 0.9
                     else None, rng.random() < 0.06, json.dumps(attrs) if attrs else None,
                     day(-rng.randint(0, 8 * 365))))
    L.copy("product.products", ["product_id", "sku", "name", "category_id", "supplier_id",
                                "unit_cost", "list_price", "vat_rate", "weight_g",
                                "is_discontinued", "attributes", "launched_on"], rows)

    # product.price_history
    def price_rows():
        for pid, price, _ in products:
            n = rng.randint(1, 4)
            starts = sorted({day(-rng.randint(0, 3 * 365)) for _ in range(n)})
            for i, start in enumerate(starts):
                end = starts[i + 1] - timedelta(days=1) if i + 1 < len(starts) else None
                factor = Decimal(rng.uniform(0.8, 1.0)) if end else Decimal(1)
                yield (pid, start, end, money(price * factor))
    L.copy("product.price_history", ["product_id", "valid_from", "valid_to", "price"],
           price_rows())

    # store.inventory
    def inventory_rows():
        for s in stores:
            for pid in rng.sample(range(1, 3001), 600):
                yield (s[0], pid, rng.randint(0, 200), rng.randint(5, 30),
                       ts(day(DAYS - rng.randint(1, 90)), rng) if rng.random() < 0.95 else None)
    L.copy("store.inventory", ["store_id", "product_id", "qty_on_hand", "reorder_level",
                               "last_counted_at"], inventory_rows())

    # crm.customers
    customers = []  # (id, code, created_date, first, last, phone, city)
    rows = []
    for cust in range(1, n_customers + 1):
        female = rng.random() < 0.52
        first = rng.choice(FIRST_F if female else FIRST_M)
        last = rng.choice(LAST)
        created = day(-rng.randint(0, 8 * 365) + rng.randint(0, DAYS))
        if created > day(DAYS - 1):
            created = day(DAYS - 1)
        phone = f"+90 5{rng.randint(0, 59):02d} {rng.randint(100, 999)} {rng.randint(10, 99)} " \
                f"{rng.randint(10, 99)}" if rng.random() < 0.9 else None
        city = rng.choice(city_list)
        code = f"C{cust:08d}"
        customers.append((cust, code, created, first, last, phone, city))
        rows.append((
            cust, code, first, last,
            f"{ascii_lower(first)}.{ascii_lower(last)}{cust}@{rng.choice(EMAIL_DOMAINS)}"
            if rng.random() < 0.85 else None,
            phone,
            tckn(rng) if rng.random() < 0.35 else None,
            date(rng.randint(1950, 2006), rng.randint(1, 12), rng.randint(1, 28))
            if rng.random() < 0.8 else None,
            "F" if female else ("M" if rng.random() < 0.97 else "U"),
            weighted(rng, [("retail", 80), ("smb", 12), ("corporate", 5), ("vip", 3)]),
            rng.random() < 0.92,
            int(rng.expovariate(1 / 400)) if rng.random() < 0.7 else 0,
            iban(rng) if rng.random() < 0.2 else None,
            rng.randint(1, cust - 1) if cust > 10 and rng.random() < 0.05 else None,
            rng.randint(1, 40) if rng.random() < 0.6 else None,
            ts(created, rng, 0, 24),
            ts(created + timedelta(days=rng.randint(1, 400)), rng, 0, 24)
            if rng.random() < 0.5 else None,
        ))
    L.copy("crm.customers",
           ["customer_id", "customer_code", "first_name", "last_name", "email", "phone",
            "national_id", "birth_date", "gender", "segment", "is_active", "loyalty_points",
            "iban", "referred_by", "preferred_store_id", "created_at", "updated_at"], rows)

    # crm.addresses
    def address_rows():
        aid = 0
        for cust, _code, _created, _f, _l, _p, city in customers:
            for k in range(weighted(rng, [(1, 55), (2, 35), (3, 10)])):
                aid += 1
                c = city if k == 0 else rng.choice(city_list)
                district = rng.choice(CITIES[c])
                line1 = f"{rng.choice(STREETS)} No:{rng.randint(1, 250)} D:{rng.randint(1, 40)}"
                postal = f"{rng.randint(1, 81):02d}{rng.randint(0, 999):03d}"
                normalized = "|".join([line1, district, c, postal, "TR"]).lower()
                yield (aid, cust, "B" if k == 0 else rng.choice("BS"), line1, district, c,
                       postal if rng.random() < 0.9 else None, "TR", k == 0,
                       hashlib.sha256(normalized.encode()).hexdigest())
    L.copy("crm.addresses", ["address_id", "customer_id", "address_type", "line1", "district",
                             "city", "postal_code", "country_code", "is_default",
                             "address_hash"], address_rows())

    # crm.customer_notes
    def note_rows():
        nid = 0
        for cust, _code, created, *_ in customers:
            if rng.random() < 0.3:
                nid += 1
                yield (nid, cust, rng.choice(NOTE_TEMPLATES).format(n=rng.randint(100, 99999)),
                       rng.choice(service_staff), ts(created + timedelta(days=rng.randint(0, 300)),
                                                    rng))
    L.copy("crm.customer_notes", ["note_id", "customer_id", "note", "created_by", "created_at"],
           note_rows())

    # finance.currencies + fx_rates
    L.copy("finance.currencies", ["currency_code", "name", "minor_unit"],
           [("TRY", "Turkish Lira", 2), ("EUR", "Euro", 2), ("USD", "US Dollar", 2),
            ("GBP", "Pound Sterling", 2)])

    def fx_rows():
        for d in range(DAYS):
            for ccy, lo, hi in (("EUR", 20.0, 48.0), ("USD", 18.5, 42.0), ("GBP", 23.0, 55.0)):
                base = lo + (hi - lo) * d / DAYS
                yield (day(d), ccy, "TRY", f"{base * rng.uniform(0.99, 1.01):.8f}")
    L.copy("finance.fx_rates", ["rate_date", "from_ccy", "to_ccy", "rate"], fx_rows())

    # sales.orders / order_lines / payments / returns / order_events
    orders, lines, payments, returns, events = [], [], [], [], []
    line_id = payment_id = event_id = 0
    for oid in range(1, n_orders + 1):
        cust = rng.randint(1, n_customers)
        d = day(rng.randint(0, DAYS - 1))
        channel = weighted(rng, [("store", 55), ("web", 30), ("mobile", 12), ("phone", 3)])
        status = weighted(rng, [("delivered", 60), ("shipped", 8), ("paid", 10), ("new", 4),
                                ("cancelled", 10), ("returned", 8)])
        ordered_at = ts(d, rng)
        shipped_at = ordered_at + timedelta(hours=rng.randint(12, 72)) \
            if status in ("shipped", "delivered", "returned") and channel != "store" else None
        delivered_at = shipped_at + timedelta(hours=rng.randint(12, 96)) \
            if shipped_at and status in ("delivered", "returned") else None
        store_id = rng.randint(1, 40) if channel == "store" else None
        foreign = channel != "store" and rng.random() >= 0.95
        currency = rng.choice(["EUR", "USD"]) if foreign else "TRY"
        subtotal = tax_total = Decimal(0)
        order_lines = []
        for ln in range(1, weighted(rng, [(1, 35), (2, 30), (3, 18), (4, 10), (5, 7)]) + 1):
            pid, price, vat = products[rng.randint(0, len(products) - 1)]
            qty = weighted(rng, [(1, 70), (2, 18), (3, 7), (4, 3), (6, 2)])
            disc = Decimal(rng.choice([0, 0, 0, 0, 5, 10, 15, 20, 25]))
            sub = (qty * price * (1 - disc / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
            tax = (sub * vat / 100).quantize(CENT, rounding=ROUND_HALF_UP)
            subtotal += sub
            tax_total += tax
            line_id += 1
            cond = "N" if rng.random() < 0.95 else rng.choice("RO")
            order_lines.append((line_id, oid, ln, pid, qty, money(price), money(disc), money(tax),
                                cond, sub + tax, qty))
        shipping = Decimal(0) if channel == "store" or subtotal > 750 else \
            Decimal(rng.choice(["29.90", "49.90"]))
        total = subtotal + tax_total + shipping
        method = weighted(rng, [("card", 70), ("cash", 15 if channel == "store" else 0),
                                ("transfer", 8), ("wallet", 7)])
        rep = rng.choice(sales_staff) if channel in ("store", "phone") else None
        source = "POS" if channel == "store" else ("ERP" if channel == "phone" else "WEB")
        if rng.random() < 0.03:
            source = "LEGACY"
        orders.append((oid, f"SO-{d.year}-{oid:07d}", cust, store_id, channel, status, d,
                       ordered_at, shipped_at, delivered_at, currency, money(subtotal),
                       money(tax_total), money(shipping), money(total), method,
                       f"CPN{rng.randint(1000, 9999)}" if rng.random() < 0.07 else None, rep,
                       source))
        lines.extend(row[:9] for row in order_lines)

        if status not in ("new", "cancelled"):
            paid_at = ordered_at + timedelta(minutes=rng.randint(0, 90))
            if method == "card" and rng.random() < 0.03:  # a declined attempt first
                payment_id += 1
                payments.append((payment_id, oid, paid_at - timedelta(minutes=3), money(total),
                                 method, f"{rng.randint(400000, 559999)}",
                                 f"{rng.randint(0, 9999):04d}", None, "D"))
            parts = [total] if rng.random() > 0.05 else [
                (total / 2).quantize(CENT), total - (total / 2).quantize(CENT)]
            for part in parts:
                payment_id += 1
                card = method == "card"
                payments.append((payment_id, oid, paid_at, money(part), method,
                                 f"{rng.randint(400000, 559999)}" if card else None,
                                 f"{rng.randint(0, 9999):04d}" if card else None,
                                 f"{rng.randint(0, 999999):06d}" if card else None,
                                 "R" if status == "returned" else "A"))
        if status == "returned":
            for (lid, _o, _n, _p, qty, _price, _disc, _t, _c, gross, _q) in order_lines:
                if rng.random() < 0.6:
                    rqty = rng.randint(1, qty)
                    refund = (gross * rqty / qty).quantize(CENT, rounding=ROUND_HALF_UP)
                    returned_at = (delivered_at or ordered_at) + timedelta(
                        days=rng.randint(1, 14))
                    reason = weighted(rng, [("DMG", 30), ("WRG", 25), ("LATE", 15), ("OTHR", 30)])
                    returns.append((lid, returned_at, rqty, reason, money(refund)))

        first_actor = "customer" if channel != "store" else f"store:S{store_id:03d}"
        lifecycle = [(1, ordered_at, first_actor)]
        if status not in ("new", "cancelled"):
            lifecycle.append((2, ordered_at + timedelta(minutes=5), "system"))
        if shipped_at:
            lifecycle.append((3, shipped_at, "courier"))
        if delivered_at:
            lifecycle.append((4, delivered_at, "courier"))
        if status == "cancelled":
            lifecycle.append((5, ordered_at + timedelta(hours=rng.randint(1, 48)), "customer"))
        if status == "returned":
            lifecycle.append((6, (delivered_at or ordered_at) + timedelta(days=3), "customer"))
        for etype, at, actor in lifecycle:
            event_id += 1
            payload = None
            if channel in ("web", "mobile") and etype == 1:
                ip = f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
                payload = json.dumps({"ip": ip})
            events.append((event_id, oid, etype, at, actor, payload))

    L.copy("sales.orders",
           ["order_id", "order_no", "customer_id", "store_id", "channel", "status", "order_date",
            "ordered_at", "shipped_at", "delivered_at", "currency_code", "subtotal", "tax_total",
            "shipping_fee", "total_amount", "payment_method", "coupon_code", "sales_rep_id",
            "source_system"], orders)
    L.copy("sales.order_lines",
           ["order_line_id", "order_id", "line_no", "product_id", "quantity", "unit_price",
            "discount_pct", "line_tax", "condition"], lines)
    L.copy("sales.payments",
           ["payment_id", "order_id", "paid_at", "amount", "method", "card_bin", "card_last4",
            "auth_code", "status"], payments)
    # ~0.3 % of returns point at order lines that do not exist: the NOT VALID FK
    # added later is then genuinely untrustworthy.
    L.copy("sales.returns",
           ["return_id", "order_line_id", "returned_at", "qty", "reason_code", "refund_amount"],
           ((i, lid if rng.random() > 0.003 else line_id + rng.randint(1, 10**6), *rest)
            for i, (lid, *rest) in enumerate(returns, start=1)))
    L.copy("sales.order_events",
           ["event_id", "order_id", "event_type", "event_at", "actor", "payload"], events)

    # finance.invoices + gl_entries
    inv_rows, gl_rows = [], []
    inv_id = 0
    for o in orders:
        oid, _no, _c, _s, _ch, status, d, ordered_at, *_rest = o
        if status in ("new", "cancelled"):
            continue
        currency, subtotal, tax, shipping = o[10], Decimal(o[11]), Decimal(o[12]), Decimal(o[13])
        inv_id += 1
        issued = ordered_at.date()
        due = issued + timedelta(days=0 if o[4] == "store" else 30)
        inv_status = 9 if status == "returned" and rng.random() < 0.4 else weighted(
            rng, [(1, 85), (2, 5), (0, 10)])
        inv_rows.append((inv_id, f"INV{issued.year}{inv_id:08d}", oid, issued, due,
                         money(subtotal + shipping), money(tax), currency, inv_status))
        period = issued.year * 100 + issued.month
        ref = f"INV{issued.year}{inv_id:08d}"
        gross = money(subtotal + shipping + tax)
        gl_rows.append((issued, "120.01", gross, "0.00", "IN", ref, period))
        gl_rows.append((issued, "600.01", "0.00", money(subtotal + shipping), "IN", ref, period))
        gl_rows.append((issued, "391.01", "0.00", money(tax), "IN", ref, period))
    for m in range(36):
        posted = date(2023 + m // 12, m % 12 + 1, 28)
        for e in employees:
            if e[5] <= posted and (e[6] is None or e[6] >= posted) and e[10] > 0:
                period = posted.year * 100 + posted.month
                gl_rows.append((posted, "770.01", money(e[10]), "0.00", "PR", e[1], period))
                gl_rows.append((posted, "335.01", "0.00", money(e[10]), "PR", e[1], period))
    L.copy("finance.invoices",
           ["invoice_id", "invoice_no", "order_id", "issued_on", "due_on", "amount", "tax_amount",
            "currency_code", "status"], inv_rows)
    L.copy("finance.gl_entries",
           ["posted_on", "account_no", "debit", "credit", "doc_type", "doc_ref", "fiscal_period"],
           gl_rows)

    # legacy.*
    def mst_rows():
        for _cust, code, created, first, last, phone, city in customers:
            if rng.random() < 0.7:
                limit = "" if rng.random() < 0.1 else f"{rng.choice([5, 10, 15, 25, 50]) * 1000}.00"
                yield (code, f"{first} {last}".upper(), "Y" if rng.random() < 0.9 else "N",
                       city.upper(), int(created.strftime("%Y%m%d")),
                       phone.replace("+", "").replace(" ", "") if phone else None, limit)
        for k in range(max(5, n_customers // 100)):  # codes unknown to crm.customers
            yield (f"C9{k:07d}", "UNKNOWN CUSTOMER", "N", None, 20190101, None, None)
    L.copy("legacy.tb_mst01", ["c01", "c02", "c03", "c04", "c05", "c06", "c07"], mst_rows())

    def trx_rows():
        legacy_start = date(2019, 1, 1)
        for i in range(n_legacy_trx):
            d = legacy_start + timedelta(days=rng.randint(0, 4 * 365 - 1))
            qty = weighted(rng, [(1, 60), (2, 20), (3, 10), (5, 6), (10, 4)])
            price = Decimal(rng.randint(100, 500_000)) / 100
            code = customers[rng.randint(0, len(customers) - 1)][1] if rng.random() > 0.01 \
                else f"C9{rng.randint(0, 9999999):07d}"
            store = rng.choice(store_codes) if rng.random() > 0.02 else f"S{rng.randint(900, 999)}"
            yield (900_000_000 + i, code, int(d.strftime("%Y%m%d")),
                   rng.randint(8, 21) * 10000 + rng.randint(0, 59) * 100 + rng.randint(0, 59),
                   qty, money(price), money(price * qty),
                   weighted(rng, [("OK", 90), ("CN", 6), ("RT", 4)]), store,
                   "TRY" if rng.random() < 0.97 else "USD")
    L.copy("legacy.tb_trx02", ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10"],
           trx_rows())

    prm = [("STS", "OK", "Completed"), ("STS", "CN", "Cancelled"), ("STS", "RT", "Returned"),
           ("CUR", "TRY", "Turkish Lira"), ("CUR", "USD", "US Dollar"), ("CUR", "EUR", "Euro"),
           ("RSN", "DMG", "Damaged item"), ("RSN", "WRG", "Wrong item"), ("RSN", "LATE", "Late"),
           ("RSN", "OTHR", "Other")]
    prm += [("CTY", f"{i:02d}", c.upper()) for i, c in enumerate(city_list, start=1)]
    prm += [("STR", s[1], s[2].upper()) for s in stores]
    L.copy("legacy.tb_prm03", ["grp", "k", "v"], prm)
    L.copy("legacy.xx_aux", ["id", "a1", "a2", "a3"],
           ((i, None, "X", 0) for i in range(1, 5001)))
    return L.counts


def post_load(conn: psycopg.Connection) -> None:
    statements = [
        # identity/serial sequences follow the explicit ids we loaded
        *(f"SELECT setval(pg_get_serial_sequence('{t}', '{c}'), (SELECT max({c}) FROM {t}))"  # noqa: S608
          for t, c in [("hr.employees", "employee_id"), ("crm.customers", "customer_id"),
                       ("crm.addresses", "address_id"), ("crm.customer_notes", "note_id"),
                       ("sales.orders", "order_id"), ("sales.order_lines", "order_line_id"),
                       ("sales.payments", "payment_id"), ("sales.returns", "return_id"),
                       ("finance.invoices", "invoice_id"), ("finance.gl_entries", "entry_id")]),
        # untrusted FK (orphans exist) and untrusted check (three interns earn 0)
        "ALTER TABLE sales.returns ADD CONSTRAINT fk_returns_order_line FOREIGN KEY "
        "(order_line_id) REFERENCES sales.order_lines (order_line_id) NOT VALID",
        "ALTER TABLE hr.employees ADD CONSTRAINT ck_employees_salary_positive "
        "CHECK (salary > 0) NOT VALID",
        # "disabled" FK: enforcement triggers off, then orphans slip in
        "ALTER TABLE finance.invoices DISABLE TRIGGER ALL",
        "INSERT INTO finance.invoices (invoice_no, order_id, issued_on, due_on, amount, "
        "tax_amount, currency_code, status) SELECT 'INVX' || lpad(g::text, 8, '0'), "
        "(SELECT max(order_id) FROM sales.orders) + g, DATE '2025-12-30', DATE '2026-01-29', "
        "100.00, 20.00, 'TRY', 0 FROM generate_series(1, 20) g",
        "REFRESH MATERIALIZED VIEW rpt.mv_product_margin",
    ]
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)


def provision_role(conn: psycopg.Connection, dbname: str, user: str, password: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(user)))
        text = ROLE_SQL.read_text(encoding="utf-8")
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
        # Split the template first, then substitute: a password may contain ";".
        for template in (s.strip() for s in text.split(";")):
            if not template:
                continue
            stmt = (template.replace(':"ro_user"', sql.Identifier(user).as_string(conn))
                            .replace(":'ro_password'", sql.Literal(password).as_string(conn))
                            .replace(':"dbname"', sql.Identifier(dbname).as_string(conn)))
            cur.execute(stmt)


def ensure_database(admin_url: str) -> str:
    parts = urlsplit(admin_url)
    dbname = parts.path.lstrip("/")
    if not dbname:
        raise SystemExit("GAIA_PG_TARGET_ADMIN_URL must name the target database")
    maintenance = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(maintenance, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0")
                         .format(sql.Identifier(dbname)))
            print(f"created database {dbname}")
    return dbname


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--scale", type=float, default=1.0, help="row-count multiplier (default 1)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--admin-url", default=os.environ.get("GAIA_PG_TARGET_ADMIN_URL"))
    p.add_argument("--ro-user", default=os.environ.get("GAIA_PG_TARGET_USER", "gaia_ro"))
    p.add_argument("--ro-password", default=os.environ.get("GAIA_PG_TARGET_PASSWORD"))
    args = p.parse_args(argv)
    if not args.admin_url:
        print("set GAIA_PG_TARGET_ADMIN_URL (see .env.example)", file=sys.stderr)
        return 2
    if not args.ro_password:
        print("set GAIA_PG_TARGET_PASSWORD (password for the read-only role)", file=sys.stderr)
        return 2

    started = time.perf_counter()
    dbname = ensure_database(args.admin_url)
    with psycopg.connect(args.admin_url) as conn:
        conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        counts = generate(conn, args.scale, args.seed)
        post_load(conn)
        provision_role(conn, dbname, args.ro_user, args.ro_password)
        conn.commit()
    with psycopg.connect(args.admin_url, autocommit=True) as conn:
        conn.execute("ANALYZE")
    total = sum(counts.values())
    width = max(len(t) for t in counts)
    for table, n in counts.items():
        print(f"  {table:<{width}}  {n:>10,}")
    print(f"loaded {total:,} rows into {dbname} (scale={args.scale}, seed={args.seed}) "
          f"in {time.perf_counter() - started:.1f}s; read-only role: {args.ro_user}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
