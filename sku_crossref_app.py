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

    ps_file      = st.file_uploader("📦 Prestashop BD (CSV semicolon)",
                                    type=["csv","txt"], key="ps")
    jabiru_file  = st.file_uploader("🇪🇸 Jabiru ES (listing Amazon)",
                                    type=["txt","tsv","csv"], key="jabiru")
    turaco_file  = st.file_uploader("🇪🇸 Turaco ES (listing Amazon)",
                                    type=["txt","tsv","csv"], key="turaco")
    de_file      = st.file_uploader("🇩🇪 DE (listing Amazon)",
                                    type=["txt","tsv","csv"], key="de")
    fr_file      = st.file_uploader("🇫🇷 FR (listing Amazon)",
                                    type=["txt","tsv","csv"], key="fr")
    it_file      = st.file_uploader("🇮🇹 IT (listing Amazon)",
                                    type=["txt","tsv","csv"], key="it")

    st.divider()
    st.markdown("""
**Lógica de validación:**
- 🇪🇸 **ES**: busca `SKU` y `S+SKU`
- 🇫🇷 **FR**: busca `SKU`, `S+SKU` y `FR+SKU`
- 🇮🇹 **IT**: busca `SKU`, `S+SKU` y `IT+SKU`
- 🇩🇪 **DE**: busca `SKU`, `S+SKU` y `DE+SKU`
""")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def read_ps(f) -> pd.Series:
    """Read Prestashop CSV (semicolon) and return Series of reference strings."""
    df = pd.read_csv(f, sep=";", dtype=str, low_memory=False)
    col = next((c for c in df.columns if c.strip().lower() == "reference"), None)
    if col is None:
        st.error("❌ No se encontró columna 'reference' en el CSV de Prestashop.")
        return pd.Series(dtype=str)
    refs = df[col].dropna().str.strip()
    refs = refs[refs != ""]
    return refs


def read_listing(f, label: str) -> pd.DataFrame:
    """
    Read an Amazon listing TSV.
    Returns DataFrame with normalised columns: sku (str), asin (str).
    """
    try:
        df = pd.read_csv(f, sep="\t", dtype=str, low_memory=False,
                         encoding="utf-8-sig", on_bad_lines="skip")
    except Exception as e:
        st.error(f"❌ Error leyendo {label}: {e}")
        return pd.DataFrame(columns=["sku", "asin"])

    # Detect SKU column (col A) and ASIN column (col B)
    sku_col  = df.columns[0]
    asin_col = df.columns[1] if len(df.columns) > 1 else None

    df = df[[sku_col, asin_col]].copy() if asin_col else df[[sku_col]].copy()
    df.columns = ["sku", "asin"] if asin_col else ["sku"]
    df["sku"]  = df["sku"].astype(str).str.strip()
    if "asin" in df.columns:
        df["asin"] = df["asin"].astype(str).str.strip()
    else:
        df["asin"] = ""

    df = df[df["sku"].notna() & (df["sku"] != "") & (df["sku"] != "nan")]
    return df


def sku_set(df: pd.DataFrame) -> set:
    return set(df["sku"].str.upper().dropna())


def sku_asin_map(df: pd.DataFrame) -> dict:
    """Devuelve dict {sku_upper: asin} (primer ASIN encontrado)."""
    return (df.drop_duplicates("sku")
              .set_index(df["sku"].str.upper())["asin"]
              .to_dict())


def variants_es(ref: str) -> list:
    """SKU y S+SKU para ES."""
    return [ref, f"S{ref}"]


def variants_intl(ref: str, prefix: str) -> list:
    """SKU, S+SKU y PREFIX+SKU para FR/IT/DE."""
    return [ref, f"S{ref}", f"{prefix}{ref}"]


def check_country(refs: pd.Series,
                  listing_skus: set,
                  listing_map: dict,
                  jabiru_map: dict,
                  get_variants_fn) -> pd.DataFrame:
    """
    Para cada ref de PS comprueba si alguna variante existe en el listing.
    Devuelve un DataFrame con los que FALTAN.
    """
    rows = []
    for ref in refs:
        ref_u = ref.upper()
        variants = [v.upper() for v in get_variants_fn(ref_u)]
        found = any(v in listing_skus for v in variants)
        if not found:
            # Buscar ASIN en Jabiru ES como referencia
            jabiru_asin = ""
            for v in [ref_u, f"S{ref_u}"]:
                if v in jabiru_map:
                    jabiru_asin = jabiru_map[v]
                    break
            rows.append({"SKU (ref PS)": ref,
                         "ASIN (Jabiru ES)": jabiru_asin,
                         "Variantes buscadas": " | ".join(variants)})
    return pd.DataFrame(rows)


def check_turaco(refs: pd.Series,
                 jabiru_skus: set,
                 jabiru_map: dict,
                 turaco_skus: set,
                 turaco_map: dict) -> pd.DataFrame:
    """
    Turaco ES debe tener los mismos SKUs que Jabiru ES.
    Busca todos los SKUs de Jabiru que no estén en Turaco.
    """
    rows = []
    for ref in refs:
        ref_u = ref.upper()
        for variant in [ref_u, f"S{ref_u}"]:
            if variant in jabiru_skus and variant not in turaco_skus:
                asin = jabiru_map.get(variant, "")
                rows.append({"SKU faltante en Turaco ES": variant,
                             "ASIN (Jabiru ES)": asin})
    return pd.DataFrame(rows)


# ─── Main ─────────────────────────────────────────────────────────────────────

files_ready = all([ps_file, jabiru_file, turaco_file, de_file, fr_file, it_file])

if not files_ready:
    st.info("👈 Carga todos los archivos en el panel izquierdo para comenzar el análisis.")
    st.stop()

with st.spinner("Cargando y procesando archivos…"):
    refs      = read_ps(ps_file)
    jabiru    = read_listing(jabiru_file,  "Jabiru ES")
    turaco    = read_listing(turaco_file,  "Turaco ES")
    de        = read_listing(de_file,      "DE")
    fr        = read_listing(fr_file,      "FR")
    it        = read_listing(it_file,      "IT")

    jabiru_skus  = sku_set(jabiru)
    turaco_skus  = sku_set(turaco)
    de_skus      = sku_set(de)
    fr_skus      = sku_set(fr)
    it_skus      = sku_set(it)

    jabiru_map   = sku_asin_map(jabiru)
    turaco_map   = sku_asin_map(turaco)
    de_map       = sku_asin_map(de)
    fr_map       = sku_asin_map(fr)
    it_map       = sku_asin_map(it)

    # ── 1) Turaco ES vs Jabiru ES ──────────────────────────────────────────────
    df_turaco_missing = check_turaco(refs, jabiru_skus, jabiru_map,
                                     turaco_skus, turaco_map)

    # ── 2) Países internacionales ──────────────────────────────────────────────
    df_es_missing = check_country(
        refs, jabiru_skus, jabiru_map, jabiru_map,
        lambda r: variants_es(r))

    df_fr_missing = check_country(
        refs, fr_skus, fr_map, jabiru_map,
        lambda r: variants_intl(r, "FR"))

    df_it_missing = check_country(
        refs, it_skus, it_map, jabiru_map,
        lambda r: variants_intl(r, "IT"))

    df_de_missing = check_country(
        refs, de_skus, de_map, jabiru_map,
        lambda r: variants_intl(r, "DE"))

# ─── KPI summary ──────────────────────────────────────────────────────────────
st.subheader("📊 Resumen")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Referencias PS", len(refs))
c2.metric("❌ Jabiru ES faltante", len(df_es_missing))
c3.metric("❌ Turaco ES faltante", len(df_turaco_missing))
c4.metric("❌ FR faltante", len(df_fr_missing))
c5.metric("❌ IT faltante", len(df_it_missing))
c6.metric("❌ DE faltante", len(df_de_missing))

st.divider()

# ─── Tabs ─────────────────────────────────────────────────────────────────────
tab_turaco, tab_es, tab_fr, tab_it, tab_de, tab_export = st.tabs([
    "🇪🇸 Turaco ES", "🇪🇸 Jabiru ES", "🇫🇷 FR", "🇮🇹 IT", "🇩🇪 DE", "📥 Exportar todo"])


def show_tab(df: pd.DataFrame, title: str, sku_col: str, asin_col: str):
    st.markdown(f"### {title}")
    if df.empty:
        st.success("✅ Sin SKUs pendientes de crear.")
    else:
        st.warning(f"⚠️ **{len(df)} SKUs** pendientes de crear.")
        # search filter
        q = st.text_input("🔍 Filtrar SKU/ASIN", key=title)
        filtered = df
        if q:
            mask = df[sku_col].str.contains(q, case=False, na=False)
            if asin_col in df.columns:
                mask |= df[asin_col].str.contains(q, case=False, na=False)
            filtered = df[mask]
        st.dataframe(filtered, use_container_width=True, height=420)

        # Download
        out_df = filtered[[sku_col, asin_col]].copy() if asin_col in filtered.columns else filtered[[sku_col]].copy()
        csv_bytes = out_df.to_csv(index=False).encode("utf-8-sig")
        slug = title.replace(" ", "_").replace("🇪🇸","ES").replace("🇫🇷","FR").replace("🇮🇹","IT").replace("🇩🇪","DE")
        st.download_button(f"⬇️ Descargar CSV – {title}",
                           data=csv_bytes,
                           file_name=f"skus_crear_{slug}.csv",
                           mime="text/csv",
                           key=f"dl_{title}")


with tab_turaco:
    show_tab(df_turaco_missing,
             "SKUs a crear en Turaco ES (que están en Jabiru ES pero no en Turaco ES)",
             "SKU faltante en Turaco ES", "ASIN (Jabiru ES)")

with tab_es:
    show_tab(df_es_missing,
             "SKUs a crear en Jabiru ES (referencias PS sin listing en Amazon ES)",
             "SKU (ref PS)", "ASIN (Jabiru ES)")

with tab_fr:
    show_tab(df_fr_missing,
             "SKUs a crear en FR",
             "SKU (ref PS)", "ASIN (Jabiru ES)")

with tab_it:
    show_tab(df_it_missing,
             "SKUs a crear en IT",
             "SKU (ref PS)", "ASIN (Jabiru ES)")

with tab_de:
    show_tab(df_de_missing,
             "SKUs a crear en DE",
             "SKU (ref PS)", "ASIN (Jabiru ES)")

with tab_export:
    st.markdown("### 📥 Exportar todos los resultados en un solo Excel")

    def to_sheet(df: pd.DataFrame, sku_col: str, asin_col: str) -> pd.DataFrame:
        cols = [sku_col, asin_col] if asin_col in df.columns else [sku_col]
        return df[cols].copy()

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        to_sheet(df_turaco_missing, "SKU faltante en Turaco ES", "ASIN (Jabiru ES)").to_excel(
            writer, sheet_name="Turaco_ES", index=False)
        to_sheet(df_es_missing, "SKU (ref PS)", "ASIN (Jabiru ES)").to_excel(
            writer, sheet_name="Jabiru_ES", index=False)
        to_sheet(df_fr_missing, "SKU (ref PS)", "ASIN (Jabiru ES)").to_excel(
            writer, sheet_name="FR", index=False)
        to_sheet(df_it_missing, "SKU (ref PS)", "ASIN (Jabiru ES)").to_excel(
            writer, sheet_name="IT", index=False)
        to_sheet(df_de_missing, "SKU (ref PS)", "ASIN (Jabiru ES)").to_excel(
            writer, sheet_name="DE", index=False)

    st.download_button(
        "⬇️ Descargar Excel completo (todas las pestañas)",
        data=buf.getvalue(),
        file_name="skus_a_crear_todos_paises.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.markdown("""
**Contenido del Excel:**
| Pestaña | Descripción |
|---|---|
| `Turaco_ES` | SKUs de Jabiru ES que faltan en Turaco ES |
| `Jabiru_ES` | Referencias PS sin listing en Amazon ES |
| `FR` | Referencias PS sin ninguna variante en Amazon FR |
| `IT` | Referencias PS sin ninguna variante en Amazon IT |
| `DE` | Referencias PS sin ninguna variante en Amazon DE |
""")
