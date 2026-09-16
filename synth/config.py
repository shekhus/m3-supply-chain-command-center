"""
Configuration for the Prairie Bend Foods synthetic dataset.

Everything that defines the fictional world lives here. Change the seed and the
whole dataset regenerates deterministically; change nothing and two machines
produce byte-identical output.

Prairie Bend Foods is a FICTIONAL multi-plant North American protein processor.

Naming note: a separate, unrelated direct-to-consumer farm business trades as
"Prairie Foods" (prairiefoods.farm). The names are distinct and the businesses
are in different markets, but every artifact this generator produces states the
fictional status explicitly, and no claim, metric, or characteristic here refers
to any real company. If you would rather avoid the adjacency entirely, change
COMPANY_NAME plus the plant and warehouse names below, then run
`make name-check` and `make verify`.
No real company, customer, or metric is represented anywhere in this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# ----------------------------------------------------------------------------
# Global
# ----------------------------------------------------------------------------

SEED = 20260912

START_DATE = date(2025, 3, 1)
END_DATE = date(2026, 8, 31)

COMPANY_NAME = "Prairie Bend Foods"

# Order lines generated per business day (before anomaly-driven surges).
LINES_PER_DAY_MEAN = 110
LINES_PER_DAY_SD = 18

# ----------------------------------------------------------------------------
# Plants, warehouses, product groups
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Plant:
    code: str
    name: str
    state: str
    lines: tuple[str, ...]
    share: float  # share of order volume originating here


PLANTS: tuple[Plant, ...] = (
    Plant("PLT-01", "Prairie Bend — Cedar Falls", "IA", ("L1", "L2", "L3"), 0.42),
    Plant("PLT-02", "Prairie Bend — Hastings", "NE", ("L1", "L2"), 0.35),
    Plant("PLT-03", "Prairie Bend — Sheldon", "MN", ("L1", "L2"), 0.23),
)

WAREHOUSES: tuple[tuple[str, str, str], ...] = (
    ("DC-EAST", "Prairie Bend DC East", "OH"),
    ("DC-WEST", "Prairie Bend DC West", "CO"),
)

# product_group -> (shelf_life_days, std_yield_pct, catch_weight, avg_lb_per_case)
PRODUCT_GROUPS: dict[str, tuple[int, float, bool, float]] = {
    "GROUND": (14, 0.780, True, 40.0),
    "PRIMALS": (21, 0.715, True, 65.0),
    "CASE-READY": (12, 0.845, True, 22.0),
    "VALUE-ADDED": (45, 0.910, False, 18.0),
    "OFFAL": (10, 0.640, True, 55.0),
}

N_ITEMS = 80
N_CUSTOMERS = 30

CHANNELS = ("RETAIL", "FOODSERVICE", "DISTRIBUTOR")

# Cities/states for customers — generic US locations, fictional customer names.
CUSTOMER_CITIES: tuple[tuple[str, str], ...] = (
    ("Columbus", "OH"), ("Denver", "CO"), ("Dallas", "TX"), ("Atlanta", "GA"),
    ("Chicago", "IL"), ("Phoenix", "AZ"), ("Seattle", "WA"), ("Charlotte", "NC"),
    ("Kansas City", "MO"), ("Nashville", "TN"), ("Minneapolis", "MN"),
    ("Salt Lake City", "UT"), ("Portland", "OR"), ("Indianapolis", "IN"),
    ("Milwaukee", "WI"),
)

# ----------------------------------------------------------------------------
# Baseline operational behaviour
# ----------------------------------------------------------------------------

# Probability a line ships on or before its confirmed delivery date.
BASE_ON_TIME_RATE = 0.945

# Per-plant modifiers on on-time performance (structural, not an anomaly).
PLANT_ON_TIME_DELTA = {"PLT-01": +0.010, "PLT-02": -0.015, "PLT-03": +0.002}

# Day-of-week modifier (Mon=0). Mondays and Fridays run slightly worse.
DOW_ON_TIME_DELTA = {0: -0.020, 1: +0.004, 2: +0.006, 3: +0.002, 4: -0.012, 5: -0.005, 6: 0.0}

# Probability a line is short-shipped on case count.
BASE_SHORT_CASE_RATE = 0.030

# Catch-weight items: invoiced weight as a fraction of ordered weight,
# normal operation. Tolerance in the metric dictionary is 2%.
CATCH_WEIGHT_MEAN = 1.000
CATCH_WEIGHT_SD = 0.008

# Yield: actual vs standard, normal operation.
YIELD_NOISE_SD = 0.012

# Public holidays that legitimately depress shipping (used for the decoy).
KNOWN_HOLIDAYS: tuple[date, ...] = (
    date(2025, 5, 26), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 11, 28), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 5, 25), date(2026, 7, 3), date(2026, 7, 4),
)

# ----------------------------------------------------------------------------
# Seeded anomalies — Project B ground truth
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Anomaly:
    anomaly_id: str
    metric: str
    segment: dict[str, str]
    start: date
    end: date
    cause: str
    mechanism: str          # how the generator injects it
    magnitude: float        # interpretation depends on mechanism
    expect_detection: bool  # False for the decoy
    notes: str = ""


ANOMALIES: tuple[Anomaly, ...] = (
    Anomaly(
        anomaly_id="A1",
        metric="otif_rate",
        segment={"plant": "PLT-02", "customer_no": "C000031"},
        start=date(2025, 9, 8), end=date(2025, 9, 28),
        cause="Late issue dates on the PLT-02 to C000031 lane",
        mechanism="late_issue_days",
        magnitude=0.55,          # share of lane lines pushed late (1-6 days)
        expect_detection=True,
        notes="Primary lane-level OTIF anomaly. Attribution should name this lane.",
    ),
    Anomaly(
        anomaly_id="A2",
        metric="fill_rate_weight",
        segment={"plant": "PLT-01", "product_group": "CASE-READY"},
        start=date(2025, 11, 10), end=date(2025, 11, 23),
        cause="Short catch weights on case-ready lines at PLT-01",
        mechanism="weight_shortfall",
        magnitude=0.91,          # invoiced weight ~91% of ordered
        expect_detection=True,
        notes="Only visible on the weight basis, not the case-count basis.",
    ),
    Anomaly(
        anomaly_id="A3",
        metric="yield_variance_pct",
        segment={"plant": "PLT-03", "line": "L2", "product_group": "PRIMALS"},
        start=date(2026, 1, 12), end=date(2026, 1, 21),
        cause="Depressed output on PLT-03 line L2 primals",
        mechanism="yield_shortfall",
        magnitude=0.075,         # output reduced by ~7.5%
        expect_detection=True,
    ),
    Anomaly(
        anomaly_id="A4",
        metric="inventory_age_days",
        segment={"location": "DC-EAST"},
        start=date(2026, 4, 6), end=date(2026, 4, 30),
        cause="Two lots at DC-EAST stopped moving and aged toward expiry",
        mechanism="stalled_lots",
        magnitude=3.0,           # number of lots stalled
        expect_detection=True,
    ),
    Anomaly(
        anomaly_id="A5",
        metric="otif_rate",
        segment={"plant": "ALL"},
        start=date(2026, 7, 2), end=date(2026, 7, 6),
        cause="Independence Day shutdown — expected seasonal dip, NOT an incident",
        mechanism="holiday_dip",
        magnitude=0.25,          # 25% of lines pushed late
        expect_detection=False,
        notes="DECOY. A detector with day-of-week and holiday awareness must not "
              "raise this as HIGH. Flagging it counts against precision.",
    ),
    Anomaly(
        anomaly_id="A6",
        metric="open_backlog_lines",
        segment={"plant": "PLT-02"},
        start=date(2026, 8, 3), end=date(2026, 8, 16),
        cause="Order surge at PLT-02 built an unusual open backlog",
        mechanism="order_surge",
        magnitude=1.8,           # multiplier on order volume for that plant
        expect_detection=True,
    ),
)

# ----------------------------------------------------------------------------
# Seeded data-quality faults — Project A ground truth
# ----------------------------------------------------------------------------

# rule_id -> (description, target_count)
# Counts are deliberately modest: recall is easier to measure honestly on a
# countable set than on a diffuse one.
FAULT_PLAN: dict[str, tuple[str, int]] = {
    "V001_missing_required": ("Required field blanked (customer_no / item_no / dates)", 140),
    "V002_date_reversal":    ("issue_date earlier than the order date", 60),
    "V003_weight_over_tol":  ("invoiced_weight_lb above 105% of ordered_weight_lb", 70),
    "V004_unknown_customer": ("customer_no not present in the customer master", 45),
    "V005_unknown_item":     ("item_no not present in the item master", 45),
    "V006_lot_after_expiry": ("Lot shipped after its expiry date", 50),
    "V007_negative_qty":     ("Negative ordered or invoiced quantity", 35),
    "V008_duplicate_line":   ("Duplicated (order_no, line_no) within a batch", 80),
    "V009_uom_mismatch":     ("Weight recorded in kg where the item master says lb", 55),
}

# Per-plant ambient quirk rates (these are format problems, not seeded faults —
# a correct pipeline normalises them silently and logs the fix).
PLANT_QUIRKS: dict[str, dict[str, object]] = {
    "PLT-01": {
        "date_format": "iso",          # 2026-03-04
        "weight_unit": "lb",
        "customer_format": "prefixed", # C000123
        "missing_weight_rate": 0.000,
        "trailing_space_rate": 0.000,
        "ambient_dup_rate": 0.000,
    },
    "PLT-02": {
        "date_format": "us",           # 03/04/2026
        "weight_unit": "kg",           # whole file in kg
        "customer_format": "bare",     # 123
        "missing_weight_rate": 0.010,
        "trailing_space_rate": 0.000,
        "ambient_dup_rate": 0.000,
    },
    "PLT-03": {
        "date_format": "excel_serial", # 46085
        "weight_unit": "lb",
        "customer_format": "dashed",   # CUST-0123
        "missing_weight_rate": 0.000,
        "trailing_space_rate": 0.035,  # item numbers with trailing whitespace
        "ambient_dup_rate": 0.000,
    },
}

# ----------------------------------------------------------------------------
# Schema drift events — Project A ground truth
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftEvent:
    drift_id: str
    effective: date
    source: str
    kind: str
    detail: str
    expected_alert: str
    affected_canonical: tuple[str, ...] = field(default=())


DRIFT_EVENTS: tuple[DriftEvent, ...] = (
    DriftEvent(
        drift_id="D1",
        effective=date(2026, 3, 1),
        source="PLT-02",
        kind="column_rename",
        detail="cust_no renamed to customer_number",
        expected_alert="Mapping for PLT-02 source column 'cust_no' is broken; "
                       "gold.fact_delivery.customer_no and any consumer view using it "
                       "are affected. Publish must be blocked until remapped.",
        affected_canonical=("customer_no",),
    ),
    DriftEvent(
        drift_id="D2",
        effective=date(2026, 6, 1),
        source="PLT-01",
        kind="column_added_then_required",
        detail="lot_no column added to the PLT-01 extract; the business subsequently "
               "declares it a required field (mid-project change request)",
        expected_alert="New source column 'lot_no' detected on PLT-01 with no mapping. "
                       "After the dictionary bump making it required, historical rows "
                       "lacking lot_no must be queued as exceptions, not dropped.",
        affected_canonical=("lot_no",),
    ),
)

# ----------------------------------------------------------------------------
# Canonical schema — the target of all mapping
# ----------------------------------------------------------------------------

CANONICAL_ORDER_LINE: dict[str, str] = {
    "order_no": "string",
    "line_no": "int",
    "customer_no": "string",
    "item_no": "string",
    "plant": "string",
    "order_date": "date",
    "requested_date": "date",
    "confirmed_delivery_date": "date",
    "issue_date": "date",
    "ordered_qty": "float",
    "invoiced_qty": "float",
    "ordered_weight_lb": "float",
    "invoiced_weight_lb": "float",
    "uom": "string",
    "lot_no": "string",
}

# Per-source physical column names -> canonical. This IS the mapping ground truth.
SOURCE_COLUMN_MAP: dict[str, dict[str, str]] = {
    "PLT-01": {
        "ord_no": "order_no",
        "ln": "line_no",
        "cust_no": "customer_no",
        "itm_no": "item_no",
        "fac": "plant",
        "ord_dt": "order_date",
        "req_dt": "requested_date",
        "cfm_dlv_dt": "confirmed_delivery_date",
        "iss_dt": "issue_date",
        "ord_qty": "ordered_qty",
        "inv_qty": "invoiced_qty",
        "ord_wt": "ordered_weight_lb",
        "inv_wt": "invoiced_weight_lb",
        "uom_cd": "uom",
        "lot_no": "lot_no",          # present only after D2
    },
    "PLT-02": {
        "ORDERNUM": "order_no",
        "ORDERLINE": "line_no",
        "cust_no": "customer_no",    # renamed to customer_number at D1
        "ITEMID": "item_no",
        "FACILITY": "plant",
        "DT_ORDER": "order_date",
        "DT_REQ": "requested_date",
        "DT_CONFIRM": "confirmed_delivery_date",
        "DT_SHIP": "issue_date",
        "QTY_ORD": "ordered_qty",
        "QTY_SHIP": "invoiced_qty",
        "WT_ORD_KG": "ordered_weight_lb",
        "WT_SHIP_KG": "invoiced_weight_lb",
        "UOM": "uom",
        "LOTID": "lot_no",
    },
    "PLT-03": {
        "Order": "order_no",
        "Line": "line_no",
        "Customer": "customer_no",
        "Item": "item_no",
        "Plant": "plant",
        "OrderDate": "order_date",
        "RequestDate": "requested_date",
        "PromiseDate": "confirmed_delivery_date",
        "ShipDate": "issue_date",
        "QtyOrdered": "ordered_qty",
        "QtyShipped": "invoiced_qty",
        "WeightOrdered": "ordered_weight_lb",
        "WeightShipped": "invoiced_weight_lb",
        "UnitOfMeasure": "uom",
        "Lot": "lot_no",
    },
}

# Column names for the downstream reporting feed. These are structural header
# names common to delivery-performance reporting; no client values are used.
REPORT_FEED_COLUMNS: tuple[str, ...] = (
    "Company Name", "Customer Number", "Customer Name", "City",
    "Customer Order Number", "Customer PO Number", "Customer Order Type",
    "Order Line Number", "Item Number", "Item Name", "Product Group",
    "Confirmed Delivery Date", "Planning Date", "Issue Date",
    "Days Between Planning and Issue Date", "Ordered Qty", "Invoiced Qty",
    "Case Fill Rate", "Case Fill Rate max. 100%", "On Time",
)

# ----------------------------------------------------------------------------
# The two BI tools — the central reconciliation problem
# ----------------------------------------------------------------------------
#
# Both tools compute "OTIF" from the same underlying shipments and both are
# confidently wrong in different ways. Neither matches the governed definition.
# Reconciliation is not about deciding who is right: it is about decomposing the
# gap into named, defensible causes so the business can choose one definition.
#
# Governed (metrics/otif.yaml v3, and gold.fact_delivery):
#     on time  = issue_date <= confirmed_delivery_date
#     in full  = weight basis with 2% tolerance for catch-weight items,
#                case count for everything else
#     scope    = all order types
#
# Tool 1 "legacy report suite" — the older, hand-maintained SQL per report.
#     Differs by: measures catch-weight items on CASE COUNT, not weight.
#
# Tool 2 "dashboard tool" — the newer self-service tool, rebuilt independently.
#     Differs by: uses the REQUESTED date not the CONFIRMED date (so a line the
#     plant legitimately re-promised counts as late), applies a 2% case
#     tolerance, and silently excludes consignment orders because of a filter
#     somebody added years ago and never documented.

TOOL1 = {
    "name": "legacy_report_suite",
    "date_basis": "confirmed_delivery_date",
    "case_tolerance": 0.0,
    "weight_basis_for_catch_weight": False,
    "excluded_order_types": (),
}

TOOL2 = {
    "name": "dashboard_tool",
    "date_basis": "requested_date",
    "case_tolerance": 0.02,
    "weight_basis_for_catch_weight": False,
    "excluded_order_types": ("CONS",),
}

# ----------------------------------------------------------------------------
# Metric definition used by the generator's self-check (mirrors metrics/otif.yaml)
# ----------------------------------------------------------------------------

WEIGHT_TOLERANCE = 0.02
