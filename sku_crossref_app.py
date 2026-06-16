import streamlit as st
import pandas as pd
import io, re

st.set_page_config(page_title="SKU CrossRef – Amazon", layout="wide", page_icon="🔗")

AMZN_AUTO = re.compile(r'^AMZN\.', re.IGNORECASE)

# ─── Core helpers ─────────────────────────────────────────────────────────────
def extract_base(ref: str) -> str:
    ref = ref.strip().rstrip(".")
    if "_" in ref:
        m = re.search(r"([A-Z]\d{2}_)", ref.upper())
        return ref[m.start():] if m else ref
    m = re.match(r"^(DE|FR|IT|NL|PL|SE|S)([A-Z]?\d+.*)$", ref, re.IGNORECASE)
    if m:
        rest = m.group(2)
        return rest.zfill(5) if re.match(r"^\d+$", rest) else rest
    return ref.zfill(5) if re.match(r"^\d+$", ref) else ref

def detect_prefix(sku: str, base: str) -> str:
    """Given a full SKU and its base, return the prefix used ('', 'S', 'FR', 'IT', 'DE'...)."""
    su = sku.upper()
    bu = base.upper()
    if su == bu:
        return ""
    if su.endswith(bu):
        return su[: len(su) - len(bu)]
    return "?"

def read_once(uploaded_file, cache_key: str) -> bytes:
    """Read an UploadedFile's bytes only once per file (avoid re-reading on every rerun)."""
    if uploaded_file is None:
        return None
    sig = (uploaded_file.name, uploaded_file.size)
    cached = st.session_state.get(cache_key)
    if cached and cached[0] == sig:
        return cached[1]
    data = uploaded_file.read()
    st.session_state[cache_key] = (sig, data)
    return data

@st.cache_data(show_spinner=False)
def parse_listing(data: bytes, label: str) -> tuple:
    """Returns (df_active, df_all, attrs). Cached by file content."""
    try:
        raw = pd.read_csv(io.BytesIO(data), sep="\t", dtype=str, low_memory=False,
                          encoding="utf-8-sig", on_bad_lines="skip")
    except Exception as e:
        st.error(f"❌ {label}: {e}")
        empty = pd.DataFrame(columns=["sku","asin"])
        return empty, empty, {"label": label, "total": 0, "active": 0}

    sc = next((c for c in raw.columns if c.strip().lower() == "status"), None)
    total = len(raw)
    active_n = int((raw[sc].str.strip().str.lower() == "active").sum()) if sc else total

    def _ex(df):
        out = df[[df.columns[0], df.columns[1]]].copy()
        out.columns = ["sku","asin"]
        out["sku"] = out["sku"].astype(str).str.strip().str.upper()
        out["asin"] = out["asin"].astype(str).str.strip()
        return out[out["sku"].notna() & (out["sku"] != "") & (out["sku"] != "nan")]

    df_all = _ex(raw)
    df_active = _ex(raw[raw[sc].str.strip().str.lower() == "active"]) if sc else df_all.copy()
    return df_active, df_all, {"label": label, "total": total, "active": active_n}

@st.cache_data(show_spinner=False)
def jabiru_bases(jdf: pd.DataFrame) -> dict:
    """Extract unique bases from Jabiru ES active SKUs. Ignores AMZN.* auto-SKUs. Vectorized."""
    skus = jdf["sku"]
    mask = ~skus.str.match(r"^AMZN\.", case=False, na=False)
    sub = jdf[mask].copy()
    sub["base"] = sub["sku"].map(extract_base).str.upper()
    sub = sub.drop_duplicates("base", keep="first")
    return dict(zip(sub["base"], zip(sub["sku"], sub["asin"])))

# ─── Cross-check: one row per missing SKU/espejo ─────────────────────────────
def missing_long(j_bases: dict, target_skus: set, prefixes: list, store_label: str) -> pd.DataFrame:
    """
    One row per missing variant. prefixes e.g. ["", "S"] or ["", "S", "FR"].
    """
    rows = []
    for base, (sku_j, asin) in j_bases.items():
        for pfx in prefixes:
            variant = f"{pfx}{base}"
            if variant not in target_skus:
                rows.append({
                    "Tienda":        store_label,
                    "Base SKU":      base,
                    "Prefijo":       pfx if pfx else "(sin prefijo)",
                    "SKU faltante":  variant,
                    "SKU Jabiru ES": sku_j,
                    "ASIN":          asin,
                })
    return pd.DataFrame(rows)

# Orden de prefijo para el Excel: sin prefijo -> S -> resto (país)
PREFIX_ORDER = {"(sin prefijo)": 0, "S": 1}
def prefix_sort_key(p: str) -> tuple:
    return (PREFIX_ORDER.get(p, 2), p)

def build_excel(df: pd.DataFrame) -> bytes:
    df = df.copy()
    df["_ord"] = df["Prefijo"].map(prefix_sort_key)
    df = df.sort_values(["Base SKU", "_ord"]).drop(columns="_ord")
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()

# ═══════════════════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════════════════
st.title("🔗 SKU CrossRef – Amazon Listings")
st.caption("Compara Jabiru ES contra otro listing y genera el Excel de SKUs faltantes, ordenado por base / S / prefijo país.")

col1, col2 = st.columns(2)
with col1:
    jabiru_file = st.file_uploader("🇪🇸 Jabiru ES (listing de referencia)", type=["txt","tsv","csv"], key="jabiru_es")
with col2:
    compare_file = st.file_uploader("📄 Listing a comparar", type=["txt","tsv","csv"], key="compare")
    compare_label = st.text_input("Nombre de la tienda/país comparada", value="Comparado", key="compare_label")
    prefixes_input = st.text_input(
        "Prefijos a comprobar (separados por coma, sin contar 'S')",
        value="", placeholder="ej: FR  /  IT  /  DE  — déjalo vacío para ES/NL/PL/SE",
        key="prefixes_input")

if not jabiru_file or not compare_file:
    st.info("👈 Sube ambos ficheros para comenzar.")
    st.stop()

with st.spinner("Leyendo ficheros…"):
    jab_raw = read_once(jabiru_file, "_cache_jabiru")
    cmp_raw = read_once(compare_file, "_cache_compare")
    jab_active, jab_all, jab_attrs = parse_listing(jab_raw, "Jabiru ES")
    _, cmp_all, cmp_attrs = parse_listing(cmp_raw, compare_label)

j_bases = jabiru_bases(jab_active)
cmp_skus = set(cmp_all["sku"].str.upper().dropna())

extra_prefixes = [p.strip().upper() for p in prefixes_input.split(",") if p.strip()]
prefixes = ["", "S"] + extra_prefixes

st.caption(f"Jabiru ES: **{jab_attrs['active']:,}** activos / {jab_attrs['total']:,} total &nbsp;|&nbsp; "
           f"{compare_label}: **{cmp_attrs['active']:,}** activos / {cmp_attrs['total']:,} total &nbsp;|&nbsp; "
           f"Bases Jabiru ES: **{len(j_bases):,}**")

df_detail = missing_long(j_bases, cmp_skus, prefixes, compare_label)

n_bases_missing = df_detail["Base SKU"].nunique() if not df_detail.empty else 0
c1, c2, c3 = st.columns(3)
c1.metric("Bases Jabiru ES", len(j_bases))
c2.metric("✅ Bases completas", len(j_bases) - n_bases_missing)
c3.metric("❌ Bases con faltantes", n_bases_missing)

st.divider()

if df_detail.empty:
    st.success(f"✅ {compare_label} tiene todas las variantes ({', '.join(prefixes)}) para cada base de Jabiru ES.")
else:
    st.warning(f"⚠️ **{len(df_detail):,}** SKUs faltantes en {compare_label}.")
    q = st.text_input("🔍 Filtrar", key="q_filter")
    df_show = df_detail.copy()
    df_show["_ord"] = df_show["Prefijo"].map(prefix_sort_key)
    df_show = df_show.sort_values(["Base SKU", "_ord"]).drop(columns="_ord")
    if q:
        mask = pd.Series(False, index=df_show.index)
        for c in df_show.columns:
            mask |= df_show[c].astype(str).str.contains(q, case=False, na=False)
        df_show = df_show[mask]
    st.dataframe(df_show, width="stretch", height=450)

    xlsx_bytes = build_excel(df_detail)
    st.download_button(
        f"⬇️ Descargar XLSX — {len(df_detail):,} SKUs faltantes",
        data=xlsx_bytes,
        file_name=f"faltantes_{compare_label.replace(' ','_')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
