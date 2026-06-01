import streamlit as st
import pandas as pd
import io
import re

st.set_page_config(page_title="SKU CrossRef – Amazon Listings", layout="wide", page_icon="🔗")

st.title("🔗 SKU CrossRef – Amazon Listings")
st.caption("Cruza el catálogo de Prestashop con los listings de Amazon y detecta SKUs a crear por país.")

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("📂 Cargar archivos")
    ps_file     = st.file_uploader("📦 Prestashop BD (CSV semicolon)",    type=["csv","txt"], key="ps")
    jabiru_file = st.file_uploader("🇪🇸 Jabiru ES (listing Amazon)",       type=["txt","tsv","csv"], key="jabiru")
    turaco_file = st.file_uploader("🇪🇸 Turaco ES (listing Amazon)",       type=["txt","tsv","csv"], key="turaco")
    de_file     = st.file_uploader("🇩🇪 DE (listing Amazon)",              type=["txt","tsv","csv"], key="de")
    fr_file     = st.file_uploader("🇫🇷 FR (listing Amazon)",              type=["txt","tsv","csv"], key="fr")
    it_file     = st.file_uploader("🇮🇹 IT (listing Amazon)",              type=["txt","tsv","csv"], key="it")

    st.divider()
    st.markdown("""
**Lógica de validación (solo SKUs Active):**

**Cruce 1 – Faltantes por país**
- 🇪🇸 Jabiru ES: refs PS sin listing activo
- 🇪🇸 Turaco ES: bases de Jabiru sin variante en Turaco
- 🇫🇷/🇮🇹/🇩🇪: bases de Jabiru sin variante activa en ese país

**Cruce 2 – Espejos S+SKU faltantes**
- Cada SKU base debe tener su `S+SKU` en ES (Jabiru y Turaco) y en FR, IT, DE
""")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def read_ps(f) -> pd.Series:
    df = pd.read_csv(f, sep=";", dtype=str, low_memory=False)
    col = next((c for c in df.columns if c.strip().lower() == "reference"), None)
    if col is None:
        st.error("❌ No se encontró columna 'reference' en el CSV de Prestashop.")
        return pd.Series(dtype=str)
    refs = df[col].dropna().str.strip()
    return refs[refs != ""]


def read_listing(f, label: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(f, sep="\t", dtype=str, low_memory=False,
                         encoding="utf-8-sig", on_bad_lines="skip")
    except Exception as e:
        st.error(f"❌ Error leyendo {label}: {e}")
        return pd.DataFrame(columns=["sku", "asin"])

    status_col = next((c for c in df.columns if c.strip().lower() == "status"), None)
    total_rows = len(df)
    if status_col:
        df = df[df[status_col].str.strip().str.lower() == "active"].copy()
    active_rows = len(df)

    sku_col  = df.columns[0]
    asin_col = df.columns[1] if len(df.columns) > 1 else None
    df = df[[sku_col, asin_col]].copy() if asin_col else df[[sku_col]].copy()
    df.columns = ["sku", "asin"] if asin_col else ["sku"]
    df["sku"] = df["sku"].astype(str).str.strip().str.upper()
    if "asin" not in df.columns:
        df["asin"] = ""
    else:
        df["asin"] = df["asin"].astype(str).str.strip()

    df = df[df["sku"].notna() & (df["sku"] != "") & (df["sku"] != "nan")]
    df.attrs["label"]       = label
    df.attrs["total_rows"]  = total_rows
    df.attrs["active_rows"] = active_rows
    return df


def extract_base(ref: str) -> str:
    """
    Extrae la BASE del SKU eliminando prefijo de país/variante.
      P82_EU01_115591   -> P82_EU01_115591
      DEP82_EU01_115716 -> P82_EU01_115716
      SA01_EU01_119670  -> A01_EU01_119670
      DE01180           -> 01180
      S05802            -> 05802
      DEV0331           -> V0331
      05152.            -> 05152
    """
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


def build_sku_map(df: pd.DataFrame) -> dict:
    """dict {sku_upper -> asin}"""
    return df.drop_duplicates("sku").set_index("sku")["asin"].to_dict()


def jabiru_bases_map(jabiru_df: pd.DataFrame) -> dict:
    """dict {base_upper -> (sku_jabiru, asin)}  — primer SKU encontrado por base."""
    result = {}
    for _, row in jabiru_df.iterrows():
        base = extract_base(row["sku"]).upper()
        if base not in result:
            result[base] = (row["sku"], row["asin"])
    return result


# ── Cruce 1: faltantes por país ───────────────────────────────────────────────

def check_jabiru_vs_ps(refs: pd.Series, jabiru_skus: set) -> pd.DataFrame:
    """Refs PS sin ninguna variante activa en Jabiru ES."""
    rows = []
    for ref in refs:
        base = extract_base(ref).upper()
        if not any(v in jabiru_skus for v in [base, f"S{base}"]):
            rows.append({"SKU (ref PS)": ref, "Base SKU": base,
                         "Variantes buscadas": f"{base} | S{base}"})
    return pd.DataFrame(rows)


def check_turaco(j_bases: dict, turaco_skus: set) -> pd.DataFrame:
    """Bases activas de Jabiru sin ninguna variante en Turaco."""
    rows = []
    for base, (sku_j, asin) in j_bases.items():
        if not any(v in turaco_skus for v in [base, f"S{base}"]):
            rows.append({"SKU Jabiru ES": sku_j, "Base SKU": base, "ASIN": asin,
                         "Variantes buscadas": f"{base} | S{base}"})
    return pd.DataFrame(rows)


def check_country(j_bases: dict, listing_skus: set, country_prefix: str) -> pd.DataFrame:
    """Bases activas de Jabiru sin ninguna variante activa en el país."""
    rows = []
    for base, (sku_j, asin) in j_bases.items():
        variants = [base, f"S{base}", f"{country_prefix}{base}"]
        if not any(v in listing_skus for v in variants):
            rows.append({"SKU Jabiru ES": sku_j, "Base SKU": base, "ASIN": asin,
                         "Variantes buscadas": " | ".join(variants)})
    return pd.DataFrame(rows)


# ── Cruce 2: espejos S+SKU faltantes ─────────────────────────────────────────

def check_mirror(j_bases: dict,
                 listing_skus: set,
                 listing_map: dict,
                 store_label: str) -> pd.DataFrame:
    """
    Para cada base activa de Jabiru, verifica que S+base exista en el listing.
    Devuelve filas donde S+base falta.
    """
    rows = []
    for base, (sku_j, asin_base) in j_bases.items():
        s_sku = f"S{base}"
        if s_sku not in listing_skus:
            rows.append({
                "Tienda":        store_label,
                "SKU Jabiru ES": sku_j,
                "Base SKU":      base,
                "SKU espejo":    s_sku,
                "ASIN base":     asin_base,
                "ASIN espejo":   listing_map.get(s_sku, ""),
            })
    return pd.DataFrame(rows)


# ─── Main ─────────────────────────────────────────────────────────────────────

files_ready = all([ps_file, jabiru_file, turaco_file, de_file, fr_file, it_file])
if not files_ready:
    st.info("👈 Carga todos los archivos en el panel izquierdo para comenzar el análisis.")
    st.stop()

with st.spinner("Cargando y procesando archivos…"):
    refs   = read_ps(ps_file)
    jabiru = read_listing(jabiru_file, "Jabiru ES")
    turaco = read_listing(turaco_file, "Turaco ES")
    de     = read_listing(de_file,     "DE")
    fr     = read_listing(fr_file,     "FR")
    it     = read_listing(it_file,     "IT")

with st.sidebar:
    st.divider()
    st.markdown("**📊 SKUs activos por listing:**")
    for _df, flag in [(jabiru,"🇪🇸 Jabiru"),(turaco,"🇪🇸 Turaco"),
                      (fr,"🇫🇷 FR"),(it,"🇮🇹 IT"),(de,"🇩🇪 DE")]:
        total  = _df.attrs.get("total_rows", "?")
        active = _df.attrs.get("active_rows", len(_df))
        pct    = f"{active/total*100:.0f}%" if isinstance(total, int) and total > 0 else "?"
        st.caption(f"{flag}: **{active:,}** activos / {total:,} total ({pct})")

with st.spinner("Calculando cruces…"):
    jabiru_skus = set(jabiru["sku"])
    turaco_skus = set(turaco["sku"])
    fr_skus     = set(fr["sku"])
    it_skus     = set(it["sku"])
    de_skus     = set(de["sku"])

    jabiru_map = build_sku_map(jabiru)
    turaco_map = build_sku_map(turaco)
    fr_map     = build_sku_map(fr)
    it_map     = build_sku_map(it)
    de_map     = build_sku_map(de)

    # Fuente de verdad: bases únicas activas de Jabiru
    j_bases = jabiru_bases_map(jabiru)

    # ── Cruce 1: faltantes ────────────────────────────────────────────────────
    df_ps_vs_jabiru   = check_jabiru_vs_ps(refs, jabiru_skus)
    df_turaco_missing = check_turaco(j_bases, turaco_skus)
    df_fr_missing     = check_country(j_bases, fr_skus,  "FR")
    df_it_missing     = check_country(j_bases, it_skus,  "IT")
    df_de_missing     = check_country(j_bases, de_skus,  "DE")

    # ── Cruce 2: espejos S+SKU ────────────────────────────────────────────────
    df_mirror_jabiru  = check_mirror(j_bases, jabiru_skus, jabiru_map, "Jabiru ES")
    df_mirror_turaco  = check_mirror(j_bases, turaco_skus, turaco_map, "Turaco ES")
    df_mirror_fr      = check_mirror(j_bases, fr_skus,     fr_map,     "FR")
    df_mirror_it      = check_mirror(j_bases, it_skus,     it_map,     "IT")
    df_mirror_de      = check_mirror(j_bases, de_skus,     de_map,     "DE")

    # Resumen unificado de espejos (todas las tiendas)
    df_mirror_all = pd.concat(
        [df_mirror_jabiru, df_mirror_turaco, df_mirror_fr, df_mirror_it, df_mirror_de],
        ignore_index=True
    )

# ─── KPI summary ──────────────────────────────────────────────────────────────
st.subheader("📊 Resumen")

st.markdown("**Cruce 1 – SKUs faltantes por país**")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Referencias PS",           len(refs))
c2.metric("❌ Sin listing Jabiru",     len(df_ps_vs_jabiru))
c3.metric("❌ Turaco ES faltante",     len(df_turaco_missing))
c4.metric("❌ FR faltante",            len(df_fr_missing))
c5.metric("❌ IT faltante",            len(df_it_missing))
c6.metric("❌ DE faltante",            len(df_de_missing))

st.markdown("**Cruce 2 – Espejos S+SKU faltantes**")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("🪞 Jabiru ES", len(df_mirror_jabiru))
m2.metric("🪞 Turaco ES", len(df_mirror_turaco))
m3.metric("🪞 FR",        len(df_mirror_fr))
m4.metric("🪞 IT",        len(df_mirror_it))
m5.metric("🪞 DE",        len(df_mirror_de))

st.divider()

# ─── Tabs ─────────────────────────────────────────────────────────────────────
(tab_ps, tab_turaco, tab_fr, tab_it, tab_de,
 tab_mirror, tab_export) = st.tabs([
    "🇪🇸 Jabiru ES", "🇪🇸 Turaco ES", "🇫🇷 FR", "🇮🇹 IT", "🇩🇪 DE",
    "🪞 Espejos S+SKU", "📥 Exportar todo"])


def show_table(df: pd.DataFrame, label_empty: str = "✅ Sin SKUs pendientes.",
               search_cols: list = None, dl_key: str = "", dl_name: str = "export.csv"):
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
    st.dataframe(filtered, use_container_width=True, height=420)
    csv = filtered.to_csv(index=False).encode("utf-8-sig")
    st.download_button(f"⬇️ Descargar CSV", data=csv,
                       file_name=dl_name, mime="text/csv", key=f"dl_{dl_key}")


# ── Cruce 1 tabs ──────────────────────────────────────────────────────────────
with tab_ps:
    st.markdown("### 🇪🇸 Jabiru ES – Referencias PS sin listing activo")
    show_table(df_ps_vs_jabiru,
               search_cols=["SKU (ref PS)", "Base SKU"],
               dl_key="ps_jabiru", dl_name="jabiru_ES_faltante.csv")

with tab_turaco:
    st.markdown("### 🇪🇸 Turaco ES – Bases de Jabiru sin variante activa en Turaco")
    show_table(df_turaco_missing,
               search_cols=["SKU Jabiru ES", "Base SKU", "ASIN"],
               dl_key="turaco", dl_name="turaco_ES_faltante.csv")

with tab_fr:
    st.markdown("### 🇫🇷 FR – Bases de Jabiru sin variante activa en FR")
    show_table(df_fr_missing,
               search_cols=["SKU Jabiru ES", "Base SKU", "ASIN"],
               dl_key="fr", dl_name="FR_faltante.csv")

with tab_it:
    st.markdown("### 🇮🇹 IT – Bases de Jabiru sin variante activa en IT")
    show_table(df_it_missing,
               search_cols=["SKU Jabiru ES", "Base SKU", "ASIN"],
               dl_key="it", dl_name="IT_faltante.csv")

with tab_de:
    st.markdown("### 🇩🇪 DE – Bases de Jabiru sin variante activa en DE")
    show_table(df_de_missing,
               search_cols=["SKU Jabiru ES", "Base SKU", "ASIN"],
               dl_key="de", dl_name="DE_faltante.csv")

# ── Cruce 2: espejos ──────────────────────────────────────────────────────────
with tab_mirror:
    st.markdown("### 🪞 Espejos S+SKU faltantes")
    st.info("Cada SKU base activo de Jabiru debe tener su `S+SKU` en ES (Jabiru y Turaco) y en FR, IT, DE.")

    subtab_all, subtab_j, subtab_t, subtab_fr2, subtab_it2, subtab_de2 = st.tabs([
        "Todas las tiendas", "Jabiru ES", "Turaco ES", "FR", "IT", "DE"])

    with subtab_all:
        st.markdown("#### Resumen unificado por tienda")
        if df_mirror_all.empty:
            st.success("✅ Todos los espejos S+SKU existen en todas las tiendas.")
        else:
            pivot = (df_mirror_all.groupby("Tienda")
                     .size().reset_index(name="S+SKU faltantes"))
            st.dataframe(pivot, use_container_width=True, hide_index=True)
            st.markdown("#### Detalle completo")
            show_table(df_mirror_all,
                       search_cols=["Tienda", "SKU Jabiru ES", "Base SKU", "SKU espejo", "ASIN base"],
                       dl_key="mirror_all", dl_name="espejos_S_SKU_todos.csv")

    for subtab, df_m, label, dk in [
        (subtab_j,   df_mirror_jabiru, "Jabiru ES", "mirror_jabiru"),
        (subtab_t,   df_mirror_turaco, "Turaco ES", "mirror_turaco"),
        (subtab_fr2, df_mirror_fr,     "FR",        "mirror_fr"),
        (subtab_it2, df_mirror_it,     "IT",        "mirror_it"),
        (subtab_de2, df_mirror_de,     "DE",        "mirror_de"),
    ]:
        with subtab:
            st.markdown(f"#### S+SKU faltantes en {label}")
            show_table(df_m,
                       search_cols=["SKU Jabiru ES", "Base SKU", "SKU espejo", "ASIN base"],
                       dl_key=dk, dl_name=f"espejos_{dk}.csv")

# ── Exportar todo ─────────────────────────────────────────────────────────────
with tab_export:
    st.markdown("### 📥 Exportar todos los resultados en un solo Excel")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Cruce 1
        df_ps_vs_jabiru.to_excel(writer,   sheet_name="C1_Jabiru_ES_faltante",  index=False)
        df_turaco_missing.to_excel(writer,  sheet_name="C1_Turaco_ES",           index=False)
        df_fr_missing.to_excel(writer,      sheet_name="C1_FR",                  index=False)
        df_it_missing.to_excel(writer,      sheet_name="C1_IT",                  index=False)
        df_de_missing.to_excel(writer,      sheet_name="C1_DE",                  index=False)
        # Cruce 2
        df_mirror_all.to_excel(writer,      sheet_name="C2_Espejos_todos",       index=False)
        df_mirror_jabiru.to_excel(writer,   sheet_name="C2_Espejos_Jabiru_ES",   index=False)
        df_mirror_turaco.to_excel(writer,   sheet_name="C2_Espejos_Turaco_ES",   index=False)
        df_mirror_fr.to_excel(writer,       sheet_name="C2_Espejos_FR",          index=False)
        df_mirror_it.to_excel(writer,       sheet_name="C2_Espejos_IT",          index=False)
        df_mirror_de.to_excel(writer,       sheet_name="C2_Espejos_DE",          index=False)

    st.download_button(
        "⬇️ Descargar Excel completo (11 pestañas)",
        data=buf.getvalue(),
        file_name="sku_crossref_completo.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.markdown("""
| Pestaña | Descripción |
|---|---|
| `C1_Jabiru_ES_faltante` | Refs PS sin listing activo en Jabiru |
| `C1_Turaco_ES` | Bases Jabiru sin variante activa en Turaco |
| `C1_FR / IT / DE` | Bases Jabiru sin variante activa en cada país |
| `C2_Espejos_todos` | S+SKU faltantes en todas las tiendas (unificado) |
| `C2_Espejos_Jabiru_ES` | S+SKU faltantes en Jabiru ES |
| `C2_Espejos_Turaco_ES` | S+SKU faltantes en Turaco ES |
| `C2_Espejos_FR / IT / DE` | S+SKU faltantes en cada país |
""")
