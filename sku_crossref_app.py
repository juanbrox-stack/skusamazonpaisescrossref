import streamlit as st
import pandas as pd
import io, re, requests
from openpyxl import load_workbook

st.set_page_config(page_title="SKU CrossRef – Amazon", layout="wide", page_icon="🔗")

# ─── Constants ────────────────────────────────────────────────────────────────
AMZN_AUTO = re.compile(r'^AMZN\.', re.IGNORECASE)

COUNTRIES = {
    "Jabiru ES":  {"prefix": "",   "feed": "https://cecotec.es/api/v3/doofinder/feed/?lang=es",      "currency": "EUR", "flag": "🇪🇸", "mirror_only": False, "es_template": True},
    "Turaco ES":  {"prefix": "",   "feed": "https://cecotec.es/api/v3/doofinder/feed/?lang=es",      "currency": "EUR", "flag": "🇪🇸", "mirror_only": False, "es_template": True},
    "FR":         {"prefix": "FR", "feed": "https://storececotec.fr/api/v3/doofinder/feed/?lang=fr", "currency": "EUR", "flag": "🇫🇷", "mirror_only": False, "es_template": False},
    "IT":         {"prefix": "IT", "feed": "https://content.storececotec.it/api/v3/doofinder/feed/?lang=it", "currency": "EUR", "flag": "🇮🇹", "mirror_only": False, "es_template": False},
    "DE":         {"prefix": "DE", "feed": "https://storececotec.de/api/v3/doofinder/feed/?lang=de", "currency": "EUR", "flag": "🇩🇪", "mirror_only": False, "es_template": False},
    "NL":         {"prefix": "",   "feed": "https://storececotec.de/api/v3/doofinder/feed/?lang=de", "currency": "EUR", "flag": "🇳🇱", "mirror_only": True,  "es_template": False},
    "SE":         {"prefix": "",   "feed": "https://storececotec.de/api/v3/doofinder/feed/?lang=de", "currency": "SEK", "flag": "🇸🇪", "mirror_only": True,  "es_template": False},
    "PL":         {"prefix": "",   "feed": "https://storececotec.de/api/v3/doofinder/feed/?lang=de", "currency": "PLN", "flag": "🇵🇱", "mirror_only": True,  "es_template": False},
}

# ─── Core helpers ─────────────────────────────────────────────────────────────
def extract_base(ref: str) -> str:
    ref = ref.strip().rstrip(".")
    if "_" in ref:
        m = re.search(r"([A-Z]\d{2}_)", ref.upper())
        return ref[m.start():] if m else ref
    m = re.match(r"^(DE|FR|IT|S)([A-Z]?\d+.*)$", ref, re.IGNORECASE)
    if m:
        rest = m.group(2)
        return rest.zfill(5) if re.match(r"^\d+$", rest) else rest
    return ref.zfill(5) if re.match(r"^\d+$", ref) else ref

@st.cache_data(show_spinner=False)
def parse_ps(data: bytes) -> pd.Series:
    df = pd.read_csv(io.BytesIO(data), sep=";", dtype=str, low_memory=False)
    col = next((c for c in df.columns if c.strip().lower() == "reference"), None)
    if not col:
        return pd.Series(dtype=str)
    s = df[col].dropna().str.strip()
    return s[s != ""]

@st.cache_data(show_spinner=False)
def parse_listing(data: bytes, label: str) -> tuple:
    """Returns (df_active, df_all, attrs). Cached by file content."""
    try:
        raw = pd.read_csv(io.BytesIO(data), sep="\t", dtype=str, low_memory=False,
                          encoding="utf-8-sig", on_bad_lines="skip")
    except Exception as e:
        st.error(f"❌ Error leyendo {label}: {e}")
        empty = pd.DataFrame(columns=["sku","asin"])
        return empty, empty, {"label": label, "total_rows": 0, "active_rows": 0}

    sc = next((c for c in raw.columns if c.strip().lower() == "status"), None)
    total = len(raw)
    active_n = int((raw[sc].str.strip().str.lower() == "active").sum()) if sc else total

    def _extract(df):
        out = df[[df.columns[0], df.columns[1]]].copy()
        out.columns = ["sku","asin"]
        out["sku"] = out["sku"].astype(str).str.strip().str.upper()
        out["asin"] = out["asin"].astype(str).str.strip()
        return out[out["sku"].notna() & (out["sku"] != "") & (out["sku"] != "nan")]

    df_all    = _extract(raw)
    df_active = _extract(raw[raw[sc].str.strip().str.lower() == "active"]) if sc else df_all.copy()
    return df_active, df_all, {"label": label, "total_rows": total, "active_rows": active_n}

def sku_map(df): return df.drop_duplicates("sku").set_index("sku")["asin"].to_dict()

def jabiru_bases(jdf):
    r = {}
    for _, row in jdf.iterrows():
        if AMZN_AUTO.match(row["sku"]): continue
        b = extract_base(row["sku"]).upper()
        if b not in r: r[b] = (row["sku"], row["asin"])
    return r

# ─── Cross checks ─────────────────────────────────────────────────────────────
def check_ps(jdf, refs):
    ps_bases = {extract_base(r).upper() for r in refs}
    rows = [{"SKU Jabiru ES": row["sku"], "Base SKU": extract_base(row["sku"]).upper(),
              "ASIN": row["asin"]}
            for _, row in jdf.iterrows()
            if extract_base(row["sku"]).upper() not in ps_bases]
    return pd.DataFrame(rows)

def check_missing(j_bases, listing_skus, prefix):
    rows = []
    for base, (sku_j, asin) in j_bases.items():
        variants = [base, f"S{base}"] + ([f"{prefix}{base}"] if prefix else [])
        if not any(v in listing_skus for v in variants):
            rows.append({"SKU Jabiru ES": sku_j, "Base SKU": base, "ASIN": asin,
                         "Variantes buscadas": " | ".join(variants)})
    return pd.DataFrame(rows)

def check_mirror(j_bases, listing_skus, listing_map, store):
    rows = []
    for base, (sku_j, asin_base) in j_bases.items():
        s = f"S{base}"
        if s not in listing_skus:
            rows.append({"Tienda": store, "SKU Jabiru ES": sku_j, "Base SKU": base,
                         "SKU espejo": s, "ASIN base": asin_base,
                         "ASIN espejo": listing_map.get(s, "")})
    return pd.DataFrame(rows)

# ─── Feed & price ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_feed(url: str) -> dict:
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("items", data.get("results", data.get("products", [])))
        m = {}
        for item in items:
            mpn = str(item.get("mpn", item.get("reference", ""))).strip().upper()
            price = str(item.get("price", item.get("sale_price", "")))
            if mpn and price:
                m[mpn] = price
                m[mpn.lstrip("0") or "0"] = price
        return m
    except Exception as e:
        return {"__error__": str(e)}

def get_price(base: str, feed: dict, fallback: dict = None) -> str:
    def _look(m, b):
        if not m or "__error__" in m: return ""
        for k in [b, b.lstrip("0") or "0", b.zfill(5)]:
            if k in m: return m[k]
        return ""
    p = _look(feed, base)
    if not p and fallback: p = _look(fallback, base)
    return p

# ─── Amazon template ──────────────────────────────────────────────────────────
def gen_template(rows, feed, rate=1.0, fallback=None):
    tmpl = st.session_state.get("template_bytes")
    if not tmpl:
        raise ValueError("Sube la plantilla Amazon ES en el panel lateral.")
    wb = load_workbook(io.BytesIO(tmpl), keep_vba=True)
    ws = wb["Plantilla"]
    for row in ws.iter_rows(min_row=7, max_row=ws.max_row):
        for cell in row: cell.value = None
    for i, item in enumerate(rows, start=7):
        asin, sku, base = item["asin"], item["sku"], item["base_sku"]
        ws.cell(i, 1).value  = asin
        ws.cell(i, 4).value  = "Añadir producto"
        ws.cell(i, 5).value  = sku
        ws.cell(i, 6).value  = asin
        ws.cell(i, 12).value = "Nuevo"
        ws.cell(i, 36).value = "Logística por parte del vendedor (predeterminado)"
        ws.cell(i, 40).value = "Habilitado"
        price_str = get_price(base, feed, fallback)
        if price_str:
            try: ws.cell(i, 41).value = round(float(price_str.replace(",",".")) * rate, 2)
            except: pass
        ws.cell(i, 66).value = "FBM NO HB"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

# ─── UI components ────────────────────────────────────────────────────────────
def show_table(df, dl_key, dl_name):
    if df.empty:
        st.success("✅ Sin SKUs pendientes.")
        return
    st.warning(f"⚠️ **{len(df):,} SKUs** pendientes.")
    q = st.text_input("🔍 Filtrar", key=f"q_{dl_key}")
    filt = df
    if q:
        mask = pd.Series(False, index=df.index)
        for c in df.columns:
            mask |= df[c].astype(str).str.contains(q, case=False, na=False)
        filt = df[mask]
    st.dataframe(filt, width="stretch", height=380)
    st.download_button("⬇️ CSV", filt.to_csv(index=False).encode("utf-8-sig"),
                       file_name=dl_name, mime="text/csv", key=f"dl_{dl_key}")

def template_btn(df, feed, rate, fallback, dl_key, label, show_es_template):
    """Render the Amazon ES template button (ES only)."""
    if df.empty or not show_es_template: return
    has = bool(st.session_state.get("template_bytes"))
    with st.expander("📋 Generar plantilla Amazon ES"):
        if not has:
            st.info("📤 Sube la plantilla Amazon ES en el panel lateral.")
            return
        if st.button(f"Generar – {label}", key=f"gen_{dl_key}"):
            rows = [{"asin": r.get("ASIN", r.get("ASIN base","")),
                     "sku":  r.get("SKU Jabiru ES", r.get("SKU espejo","")),
                     "base_sku": r.get("Base SKU","")}
                    for _, r in df.iterrows()]
            with st.spinner("Generando…"):
                try:
                    data = gen_template(rows, feed, rate, fallback)
                    st.download_button("⬇️ Plantilla Amazon ES",
                                       data=data,
                                       file_name=f"amz_ES_{dl_key}.xlsm",
                                       mime="application/vnd.ms-excel.sheet.macroEnabled.12",
                                       key=f"dl_t_{dl_key}")
                except Exception as e:
                    st.error(str(e))

def country_section(key, label, flag, df_c1, df_c2,
                    feed, rate, fallback, show_es_template):
    """Render one country tab: C1 + C2 + optional template."""
    c1, c2 = st.tabs([f"Cruce 1 – Faltantes ({len(df_c1)})",
                       f"Cruce 2 – Espejos ({len(df_c2)})"])
    with c1:
        show_table(df_c1, f"c1_{key}", f"c1_{key}.csv")
        template_btn(df_c1, feed, rate, fallback, f"c1t_{key}", label, show_es_template)
    with c2:
        show_table(df_c2, f"c2_{key}", f"c2_{key}.csv")
        template_btn(df_c2, feed, rate, fallback, f"c2t_{key}", label, show_es_template)

# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.header("📂 Archivos base")
    ps_file     = st.file_uploader("📦 Prestashop BD (CSV ;)",  type=["csv","txt"], key="ps")
    jabiru_file = st.file_uploader("🇪🇸 Jabiru ES",              type=["txt","tsv","csv"], key="jabiru")

    st.divider()
    st.header("🌍 Listings por país")
    st.caption("Activa solo los que quieras procesar:")

    listing_files = {}
    listing_checks = {}
    LISTING_DEFS = [
        ("turaco", "🇪🇸 Turaco ES"),
        ("fr",     "🇫🇷 FR"),
        ("it",     "🇮🇹 IT"),
        ("de",     "🇩🇪 DE"),
        ("nl",     "🇳🇱 NL"),
        ("se",     "🇸🇪 SE"),
        ("pl",     "🇵🇱 PL"),
    ]
    for key, lbl in LISTING_DEFS:
        checked = st.checkbox(lbl, value=True, key=f"chk_{key}")
        listing_checks[key] = checked
        if checked:
            f = st.file_uploader(f"  ↳ Fichero {lbl}", type=["txt","tsv","csv"],
                                 key=f"file_{key}", label_visibility="collapsed")
            listing_files[key] = f
        else:
            listing_files[key] = None

    st.divider()
    st.header("📋 Plantilla Amazon ES")
    tpl = st.file_uploader("Sube la plantilla (.xlsm)", type=["xlsm","xlsx"], key="tpl")
    if tpl:
        st.session_state["template_bytes"] = tpl.read()
        st.success("✅ Plantilla cargada")

    st.divider()
    st.header("💱 Tipos de cambio")
    rate_sek = st.number_input("SEK / EUR", value=st.session_state.get("rate_sek", 11.5),
                                min_value=0.01, step=0.1, key="rate_sek")
    rate_pln = st.number_input("PLN / EUR", value=st.session_state.get("rate_pln", 4.25),
                                min_value=0.01, step=0.01, key="rate_pln")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
st.title("🔗 SKU CrossRef – Amazon Listings")
st.caption("Cruza el catálogo de Prestashop con los listings de Amazon y detecta SKUs a crear por país.")

if not ps_file or not jabiru_file:
    st.info("👈 Carga al menos el CSV de Prestashop y el listing de Jabiru ES para comenzar.")
    st.stop()

# ─── Load base files ──────────────────────────────────────────────────────────
with st.spinner("Leyendo Prestashop y Jabiru ES…"):
    refs = parse_ps(ps_file.read())
    jab_active, jab_all, jab_attrs = parse_listing(jabiru_file.read(), "Jabiru ES")

j_bases  = jabiru_bases(jab_active)
jab_skus = set(jab_all["sku"])
jab_map  = sku_map(jab_all)

# ─── Feeds (optional) ─────────────────────────────────────────────────────────
feed_maps = st.session_state.get("feed_maps", {})
with st.expander("🌐 Precios desde feeds Cecotec (opcional)"):
    active_countries = [k for k,v in listing_checks.items() if v] + ["jabiru"]
    to_load = st.multiselect("Países a cargar", list(COUNTRIES.keys()),
                              default=[c for c in COUNTRIES if c in ["Jabiru ES","FR","IT","DE"]])
    if st.button("Cargar feeds seleccionados"):
        for country in to_load:
            cfg = COUNTRIES[country]
            with st.spinner(f"Cargando {country}…"):
                feed_maps[country] = fetch_feed(cfg["feed"])
                if "__error__" in feed_maps[country]:
                    st.warning(f"⚠️ {country}: {feed_maps[country]['__error__']}")
                else:
                    st.success(f"✅ {country}: {len(feed_maps[country])//2} productos")
        st.session_state["feed_maps"] = feed_maps

es_feed = feed_maps.get("Jabiru ES", {})

# ─── Jabiru ES section (always shown) ─────────────────────────────────────────
st.subheader(f"🇪🇸 Jabiru ES   ({jab_attrs['active_rows']:,} activos / {jab_attrs['total_rows']:,})")

df_jabiru_ps = check_ps(jab_active, refs)
df_jabiru_mirror = check_mirror(j_bases, jab_skus, jab_map, "Jabiru ES")

country_section("jabiru", "Jabiru ES", "🇪🇸",
                df_jabiru_ps, df_jabiru_mirror,
                es_feed, 1.0, None,
                show_es_template=True)

st.divider()

# ─── Per-country sections ─────────────────────────────────────────────────────
COUNTRY_META = {
    "turaco": ("Turaco ES", "🇪🇸", "", 1.0, "EUR", True),
    "fr":     ("FR",        "🇫🇷", "FR", 1.0, "EUR", False),
    "it":     ("IT",        "🇮🇹", "IT", 1.0, "EUR", False),
    "de":     ("DE",        "🇩🇪", "DE", 1.0, "EUR", False),
    "nl":     ("NL",        "🇳🇱", "",   1.0, "EUR", False),
    "se":     ("SE",        "🇸🇪", "",   None, "SEK", False),
    "pl":     ("PL",        "🇵🇱", "",   None, "PLN", False),
}

for key, lbl in LISTING_DEFS:
    if not listing_checks[key]:
        continue
    f = listing_files[key]
    label, flag, prefix, rate_fixed, currency, is_es = COUNTRY_META[key]
    rate = (st.session_state.get("rate_sek", 11.5) if currency == "SEK"
            else st.session_state.get("rate_pln", 4.25) if currency == "PLN"
            else 1.0)

    cfg_key = label  # matches COUNTRIES dict key
    country_feed = feed_maps.get(cfg_key, {})
    fallback = es_feed if country_feed is not es_feed else None

    if f is None:
        st.info(f"{flag} **{label}** — activa el checkbox y sube el fichero para procesar.")
        st.divider()
        continue

    with st.spinner(f"Leyendo {label}…"):
        c_active, c_all, c_attrs = parse_listing(f.read(), label)

    c_skus = set(c_all["sku"])
    c_map  = sku_map(c_all)

    df_c1 = check_missing(j_bases, c_skus, prefix)
    df_c2 = check_mirror(j_bases, c_skus, c_map, label)

    active_n = c_attrs["active_rows"]
    total_n  = c_attrs["total_rows"]
    st.subheader(f"{flag} {label}   ({active_n:,} activos / {total_n:,})")

    country_section(key, label, flag, df_c1, df_c2,
                    country_feed, rate, fallback, is_es)
    st.divider()

# ─── Global export ────────────────────────────────────────────────────────────
if st.button("📥 Exportar resumen Excel completo"):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df_jabiru_ps.to_excel(w,     sheet_name="Jabiru_sin_PS",    index=False)
        df_jabiru_mirror.to_excel(w, sheet_name="Espejos_Jabiru",   index=False)
    st.download_button("⬇️ Descargar Excel",
                       data=buf.getvalue(),
                       file_name="sku_crossref_resumen.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       key="dl_global_excel")
