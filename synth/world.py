"""
Build the static master data for Prairie Bend Foods: customers, items,
warehouses. Deterministic given the seed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C

# Fictional customer name components. Combined to produce plausible,
# clearly-invented trade names.
_C1 = ("Northgate", "Halloway", "Brightmoor", "Stonecrest", "Fairhaven", "Westvale",
       "Kingsbury", "Ridgefield", "Ellerby", "Marchwood", "Oakhurst", "Pinebluff",
       "Ardenton", "Quillford", "Thornbury")
_C2 = ("Markets", "Food Group", "Provisions", "Grocers", "Food Service",
       "Distributing", "Retail Co", "Wholesale")

_ITEM_DESCRIPTOR = {
    "GROUND": ("81/19 Ground Chuck", "73/27 Ground Beef", "90/10 Ground Round",
               "85/15 Ground Sirloin", "Ground Beef Patty 4oz", "Ground Beef Patty 6oz"),
    "PRIMALS": ("Boneless Chuck Roll", "Beef Brisket Deckle Off", "Short Plate",
                "Beef Knuckle Peeled", "Top Sirloin Butt", "Inside Round",
                "Bottom Round Flat", "Beef Tenderloin PSMO"),
    "CASE-READY": ("Case Ready Sirloin Steak", "Case Ready Ribeye", "Case Ready Strip Steak",
                   "Case Ready Stew Meat", "Case Ready Kabob", "Case Ready Flat Iron"),
    "VALUE-ADDED": ("Seasoned Beef Strips", "Marinated Fajita Meat", "Pre-Cooked Meatball",
                    "Beef Taco Filling", "Seasoned Pot Roast", "Shredded Beef"),
    "OFFAL": ("Beef Liver", "Beef Heart", "Beef Tongue", "Beef Tripe", "Beef Oxtail"),
}


def build_customers(rng: np.random.Generator) -> pd.DataFrame:
    """One row per customer. C000031 is reserved: it is the A1 anomaly lane."""
    rows = []
    for i in range(1, C.N_CUSTOMERS + 1):
        city, state = C.CUSTOMER_CITIES[(i - 1) % len(C.CUSTOMER_CITIES)]
        name = f"{_C1[(i * 7) % len(_C1)]} {_C2[(i * 3) % len(_C2)]}"
        channel = C.CHANNELS[(i - 1) % len(C.CHANNELS)]
        rows.append(
            {
                "customer_no": f"C{i:06d}",
                "customer_name": name,
                "city": city,
                "state": state,
                "channel": channel,
                # Relative order volume: a few large accounts, a long tail.
                "volume_weight": float(np.round(rng.gamma(shape=2.2, scale=1.0) + 0.3, 3)),
            }
        )

    df = pd.DataFrame(rows)

    # Guarantee C000031 exists and is a sizeable retail account, since A1
    # attribution depends on that lane being material.
    if "C000031" not in set(df["customer_no"]):
        df.loc[len(df)] = {
            "customer_no": "C000031",
            "customer_name": "Northgate Markets",
            "city": "Columbus",
            "state": "OH",
            "channel": "RETAIL",
            "volume_weight": 6.0,
        }
    else:
        idx = df.index[df["customer_no"] == "C000031"][0]
        df.loc[idx, "channel"] = "RETAIL"
        df.loc[idx, "volume_weight"] = 6.0

    df["volume_share"] = df["volume_weight"] / df["volume_weight"].sum()
    return df.reset_index(drop=True)


def build_items(rng: np.random.Generator) -> pd.DataFrame:
    """One row per item, spread across the product groups."""
    groups = list(C.PRODUCT_GROUPS.keys())
    rows = []
    for i in range(1, C.N_ITEMS + 1):
        group = groups[(i - 1) % len(groups)]
        shelf_life, std_yield, catch_weight, lb_per_case = C.PRODUCT_GROUPS[group]
        descriptors = _ITEM_DESCRIPTOR[group]
        base = descriptors[(i // len(groups)) % len(descriptors)]
        rows.append(
            {
                "item_no": f"IT{i:05d}",
                "item_name": f"{base} {(i % 4) + 1}",
                "product_group": group,
                "catch_weight_flag": bool(catch_weight),
                "shelf_life_days": int(shelf_life),
                "std_yield_pct": float(np.round(std_yield + rng.normal(0, 0.012), 4)),
                "avg_lb_per_case": float(np.round(lb_per_case * rng.uniform(0.9, 1.1), 2)),
                "uom": "CASE",
                "volume_weight": float(np.round(rng.gamma(shape=2.0, scale=1.0) + 0.25, 3)),
            }
        )
    df = pd.DataFrame(rows)
    df["volume_share"] = df["volume_weight"] / df["volume_weight"].sum()
    return df


def build_warehouses() -> pd.DataFrame:
    return pd.DataFrame(
        [{"location": code, "location_name": name, "state": st}
         for code, name, st in C.WAREHOUSES]
    )


def build_plants() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"plant": p.code, "plant_name": p.name, "state": p.state,
             "lines": ",".join(p.lines), "volume_share": p.share}
            for p in C.PLANTS
        ]
    )
