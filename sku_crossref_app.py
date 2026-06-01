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
**Lógica de validación (solo SKUs Active):**
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
    Only rows with status == 'Active' are kept.
    """
    try:
        df = pd.read_csv(f, sep="\t", dtype=str, low_memory=False,
                         encoding="utf-8-sig", on_bad_lines="skip")
    except Exception as e:
        st.error(f"❌ Error leyendo {label}: {e}")
        return pd.DataFrame(columns=["sku", "asin"])

    # Filter to Active only (column 'status' present in all listing files)
    status_col = next((c for c in df.columns if c.strip().lower() == "status"), None)
    total_rows = len(df)
    if status_col:
        df = df[df[status_col].str.strip().str.lower() == "active"].copy()
        active_rows = len(df)
    else:
        active_rows = total_rows  # no status column, keep all

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

    # Store stats as attributes for display in sidebar
    df.attrs["label"]       = label
    df.attrs["total_rows"]  = total_rows
    df.attrs["active_rows"] = active_rows

    return df


def sku_set(df: pd.DataFrame) -> set:
    return set(df["sku"].str.upper().dropna())


def sku_asin_map(df: pd.DataFrame) -> dict:
    """Devuelve dict {sku_upper: asin} (primer ASIN encontrado)."""
    return (df.drop_duplicates("sku")
              .set_index(df["sku"].str.upper())["asin"]
              .to_dict())


def extract_base(ref: str) -> str:
    """
    Extrae la BASE del SKU de una referencia PS, eliminando el prefijo de país/variante.

    Tipos de referencia:
      Simple numérico : '01696'             -> '01696'
      S-prefix        : 'S05802'            -> '05802'
      País+numérico   : 'DE01180'           -> '01180'
      Con guión bajo  : 'P82_EU01_115591'   -> 'P82_EU01_115591'
      País+guión bajo : 'DEP82_EU01_115716' -> 'P82_EU01_115716'
                        'SA01_EU01_119670'  -> 'A01_EU01_119670'
      Alfanumérico    : 'V0331'             -> 'V0331'
      País+alfanum    : 'DEV0331'           -> 'V0331'
      Con punto final : '05152.'            -> '05152'
    """
    ref = ref.strip().rstrip(".")

    if "_" in ref:
        # Refs con guión bajo: el modelo empieza por letra+2dígitos+_ (ej. A01_, P82_)
        # El prefijo de país precede al modelo: SA01_, DEP82_, FRA01_...
        m = re.search(r"([A-Z]\d{2}_)", ref.upper())
        if m:
            return ref[m.start():]      # devolver desde donde empieza el modelo
        return ref                      # fallback

    # Sin guión bajo: quitar prefijo DE|FR|IT|S si existe
    m = re.match(r"^(DE|FR|IT|S)([A-Z]?\d+.*)$", ref, re.IGNORECASE)
    if m:
        rest = m.group(2)
        if re.match(r"^\d+$", rest):    # puro numérico -> zero-pad a 5
            return rest.zfill(5)
        return rest                     # alfanumérico tipo V0331

    # Puro numérico sin prefijo
    if re.match(r"^\d+$", ref):
        return ref.zfill(5)

    return ref                          # fallback: tal cual


def build_variants(ref: str, country_prefix: str = "") -> list:
    """
    Construye las variantes de búsqueda para un ref PS dado.

    Para ES:       base, S+base
    Para FR/IT/DE: base, S+base, PREFIX+base
    """
    base = extract_base(ref)
    variants = [base, f"S{base}"]
    if country_prefix:
        variants.append(f"{country_prefix}{base}")
    return variants


def jabiru_bases_map(jabiru_df: pd.DataFrame) -> dict:
    """
    Construye un dict {base_upper -> (sku_jabiru, asin)} a partir de los SKUs
    activos de Jabiru ES. La base es el núcleo sin prefijo de país.
    """
    result = {}
    for _, row in jabiru_df.iterrows():
        sku  = row["sku"]                       # ya está en upper
        base = extract_base(sku).upper()
        if base not in result:                  # primer SKU encontrado gana
            result[base] = (sku, row["asin"])
    return result


def check_country(jabiru_bases: dict,
                  listing_skus: set,
                  country_prefix: str = "") -> pd.DataFrame:
    """
    Para cada base activa de Jabiru ES, comprueba si existe alguna variante
    en el listing del país. Devuelve los que FALTAN.

    Para ES (Jabiru):   base, S+base
    Para FR/IT/DE:      base, S+base, PREFIX+base
    """
    rows = []
    for base, (sku_jabiru, asin) in jabiru_bases.items():
        variants = [base, f"S{base}"]
        if country_prefix:
            variants.append(f"{country_prefix}{base}")
        if not any(v in listing_skus for v in variants):
            rows.append({
                "SKU Jabiru ES":      sku_jabiru,
                "Base SKU":           base,
                "ASIN":               asin,
                "Variantes buscadas": " | ".join(variants),
            })
    return pd.DataFrame(rows)


def check_turaco(jabiru_bases: dict,
                 turaco_skus: set) -> pd.DataFrame:
    """
    Turaco ES debe tener los mismos SKUs activos que Jabiru ES.
    """
    rows = []
    for base, (sku_jabiru, asin) in jabiru_bases.items():
        variants = [base, f"S{base}"]
        if not any(v in turaco_skus for v in variants):
            rows.append({
                "SKU Jabiru ES": sku_jabiru,
                "Base SKU":      base,
                "ASIN":          asin,
            })
    return pd.DataFrame(rows)


def check_jabiru_vs_ps(refs: pd.Series,
                       jabiru_skus: set,
                       jabiru_map: dict) -> pd.DataFrame:
    """
    Referencias PS que no tienen ninguna variante activa en Jabiru ES.
    Estos son los SKUs que faltaría crear/activar primero en Jabiru.
    """
    rows = []
    for ref in refs:
        base     = extract_base(ref).upper()
        variants = [base, f"S{base}"]
        if not any(v in jabiru_skus for v in variants):
            rows.append({
                "SKU (ref PS)":       ref,
                "Base SKU":           base,
                "Variantes buscadas": " | ".join(variants),
            })
    return pd.DataFrame(rows)
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

# Show active/total stats per listing in sidebar
with st.sidebar:
    st.divider()
    st.markdown("**📊 SKUs activos por listing:**")
    for df, flag in [(jabiru, "🇪🇸 Jabiru"), (turaco, "🇪🇸 Turaco"),
                     (fr, "🇫🇷 FR"), (it, "🇮🇹 IT"), (de, "🇩🇪 DE")]:
        total  = df.attrs.get("total_rows", "?")
        active = df.attrs.get("active_rows", len(df))
        pct    = f"{active/total*100:.0f}%" if isinstance(total, int) and total > 0 else "?"
        st.caption(f"{flag}: **{active:,}** activos / {total:,} total ({pct})")

with st.spinner("Calculando cruces…"):

    jabiru_skus  = sku_set(jabiru)
    turaco_skus  = sku_set(turaco)
    de_skus      = sku_set(de)
    fr_skus      = sku_set(fr)
    it_skus      = sku_set(it)

    jabiru_map   = sku_asin_map(jabiru)

    # Base única de cada SKU activo de Jabiru -> fuente de verdad para cruces
    j_bases = jabiru_bases_map(jabiru)

    # ── 1) Referencias PS sin presencia activa en Jabiru ES ───────────────────
    df_es_missing = check_jabiru_vs_ps(refs, jabiru_skus, jabiru_map)

    # ── 2) Jabiru ES vs Turaco ES ─────────────────────────────────────────────
    df_turaco_missing = check_turaco(j_bases, turaco_skus)

    # ── 3) Jabiru ES vs países internacionales ────────────────────────────────
    df_fr_missing = check_country(j_bases, fr_skus, country_prefix="FR")
    df_it_missing = check_country(j_bases, it_skus, country_prefix="IT")
    df_de_missing = check_country(j_bases, de_skus, country_prefix="DE")

# ─── KPI summary ──────────────────────────────────────────────────────────────
st.subheader("📊 Resumen")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Referencias PS",          len(refs))
c2.metric("❌ Sin listing en Jabiru", len(df_es_missing))
c3.metric("❌ Turaco ES faltante",    len(df_turaco_missing))
c4.metric("❌ FR faltante",           len(df_fr_missing))
c5.metric("❌ IT faltante",           len(df_it_missing))
c6.metric("❌ DE faltante",           len(df_de_missing))

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
             "SKUs activos en Jabiru ES que faltan en Turaco ES",
             "SKU Jabiru ES", "ASIN")

with tab_es:
    show_tab(df_es_missing,
             "Referencias PS sin listing activo en Jabiru ES",
             "SKU (ref PS)", "Base SKU")

with tab_fr:
    show_tab(df_fr_missing,
             "SKUs activos de Jabiru ES sin variante activa en FR",
             "SKU Jabiru ES", "ASIN")

with tab_it:
    show_tab(df_it_missing,
             "SKUs activos de Jabiru ES sin variante activa en IT",
             "SKU Jabiru ES", "ASIN")

with tab_de:
    show_tab(df_de_missing,
             "SKUs activos de Jabiru ES sin variante activa en DE",
             "SKU Jabiru ES", "ASIN")

with tab_export:
    st.markdown("### 📥 Exportar todos los resultados en un solo Excel")

    def to_sheet(df: pd.DataFrame, sku_col: str, asin_col: str) -> pd.DataFrame:
        cols = [sku_col, asin_col] if asin_col in df.columns else [sku_col]
        return df[cols].copy()

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Tab Jabiru ES: refs PS sin listing activo
        df_es_missing[["SKU (ref PS)", "Base SKU"]].to_excel(
            writer, sheet_name="Jabiru_ES_faltante", index=False)
        # Turaco, FR, IT, DE: SKU Jabiru + ASIN + variantes
        for df_out, sheet in [
            (df_turaco_missing, "Turaco_ES"),
            (df_fr_missing,     "FR"),
            (df_it_missing,     "IT"),
            (df_de_missing,     "DE"),
        ]:
            cols = [c for c in ["SKU Jabiru ES", "Base SKU", "ASIN", "Variantes buscadas"] if c in df_out.columns]
            df_out[cols].to_excel(writer, sheet_name=sheet, index=False)

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
