# CLAUDE.md — Project Context & Engineering Guidelines for `ortho-bulkhead`

## 1. Project Overview
`ortho-bulkhead` (**OrthoBulkhead**) is an R&D, marine refit engineering, and CAD/CAM fabrication repository for **yacht interior restoration, surface remediation, frameless acrylic glazing, and optical-to-DXF CNC joinery manufacturing** on `30–45 ft` cruising sailboats.

Primary reference documents:
* [`marine-interior-cnc-refit-guide.md`](marine-interior-cnc-refit-guide.md) — Complete engineering, materials, remediation, and cost/labor guide.
* [`process_window_templates.py`](process_window_templates.py) — Planar homography camera-to-DXF CNC profile generator.

---

## 2. Core Engineering & Restoration Rules (Do Not Violate)

When generating refit plans, BOMs, structural tabbing specs, chemical remediation steps, or CAD/CAM scripts in this repository, always adhere to these physical and chemical constraints:

1. **Sequence of Operations (Envelope & Rig First):**
   * **Rig Support Before Bulkhead Removal:** Never cut out a main structural bulkhead without first slacking the upper/lower shrouds and placing an adjustable screw-jack post under the mast compression beam supported directly on the keel spine.
   * **Dry Boat Before New Wood:** Always complete `100%` of window/portlight replacement, deck hardware re-bedding (`butyl tape` / `Sikaflex`), and aggressive hose-testing *before* installing new insulation, bulkheads, cabinets, or cushions.

2. **Frameless Cast Acrylic Window Glazing (Zero Through-Bolts):**
   * Always specify **cell-cast acrylic (`Plexiglas G` / `Acrylite GP`, `3/8"` / `9.5 mm` thick)**—**never extruded acrylic**, which suffers severe environmental stress cracking (`ESC`) and crazes on contact with primers and sealants.
   * Because acrylic has a coefficient of thermal expansion `~5×` higher than fiberglass (`α ≈ 7 × 10⁻⁵ /K`), **never through-bolt fixed panoramic acrylic windows**.
   * Always specify **structural elastomeric bonding**: abrade + UV-prime (`Sika Primer 209 D`) the perimeter flange, use **`3M VHB 4991` (`2.3 mm` / `90 mil`) structural foam tape** as a calibrated bond-line thickness spacer so clamping cannot squeeze out the adhesive, and seal the perimeter bevel with **`Sikaflex 295 UV` or `Dow 795` structural glazing silicone**.

3. **Surface Remediation & Overhead Laminate Chemistry:**
   * **No Chlorine Bleach on Boats:** Never specify raw sodium hypochlorite (chlorine bleach) for interior mold remediation; chlorine off-gassing corrodes tinned copper wiring and pits `316` stainless steel. Specify **Borax / Concrobium / Quaternary Ammonium** cleaners.
   * **3-Stage Smoke / `PAH` Decontamination Lock:** Never paint, varnish, or apply architectural vinyl (`3M DI-NOC`) directly over smoke-exposed fiberglass or wood. Always enforce: (1) alkaline **TSP (Trisodium Phosphate)** degrease scrub, (2) heated **Hydroxyl / Ozone** treatment, and (3) chemical vapor-barrier seal (**100% solids 2-part epoxy primer** on fiberglass/bilges; **pigmented shellac `Zinsser B-I-N`** on hidden wood backs/lockers).
   * **Epoxy Only for Interior Structural Re-Skinning:** Never specify polyester resin or waxed polyester gelcoat for overhead interior cabin repairs (weak secondary bond to aged polyester + hazardous styrene vapor). Always specify **100% solids marine epoxy + `1708` biaxial E-glass**, faired with lightweight epoxy fairing compound (`TotalFair` / `West System 407`) and topcoated with **2-part Linear Polyurethane (`Alexseal 501` / `Awlgrip`)** or covered with **`Fastmount` clip-in insulated vinyl liner panels** over `6–13 mm` closed-cell elastomeric foam (`Armaflex` / `Minicel`).
   * **Discard Smoke- or Mold-Contaminated Open-Cell Foam:** Open-cell polyurethane cushion foam absorbs `PAHs`, aldehydes, and fungal spores throughout its core and cannot be deodorized. Wrap old cushions in polyethylene film to use *only* as `3D` cutting guides and unpicked `1:1` sewing patterns, then discard the old foam.

4. **Marine Joinery, `Alpi` Veneer & Bulkhead Tabbing:**
   * **Hard-Spot Gap on Structural Bulkheads:** Always leave a **`6–10 mm` (`1/4"–3/8"`) gap** (backed by medium-density closed-cell foam strips) between the edge of a `BS1088` plywood or `Coosa Bluewater 26` bulkhead and the fiberglass hull skin before pulling a `20–25 mm` structural epoxy fillet and laying `2 layers` of `1708` biaxial tape (`100 mm` + `150 mm` staggered width).
   * **`Alpi` Reconstituted Veneer (`0.5–0.8 mm`):** Never belt-sand or use `<180 grit` abrasives on factory `Alpi` veneer (`Beneteau`, `Jeanneau`, `Dufour`). Scuff intact `Alpi` by hand (`220–320 grit`) for `2K` conversion varnish, or finish heat-damaged flat panels in a **two-tone Satin Oyster White + natural varnished hardwood trim** scheme (or `3M DI-NOC` architectural film over adhesion primer).
   * **`6-Side` Epoxy Encapsulation for Floorboards:** Every cabin sole panel must be sealed with **3 coats of penetrating epoxy on the underside, screw holes, and all raw cut edges** to prevent bilge moisture wicking.

5. **Optical Template-to-DXF Software Conventions ([`process_window_templates.py`](process_window_templates.py)):**
   * Maintain `$INSUNITS = 1` (**Inches**) for window/bulkhead `DXF` exports matching standard North American sheet goods (`48" × 96"`), or document unit scaling explicitly.
   * Always generate and inspect the `1:1` **visual verification `SVG/PNG` overlay** and verify that homography reprojection error is **`<0.5 px`** and bounding-box diagonals match physical tape-measure checks within **`±1/32"` (`0.8 mm`)** before sending `DXF` files to a CNC router.
