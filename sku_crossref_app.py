import streamlit as st
import pandas as pd
import io
import re
import json
import requests
from copy import copy
import openpyxl
from openpyxl import load_workbook

st.set_page_config(page_title="SKU CrossRef – Amazon Listings", layout="wide", page_icon="🔗")

# ─── Config ───────────────────────────────────────────────────────────────────
COUNTRY_CONFIG = {
    "Jabiru ES":  {"prefix": "",    "feed": "https://cecotec.es/api/v3/doofinder/feed/?lang=es",  "currency": "EUR", "flag": "🇪🇸", "mirror_only": False},
    "Turaco ES":  {"prefix": "",    "feed": "https://cecotec.es/api/v3/doofinder/feed/?lang=es",  "currency": "EUR", "flag": "🇪🇸", "mirror_only": False},
    "FR":         {"prefix": "FR",  "feed": "https://storececotec.fr/api/v3/doofinder/feed/?lang=fr", "currency": "EUR", "flag": "🇫🇷", "mirror_only": False},
    "IT":         {"prefix": "IT",  "feed": "https://content.storececotec.it/api/v3/doofinder/feed/?lang=it", "currency": "EUR", "flag": "🇮🇹", "mirror_only": False},
    "DE":         {"prefix": "DE",  "feed": "https://storececotec.de/api/v3/doofinder/feed/?lang=de", "currency": "EUR", "flag": "🇩🇪", "mirror_only": False},
    "NL":         {"prefix": "",    "feed": "https://storececotec.de/api/v3/doofinder/feed/?lang=de", "currency": "EUR", "flag": "🇳🇱", "mirror_only": True},
    "SE":         {"prefix": "",    "feed": "https://storececotec.de/api/v3/doofinder/feed/?lang=de", "currency": "SEK", "flag": "🇸🇪", "mirror_only": True},
    "PL":         {"prefix": "",    "feed": "https://storececotec.de/api/v3/doofinder/feed/?lang=de", "currency": "PLN", "flag": "🇵🇱", "mirror_only": True},
}
AMZN_AUTO = re.compile(r'^AMZN\.', re.IGNORECASE)

# ─── Helpers ──────────────────────────────────────────────────────────────────
def extract_base(ref: str) -> str:
    ref = ref.strip().rstrip(".")
    if "_" in ref:
        m = re.search(r"([A-Z]\d{2}_)", ref.upper())
        if m:
            return ref[m.start():]
        return ref
    m = re.match(r"^(DE|FR|IT|S)([A-Z]?\d+.*)$", ref, re.IGNORECASE)
    if m:
        rest = m.group(2)
        return rest.zfill(5) if re.match(r"^\d+$", rest) else rest
    if re.match(r"^\d+$", ref):
        return ref.zfill(5)
    return ref

def read_ps(f) -> pd.Series:
    df = pd.read_csv(f, sep=";", dtype=str, low_memory=False)
    col = next((c for c in df.columns if c.strip().lower() == "reference"), None)
    if col is None:
        st.error("❌ No se encontró columna 'reference' en el CSV de Prestashop.")
        return pd.Series(dtype=str)
    refs = df[col].dropna().str.strip()
    return refs[refs != ""]

def read_listing(f, label: str) -> tuple:
    try:
        raw = pd.read_csv(f, sep="\t", dtype=str, low_memory=False,
                          encoding="utf-8-sig", on_bad_lines="skip")
    except Exception as e:
        st.error(f"❌ Error leyendo {label}: {e}")
        empty = pd.DataFrame(columns=["sku", "asin"])
        return empty, empty, {"label": label, "total_rows": 0, "active_rows": 0}

    status_col = next((c for c in raw.columns if c.strip().lower() == "status"), None)
    total_rows = len(raw)
    active_rows = int((raw[status_col].str.strip().str.lower() == "active").sum()) if status_col else total_rows

    def _extract(df):
        out = df[[df.columns[0], df.columns[1]]].copy()
        out.columns = ["sku", "asin"]
        out["sku"] = out["sku"].astype(str).str.strip().str.upper()
        out["asin"] = out["asin"].astype(str).str.strip()
        return out[out["sku"].notna() & (out["sku"] != "") & (out["sku"] != "nan")]

    df_all = _extract(raw)
    df_active = _extract(raw[raw[status_col].str.strip().str.lower() == "active"].copy()) if status_col else df_all.copy()
    return df_active, df_all, {"label": label, "total_rows": total_rows, "active_rows": active_rows}

def build_sku_map(df): return df.drop_duplicates("sku").set_index("sku")["asin"].to_dict()

def jabiru_bases_map(jabiru_df):
    result = {}
    for _, row in jabiru_df.iterrows():
        sku = row["sku"]
        if AMZN_AUTO.match(sku): continue
        base = extract_base(sku).upper()
        if base not in result:
            result[base] = (sku, row["asin"])
    return result

# ─── Cross-check functions ────────────────────────────────────────────────────
def check_jabiru_vs_ps(jabiru_df, refs):
    ps_bases = set(extract_base(r).upper() for r in refs)
    rows = []
    for _, row in jabiru_df.iterrows():
        base = extract_base(row["sku"]).upper()
        if base not in ps_bases:
            rows.append({"SKU Jabiru ES": row["sku"], "Base SKU": base, "ASIN": row["asin"]})
    return pd.DataFrame(rows)

def check_turaco(j_bases, turaco_skus):
    rows = []
    for base, (sku_j, asin) in j_bases.items():
        if not any(v in turaco_skus for v in [base, f"S{base}"]):
            rows.append({"SKU Jabiru ES": sku_j, "Base SKU": base, "ASIN": asin,
                         "Variantes buscadas": f"{base} | S{base}"})
    return pd.DataFrame(rows)

def check_country(j_bases, listing_skus, country_prefix):
    rows = []
    for base, (sku_j, asin) in j_bases.items():
        variants = [base, f"S{base}"] + ([f"{country_prefix}{base}"] if country_prefix else [])
        if not any(v in listing_skus for v in variants):
            rows.append({"SKU Jabiru ES": sku_j, "Base SKU": base, "ASIN": asin,
                         "Variantes buscadas": " | ".join(variants)})
    return pd.DataFrame(rows)

def check_mirror(j_bases, listing_skus, listing_map, store_label):
    rows = []
    for base, (sku_j, asin_base) in j_bases.items():
        s_sku = f"S{base}"
        if s_sku not in listing_skus:
            rows.append({"Tienda": store_label, "SKU Jabiru ES": sku_j, "Base SKU": base,
                         "SKU espejo": s_sku, "ASIN base": asin_base,
                         "ASIN espejo": listing_map.get(s_sku, "")})
    return pd.DataFrame(rows)

# ─── Feed fetch ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_feed(url: str) -> dict:
    """Fetches Cecotec product feed and returns {mpn_upper: price}."""
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("items", data.get("results", data.get("products", [])))
        price_map = {}
        for item in items:
            mpn = str(item.get("mpn", item.get("id", item.get("reference", "")))).strip().upper()
            # Remove leading zeros for matching
            mpn_no_zero = mpn.lstrip("0") or "0"
            price = item.get("price", item.get("sale_price", ""))
            if mpn and price:
                price_map[mpn] = str(price)
                price_map[mpn_no_zero] = str(price)
        return price_map
    except Exception as e:
        return {"__error__": str(e)}

def get_price_for_sku(sku: str, feed_map: dict) -> str:
    """Try to match a SKU (with or without leading zeros) to a feed price."""
    if not feed_map or "__error__" in feed_map:
        return ""
    base = extract_base(sku).upper()
    # Try as-is, stripped of zeros, and zero-padded to 5
    for key in [base, base.lstrip("0"), base.zfill(5)]:
        if key in feed_map:
            return feed_map[key]
    return ""

# ─── Amazon template generator ───────────────────────────────────────────────
def generate_amazon_template(rows: list, feed_map: dict, currency_rate: float = 1.0,
                              currency_symbol: str = "EUR") -> bytes:
    """
    rows: list of dicts with keys: asin, sku, base_sku
    Returns xlsx bytes based on the Amazon template ES loaded from session state.
    """
    tmpl_bytes = st.session_state.get("template_bytes")
    if not tmpl_bytes:
        raise ValueError("No hay plantilla Amazon ES cargada. Súbela en el panel lateral.")
    wb = load_workbook(io.BytesIO(tmpl_bytes), keep_vba=True)
    ws = wb["Plantilla"]

    # Clear existing data rows (7+)
    for row in ws.iter_rows(min_row=7, max_row=ws.max_row):
        for cell in row:
            cell.value = None

    start_row = 7
    for i, item in enumerate(rows):
        r = start_row + i
        asin = item.get("asin", "")
        sku  = item.get("sku", "")
        base = item.get("base_sku", "")

        # Col A (1): ASIN
        ws.cell(row=r, column=1).value = asin
        # Col D (4): Grabar acción = "Añadir producto"
        ws.cell(row=r, column=4).value = "Añadir producto"
        # Col E (5): SKU
        ws.cell(row=r, column=5).value = sku
        # Col F (6): ASIN recomendado
        ws.cell(row=r, column=6).value = asin
        # Col L (12): Estado del producto = "Nuevo"
        ws.cell(row=r, column=12).value = "Nuevo"
        # Col AJ (36): Cumplimiento = "Logística por parte del vendedor (predeterminado)"
        ws.cell(row=r, column=36).value = "Logística por parte del vendedor (predeterminado)"
        # Col AN (40): Inventario siempre disponible = "Habilitado"
        ws.cell(row=r, column=40).value = "Habilitado"
        # Col AO (41): Precio
        price_str = get_price_for_sku(base, feed_map)
        if price_str:
            try:
                price_val = float(price_str.replace(",", ".")) * currency_rate
                ws.cell(row=r, column=41).value = round(price_val, 2)
            except Exception:
                pass
        # Col BN (66): Plantilla de envío = "FBM NO HB"
        ws.cell(row=r, column=66).value = "FBM NO HB"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()

# ─── UI helpers ───────────────────────────────────────────────────────────────
def show_table(df, label_empty="✅ Sin SKUs pendientes.", search_cols=None,
               dl_key="", dl_name="export.csv"):
    if df.empty:
        st.success(label_empty)
        return
    st.warning(f"⚠️ **{len(df):,} SKUs** pendientes.")
    q = st.text_input("🔍 Filtrar", key=f"q_{dl_key}")
    filtered = df
    if q:
        cols = search_cols or list(df.columns)
        mask = pd.Series(False, index=df.index)
        for c in cols:
            if c in df.columns:
                mask |= df[c].astype(str).str.contains(q, case=False, na=False)
        filtered = df[mask]
    st.dataframe(filtered, width="stretch", height=400)
    csv = filtered.to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ Descargar CSV", data=csv,
                       file_name=dl_name, mime="text/csv", key=f"dl_{dl_key}")


def amazon_template_section(title: str, df: pd.DataFrame, feed_map: dict,
                             currency_rate: float, currency_symbol: str,
                             dl_key: str, country_label: str,
                             es_template: bool = False):
    """Shows table. For ES countries also offers Amazon template generation."""
    show_table(df, search_cols=list(df.columns), dl_key=dl_key,
               dl_name=f"{dl_key}.csv")

    if not df.empty:
        if es_template:
            # Only for ES marketplace listings (Jabiru ES, Turaco ES)
            with st.expander("📋 Generar plantilla Amazon ES (.xlsm)", expanded=False):
                has_template = bool(st.session_state.get("template_bytes"))
                if not has_template:
                    st.info("📤 Sube la plantilla Amazon ES en el panel lateral para activar esta función.")
                else:
                    if st.button(f"Generar plantilla ES – {country_label}", key=f"gen_{dl_key}"):
                        rows = []
                        for _, row in df.iterrows():
                            asin = row.get("ASIN", row.get("ASIN base", ""))
                            sku  = row.get("SKU Jabiru ES", row.get("SKU espejo", ""))
                            base = row.get("Base SKU", extract_base(sku))
                            rows.append({"asin": asin, "sku": sku, "base_sku": base})
                        with st.spinner("Generando plantilla…"):
                            try:
                                xlsm_bytes = generate_amazon_template(
                                    rows, feed_map, currency_rate, currency_symbol)
                                st.download_button(
                                    f"⬇️ Descargar plantilla Amazon ES – {country_label}",
                                    data=xlsm_bytes,
                                    file_name=f"amazon_template_ES_{dl_key}.xlsm",
                                    mime="application/vnd.ms-excel.sheet.macroEnabled.12",
                                    key=f"dl_tmpl_{dl_key}")
                            except Exception as e:
                                st.error(f"Error generando plantilla: {e}")
        else:
            st.caption("ℹ️ La plantilla Amazon varía por país e idioma. "
                       "Descarga el CSV y usa la plantilla del país correspondiente desde Seller Central.")

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("📂 Archivos")
    ps_file     = st.file_uploader("📦 Prestashop BD (CSV ;)",      type=["csv","txt"], key="ps")
    jabiru_file = st.file_uploader("🇪🇸 Jabiru ES",                  type=["txt","tsv","csv"], key="jabiru")
    turaco_file = st.file_uploader("🇪🇸 Turaco ES",                  type=["txt","tsv","csv"], key="turaco")
    fr_file     = st.file_uploader("🇫🇷 FR",                         type=["txt","tsv","csv"], key="fr")
    it_file     = st.file_uploader("🇮🇹 IT",                         type=["txt","tsv","csv"], key="it")
    de_file     = st.file_uploader("🇩🇪 DE / NL / SE / PL",          type=["txt","tsv","csv"], key="de")

    st.divider()
    st.markdown("**📋 Plantilla Amazon ES**")
    template_file = st.file_uploader("Sube la plantilla Amazon ES (.xlsm)",
                                     type=["xlsm","xlsx"], key="template_es")
    if template_file:
        st.session_state["template_bytes"] = template_file.read()
        st.success("✅ Plantilla cargada")

    st.divider()
    st.markdown("**💱 Tipos de cambio (base EUR)**")
    rate_sek = st.number_input("SEK / EUR", value=st.session_state.get("rate_sek", 11.5),
                               min_value=0.01, step=0.1, key="rate_sek")
    rate_pln = st.number_input("PLN / EUR", value=st.session_state.get("rate_pln", 4.25),
                               min_value=0.01, step=0.01, key="rate_pln")

    st.divider()
    st.markdown("""
**Lógica (solo Active como fuente):**
- Cruce 1: ¿existe el SKU? → todos los estados
- Cruce 2: ¿S+SKU activo? → todos los estados
- NL / SE / PL: solo espejo S+SKU (sin prefijo país)
""")

# ─── Main ─────────────────────────────────────────────────────────────────────
st.title("🔗 SKU CrossRef – Amazon Listings")
st.caption("Cruza el catálogo de Prestashop con los listings de Amazon y detecta SKUs a crear por país.")

files_ready = all([ps_file, jabiru_file, turaco_file, de_file, fr_file, it_file])
if not files_ready:
    st.info("👈 Carga todos los archivos en el panel izquierdo para comenzar.")
    st.stop()

# ─── Load ─────────────────────────────────────────────────────────────────────
with st.spinner("Cargando archivos…"):
    refs = read_ps(ps_file)
    jabiru_active, jabiru_all, jabiru_attrs = read_listing(jabiru_file, "Jabiru ES")
    turaco_active, turaco_all, turaco_attrs = read_listing(turaco_file, "Turaco ES")
    de_active,     de_all,     de_attrs     = read_listing(de_file,     "DE/NL/SE/PL")
    fr_active,     fr_all,     fr_attrs     = read_listing(fr_file,     "FR")
    it_active,     it_all,     it_attrs     = read_listing(it_file,     "IT")
    jabiru = jabiru_active

with st.sidebar:
    st.divider()
    st.markdown("**📊 SKUs activos por listing:**")
    for attrs, flag in [(jabiru_attrs,"🇪🇸 Jabiru"),(turaco_attrs,"🇪🇸 Turaco"),
                        (fr_attrs,"🇫🇷 FR"),(it_attrs,"🇮🇹 IT"),(de_attrs,"🇩🇪 DE/NL/SE/PL")]:
        total  = attrs.get("total_rows", "?")
        active = attrs.get("active_rows", "?")
        pct    = f"{active/total*100:.0f}%" if isinstance(total,int) and total > 0 else "?"
        st.caption(f"{attrs['label']}: **{active:,}** / {total:,} ({pct})")

# ─── Calculate ────────────────────────────────────────────────────────────────
with st.spinner("Calculando cruces…"):
    jabiru_skus_active = set(jabiru_active["sku"])
    jabiru_skus_all    = jabiru_skus_active
    turaco_skus_all    = set(turaco_all["sku"])
    fr_skus_all        = set(fr_all["sku"])
    it_skus_all        = set(it_all["sku"])
    de_skus_all        = set(de_all["sku"])

    jabiru_map = build_sku_map(jabiru_active)
    turaco_map = build_sku_map(turaco_all)
    fr_map     = build_sku_map(fr_all)
    it_map     = build_sku_map(it_all)
    de_map     = build_sku_map(de_all)

    j_bases = jabiru_bases_map(jabiru_active)

    # Cruce 1
    df_ps_vs_jabiru   = check_jabiru_vs_ps(jabiru_active, refs)
    df_turaco_missing = check_turaco(j_bases, turaco_skus_all)
    df_fr_missing     = check_country(j_bases, fr_skus_all,  "FR")
    df_it_missing     = check_country(j_bases, it_skus_all,  "IT")
    df_de_missing     = check_country(j_bases, de_skus_all,  "DE")
    df_nl_missing     = check_country(j_bases, de_skus_all,  "")   # NL usa mismo listing DE, solo S+base
    df_se_missing     = check_country(j_bases, de_skus_all,  "")
    df_pl_missing     = check_country(j_bases, de_skus_all,  "")

    # Cruce 2 – espejos (todos los estados)
    df_mirror_jabiru  = check_mirror(j_bases, jabiru_skus_all, jabiru_map, "Jabiru ES")
    df_mirror_turaco  = check_mirror(j_bases, turaco_skus_all, turaco_map, "Turaco ES")
    df_mirror_fr      = check_mirror(j_bases, fr_skus_all,     fr_map,     "FR")
    df_mirror_it      = check_mirror(j_bases, it_skus_all,     it_map,     "IT")
    df_mirror_de      = check_mirror(j_bases, de_skus_all,     de_map,     "DE")
    df_mirror_nl      = check_mirror(j_bases, de_skus_all,     de_map,     "NL")
    df_mirror_se      = check_mirror(j_bases, de_skus_all,     de_map,     "SE")
    df_mirror_pl      = check_mirror(j_bases, de_skus_all,     de_map,     "PL")

    df_mirror_all = pd.concat([df_mirror_jabiru, df_mirror_turaco, df_mirror_fr,
                                df_mirror_it, df_mirror_de, df_mirror_nl,
                                df_mirror_se, df_mirror_pl], ignore_index=True)

# ─── Feed fetch (optional) ────────────────────────────────────────────────────
feed_maps = {}
with st.expander("🌐 Cargar precios desde feeds Cecotec (opcional)", expanded=False):
    if st.button("Cargar feeds de precios"):
        for country, cfg in COUNTRY_CONFIG.items():
            with st.spinner(f"Cargando feed {country}…"):
                feed_maps[country] = fetch_feed(cfg["feed"])
                if "__error__" in feed_maps[country]:
                    st.warning(f"⚠️ {country}: {feed_maps[country]['__error__']}")
                else:
                    st.success(f"✅ {country}: {len(feed_maps[country])} productos")
        st.session_state["feed_maps"] = feed_maps

if "feed_maps" in st.session_state:
    feed_maps = st.session_state["feed_maps"]

# ─── KPIs ─────────────────────────────────────────────────────────────────────
st.subheader("📊 Resumen")
st.markdown("**Cruce 1 – SKUs faltantes por país**")
c1,c2,c3,c4,c5,c6 = st.columns(6)
c1.metric("Referencias PS",          len(refs))
c2.metric("❌ Jabiru sin ref PS",     len(df_ps_vs_jabiru))
c3.metric("❌ Turaco ES",            len(df_turaco_missing))
c4.metric("❌ FR",                   len(df_fr_missing))
c5.metric("❌ IT",                   len(df_it_missing))
c6.metric("❌ DE",                   len(df_de_missing))

st.markdown("**Cruce 2 – Espejos S+SKU faltantes**")
m1,m2,m3,m4,m5,m6,m7,m8 = st.columns(8)
m1.metric("🪞 Jabiru",  len(df_mirror_jabiru))
m2.metric("🪞 Turaco",  len(df_mirror_turaco))
m3.metric("🪞 FR",      len(df_mirror_fr))
m4.metric("🪞 IT",      len(df_mirror_it))
m5.metric("🪞 DE",      len(df_mirror_de))
m6.metric("🪞 NL",      len(df_mirror_nl))
m7.metric("🪞 SE",      len(df_mirror_se))
m8.metric("🪞 PL",      len(df_mirror_pl))

st.divider()

# ─── Tabs ─────────────────────────────────────────────────────────────────────
tabs = st.tabs(["🇪🇸 Jabiru ES","🇪🇸 Turaco ES","🇫🇷 FR","🇮🇹 IT","🇩🇪 DE",
                "🇳🇱 NL","🇸🇪 SE","🇵🇱 PL",
                "🪞 Espejos S+SKU","📥 Exportar todo"])
tab_ps, tab_turaco, tab_fr, tab_it, tab_de, tab_nl, tab_se, tab_pl, tab_mirror, tab_export = tabs

with tab_ps:
    st.markdown("### 🇪🇸 Jabiru ES – SKUs activos sin referencia en Prestashop")
    amazon_template_section("Jabiru ES", df_ps_vs_jabiru,
                             feed_maps.get("Jabiru ES", {}), 1.0, "EUR",
                             "ps_jabiru", "Jabiru ES", es_template=True)

with tab_turaco:
    st.markdown("### 🇪🇸 Turaco ES – Bases Jabiru sin variante en Turaco")
    amazon_template_section("Turaco ES", df_turaco_missing,
                             feed_maps.get("Turaco ES", {}), 1.0, "EUR",
                             "turaco", "Turaco ES", es_template=True)

with tab_fr:
    st.markdown("### 🇫🇷 FR – Bases Jabiru sin variante activa en FR")
    amazon_template_section("FR", df_fr_missing,
                             feed_maps.get("FR", {}), 1.0, "EUR",
                             "fr", "FR")

with tab_it:
    st.markdown("### 🇮🇹 IT – Bases Jabiru sin variante activa en IT")
    amazon_template_section("IT", df_it_missing,
                             feed_maps.get("IT", {}), 1.0, "EUR",
                             "it", "IT")

with tab_de:
    st.markdown("### 🇩🇪 DE – Bases Jabiru sin variante activa en DE")
    amazon_template_section("DE", df_de_missing,
                             feed_maps.get("DE", {}), 1.0, "EUR",
                             "de", "DE")

with tab_nl:
    st.markdown("### 🇳🇱 NL – Bases Jabiru sin S+SKU en listing DE (usado para NL)")
    amazon_template_section("NL", df_nl_missing,
                             feed_maps.get("DE", {}), 1.0, "EUR",
                             "nl", "NL")

with tab_se:
    st.markdown("### 🇸🇪 SE – Bases Jabiru sin S+SKU en listing DE (usado para SE)")
    rate = st.session_state.get("rate_sek", 11.5)
    amazon_template_section("SE", df_se_missing,
                             feed_maps.get("SE", {}), rate, "SEK",
                             "se", "SE")

with tab_pl:
    st.markdown("### 🇵🇱 PL – Bases Jabiru sin S+SKU en listing DE (usado para PL)")
    rate = st.session_state.get("rate_pln", 4.25)
    amazon_template_section("PL", df_pl_missing,
                             feed_maps.get("PL", {}), rate, "PLN",
                             "pl", "PL")

# ─── Espejos tab ──────────────────────────────────────────────────────────────
with tab_mirror:
    st.markdown("### 🪞 Espejos S+SKU faltantes")
    st.info("Cada SKU base de Jabiru debe tener su `S+SKU` creado (cualquier estado) en todas las tiendas y países.")

    subtabs = st.tabs(["Resumen","Jabiru ES","Turaco ES","FR","IT","DE","NL","SE","PL"])
    with subtabs[0]:
        if df_mirror_all.empty:
            st.success("✅ Todos los espejos existen.")
        else:
            pivot = df_mirror_all.groupby("Tienda").size().reset_index(name="S+SKU faltantes")
            st.dataframe(pivot, width="stretch", hide_index=True)
            show_table(df_mirror_all, dl_key="mirror_all", dl_name="espejos_todos.csv")

    for sub, df_m, label, dk, feed_key, rate, curr, is_es in [
        (subtabs[1], df_mirror_jabiru, "Jabiru ES", "mirror_jabiru", "Jabiru ES", 1.0, "EUR", True),
        (subtabs[2], df_mirror_turaco, "Turaco ES", "mirror_turaco", "Turaco ES", 1.0, "EUR", True),
        (subtabs[3], df_mirror_fr,     "FR",        "mirror_fr",     "FR",        1.0, "EUR", False),
        (subtabs[4], df_mirror_it,     "IT",        "mirror_it",     "IT",        1.0, "EUR", False),
        (subtabs[5], df_mirror_de,     "DE",        "mirror_de",     "DE",        1.0, "EUR", False),
        (subtabs[6], df_mirror_nl,     "NL",        "mirror_nl",     "DE",        1.0, "EUR", False),
        (subtabs[7], df_mirror_se,     "SE",        "mirror_se",     "SE",        st.session_state.get("rate_sek",11.5), "SEK", False),
        (subtabs[8], df_mirror_pl,     "PL",        "mirror_pl",     "PL",        st.session_state.get("rate_pln",4.25), "PLN", False),
    ]:
        with sub:
            st.markdown(f"#### {label}")
            amazon_template_section(label, df_m, feed_maps.get(feed_key, {}),
                                    rate, curr, dk, label, es_template=is_es)

# ─── Export tab ───────────────────────────────────────────────────────────────
with tab_export:
    st.markdown("### 📥 Exportar todos los resultados – Excel completo")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_ps_vs_jabiru.to_excel(writer,    sheet_name="C1_Jabiru_sin_PS",    index=False)
        df_turaco_missing.to_excel(writer,  sheet_name="C1_Turaco_ES",        index=False)
        df_fr_missing.to_excel(writer,      sheet_name="C1_FR",               index=False)
        df_it_missing.to_excel(writer,      sheet_name="C1_IT",               index=False)
        df_de_missing.to_excel(writer,      sheet_name="C1_DE",               index=False)
        df_nl_missing.to_excel(writer,      sheet_name="C1_NL",               index=False)
        df_se_missing.to_excel(writer,      sheet_name="C1_SE",               index=False)
        df_pl_missing.to_excel(writer,      sheet_name="C1_PL",               index=False)
        df_mirror_all.to_excel(writer,      sheet_name="C2_Espejos_todos",    index=False)
        df_mirror_jabiru.to_excel(writer,   sheet_name="C2_Espejos_Jabiru",   index=False)
        df_mirror_turaco.to_excel(writer,   sheet_name="C2_Espejos_Turaco",   index=False)
        df_mirror_fr.to_excel(writer,       sheet_name="C2_Espejos_FR",       index=False)
        df_mirror_it.to_excel(writer,       sheet_name="C2_Espejos_IT",       index=False)
        df_mirror_de.to_excel(writer,       sheet_name="C2_Espejos_DE",       index=False)
        df_mirror_nl.to_excel(writer,       sheet_name="C2_Espejos_NL",       index=False)
        df_mirror_se.to_excel(writer,       sheet_name="C2_Espejos_SE",       index=False)
        df_mirror_pl.to_excel(writer,       sheet_name="C2_Espejos_PL",       index=False)

    st.download_button("⬇️ Descargar Excel completo (17 pestañas)",
                       data=buf.getvalue(),
                       file_name="sku_crossref_completo.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.markdown("""
| Pestaña | Descripción |
|---|---|
| `C1_Jabiru_sin_PS` | SKUs activos Jabiru sin referencia en PS |
| `C1_Turaco_ES … C1_PL` | Cruce 1 faltantes por país |
| `C2_Espejos_todos` | S+SKU faltantes unificado |
| `C2_Espejos_*` | S+SKU faltantes por tienda/país |
""")
