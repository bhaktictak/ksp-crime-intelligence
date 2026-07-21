# KSP Datathon 2026 — PS2 Crime Intelligence Platform
## Final Implementation Plan

**Problem Statement 2:** State-of-the-art data visualization, statistics, and AI-driven analytics platform for crime investigations, Karnataka State Police.

**Core differentiator:** Confidence-aware, fairness-audited spatiotemporal crime forecasting — not just a heatmap, but a heatmap that is honest about what it doesn't know, calibrated fairly across districts, and audited against reporting bias without ever training on protected attributes.

---

## 0. Ground Rules (apply to every phase)

- **No protected attribute ever enters a trained model.** `CasteID`, `ReligionID`, `GenderID` (complainant/victim) live in a quarantined table used *only* by the read-only audit layer. This is stated explicitly in code comments, the architecture diagram, and the prototype brief.
- **Every formula below is either (a) a named, citable statistical method, or (b) explicitly labeled a "design heuristic — tunable, not derived."** Never present (b) as (a) to judges.
- **Two audiences, two views.** Operational dashboard (captain, big screen, four colors, fast) is structurally separate from the analyst/audit dashboard (fairness numbers, per-district PAI). The fairness machinery is infrastructure, not a UI feature the officer has to interpret.

---

## Phase 0 — Data Layer (Synthetic Dataset)

**Goal:** Build a synthetic dataset matching the real KSP `CaseMaster` ER schema, realistic enough that every downstream model and audit check has real signal to work with.

**Tables to generate (per the provided ER diagram):**
- `CaseMaster` — spine table: `CaseMasterID`, `CrimeRegisteredDate`, `IncidentFromDate`, `IncidentToDate`, `InfoReceivedPSDate`, `latitude`, `longitude`, `PoliceStationID`, `CrimeMajorHeadID`, `CrimeMinorHeadID`, `CaseCategoryID`, `GravityOffenceID`, `CaseStatusID`
- `Accused` — `AccusedMasterID`, `CaseMasterID`, `AgeYear`, `GenderID`
- `ArrestSurrender` — links accused to arrest events across districts/stations
- `ComplainantDetails` — **quarantined table**: `CasteID`, `ReligionID`, `OccupationID`, `GenderID`, `AgeYear`
- `ChargesheetDetails` — `cstype` (A = chargesheet, B = false case, C = undetected) — needed for the false-case disparity check
- `Unit` / `District` / `State` — hierarchy for zone aggregation
- `CrimeHead` / `CrimeSubHead` / `Act` / `Section` — for filtering cyber/financial crime subset

**Design requirements for the generator:**
1. Inject **realistic seasonal structure** (festival/payday spikes) into `IncidentFromDate` distributions — this is what SARIMA needs to have anything to learn (Phase 2).
2. Inject **spatial clustering** (not uniform random lat/long) so DBSCAN has real density variation to find (Phase 1).
3. Inject **reporting-volume variation across zones** — some zones should have systematically sparse/noisy reporting, independent of true underlying risk, so Phase 5 (confidence) has a real signal to detect, not noise.
4. Inject a **population or occupation-proxy field per zone**, if feasible, so any future audit check has a real baseline to compare complainant composition against (this was flagged in Phase 6 design as a hard requirement — do not run disparate impact analysis without a real baseline to compare to).
5. Give `Accused` table realistic **repeat-linkage structure** — a small fraction of accused IDs should appear across multiple cases/districts (this feeds the Phase 7 network module and reflects the real chronic-offender pattern; see citation below).

> **Citation for repeat-offender concentration design assumption:** Wolfgang, Figlio & Sellin's Philadelphia Birth Cohort studies established that a small fraction of offenders (roughly 6%) account for a disproportionate share (~50%+) of offenses — the empirical basis for building "prolific offender" concentration into the synthetic accused distribution rather than uniform randomness.

**Deliverable:** CSV/Parquet files per table, a data dictionary matching the ER diagram, and a short README documenting exactly which fields were injected with which distributions and why.

---

## Phase 1 — Hotspot Detection (Spatial)

**Method:** DBSCAN (Density-Based Spatial Clustering of Applications with Noise) over `latitude`/`longitude` from `CaseMaster`, run per time window (e.g., weekly), grouped by `PoliceStationID` as the reporting unit.

**Why DBSCAN specifically:** Identified as a comparably strong, reliable clustering technique for spatio-temporal crime hotspot detection across the reviewed literature.

> **Citation:** Butt, U. M., Letchmunan, S., Hassan, F. H., Ali, M., Baqir, A., & Sherazi, H. H. R. (2020). Spatio-Temporal Crime HotSpot Detection and Prediction: A Systematic Literature Review. *IEEE Access*, 8, 166553–166574. — DBSCAN and Random Forest identified as reliable, comparable-performance techniques for hotspot detection (Table 9); clustering is the most-used approach category (40% of techniques, Fig. 7).

**Note on spatial unit choice:** Grid cell / police station jurisdiction, not a road-network graph — this matches the dominant spatial-unit choice across the reviewed literature (used in the majority of studies) and keeps the operational heatmap interpretable at a glance.

> **Citation:** Kounadi, O., Ristea, A., Araujo Jr., A., & Leitner, M. (2020). A systematic review on spatial crime forecasting. *Crime Science*, 9(7). — Grid cell reported as the preferable spatial unit type, used in the majority of the 32 reviewed papers (n=20/32).

---

## Phase 2 — Temporal Forecasting (SARIMA)

**Method:** SARIMA (Seasonal ARIMA) on aggregated incident counts per zone, per day/week, using `CrimeRegisteredDate`/`IncidentFromDate`.

**Why SARIMA over plain ARIMA:** ARIMA is reported to outperform other time-series approaches for crime forecasting, but its documented weakness is an inability to handle seasonality — directly relevant given festival/payday seasonal spikes in the Karnataka context. SARIMA extends ARIMA specifically to model that seasonal component.

> **Citation:** Butt et al. (2020), *IEEE Access* — ARIMA outperforms other time-series approaches for crime forecasting (Table 12), but "cannot handle the seasonality and repeated behaviour of event, especially crime events... a research gap still exist[s] in this area for future researchers" (Section IV.B.1). SARIMA is the identified future-research direction addressing this gap.

**Output:** Predicted crime volume per zone per future window — feeds forward as a feature into Phase 3, not as a final output.

---

## Phase 3 — Risk Regression + Crime-Type Classification (Two-Headed Random Forest)

**Method:** Two Random Forest models trained on a shared feature matrix (SARIMA forecast, `CrimeMajorHeadID`/`CrimeMinorHeadID` historical distribution, `GravityOffenceID` mix, day-of-week and lag counts — all strictly lagged/past-only, never same-day, to avoid feature leakage), each with a different target:

- **Head A — Risk Regressor:** predicts risk magnitude/expected volume per zone per day. Splitting criterion: MSE (default), or MAE if festival-day outliers distort tree splits. Feeds Phase 4 (near-repeat) → Phase 5 (confidence) → Phase 6 (PAI-calibrated hotspot status). Answers "how much risk."
- **Head B — Crime-Type Classifier:** predicts the most likely crime category (`CrimeMajorHeadID`, or `CrimeMinorHeadID` if sample size per class allows) for a given zone-day, output as a **ranked probability distribution (top-2/top-3), not a single hard label** — a single-label prediction on imbalanced crime-type data will be overconfident and often wrong; a ranked distribution is both more honest and operationally useful (e.g., informs whether to send a cyber-aware unit vs. general patrol). Splitting criterion: Gini impurity (default). Answers "what kind of risk."

**Explicitly excluded from features, both heads:** Everything in `ComplainantDetails` (caste, religion, occupation, age, gender). These models predict risk of *place, time, and category*, never anything associated with *who reports*.

**Loss function / imbalance handling:** Random Forest does not use a gradient-descent loss function — no custom loss design needed for either head; splitting criteria (MSE/MAE for Head A, Gini/entropy for Head B) are standard library defaults. The one real consideration is **class imbalance in Head B** (e.g., theft vastly outnumbering homicide) — handled via standard class weighting (`class_weight='balanced'`), not a custom loss function. This is a solved, library-level parameter, not a research problem.

**Why Random Forest:** Frequently reported as the most-used and best-performing proposed method for both hotspot detection and crime forecasting/prediction tasks across the reviewed literature.

> **Citation:** Butt et al. (2020) — Random Forest reported as efficient and effective, compared with state-of-the-art techniques (Section VI). Kounadi et al. (2020) — Random Forest (RF) is the most frequently used proposed method across 32 selected papers (Table 4), tied with Multilayer Perceptron as a top method in proposed, best-performing, and baseline categories.

**Head B is a recognized forecasting task, not a bolt-on:** Kounadi et al. (2020) document five distinct "inference types" in the spatial crime forecasting literature — hotspots, number of crimes, crime rate, **category of crime**, and properties of clusters. Huang et al. (2018), cited in that review, evaluated forecasted crime category as a binary/multi-class classification output in exactly this style.

**Evaluation differs by head — do not force one metric to cover both:**
- Head A (regression/risk): evaluated via PAI (Phase 6), consistent with the rest of the pipeline.
- Head B (classification): evaluated via **F1-score (macro or weighted, given class imbalance)** and precision/recall — reported as commonly used metrics for classification-style crime forecasting tasks across the reviewed literature (Kounadi et al. 2020, Overview of evaluation metrics). PAI does not apply to Head B; it is a spatial-efficiency metric, not a classification metric.

---

## Phase 4 — Near-Repeat Ripple Effect (Physical Crime Chaining)

**Method:** After an incident, apply a temporary, spatially-decaying risk bump to nearby zones (small radius, e.g. ~400m) for a limited time window following the event, applied on top of the Phase 3 base risk score.

**Why this is grounded, not invented:** Near-repeat victimization — the finding that after a crime, nearby locations face elevated risk for a limited period — is a well-established, empirically replicated pattern in environmental criminology.

> **Citation:** Near-repeat victimization theory, foundational methods documented in Johnson, S. D., et al. — near-repeat burglary pattern studies (e.g., the Vienna burglary near-repeat analysis, PMC6417393) and "boost account" theory of near-repeat risk elevation (PMC11661938), both cited in the earlier literature review for this project.

**Design note:** The exact decay radius (400m) and time window are **design heuristics**, not derived from a specific paper — state this explicitly; they should be tunable parameters, ideally calibrated against the synthetic data's injected spatial clustering structure during testing.

---

## Phase 5 — Confidence via Empirical Bayes Shrinkage

**This replaces the earlier ad hoc weighted "Reporting Confidence Score."** Real, citable, closed-form.

**Core idea:** A zone's raw observed crime rate is unreliable when based on few observations (the "small number problem" in small-area crime mapping). The fix is to shrink the zone's raw rate toward a more stable district-level average, weighted by how much data that zone actually has — this produces both a corrected risk estimate *and* a genuine confidence measure, from the same calculation.

**Formula:**

For zone *z* with observed rate λ_z (over *n_z* case-days) and district average rate λ̄:

```
λ̂_z(EB) = w_z · λ_z + (1 − w_z) · λ̄

w_z = σ²_between / (σ²_between + λ̄ / n_z)
```

- `λ̂_z(EB)` = shrunk risk estimate → feeds the **risk axis**
- `w_z` ∈ [0, 1] = shrinkage weight → **is** the confidence score directly (no separate formula needed, no division-by-confidence instability)
  - Zones with few observations (n_z small): w_z → 0, estimate pulled toward district average, confidence low
  - Zones with many observations (n_z large): w_z → 1, zone's own estimate trusted, confidence high

**Four-zone classification (unchanged concept, now bug-free):**

```
if λ̂_z(EB) is high  AND  w_z is high  →  HOTSPOT
if λ̂_z(EB) is high  AND  w_z is low   →  WATCH
if λ̂_z(EB) is low   AND  w_z is low   →  UNKNOWN
if λ̂_z(EB) is low   AND  w_z is high  →  SAFE
```

("high"/"low" cutoffs are set per Phase 6's PAI calibration, not arbitrary fixed numbers.)

> **Citation:** Empirical Bayes small-area estimation for rate reliability — Clayton, D., & Kaldor, J. (1987). Empirical Bayes estimates of age-standardized relative risks for use in disease mapping. *Biometrics*, 43(3), 671–681 (foundational method). Applied directly to the "small number problem" in crime hotspot mapping: Law, J., & Quick, M. (2015). Analyzing Hotspots of Crime Using a Bayesian Spatiotemporal Modeling Approach: A Case Study of Violent Crime in the Greater Toronto Area. *Geographical Analysis*, 47(1). Applied to small-area crime rate estimation specifically: Buil-Gil, D., et al. Small area estimation in criminological research: Theory, methods, and applications — including mapping the "dark figure" of unreported crime using small area estimation techniques, directly analogous to this project's confidence layer.

---

## Phase 6 — PAI-Calibrated Per-District Thresholds

**Method:** Instead of one global risk cutoff for "hotspot" status, sweep threshold values *per district* and select the cutoff that achieves a target Prediction Accuracy Index (PAI) for that district — so districts are judged by the same *efficiency standard*, not the same raw number, which is more defensible given uneven data density/quality across districts.

**PAI formula (standard, from the literature):**

```
PAI = (n / N) / (a / A)
```
where *n* = crimes captured in flagged area, *N* = total crimes, *a* = flagged area, *A* = total area.

> **Citation:** PAI as the primary criminology-preferred evaluation metric — Chainey, S., Tompson, L., & Uhlig, S. (2008). The utility of hotspot mapping for predicting spatial patterns of crime. *Security Journal*, 21, 4–28 (KDE yielded highest PAI score among spatial methods examined). Confirmed as the top-3 most-used evaluation metric across 32 reviewed spatial crime forecasting studies, and specifically preferred by criminologists over raw Prediction Accuracy: Kounadi et al. (2020) — "computer scientists exclusively use the PA, while criminologists prefer to apply the PAI" (Results, Overview of evaluation metrics).

**Novel extension (own contribution, state as such):** Existing literature reports PAI *post-hoc* as an evaluation metric after model training. This project uses PAI *pre-deployment*, as a per-district threshold-calibration criterion — this distinction should be stated explicitly and honestly in the prototype brief as the project's own methodological contribution, not attributed to prior work.

**Deliverable:** Per-district PAI-at-calibrated-threshold table, surfaced on the audit dashboard (Phase 9).

---

## Phase 7 — Fairness Audit Layer (Read-Only, Post-Hoc)

**Runs after Phase 6, never inside training. Reads the quarantined `ComplainantDetails` table.**

### 7a. Disparate Impact Ratio (Four-Fifths Rule)

```
DIR = min_g(flag_rate(g)) / max_g(flag_rate(g))

flag_rate(g) = (# zones where group g > 50% of complainants AND flagged hotspot)
               / (# zones where group g > 50% of complainants)

If DIR < 0.8 → Disparate Impact Alert
```

> **Citation:** The "four-fifths rule" — U.S. Equal Employment Opportunity Commission (1978). Uniform Guidelines on Employee Selection Procedures, 29 C.F.R. §1607.4(D). This is a well-established legal/statistical disparate-impact threshold. **Explicitly note in the brief:** this rule originates in U.S. employment discrimination law; its use here is an adaptation to a policing context, not an existing policing standard — that adaptation is this project's contribution.

### 7b. False-Case Disparity Ratio

```
FCD(g) = false_case_rate(zones where group g > 50% of complainants)
         / statewide_average_false_case_rate

If FCD(g) > 1.5 → Flag: "Cases from group g zones dismissed at 1.5× state average rate"
```

This is a standardized rate ratio — the same logic used for standardized mortality/incidence ratios in epidemiology (the same family of methods underlying Phase 5's empirical Bayes shrinkage).

### 7c. What was cut and why

The earlier draft's Shannon-entropy-based "reporting silencing" signal is **removed**. Low caste/religion diversity among complainants in a zone is confounded with genuine population homogeneity — without a real population baseline to compare against, the signal cannot distinguish "suppression" from "demographics." If Phase 0's synthetic data includes a population-proxy field, this can be reintroduced as a *comparison-to-baseline* metric in a later iteration; it should not ship as a standalone confidence input without that baseline.

### 7d. Known open problem, cite honestly

Algorithmic feedback loops from differential policing intensity (more patrols → more minor incidents logged → model recommends more patrols) are a documented, unresolved issue in predictive policing generally. This project does not claim to solve this; it is named explicitly as a limitation.

> **Citation:** Mohler, G., Raje, R., Carter, J., Valasik, M., & Brantingham, J. (2018). A penalized likelihood method for balancing accuracy and fairness in predictive policing. *2018 IEEE International Conference on Systems, Man, and Cybernetics (SMC)*. — cited as prior art on this exact problem; this project's audit layer detects the symptom (disparate flagging), it does not implement Mohler et al.'s penalized-likelihood correction, and that gap should be named as future work.

---

## Phase 8 — Cyber/Financial Crime Module (Parallel Branch)

**Rationale for separate treatment:** Physical crime chains spatially (Phase 4); cyber/financial crime chains through shared entities and infrastructure instead, so it needs a different mechanism, not a forced reuse of the spatial near-repeat model.

**8a. Filtering:** Subset `CaseMaster` via `CrimeHeadActSection`/`Act`/`Section` to isolate cyber/financial crime categories.

**8b. Temporal forecasting:** Same SARIMA approach as Phase 2, run on the cyber-crime subset independently (different seasonal pattern expected — e.g., festival online-shopping fraud spikes vs. physical crime patterns).

**8c. Network detection:** Bipartite graph — `Accused` ↔ `CaseMasterID` ↔ `PoliceStationID`/`DistrictID`. An accused person linked to multiple cases across different jurisdictions within an overlapping time window is flagged as a possible coordinated network node.

**8d. Prolific offender ranking:** Rank accused by case frequency and **betweenness centrality** on the accused-case graph — betweenness identifies individuals who bridge otherwise-separate case clusters (i.e., likely network connectors, not just high-volume individuals).

> **Citation for design inspiration (concept only, not architecture):** Reserve Bank Innovation Hub (RBIH), a subsidiary of the Reserve Bank of India, developed MuleHunter.AI — an AI/ML tool that analyzes 19 distinct behavioral patterns to detect money mule bank accounts, piloted with public sector banks starting December 2024 and expanded to 15+ banks by mid-2025 (Business Standard, Dec 2024; Moneycontrol, Aug 2025). **State explicitly:** the internal algorithmic architecture of MuleHunter.AI is not publicly disclosed; this module is inspired by the *concept* of entity-network fraud detection, not a reproduction of RBIH's actual system.

> **Citation for RAT/cyber-RAT seasonal framing (concept only):** Routine Activity Theory — Cohen, L. E., & Felson, M. (1979). Social Change and Crime Rate Trends: A Routine Activity Approach. *American Sociological Review*, 44(4), 588–608. Its extension to cybercrime contexts is documented in the criminology literature (e.g., Reyns' cyber-lifestyle routine activity theory work). **Note:** the specific "Festive Storm four-phase" seasonal breakdown discussed earlier is a project-original hypothesis, not sourced from a specific paper — label it as such if used in the pitch.

---

## Phase 9 — Anomaly Detection

**Method:** Rolling z-score on `CrimeMinorHeadID` counts per `PoliceStationID` per week, compared against **that same station's own historical baseline** — never cross-station.

**Why same-station-only comparison:** Cross-station comparison risks becoming another socioeconomic proxy channel (stations in different districts have different baseline crime compositions for reasons unrelated to anomalous activity). Comparing a station only to its own history avoids this.

**Deliverable:** Weekly anomaly alert list per station, surfaced on the operational dashboard.

---

## Phase 10 — Dashboard (Two Audiences, Structurally Separated)

**10a. Operational view (captain / big-screen):**
- Four-color hotspot map (Hotspot / Watch / Unknown / Safe) from Phase 5+6
- Cyber network graph tab (Phase 8)
- Anomaly alerts tab (Phase 9)
- Design constraint: glanceable, minimal text, fast decision support — no fairness statistics visible here by design.

**10b. Analyst/audit view (SCRB oversight):**
- DIR gauge (Phase 7a), should stay ≥ 0.8, alert if not
- FCD-by-group table (Phase 7b)
- Per-district PAI comparison (Phase 6)
- Empirical Bayes shrinkage weight (w_z) distribution — how much of the state is currently "low confidence"
- Explicit "known limitations" panel referencing Phase 7d

---

## Summary Table — Every Method, Its Citation Status

| Component | Method | Citation status |
|---|---|---|
| Hotspot detection | DBSCAN | ✅ Literature-supported (Butt et al. 2020) |
| Spatial unit | Grid/station, not road-graph | ✅ Literature-supported (Kounadi et al. 2020) |
| Forecasting | SARIMA | ✅ Literature-supported gap-filling (Butt et al. 2020) |
| Risk classification | Random Forest | ✅ Literature-supported (both papers) |
| Physical chaining | Near-repeat, 400m/decay | ✅ Concept grounded; exact parameters are tunable heuristics |
| Confidence | Empirical Bayes shrinkage | ✅ Real, citable closed-form method (Clayton & Kaldor 1987; Law & Quick 2015; Buil-Gil et al.) |
| Threshold calibration | PAI per-district sweep | ✅ PAI is literature-standard; per-district calibration use is this project's own extension (say so) |
| Disparate impact | Four-fifths rule | ✅ Real legal/statistical standard (EEOC 1978); application to policing is this project's adaptation (say so) |
| False-case check | Standardized rate ratio | ✅ Standard epidemiological technique |
| Removed: entropy "silencing" score | — | ❌ Cut — confounded without population baseline |
| Cyber network | Bipartite graph + betweenness centrality | ✅ Betweenness centrality is standard SNA; MuleHunter.AI cited as concept inspiration only, not architecture |
| Anomaly detection | Same-station rolling z-score | ✅ Standard statistical technique, design choice to avoid cross-station confound |
| Repeat offender base rate | Synthetic data design assumption | ✅ Grounded in Wolfgang chronic-offender cohort findings |

---

## Build Order (Suggested)

1. Phase 0 (data) — everything depends on this
2. Phase 1 + 2 (DBSCAN + SARIMA) — independent, can be built in parallel
3. Phase 3 (Random Forest) — needs Phase 2 output
4. Phase 4 (near-repeat) — needs Phase 3 output
5. Phase 5 (Empirical Bayes confidence) — can be built in parallel with Phase 3/4, needs only Phase 0 data
6. Phase 6 (PAI calibration) — needs Phase 4 + Phase 5 combined
7. Phase 7 (fairness audit) — needs Phase 6 output + quarantined table
8. Phase 8 (cyber module) — independent parallel branch, only needs Phase 0
9. Phase 9 (anomaly detection) — independent, only needs Phase 0
10. Phase 10 (dashboard) — integrates everything last

**Critical path for a minimum viable demo:** Phase 0 → 1 → 2 → 3 → 5 → 6 → 10a (operational view only). Phases 4, 7, 8, 9, and 10b can be added incrementally if time allows, in that priority order — 7 (fairness audit) before 8/9, since fairness is the stated differentiator.
