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
    """
    First sheet 'Global' with everything combined (sorted by Base SKU then prefix order),
    followed by one sheet per prefix type: 'Sin_prefijo', 'S', and one per country prefix.
    Prefixes kept separated in their own sheets so Amazon's "first match" upload behavior
    doesn't accidentally pick an S+SKU or wrong-country row.
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Global sheet: all rows, ordered by Base SKU then (sin prefijo -> S -> país)
        global_df = df.copy()
        global_df["_ord"] = global_df["Prefijo"].map(prefix_sort_key)
        global_df = global_df.sort_values(["Base SKU", "_ord"]).drop(columns="_ord")
        global_df.to_excel(writer, sheet_name="Global", index=False)

        # Determine sheet order: sin prefijo -> S -> rest alphabetically
        prefixes_present = list(df["Prefijo"].unique())
        def _key(p):
            return (0 if p == "(sin prefijo)" else 1 if p == "S" else 2, p)
        prefixes_present.sort(key=_key)

        for pfx in prefixes_present:
            sheet_name = "Sin_prefijo" if pfx == "(sin prefijo)" else pfx
            sub = df[df["Prefijo"] == pfx].sort_values("Base SKU").drop(columns="Prefijo")
            sub.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return buf.getvalue()

# ─── Prestashop helpers (new) ─────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def parse_ps(data: bytes) -> pd.Series:
    """Parse Prestashop BD export (CSV ';' separated, column 'reference')."""
    df = pd.read_csv(io.BytesIO(data), sep=";", dtype=str, low_memory=False)
    col = next((c for c in df.columns if c.strip().lower() == "reference"), None)
    if not col:
        return pd.Series(dtype=str)
    s = df[col].dropna().str.strip()
    return s[s != ""]

def missing_jabiru_to_ps(j_bases: dict, ps_refs_upper: set, prefixes: list) -> pd.DataFrame:
    """Jabiru ES variant (per prefix) missing as a Prestashop reference."""
    rows = []
    for base, (sku_j, asin) in j_bases.items():
        for pfx in prefixes:
            variant = f"{pfx}{base}"
            if variant not in ps_refs_upper:
                rows.append({
                    "Base SKU":      base,
                    "Prefijo":       pfx if pfx else "(sin prefijo)",
                    "SKU faltante":  variant,
                    "SKU Jabiru ES": sku_j,
                    "ASIN":          asin,
                })
    return pd.DataFrame(rows)

def missing_ps_to_jabiru(ps_refs: pd.Series, jab_all_skus_base: set) -> pd.DataFrame:
    """PS references whose base has NO match at all among Jabiru ES SKUs (any state)."""
    base_series = ps_refs.map(extract_base).str.upper()
    mask = ~base_series.isin(jab_all_skus_base)
    return pd.DataFrame({
        "Referencia PS": ps_refs[mask],
        "Base SKU":      base_series[mask],
    }).reset_index(drop=True)

# ═══════════════════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════════════════
st.title("🔗 SKU CrossRef – Amazon Listings")
st.caption("Compara Jabiru ES contra otro listing o contra Prestashop y genera el Excel de SKUs faltantes.")

modo = st.radio("Tipo de comparación", ["📄 Jabiru ES ↔ Otro listing Amazon", "📦 Jabiru ES ↔ Prestashop"],
                horizontal=True, key="modo")

st.divider()

if modo.startswith("📄"):
    # ── MODO 1: Jabiru ES vs otro listing Amazon (sin cambios respecto a la versión anterior) ──
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

else:
    # ── MODO 2 (nuevo): Jabiru ES vs Prestashop, en ambos sentidos ──
    col1, col2 = st.columns(2)
    with col1:
        jabiru_file = st.file_uploader("🇪🇸 Jabiru ES (listing Amazon)", type=["txt","tsv","csv"], key="jabiru_es_ps")
    with col2:
        ps_file = st.file_uploader("📦 Prestashop BD (CSV ';' — columna 'reference')", type=["csv","txt"], key="ps_file")

    if not jabiru_file or not ps_file:
        st.info("👈 Sube el listing de Jabiru ES y el CSV de Prestashop para comenzar.")
        st.stop()

    with st.spinner("Leyendo ficheros…"):
        jab_raw = read_once(jabiru_file, "_cache_jabiru_ps")
        ps_raw  = read_once(ps_file, "_cache_ps")
        jab_active, jab_all, jab_attrs = parse_listing(jab_raw, "Jabiru ES")
        ps_refs = parse_ps(ps_raw)

    j_bases = jabiru_bases(jab_active)
    jab_all_bases = set(jab_all["sku"].map(extract_base).str.upper())
    ps_refs_upper = set(ps_refs.str.strip().str.upper())

    prefixes_input_ps = st.text_input(
        "Prefijos de país a comprobar en Prestashop (separados por coma, sin contar 'S')",
        value="", placeholder="ej: FR, IT, DE — déjalo vacío para comprobar solo SKU y S+SKU",
        key="prefixes_input_ps")
    extra_prefixes_ps = [p.strip().upper() for p in prefixes_input_ps.split(",") if p.strip()]
    prefixes_ps = ["", "S"] + extra_prefixes_ps

    st.caption(f"Jabiru ES: **{jab_attrs['active']:,}** activos / {jab_attrs['total']:,} total &nbsp;|&nbsp; "
               f"Prestashop: **{len(ps_refs):,}** referencias &nbsp;|&nbsp; "
               f"Bases Jabiru ES: **{len(j_bases):,}**")

    tab1, tab2 = st.tabs([
        "Jabiru ES → Prestashop (variantes que faltan crear en PS)",
        "Prestashop → Jabiru ES (referencias PS sin match en Jabiru)"
    ])

    with tab1:
        df_j2p = missing_jabiru_to_ps(j_bases, ps_refs_upper, prefixes_ps)
        n_missing_bases = df_j2p["Base SKU"].nunique() if not df_j2p.empty else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("Bases Jabiru ES", len(j_bases))
        c2.metric("✅ Completas en PS", len(j_bases) - n_missing_bases)
        c3.metric("❌ Con faltantes en PS", n_missing_bases)

        if df_j2p.empty:
            st.success(f"✅ Prestashop tiene todas las variantes ({', '.join(prefixes_ps)}) para cada base de Jabiru ES.")
        else:
            st.warning(f"⚠️ **{len(df_j2p):,}** referencias que faltan crear en Prestashop.")
            q1 = st.text_input("🔍 Filtrar", key="q_filter_j2p")
            df_show1 = df_j2p.copy()
            df_show1["_ord"] = df_show1["Prefijo"].map(prefix_sort_key)
            df_show1 = df_show1.sort_values(["Base SKU", "_ord"]).drop(columns="_ord")
            if q1:
                mask = pd.Series(False, index=df_show1.index)
                for c in df_show1.columns:
                    mask |= df_show1[c].astype(str).str.contains(q1, case=False, na=False)
                df_show1 = df_show1[mask]
            st.dataframe(df_show1, width="stretch", height=420)

            xlsx_bytes1 = build_excel(df_j2p)
            st.download_button(
                f"⬇️ Descargar XLSX — {len(df_j2p):,} referencias a crear en PS",
                data=xlsx_bytes1,
                file_name="faltantes_jabiru_a_prestashop.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_j2p")

    with tab2:
        df_p2j = missing_ps_to_jabiru(ps_refs, jab_all_bases)
        c1, c2 = st.columns(2)
        c1.metric("Referencias Prestashop", len(ps_refs))
        c2.metric("❌ Sin match en Jabiru ES", len(df_p2j))

        if df_p2j.empty:
            st.success("✅ Todas las referencias de Prestashop tienen al menos un SKU en Jabiru ES.")
        else:
            st.warning(f"⚠️ **{len(df_p2j):,}** referencias PS sin ningún SKU en Jabiru ES (ningún estado).")
            q2 = st.text_input("🔍 Filtrar", key="q_filter_p2j")
            df_show2 = df_p2j.sort_values("Base SKU")
            if q2:
                mask = pd.Series(False, index=df_show2.index)
                for c in df_show2.columns:
                    mask |= df_show2[c].astype(str).str.contains(q2, case=False, na=False)
                df_show2 = df_show2[mask]
            st.dataframe(df_show2, width="stretch", height=420)

            csv_bytes = df_p2j.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                f"⬇️ Descargar CSV — {len(df_p2j):,} referencias PS sin match",
                data=csv_bytes,
                file_name="ps_sin_match_jabiru.csv",
                mime="text/csv",
                key="dl_p2j")
