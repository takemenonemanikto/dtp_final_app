import json
import io
import csv
import re
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

import pandas as pd

import io
import zipfile
st.set_page_config(page_title="AlfaLabs DTP", layout="wide")

# ------------------------
# VERSIONING
# ------------------------
APP_VERSION = "1.0.0"
SCHEMA_VERSION = "v1"

# ------------------------
# SESSION DEFAULTS
# ------------------------
if "assessment_meta" not in st.session_state:
    st.session_state["assessment_meta"] = {
        "institution": "",
        "country": "",
        "assessor": "",
        "notes": "",
        "version_note": "",
    }
if "assessment_locked" not in st.session_state:
    st.session_state["assessment_locked"] = False
if "disclaimer_accepted" not in st.session_state:
    st.session_state["disclaimer_accepted"] = False


# ------------------------
# DISCLAIMER
# ------------------------
DISCLAIMER_TITLE = "Disclaimer and Data Handling Notice"
DISCLAIMER_TEXT = """
This prototype application is intended to support institutional self-assessment within the AlfaLabs project. It is designed for reflection, discussion, and action planning in the area of AI maturity and digital transformation in higher education institutions.

Users should not enter personal data, special category data, or other confidential information into the application unless explicitly permitted by their institution’s internal rules and technical deployment environment. Where possible, please use aggregated, non-identifiable, and non-sensitive information only.

Any uploaded documents or evidence files should be limited to materials that the user is authorized to use and share for assessment purposes. The user remains responsible for the content uploaded into the application.

Please note that this tool does not constitute a formal audit, legal assessment, or compliance certification. The handling, storage, and retention of any entered data may depend on the specific deployment context of the application, especially when used via a web-based environment.

By continuing, you confirm that you understand these conditions and will use the application accordingly.
"""


def render_disclaimer_gate():
    st.title(DISCLAIMER_TITLE)
    st.markdown(DISCLAIMER_TEXT)
    st.warning(
        "Do not enter personal, sensitive, or confidential data unless this is explicitly allowed by your institution and deployment environment."
    )

    st.markdown("### Confirmation")
    c1 = st.checkbox(
        "I understand that this tool is intended for institutional self-assessment and planning purposes only.",
        key="disclaimer_confirm_purpose",
    )
    c2 = st.checkbox(
        "I confirm that I will not enter personal, sensitive, or confidential data unless permitted by my institution and the deployment environment.",
        key="disclaimer_confirm_data",
    )
    c3 = st.checkbox(
        "I confirm that any uploaded files are materials I am authorized to use for this assessment.",
        key="disclaimer_confirm_uploads",
    )

    can_continue = c1 and c2 and c3
    st.button(
        "Continue to the application",
        type="primary",
        disabled=not can_continue,
        key="disclaimer_continue_btn",
    )

    if can_continue and st.session_state.get("disclaimer_continue_btn"):
        st.session_state["disclaimer_accepted"] = True
        if hasattr(st, "rerun"):
            st.rerun()
        else:
            st.experimental_rerun()


def render_sidebar_data_notice():
    st.caption("⚠️ Data handling reminder")
    st.caption("Do not upload personal, sensitive, or confidential data.")

# ------------------------
# CONFIG
# ------------------------
APP_DIR = Path(__file__).parent
if (APP_DIR / "areas").exists():
    AREAS_DIR = APP_DIR / "areas"
else:
    AREAS_DIR = APP_DIR

# Sidebar labels (kept as before)
AREAS = {
    "Summary": "summary",
    "3.1 Leadership & Governance Practices": "3_1",
    "3.2 Teaching & Learning Practices": "3_2",
    "3.3 Content & Curricula": "3_3",
    "3.4 Professional Development": "3_4",
    "3.5 AI supported Assessment Practices": "3_5",
    "3.6 Collaboration & Networking": "3_6",
    "3.7 Infrastructure": "3_7",
    "3.8 AI regulations & Ethics": "3_8",
    "3.9 Security & Privacy": "3_9",
}

# ------------------------
# HELPERS
# ------------------------
def load_area_json(area_code: str) -> dict:
    """
    Loads JSON config from ./areas/<area_code>.json (e.g., 3_7.json).
    """
    path = AREAS_DIR / f"{area_code}.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing area config: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())

def parse_target(target_str: str):
    """
    Parse target strings like:
      '≥30%' -> ('num','ge',30.0,'%')
      '≤48'  -> ('num','le',48.0,'')
      'Baseline review' -> ('cat',None,'Baseline review',None)
    """
    s = (target_str or "").strip()
    if not s:
        return ("cat", None, "", None)

    # Numeric with operators
    m = re.match(r"^(≥|<=|≤|>=)\s*([0-9]+(?:\.[0-9]+)?)\s*%?\s*$", s)
    if m:
        op = m.group(1)
        val = float(m.group(2))
        unit = "%" if "%" in s else ""
        direction = "ge" if op in ("≥", ">=") else "le"
        return ("num", direction, val, unit)

    # Numeric without operator (rare) -> treat as categorical
    return ("cat", None, s, None)

def kpi_status(direction: str, current: float, target: float):
    """
    Returns emoji + status based on direction:
    - ge: current >= target is good
    - le: current <= target is good
    With near-threshold bands (±10%).
    """
    if direction == "ge":
        if current >= target:
            return "🟢", "On track"
        if current >= 0.9 * target:
            return "🟡", "Near target"
        return "🔴", "Behind"
    if direction == "le":
        if current <= target:
            return "🟢", "On track"
        if current <= 1.1 * target:
            return "🟡", "Near target"
        return "🔴", "Behind"
    return "⚪", "N/A"

def ensure_state(area_key: str, matrix_items: list, foundational_actions: list, kpis: list):
    """
    Initialize per-area state in st.session_state under a namespace.
    """
    ns = st.session_state.setdefault("areas_state", {})
    area_state = ns.setdefault(area_key, {})

    # Items
    item_keys = [f"i{it['id']}" for it in matrix_items]
    area_state.setdefault("scores", {k: 0 for k in item_keys})
    area_state.setdefault("evidence", {k: "" for k in item_keys})
    area_state.setdefault("owner", {k: "" for k in item_keys})
    area_state.setdefault("evidence_files", {k: [] for k in item_keys})

    # Foundations
    fa_keys = [fa["id"] for fa in foundational_actions]
    area_state.setdefault("foundations_done", {k: False for k in fa_keys})

    # KPIs current
    kpi_keys = [k["id"] for k in kpis]
    area_state.setdefault("kpi_current", {k: "" for k in kpi_keys})

    return area_state

def reset_area_state(area_key: str, matrix_items: list, foundational_actions: list, kpis: list):
    ns = st.session_state.setdefault("areas_state", {})
    item_keys = [f"i{it['id']}" for it in matrix_items]
    fa_keys = [fa["id"] for fa in foundational_actions]
    kpi_keys = [k["id"] for k in kpis]

    ns[area_key] = {
        "scores": {k: 0 for k in item_keys},
        "evidence": {k: "" for k in item_keys},
        "owner": {k: "" for k in item_keys},
        "evidence_files": {k: [] for k in item_keys},
        "foundations_done": {k: False for k in fa_keys},
        "kpi_current": {k: "" for k in kpi_keys},
    }

# ------------------------
# GENERIC AREA RENDERER (v1)
# ------------------------
def render_area_from_json(area_code: str):
    cfg = load_area_json(area_code)
    LOCKED = bool(st.session_state.get('assessment_locked', False))

    area_id = cfg.get("area_id", area_code.replace("_", "."))
    area_name = cfg.get("area_name", area_code)
    area_key = area_code  # session namespace key

    maturity_items = cfg["maturity_matrix"]["items"]
    score_ranges = cfg["maturity_matrix"]["score_ranges"]
    foundational_actions = cfg["build_action_plan"]["foundational_actions"]
    actions_by_level = cfg["build_action_plan"]["actions_by_maturity_level"]
    kpis = cfg["kpis"]["items"]
    owners_roles = cfg.get("owners_and_roles", {})

    area_state = ensure_state(area_key, maturity_items, foundational_actions, kpis)

    # Per-area metadata (different assessors can fill different areas)
    area_meta = area_state.setdefault("meta", {
        "assessor": "",
        "unit": "",
        "date": "",
        "notes": "",
    })

    with st.expander("Assessment data (who filled this area)", expanded=False):
        # These fields are per-area (stored inside the area state JSON)
        colm1, colm2 = st.columns(2)
        with colm1:
            area_meta["assessor"] = st.text_input(
                "Area assessor / contributor",
                value=area_meta.get("assessor", ""),
                key=f"{area_key}_meta_assessor",
                disabled=LOCKED,
            )
            area_meta["unit"] = st.text_input(
                "Unit / department (optional)",
                value=area_meta.get("unit", ""),
                key=f"{area_key}_meta_unit",
                disabled=LOCKED,
            )
        with colm2:
            area_meta["date"] = st.text_input(
                "Date (optional)",
                value=area_meta.get("date", ""),
                key=f"{area_key}_meta_date",
                disabled=LOCKED,
            )
        area_meta["notes"] = st.text_area(
            "Notes (optional)",
            value=area_meta.get("notes", ""),
            key=f"{area_key}_meta_notes",
            height=90,
            disabled=LOCKED,
        )
        area_state["meta"] = area_meta

    # Global lock control (applies across ALL areas)
    lock_key = f"{area_key}_lock_toggle"
    st.toggle(
        "🔒 Lock assessment (global)",
        value=bool(st.session_state.get("assessment_locked", False)),
        key=lock_key,
        help="When locked, inputs are disabled across all areas.",
    )
    # Sync toggle -> global lock
    if st.session_state.get(lock_key) != st.session_state.get("assessment_locked", False):
        st.session_state["assessment_locked"] = bool(st.session_state.get(lock_key))
        if hasattr(st, "rerun"):
            st.rerun()

    st.title(f"{area_id} {area_name} — Prototype (AlfaLabs DTP)")
    if LOCKED:
        st.info("🔒 Assessment is locked. Inputs are disabled.")
    else:
        st.caption("Assessment is editable. Use the sidebar toggle to lock/unlock globally.")

    # Intro + Overview
    if cfg.get("intro"):
        st.caption(cfg["intro"].strip())
    if cfg.get("overview"):
        with st.expander("📖 Area Overview"):
            st.markdown(cfg["overview"].strip())

    # Reset
    if st.button(f"Reset {area_id} form"):
        reset_area_state(area_key, maturity_items, foundational_actions, kpis)
        if hasattr(st, "rerun"):
            st.rerun()
        else:
            st.experimental_rerun()

    st.markdown(
        "This page captures the **maturity matrix**, computes the **score & level**, "
        "and provides **foundational actions**, **level-based actions**, and **KPIs**."
    )

    # Scoring anchors
    with st.expander("ℹ️ Scoring anchors (0–1–2)"):
        st.markdown("""
- **0** — Not in place  
- **1** — Partially in place  
- **2** — Fully in place  
""")

    # ------------------------
    # 3.7.2 Maturity matrix
    # ------------------------
    st.subheader(f"{area_id}.2 Score Maturity Level (Self-assessment)")
    st.markdown(cfg.get("instructions", {}).get("maturity_scoring", ""))

    # Table header
    h = st.columns([3, 2, 2, 3, 2])
    h[0].markdown("**Item**")
    h[1].markdown("**Score (0–2)**")
    h[2].markdown("**Owner**")
    h[3].markdown("**Evidence / Note**")
    h[4].markdown("**Evidence files**")

    # Render items
    for it in maturity_items:
        item_key = f"i{it['id']}"
        label = it["item"]
        is_critical = bool(it.get("critical"))
        linked_kpis = ", ".join(it.get("linked_kpis", []))

        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 3, 2])
        c1.write(label + ("  🔴" if is_critical else ""))
        if linked_kpis:
            c1.caption(f"Linked KPIs: {linked_kpis}")

        anchors = it.get("score_anchors", {})
        def fmt(v):
            t = anchors.get(v, "")
            return f"{v} — {t}" if t else str(v)

        area_state["scores"][item_key] = c2.radio(
            f"{area_key}_score_{item_key}",
            [0, 1, 2],
            index=int(area_state["scores"].get(item_key, 0)),
            format_func=fmt,
            horizontal=True,
            disabled=LOCKED,
            label_visibility="collapsed",
        )

        # Owner: free text but with suggestion (from JSON)
        owner_suggestion = it.get("owners", "")
        
        # Owner dropdown (from JSON owners_and_roles)
        owner_options = list(owners_roles.keys()) if owners_roles else []
        current_owner = area_state["owner"].get(item_key, "")
        if current_owner not in owner_options:
            owner_options = owner_options + ([current_owner] if current_owner else [])
        area_state["owner"][item_key] = c3.selectbox(
            f"{area_key}_owner_{item_key}",
            owner_options if owner_options else ["Select owner"],
            index=owner_options.index(current_owner) if current_owner in owner_options else 0,
            label_visibility="collapsed",
            disabled=LOCKED,
        )


        area_state["evidence"][item_key] = c4.text_input(
            f"{area_key}_evidence_{item_key}",
            value=area_state["evidence"].get(item_key, ""),
            placeholder=it.get("evidence_examples", "Evidence / link / short note"),
            label_visibility="collapsed",
            disabled=LOCKED,
        )

        up = c5.file_uploader(
            "Upload",
            type=["pdf", "doc", "docx", "xls", "xlsx", "png", "jpg"],
            key=f"{area_key}_file_{item_key}",
            label_visibility="collapsed",
            disabled=LOCKED,
        )
        if up is not None:
            area_state.setdefault("evidence_files", {})
            area_state["evidence_files"].setdefault(item_key, [])
            area_state["evidence_files"][item_key].append((up.name, up.read()))

        files_for_item = area_state.get("evidence_files", {}).get(item_key, [])
        if files_for_item:
            c5.caption(", ".join([n for (n, _) in files_for_item]))
    # ------------------------
    # Score + level
    # ------------------------
    total_score = sum(int(v) for v in area_state["scores"].values())

    def level_from_score(score: int) -> str:
        for r in score_ranges:
            rng = r["score_range"]
            # e.g. "0–5", "6–10", "11–14"
            m = re.match(r"^\s*(\d+)\s*[–-]\s*(\d+)\s*$", rng)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if lo <= score <= hi:
                    return r["maturity_level"]
        # fallback
        if score <= 5:
            return "Emerging"
        if score <= 10:
            return "Established"
        return "Enhanced"

    level = level_from_score(total_score)

    st.markdown("---")
    m1, m2 = st.columns(2)
    m1.metric("Area score", total_score)
    m2.metric("Maturity level", level)

    # Score interpretation (from JSON score_ranges)
    if score_ranges:
        # Show a short interpretation for the *current* level, plus optional details for all levels.
        current_interp = next((r for r in score_ranges if str(r.get("maturity_level","")).strip() == str(level).strip()), None)
        if current_interp:
            st.markdown("**What this level means**")
            st.info(f"{current_interp.get('maturity_level','')}: {current_interp.get('interpretation','')}")
            st.caption(f"Score range: {current_interp.get('score_range','')}")
        with st.expander("See interpretations for all levels", expanded=False):
            for r in score_ranges:
                lvl = str(r.get("maturity_level","")).strip()
                rng = str(r.get("score_range","")).strip()
                interp = str(r.get("interpretation","")).strip()
                title = f"{lvl} ({rng})" if rng else lvl
                if lvl == str(level).strip():
                    with st.expander(f"✅ {title}", expanded=True):
                        st.write(interp)
                else:
                    with st.expander(title, expanded=False):
                        st.write(interp)

    # Radar
    st.subheader("Maturity profile (radar)")
    if not maturity_items:
        st.info("No maturity items are defined for this area yet.")
    else:
        categories = [f"Item {it['id']}" for it in maturity_items]
        scores = [area_state["scores"][f"i{it['id']}"] for it in maturity_items]
        fig_radar = go.Figure()
        # Close the loop only if we have at least one point
        fig_radar.add_trace(
            go.Scatterpolar(
                r=scores + [scores[0]],
                theta=categories + [categories[0]],
                fill="toself",
                name="Score",
            )
        )
        fig_radar.update_polars(radialaxis=dict(visible=True, range=[0, 2]))
        st.plotly_chart(fig_radar, width="stretch", config={})

    # ------------------------
    # Foundational actions + gating
    # ------------------------
    critical_zero_item_ids = [it["id"] for it in maturity_items if it.get("critical") and area_state["scores"][f"i{it['id']}"] == 0]

    # Build mapping: item_id -> foundational action ids from JSON "required_if"
    # Supports both:
    # - required_if: {"item_id": 1, "score_equals": 0}
    # - required_if: [{"item_id": 1, "score_equals": 0}, {"item_id": 7, "score_equals": 0}]
    item_to_fas = {}
    for fa in foundational_actions:
        cond = fa.get("required_if", {})
        cond_list = cond if isinstance(cond, list) else [cond]
        for c in cond_list:
            if not isinstance(c, dict):
                continue
            if c.get("item_id") is not None and c.get("score_equals") is not None:
                item_to_fas.setdefault(int(c["item_id"]), []).append(fa["id"])
    required_fas = sorted({fa_id for item_id in critical_zero_item_ids for fa_id in item_to_fas.get(int(item_id), [])})

    st.markdown("---")
    st.subheader(f"{area_id}.3 Build the Action Plan")

    if cfg["build_action_plan"].get("intro"):
        st.markdown(cfg["build_action_plan"]["intro"].strip())

    if critical_zero_item_ids:
        st.warning(
            "At least one **critical** item is scored **0**. "
            "Complete the required foundational actions below to unlock level-based actions."
        )
        st.markdown("#### Required foundational actions")
        # Render only required ones
        for fa_id in required_fas:
            fa = next((x for x in foundational_actions if x["id"] == fa_id), None)
            if not fa:
                continue
            label = fa["action"]
            area_state["foundations_done"][fa_id] = st.checkbox(
                label,
                value=bool(area_state["foundations_done"].get(fa_id, False)),
                disabled=LOCKED,
                key=f"{area_key}_fa_{fa_id}",
            )
            owner_hint = fa.get("owners", "")
            if owner_hint:
                st.caption(f"Owner: {owner_hint}")

        done_count = sum(1 for fa_id in required_fas if area_state["foundations_done"].get(fa_id, False))
        st.caption(f"Foundations completed: **{done_count}/{len(required_fas)}**")

    # IMPORTANT: compute gating AFTER checkbox render (to avoid check/uncheck glitch)
    if critical_zero_item_ids:
        all_required_done = (len(required_fas) > 0) and all(area_state["foundations_done"].get(fa_id, False) for fa_id in required_fas)
        unlock_actions = all_required_done
    else:
        unlock_actions = True

    # Optional debug
    with st.expander("🐞 Gating debug (optional)"):
        st.write({
            "critical_zero_item_ids": critical_zero_item_ids,
            "required_foundations": required_fas,
            "foundations_done": {k: area_state["foundations_done"].get(k) for k in required_fas},
            "unlock_actions": unlock_actions,
        })

    st.markdown("#### Level-based actions")
    st.caption(f"Current level: **{level}**")

    if unlock_actions:
        for idx, a in enumerate(actions_by_level.get(level, []), start=1):
            action_text = a.get("action", "")
            st.checkbox(action_text, key=f"{area_key}_act_{level}_{idx}")
            meta = []
            if a.get("linked_kpis"):
                meta.append("Linked KPIs: " + ", ".join(a["linked_kpis"]))
            if a.get("owners"):
                meta.append("Owner: " + a["owners"])
            if meta:
                st.caption(" • ".join(meta))
    else:
        st.info("Actions will appear once all required foundational actions are completed for critical items scored 0.")

    # ------------------------
    # KPI section
    # ------------------------
    st.markdown("---")
    st.subheader(f"{area_id}.5 Key Performance Indicators (KPIs)")
    if cfg["kpis"].get("intro"):
        st.markdown(cfg["kpis"]["intro"].strip())

    # KPI evaluation (heuristic based on targets)
    for k in kpis:
        kpi_id = k["id"]
        name = k["name"]
        targets = k["targets"]
        owner = k.get("owners", "")

        # Use the target of current level for parsing (best available)
        t_str = targets.get(level, "")
        ktype, direction, t_val, unit = parse_target(t_str)

        c1, c2, c3, c4 = st.columns([4, 2, 2, 2])
        c1.markdown(f"**{kpi_id}: {name}**")
        if owner:
            c1.caption(f"Owner: {owner}")

        c2.markdown(f"**Target ({level})**")
        c2.write(t_str)

        if ktype == "num" and direction:
            # numeric current input
            prev = area_state["kpi_current"].get(kpi_id, "")
            try:
                prev_val = float(prev) if str(prev).strip() != "" else 0.0
            except Exception:
                prev_val = 0.0

            cur_val = c3.number_input(
                "Current",
                value=float(prev_val),
                step=1.0,
                key=f"{area_key}_kpi_{kpi_id}",
                label_visibility="collapsed",
            )
            area_state["kpi_current"][kpi_id] = str(cur_val)

            emoji, status = kpi_status(direction, float(cur_val), float(t_val))
            c4.markdown(f"**{emoji} {status}**")
            if unit:
                c3.caption(unit)
        else:
            # categorical current selection: use the 3 level targets as ordered options (Emerging->Established->Enhanced)
            options = [
                targets.get("Emerging", "").strip(),
                targets.get("Established", "").strip(),
                targets.get("Enhanced", "").strip(),
            ]
            # Remove empties while keeping order
            options = [o for o in options if o]
            # Ensure unique
            uniq = []
            for o in options:
                if o not in uniq:
                    uniq.append(o)
            options = uniq if uniq else [""]

            prev = area_state["kpi_current"].get(kpi_id, options[0] if options else "")
            if prev not in options and options:
                prev = options[0]

            cur = c3.selectbox(
                "Current",
                options,
                index=options.index(prev) if prev in options else 0,
                key=f"{area_key}_kpi_{kpi_id}",
                label_visibility="collapsed",
            )
            area_state["kpi_current"][kpi_id] = cur

            # Status: compare ordinal position vs target option (assume higher maturity option is better)
            try:
                target_opt = targets.get(level, "").strip()
                if target_opt in options and cur in options:
                    ci, ti = options.index(cur), options.index(target_opt)
                    if ci >= ti:
                        emoji, status = "🟢", "On track"
                    elif ci == ti - 1:
                        emoji, status = "🟡", "Near target"
                    else:
                        emoji, status = "🔴", "Behind"
                else:
                    emoji, status = "⚪", "N/A"
            except Exception:
                emoji, status = "⚪", "N/A"

            c4.markdown(f"**{emoji} {status}**")

    # Owner glossary
    if owners_roles:
        with st.expander("👥 Role Descriptions (KPI Owners)"):
            for role, desc in owners_roles.items():
                st.markdown(f"**{role}** — {desc}")

    # ------------------------
    # Exports
    # ------------------------
    st.markdown("---")
    st.subheader("Exports and Imports")

    # Matrix CSV
    matrix_csv = io.StringIO()
    w = csv.writer(matrix_csv)
    w.writerow(["Item ID", "Item", "Critical", "Score", "Owner", "Evidence", "Evidence files", "Linked KPIs"])
    for it in maturity_items:
        item_key = f"i{it['id']}"
        files_list = area_state["evidence_files"].get(item_key, [])
        w.writerow([
            it["id"],
            norm_ws(it["item"]),
            "Yes" if it.get("critical") else "No",
            area_state["scores"].get(item_key, 0),
            area_state["owner"].get(item_key, ""),
            area_state["evidence"].get(item_key, ""),
            "; ".join([n for (n, _) in files_list]),
            ", ".join(it.get("linked_kpis", [])),
        ])
    st.download_button("Save Matrix CSV", data=matrix_csv.getvalue().encode("utf-8"), file_name=f"{area_code}_matrix.csv", mime="text/csv")

    # KPI CSV
    kpi_csv = io.StringIO()
    w = csv.writer(kpi_csv)
    w.writerow(["KPI ID", "KPI", "Owner", "Target Emerging", "Target Established", "Target Enhanced", "Current"])
    for k in kpis:
        w.writerow([
            k["id"],
            norm_ws(k["name"]),
            k.get("owners", ""),
            k["targets"].get("Emerging", ""),
            k["targets"].get("Established", ""),
            k["targets"].get("Enhanced", ""),
            area_state["kpi_current"].get(k["id"], ""),
        ])
    st.download_button("Save KPIs CSV", data=kpi_csv.getvalue().encode("utf-8"), file_name=f"{area_code}_kpis.csv", mime="text/csv")

    # Snapshot JSON (state only)
    snapshot = {
        "area": area_id,
        "area_name": area_name,
        "score": total_score,
        "level": level,
        "scores": area_state["scores"],
        "owners": area_state["owner"],
        "evidence": area_state["evidence"],
        "foundations_done": {k: area_state["foundations_done"].get(k, False) for k in required_fas},
        "kpi_current": area_state["kpi_current"],
        "evidence_files_names": {k: [n for (n, _) in v] for k, v in area_state["evidence_files"].items()},
    }
    #st.download_button(
     #   "Download snapshot (JSON)",
      #  data=json.dumps(snapshot, indent=2, ensure_ascii=False).encode("utf-8"),
       # file_name=f"{area_code}_snapshot.json",
        #mime="application/json",
    #)
    #st.caption("Note: The downloaded snapshot includes assessment responses, notes, and selected values, but does not include uploaded files. If needed, please store evidence files separately.")


# ------------------------
# SUMMARY PAGE (kept minimal)
# ------------------------

def _safe(text, limit=500):
    """Safe text for PDF (avoid None, overly long strings)."""
    if text is None:
        return ""
    s = str(text).strip()
    s = s.replace("\u2013", "-").replace("\u2014", "-")  # avoid unicode dashes
    if len(s) > limit:
        s = s[:limit-3] + "..."
    return s

def generate_pdf_report(summary_df, areas_state, configs, blockers_only, all_critical_gaps, suggested_actions, meta=None, app_version=None, schema_version=None):
    """
    Build a concise PDF report from current session state.
    Returns: bytes
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2.0*cm,
        rightMargin=2.0*cm,
        topMargin=1.8*cm,
        bottomMargin=1.8*cm,
        title="DTP Assessment Report",
        author="AlfaLabs Prototype",
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H2", parent=styles["Heading2"], spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=9, leading=11))
    styles.add(ParagraphStyle(name="Tiny", parent=styles["BodyText"], fontSize=8, leading=10))

    elements = []
    elements.append(Paragraph("Digital Transformation Plan - AI Maturity Assessment Report", styles["Title"]))
    elements.append(Spacer(1, 10))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    elements.append(Paragraph(_safe(ts), styles["Small"]))
    if app_version or schema_version:
        elements.append(Paragraph(_safe(f"App version: {app_version or '?'} | Schema: {schema_version or '?'}"), styles["Small"]))
    if meta:
        inst = _safe(meta.get("institution",""))
        country = _safe(meta.get("country",""))
        assessor = _safe(meta.get("assessor",""))
        note = _safe(meta.get("version_note",""))
        if inst or country or assessor or note:
            elements.append(Spacer(1, 6))
            elements.append(Paragraph(_safe(f"Institution: {inst}"), styles["Small"]))
            if country:
                elements.append(Paragraph(_safe(f"Country: {country}"), styles["Small"]))
            if assessor:
                elements.append(Paragraph(_safe(f"Assessor: {assessor}"), styles["Small"]))
            if note:
                elements.append(Paragraph(_safe(f"Assessment note: {note}"), styles["Small"]))
    elements.append(Spacer(1, 14))

    # Executive summary
    elements.append(Paragraph("Executive Summary", styles["H2"]))
    elements.append(Paragraph(
        "This report summarizes the current self-assessment results across DTP areas. "
        "Scores and maturity levels are based on the selected anchors (0-1-2) and the area score ranges.",
        styles["BodyText"]
    ))
    elements.append(Spacer(1, 10))

    # Summary table
    tbl_data = [list(summary_df.columns)]
    for _, r in summary_df.iterrows():
        tbl_data.append([_safe(r[c], 120) for c in summary_df.columns])

    table = Table(tbl_data, colWidths=[6.7*cm, 2.6*cm, 3.2*cm, 3.0*cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("TEXTCOLOR", (0,0), (-1,0), colors.black),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9),
        ("FONTSIZE", (0,1), (-1,-1), 9),
        ("ALIGN", (1,1), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.white]),
        ("BOTTOMPADDING", (0,0), (-1,0), 6),
        ("TOPPADDING", (0,0), (-1,0), 6),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))

    # Critical gaps overview
    elements.append(Paragraph("Blocking Critical Gaps (score = 0)", styles["H2"]))
    if not blockers_only:
        elements.append(Paragraph("No blocking critical gaps detected in started areas.", styles["BodyText"]))
    else:
        by_area = {}
        for area_label, item_id, item_text, sc in blockers_only:
            by_area.setdefault(area_label, []).append((item_id, item_text, sc))
        for area_label, rows in by_area.items():
            elements.append(Paragraph(_safe(area_label), styles["Heading3"]))
            for item_id, item_text, sc in sorted(rows, key=lambda x: x[0]):
                elements.append(Paragraph(f"- Item {item_id}: {_safe(item_text, 240)}", styles["Small"]))
            elements.append(Spacer(1, 6))
    elements.append(Spacer(1, 10))

    # Detailed area pages (only started areas)
    elements.append(PageBreak())
    elements.append(Paragraph("Area Details", styles["H2"]))
    elements.append(Paragraph(
        "The following pages include the filled maturity matrix values (score, owner, evidence) and the top suggested actions.",
        styles["BodyText"]
    ))
    elements.append(Spacer(1, 10))

    # Prepare quick lookup for gaps/actions
    sug_by_area = {}
    for area_label, level, text in suggested_actions:
        sug_by_area.setdefault(area_label, []).append((level, text))

    gaps_all_by_area = {}
    for area_label, item_id, item_text, sc in all_critical_gaps:
        gaps_all_by_area.setdefault(area_label, []).append((item_id, item_text, sc))

    for code_key, st_area in (areas_state or {}).items():
        if not str(code_key).startswith("3_"):
            continue
        scores_dict = st_area.get("scores", {})
        if not scores_dict:
            continue  # only started areas

        cfg = configs.get(code_key) or {}
        area_id = cfg.get("area_id", code_key.replace("_", "."))
        area_name = cfg.get("area_name", code_key)
        area_label = f"{area_id} {area_name}"

        maturity_items = cfg.get("maturity_matrix", {}).get("items", [])
        score_ranges = cfg.get("maturity_matrix", {}).get("score_ranges", [])

        max_area_score = 2 * len(maturity_items) if maturity_items else 0
        area_score = sum(int(v) for v in scores_dict.values()) if scores_dict else 0

        # Determine level from score ranges (same logic as UI)
        level = "N/A"
        for r in score_ranges:
            rng = r.get("score_range", "")
            m = re.match(r"^\\s*(\\d+)\\s*[–-]\\s*(\\d+)\\s*$", rng)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if lo <= area_score <= hi:
                    level = r.get("maturity_level", "Emerging")
                    break
        if level == "N/A":
            if area_score <= 5:
                level = "Emerging"
            elif area_score <= 10:
                level = "Established"
            else:
                level = "Enhanced"

        elements.append(Paragraph(_safe(area_label), styles["Heading2"]))
        elements.append(Paragraph(f"Score: {area_score} / {max_area_score} | Level: {level}", styles["Small"]))
        area_meta = st_area.get("meta", {})
        if area_meta:
            assessor = _safe(area_meta.get("assessor",""))
            unit = _safe(area_meta.get("unit",""))
            date = _safe(area_meta.get("date",""))
            notes = _safe(area_meta.get("notes",""))
            if assessor or unit or date or notes:
                elements.append(Paragraph("Assessment data (who filled this area):", styles["Small"]))
                if assessor:
                    elements.append(Paragraph(f"- Assessor: {assessor}", styles["Tiny"]))
                if unit:
                    elements.append(Paragraph(f"- Unit: {unit}", styles["Tiny"]))
                if date:
                    elements.append(Paragraph(f"- Date: {date}", styles["Tiny"]))
                if notes:
                    elements.append(Paragraph(f"- Notes: {notes}", styles["Tiny"]))
        elements.append(Spacer(1, 8))

        # Critical gaps (0 or 1)
        gaps = gaps_all_by_area.get(area_label, [])
        if gaps:
            elements.append(Paragraph("Critical items not fully implemented (0 or 1):", styles["Small"]))
            for item_id, item_text, sc in sorted(gaps, key=lambda x: x[0]):
                status = "0 (Not implemented)" if sc == 0 else "1 (Partially implemented)"
                elements.append(Paragraph(f"- Item {item_id}: {_safe(item_text, 220)} - {status}", styles["Tiny"]))
            elements.append(Spacer(1, 6))

        # Suggested actions
        sug = sug_by_area.get(area_label, [])
        if sug:
            elements.append(Paragraph("Suggested actions (all for current level):", styles["Small"]))
            for lv, t in sug:
                elements.append(Paragraph(f"- {_safe(t, 260)}", styles["Tiny"]))
            elements.append(Spacer(1, 6))

        # Filled matrix table (score/owner/evidence)
        scores = st_area.get("scores", {})
        owners_sel = st_area.get("owner", {})
        evidence = st_area.get("evidence", {})

        mat_rows = [["Item", "Score", "Owner", "Evidence"]]
        for it in maturity_items:
            iid = it.get("id")
            key = f"i{iid}"
            sc = scores.get(key, "")
            owner = owners_sel.get(key, "")
            ev = evidence.get(key, "")
            mat_rows.append([
                f"Item {iid}",
                _safe(sc, 10),
                _safe(owner, 60),
                _safe(ev, 220),
            ])

        mat_tbl = Table(mat_rows, colWidths=[2.0*cm, 1.3*cm, 4.0*cm, 8.2*cm])
        mat_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,0), 8),
            ("FONTSIZE", (0,1), (-1,-1), 8),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.white]),
        ]))
        elements.append(mat_tbl)

        elements.append(PageBreak())

    doc.build(elements)
    return buf.getvalue()

def make_areas_state_json_safe(areas_state: dict) -> dict:
    """
    Create a JSON-safe copy of the assessment state.
    Uploaded files are excluded from the exported snapshot.
    """
    safe_state = {}

    for area_code, area_data in (areas_state or {}).items():
        if not isinstance(area_data, dict):
            safe_state[area_code] = area_data
            continue

        safe_area = dict(area_data)

        # Remove uploaded files from export entirely
        if "evidence_files" in safe_area:
            safe_area["evidence_files"] = {}

        safe_state[area_code] = safe_area

    return safe_state
def build_zip_export(areas_state: dict) -> bytes:
    """
    Creates a ZIP export containing:
    - snapshot.json (JSON-safe)
    - all uploaded evidence files
    """

    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:

        # --- 1. Add JSON snapshot ---
        safe_state = make_areas_state_json_safe(areas_state)

        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "areas_state": safe_state,
        }

        zf.writestr(
            "snapshot.json",
            json.dumps(payload, ensure_ascii=False, indent=2)
        )

        # --- 2. Add evidence files ---
        for area_code, area_data in (areas_state or {}).items():
            evidence_files = area_data.get("evidence_files", {})

            for item_key, files in evidence_files.items():
                for file_item in files or []:

                    # support both formats: (name, bytes) OR just "name"
                    if isinstance(file_item, (list, tuple)) and len(file_item) >= 2:
                        name, content = file_item

                        if content:  # only if we have bytes
                            file_name = f"evidence/{area_code}_{item_key}_{name}"
                            zf.writestr(file_name, content)

        # finish writing
    buffer.seek(0)
    return buffer.getvalue()
def render_summary():
    st.title("Summary")
    st.caption("High-level, cross-area overview based on the areas you have filled so far.")

    LOCKED = bool(st.session_state.get("assessment_locked", False))
    meta = st.session_state.get("assessment_meta", {})

    # ------------------------
    # Assessment data + lock
    # ------------------------
        # ------------------------
    # Controls (global lock)
    # ------------------------
    st.subheader("Controls")
    if st.session_state.get("assessment_locked", False):
        st.info("🔒 Assessment is locked (global). Inputs are disabled across all areas.")
        if st.button("Unlock assessment", key="unlock_assessment_btn"):
            st.session_state["assessment_locked"] = False
            if hasattr(st, "rerun"):
                st.rerun()
    else:
        st.success("Assessment is editable.")
        if st.button("Lock assessment", key="lock_assessment_btn"):
            st.session_state["assessment_locked"] = True
            if hasattr(st, "rerun"):
                st.rerun()

    # ------------------------
    # Assessment data
    # ------------------------
    with st.expander("Assessment data", expanded=False):
        meta["institution"] = st.text_input("Institution", value=meta.get("institution", ""), disabled=LOCKED)
        meta["country"] = st.text_input("Country", value=meta.get("country", ""), disabled=LOCKED)
        meta["assessor"] = st.text_input("Assessor name", value=meta.get("assessor", ""), disabled=LOCKED)
        meta["version_note"] = st.text_input("Assessment version / note", value=meta.get("version_note", ""), disabled=LOCKED)
        meta["notes"] = st.text_area("Notes (optional)", value=meta.get("notes", ""), height=90, disabled=LOCKED)
        st.session_state["assessment_meta"] = meta


    ns = st.session_state.get("areas_state", {})

    # ------------------------
    # Build per-area summary + diagnostics
    # ------------------------
    area_rows = []
    critical_blockers = []   # critical score == 0
    critical_all = []        # critical score == 0 or 1
    actions_suggested = []   # all actions for area level (started areas only)

    total_max_score = 0
    total_score = 0
    started = 0

    # Completeness
    total_items_all = 0
    filled_items_all = 0
    total_items_started = 0
    filled_items_started = 0

    # Validation
    missing_evidence = []  # (area_label, item_id, item_text)
    missing_owner = []     # (area_label, item_id, item_text)

    for label, code_key in AREAS.items():
        if not str(code_key).startswith("3_"):
            continue

        try:
            cfg = load_area_json(code_key)
        except FileNotFoundError:
            continue

        area_id = cfg.get("area_id", code_key.replace("_", "."))
        area_name = cfg.get("area_name", label)
        area_label = f"{area_id} {area_name}"

        maturity_items = cfg.get("maturity_matrix", {}).get("items", [])
        score_ranges = cfg.get("maturity_matrix", {}).get("score_ranges", [])

        st_area = ns.get(code_key, {})
        scores_dict = st_area.get("scores", {}) or {}
        evidence_dict = st_area.get("evidence", {}) or {}
        owners_dict = st_area.get("owner", {}) or {}
        foundations_done = st_area.get("foundations_done", {}) or {}

        # Started definition (avoid false positives from default owner dropdown)
        is_started = False
        if isinstance(scores_dict, dict) and scores_dict:
            try:
                is_started = any(int(v) != 0 for v in scores_dict.values())
            except Exception:
                is_started = any(str(v).strip() not in ("", "0") for v in scores_dict.values())
        if not is_started:
            if any(str(v).strip() for v in (evidence_dict or {}).values()):
                is_started = True
            elif any(bool(v) for v in (foundations_done or {}).values()):
                is_started = True

        if is_started:
            started += 1

        # Completeness accounting
        total_items_all += len(maturity_items)
        filled_items_all += sum(1 for it in maturity_items if int(scores_dict.get(f"i{it.get('id')}", 0)) != 0)

        if is_started:
            total_items_started += len(maturity_items)
            filled_items_started += sum(1 for it in maturity_items if int(scores_dict.get(f"i{it.get('id')}", 0)) != 0)

        max_area_score = 2 * len(maturity_items) if maturity_items else 0
        area_score = sum(int(v) for v in scores_dict.values()) if scores_dict else 0

        total_max_score += max_area_score
        total_score += area_score

        # Determine level by score_ranges (fallback if missing)
        def level_from_score(score: int) -> str:
            for r in score_ranges:
                rng = str(r.get("score_range", "")).strip()
                m = re.match(r"^\s*(\d+)\s*[–-]\s*(\d+)", rng)
                if m:
                    lo, hi = int(m.group(1)), int(m.group(2))
                    if lo <= score <= hi:
                        return r.get("maturity_level", "Emerging")
            if score <= 5:
                return "Emerging"
            if score <= 10:
                return "Established"
            return "Enhanced"

        level = level_from_score(area_score) if max_area_score else "N/A"

        level_badge = level
        if level == "Emerging":
            level_badge = "🟠 Emerging"
        elif level == "Established":
            level_badge = "🟡 Established"
        elif level == "Enhanced":
            level_badge = "🟢 Enhanced"

        maturity_pct = round((area_score / max_area_score) * 100, 1) if max_area_score else 0.0

        blockers = 0
        if is_started:
            for it in maturity_items:
                if not it.get("critical"):
                    continue
                iid = it.get("id")
                key = f"i{iid}"
                sc = int(scores_dict.get(key, 0))
                if sc == 0:
                    blockers += 1
                    critical_blockers.append((area_label, iid, it.get("item", ""), sc))
                    critical_all.append((area_label, iid, it.get("item", ""), sc))
                elif sc == 1:
                    critical_all.append((area_label, iid, it.get("item", ""), sc))

                # Validation checks
                if sc == 2:
                    ev = str(evidence_dict.get(key, "")).strip()
                    if not ev:
                        missing_evidence.append((area_label, iid, it.get("item", "")))
                if sc != 0:
                    owner = str(owners_dict.get(key, "")).strip()
                    if not owner or owner.lower().startswith("select owner"):
                        missing_owner.append((area_label, iid, it.get("item", "")))

        area_rows.append({
            "Area": area_label,
            "Maturity (%)": maturity_pct,
            "Level": level_badge,
            "Blocking critical gaps": blockers,
            "Started": is_started,
        })

        # Suggested actions: ALL actions for that level (started areas only)
        if is_started:
            actions_by_level = cfg.get("build_action_plan", {}).get("actions_by_maturity_level", {})
            for a in actions_by_level.get(level, []):
                if a.get("action"):
                    actions_suggested.append((area_label, level, a.get("action", "")))

    if not area_rows:
        st.info("No areas available yet. Add JSON configs under ./areas.")
        return

    df = pd.DataFrame(area_rows).sort_values(by="Area").reset_index(drop=True)

    total_areas = int(df.shape[0])
    total_blockers = int(df["Blocking critical gaps"].sum())
    blocked_areas = int((df["Blocking critical gaps"] > 0).sum())

    overall_pct = round((total_score / total_max_score) * 100, 1) if total_max_score else 0.0

    # ------------------------
    # Overall progress (top)
    # ------------------------
    st.subheader("Overall progress")

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        st.markdown(f"**Overall maturity:** {overall_pct}%")
        st.progress(min(max(overall_pct / 100.0, 0.0), 1.0))
        st.caption(f"{total_score} / {total_max_score} points")
    with c2:
        st.markdown(f"**Blocking critical gaps:** {total_blockers}")
        if total_blockers == 0:
            st.success("No blockers")
        else:
            st.error(f"Advancement blocked in {blocked_areas} area(s)")
    with c3:
        denom_all = total_items_all if total_items_all else 1
        denom_started = total_items_started if total_items_started else 1
        completeness_all = round((filled_items_all / denom_all) * 100, 1) if total_items_all else 0.0
        completeness_started = round((filled_items_started / denom_started) * 100, 1) if total_items_started else 0.0
        st.markdown(f"**Completeness:** {completeness_all}%")
        st.progress(min(max(completeness_all / 100.0, 0.0), 1.0))
        st.caption(f"Started areas completeness: {completeness_started}%")

    st.markdown("")
    st.markdown(f"**Areas started:** {started}/{total_areas}")
    st.progress(min(max((started / total_areas) if total_areas else 0.0, 0.0), 1.0))

    # ------------------------
    # Filters (affect views below)
    # ------------------------
    st.subheader("Filters")
    f1, f2, f3 = st.columns([1, 1, 2])
    with f1:
        only_started = st.checkbox("Show only started areas", value=True)
    with f2:
        only_blocked = st.checkbox("Show only blocked areas", value=False)
    with f3:
        levels = ["🟠 Emerging", "🟡 Established", "🟢 Enhanced", "N/A"]
        sel_levels = st.multiselect("Filter by maturity level", options=levels, default=["🟠 Emerging", "🟡 Established", "🟢 Enhanced", "N/A"])

    df_view = df.copy()
    if only_started:
        df_view = df_view[df_view["Started"] == True]
    if only_blocked:
        df_view = df_view[df_view["Blocking critical gaps"] > 0]
    if sel_levels:
        df_view = df_view[df_view["Level"].isin(sel_levels)]

    # ------------------------
    # Per-area overview
    # ------------------------
    st.subheader("Per-area overview")
    st.caption("Tip: focus on areas with low maturity and blocking critical gaps.")
    df_show = df_view.drop(columns=["Started"])
    try:
        styler = df_show.style.background_gradient(subset=["Maturity (%)"])
        st.dataframe(styler, width="stretch", hide_index=True)
    except Exception:
        st.dataframe(df_show, width="stretch", hide_index=True)

    # ------------------------
    # Maturity by area (bar)
    # ------------------------
    st.subheader("Maturity by area")
    if len(df_view) == 0:
        st.info("No areas match the current filters.")
    else:
        df_chart = df_view.sort_values(by="Maturity (%)", ascending=True)
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df_chart["Maturity (%)"],
            y=df_chart["Area"],
            orientation="h",
            name="Maturity (%)"
        ))
        fig.update_layout(
            xaxis_title="Maturity (%)",
            yaxis_title="Area",
            margin=dict(l=10, r=10, t=30, b=10),
            height=500
        )
        st.plotly_chart(fig, width="stretch", config={})

    # ------------------------
    # Critical gaps sections
    # ------------------------
    st.subheader("Critical gaps to address")
    st.caption("Shows only **blocking** critical items (score = 0) in started areas.")
    blockers_for_view = [x for x in critical_blockers if x[0] in set(df_view["Area"])]
    if not blockers_for_view:
        st.success("No blocking critical gaps detected (for the selected filters).")
    else:
        by_area = {}
        for area_label, item_id, item_text, sc in blockers_for_view:
            by_area.setdefault(area_label, []).append((item_id, item_text, sc))
        with st.expander(f"View blocking critical gaps ({len(blockers_for_view)})"):
            for area_label, rows in by_area.items():
                st.markdown(f"**{area_label}**")
                for item_id, item_text, sc in sorted(rows, key=lambda x: x[0]):
                    st.write(f"🔴 Item {item_id}: {item_text} — Not implemented (0)")

    st.subheader("All critical gaps")
    st.caption("Shows all critical items that are **not fully implemented** (score 0 or 1) in started areas.")
    all_for_view = [x for x in critical_all if x[0] in set(df_view["Area"])]
    if not all_for_view:
        st.success("No critical gaps detected (for the selected filters).")
    else:
        by_area = {}
        for area_label, item_id, item_text, sc in all_for_view:
            by_area.setdefault(area_label, []).append((item_id, item_text, sc))
        with st.expander(f"View all critical gaps ({len(all_for_view)})"):
            for area_label, rows in by_area.items():
                st.markdown(f"**{area_label}**")
                for item_id, item_text, sc in sorted(rows, key=lambda x: x[0]):
                    status = "Not implemented (0)" if sc == 0 else "Partially implemented (1)"
                    st.write(f"🔴 Item {item_id}: {item_text} — {status}")

    # ------------------------
    # Next steps (all actions)
    # ------------------------
    st.subheader("Recommended next steps")
    st.caption("Lists all suggested actions for each started area (based on the current maturity level).")
    actions_for_view = [x for x in actions_suggested if x[0] in set(df_view["Area"])]
    if actions_for_view:
        with st.expander(f"Suggested actions (all, by area) ({len(actions_for_view)})"):
            by_area_actions = {}
            for area_label, level, text in actions_for_view:
                by_area_actions.setdefault(area_label, []).append((level, text))
            for area_label, items in by_area_actions.items():
                st.markdown(f"**{area_label}**")
                for level, text in items:
                    st.write(f"- ({level}) {text}")
                st.markdown("")
    else:
        st.info("No suggested actions available yet (start at least one area).")

    # ------------------------
    # Validation (pre-report checks)
    # ------------------------

    # ------------------------
    # Filters

    st.subheader("Pre-report checks")
    if started == 0:
        st.warning("No areas started yet. The report will be mostly empty.")
    if total_blockers > 0:
        st.error(f"Blocking critical gaps detected: {total_blockers}.")
    else:
        st.success("No blocking critical gaps detected.")

    partials_count = sum(1 for *_, sc in critical_all if sc == 1)
    if partials_count:
        st.warning(f"Critical items partially implemented (score=1): {partials_count}.")
    else:
        st.success("No partially implemented critical items detected (score=1).")

    if missing_evidence:
        st.warning(f"Missing evidence for score=2 items: {len(missing_evidence)}.")
    else:
        st.success("Evidence present for all score=2 items (in started areas).")

    if missing_owner:
        st.info(f"Owner not specified for scored items: {len(missing_owner)} (recommended to fill).")


    # ------------------------
    # Save / Load (versioned JSON)
    # ------------------------
    st.subheader("Save or Load Assesment")
    with st.expander("Save / Load assessment", expanded=False):
        current_state = st.session_state.get("areas_state", {})
        safe_state = make_areas_state_json_safe(current_state)

        payload = {
            "_meta": {
                "schema_version": SCHEMA_VERSION,
                "app_version": APP_VERSION,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                **meta,
            },
            "areas_state": safe_state,
        }
        st.subheader("Partial Export")
        st.caption("Note: This JSON snapshot saves text inputs and assessment selections only. Uploaded evidence files will not be included.")

        st.download_button(
            "Download partial assessment (JSON)",
            data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name="dtp_assessment_state.json",
            mime="application/json",
        )
        st.markdown("---")
        st.subheader("Full Export")

        st.caption("Includes snapshot.json and all uploaded evidence files.")

        zip_bytes = build_zip_export(st.session_state.get("areas_state", {}))

        st.download_button(
            label="Download full export (.zip)",
            data=zip_bytes,
            file_name="alfalabs_export.zip",
            mime="application/zip",
        )

        st.markdown("---")

        if "assessment_uploader_nonce" not in st.session_state:
            st.session_state["assessment_uploader_nonce"] = 0
        uploader_key = f"assessment_state_uploader_{st.session_state['assessment_uploader_nonce']}"
        st.subheader("Upload assessment to continue your work")
        uploaded = st.file_uploader("Upload assessment in JSON to restore. Do not try to upload the .zip file, evidence files cannot be included here.", type=["json"], key=uploader_key)

        col_load_1, col_load_2 = st.columns([1, 2])
        with col_load_1:
            load_clicked = st.button("Load assessment", type="primary", disabled=(uploaded is None))
        with col_load_2:
            clear_clicked = st.button("Clear uploaded file", disabled=(uploaded is None))

        if clear_clicked:
            st.session_state["assessment_uploader_nonce"] += 1
            if hasattr(st, "rerun"):
                st.rerun()
            else:
                st.info("Upload cleared. Please refresh the page (Ctrl+R).")

        if load_clicked and uploaded is not None:
            try:
                loaded = json.loads(uploaded.read().decode("utf-8"))

                # Backward compatible: allow old files that were just the areas_state dict
                if isinstance(loaded, dict) and "areas_state" in loaded:
                    st.session_state["areas_state"] = loaded.get("areas_state", {}) or {}
                    loaded_meta = loaded.get("_meta", {}) or {}
                    if isinstance(loaded_meta, dict):
                        merged = st.session_state.get("assessment_meta", {}).copy()
                        for k in ["institution", "country", "assessor", "notes", "version_note"]:
                            if k in loaded_meta:
                                merged[k] = loaded_meta.get(k, "")
                        st.session_state["assessment_meta"] = merged
                elif isinstance(loaded, dict):
                    st.session_state["areas_state"] = loaded
                else:
                    st.error("Invalid file format: expected a JSON object.")
                    return

                st.session_state["assessment_uploader_nonce"] += 1
                st.success("Assessment loaded.")
                if hasattr(st, "rerun"):
                    st.rerun()
                else:
                    st.info("Please refresh the page (Ctrl+R) to see loaded values.")
            except Exception as e:
                st.error(f"Failed to load file: {e}")

    ns = st.session_state.get("areas_state", {})


    # ------------------------
    # Exports
    # ------------------------
    st.subheader("Exports and Imports")

    try:
        configs = {}
        for _, area_code in AREAS.items():
            if str(area_code).startswith("3_"):
                try:
                    configs[area_code] = load_area_json(area_code)
                except FileNotFoundError:
                    pass

        pdf_bytes = generate_pdf_report(
            summary_df=df_show,
            areas_state=ns,
            configs=configs,
            blockers_only=critical_blockers,
            all_critical_gaps=critical_all,
            suggested_actions=actions_suggested,
            meta=meta,
            app_version=APP_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        label = "Download PDF report" if total_blockers == 0 else "Download PDF report (issues detected)"
        st.download_button(label, data=pdf_bytes, file_name="dtp_ai_maturity_report.pdf", mime="application/pdf")
    except Exception as e:
        st.warning(f"PDF export is currently unavailable: {e}")

    csv_buf = io.StringIO()
    df_show.to_csv(csv_buf, index=False)
    st.download_button(
        "Download summary table (CSV)",
        data=csv_buf.getvalue().encode("utf-8"),
        file_name="dtp_summary.csv",
        mime="text/csv",
    )

def render_placeholder(area_label: str):
    st.title(area_label)
    st.info("Placeholder page — add the JSON config in ./areas to enable this area.")


# ------------------------
# DISCLAIMER GATE
# ------------------------
if not st.session_state.get("disclaimer_accepted", False):
    render_disclaimer_gate()
    st.stop()

# ------------------------
# ROUTER
# ------------------------
with st.sidebar:
    st.markdown("### ☰ Areas")
    render_sidebar_data_notice()
    sel_label = st.radio("Select area", list(AREAS.keys()), label_visibility="collapsed")

area_code = AREAS[sel_label]
if area_code == "summary":
    render_summary()
elif area_code.startswith("3_"):
    try:
        render_area_from_json(area_code)
    except FileNotFoundError:
        render_placeholder(sel_label)
else:
    render_placeholder(sel_label)