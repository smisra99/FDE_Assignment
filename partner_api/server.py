"""
Kestrel Logistics Partner API - mock server (assessment use only).

Run:
    pip install fastapi uvicorn
    python server.py
    # serves on http://localhost:8088

Auth:
    Every request to /v1/* requires header:  X-API-Key: kp_live_7f3a9c21
    Missing or wrong key returns 401.

Behaviour you should expect (and handle):
    - /v1/freight_invoices is CURSOR paginated. 200 records per page. Follow next_cursor.
    - Roughly 1 request in 9 returns 429 with a Retry-After header.
    - Roughly 1 request in 25 returns 503.
    - p95 latency is deliberately slow on the first page of each cursor walk.
    - Timestamps are UTC. The operational database is Asia/Kolkata local time.
    - The `amount` field is in paise, not rupees. This is not in the docs on purpose;
      it is documented here.

Endpoints:
    GET  /v1/health
    GET  /v1/carriers
    GET  /v1/freight_invoices?cursor=&limit=&from=&to=
    GET  /v1/shipment_events?invoice_id=
    GET  /v1/fuel_surcharge?month=YYYY-MM
"""
import hashlib
import random
import time
import datetime as dt
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
import uvicorn

API_KEY = "kp_live_7f3a9c21"
app = FastAPI(title="Kestrel Logistics Partner API", version="1.4.2")

CARRIERS = [
    {"carrier_id": "CR-101", "name": "Bluewheel Logistics", "mode": "SURFACE", "reefer_capable": True,
     "sla_hours": 24, "regions": ["West", "Central"]},
    {"carrier_id": "CR-102", "name": "Trident Roadways", "mode": "SURFACE", "reefer_capable": False,
     "sla_hours": 36, "regions": ["North", "Central"]},
    {"carrier_id": "CR-103", "name": "ColdLink Express", "mode": "REEFER", "reefer_capable": True,
     "sla_hours": 18, "regions": ["South", "West"]},
    {"carrier_id": "CR-104", "name": "Eastbound Freight", "mode": "SURFACE", "reefer_capable": False,
     "sla_hours": 48, "regions": ["East"]},
    {"carrier_id": "CR-105", "name": "Metro Milkrun", "mode": "LCV", "reefer_capable": True,
     "sla_hours": 12, "regions": ["West", "South", "North"]},
]

START = dt.date(2025, 1, 1)
DAYS = (dt.date(2026, 6, 30) - START).days + 1
TOTAL_INVOICES = 41_500


def _rand(seed_parts, lo, hi):
    h = int(hashlib.md5("|".join(map(str, seed_parts)).encode()).hexdigest()[:8], 16)
    return lo + (h % max(1, (hi - lo)))


def _invoice(i: int):
    d = START + dt.timedelta(days=_rand(("d", i), 0, DAYS))
    carrier = CARRIERS[_rand(("c", i), 0, len(CARRIERS))]
    base = _rand(("a", i), 180_00, 94_000_00)          # paise
    return {
        "invoice_id": f"FI-{i:07d}",
        "carrier_id": carrier["carrier_id"],
        "carrier_name": carrier["name"],
        "warehouse_code": f"WH{_rand(('w', i), 1, 9):02d}",
        "route_code": f"RT{_rand(('r', i), 1, 141):04d}",
        "invoice_date": d.isoformat(),
        "service_date": (d - dt.timedelta(days=_rand(("s", i), 0, 4))).isoformat(),
        "amount": base,                                  # PAISE
        "currency": "INR",
        "fuel_surcharge_pct": round(_rand(("f", i), 200, 1400) / 100, 2),
        "detention_charge": _rand(("dt", i), 0, 320_00),
        "distance_km": round(_rand(("k", i), 40, 1900) / 10, 1),
        "weight_kg": round(_rand(("wt", i), 200, 89000) / 10, 1),
        "temperature_controlled": bool(_rand(("tc", i), 0, 10) < 3),
        "status": ["PAID", "PAID", "PAID", "DISPUTED", "PENDING"][_rand(("st", i), 0, 5)],
        "created_at_utc": dt.datetime.combine(d, dt.time(_rand(("h", i), 0, 24),
                                                         _rand(("m", i), 0, 60))).isoformat() + "Z",
    }


def _auth(key: Optional[str]):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def _chaos():
    r = random.random()
    if r < 0.111:
        return JSONResponse(status_code=429, content={"error": "rate_limited",
                            "message": "Too many requests. Back off and retry."},
                            headers={"Retry-After": str(random.choice([1, 2, 3]))})
    if r < 0.151:
        return JSONResponse(status_code=503, content={"error": "upstream_unavailable",
                            "message": "Billing service temporarily unavailable."})
    return None


@app.get("/v1/health")
def health():
    return {"status": "ok", "version": "1.4.2", "server_time_utc": dt.datetime.utcnow().isoformat() + "Z"}


@app.get("/v1/carriers")
def carriers(x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    return {"data": CARRIERS, "count": len(CARRIERS)}


@app.get("/v1/freight_invoices")
def freight_invoices(cursor: Optional[str] = None,
                     limit: int = Query(200, le=200),
                     date_from: Optional[str] = Query(None, alias="from"),
                     date_to: Optional[str] = Query(None, alias="to"),
                     x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    c = _chaos()
    if c is not None:
        return c
    start = 0 if not cursor else int(cursor.split("_")[-1])
    if start == 0:
        time.sleep(random.uniform(0.8, 1.9))     # slow first page
    else:
        time.sleep(random.uniform(0.05, 0.3))
    rows = []
    i = start
    while len(rows) < limit and i < TOTAL_INVOICES:
        inv = _invoice(i)
        i += 1
        if date_from and inv["invoice_date"] < date_from:
            continue
        if date_to and inv["invoice_date"] > date_to:
            continue
        rows.append(inv)
    nxt = f"cur_{i}" if i < TOTAL_INVOICES else None
    return {"data": rows, "next_cursor": nxt, "page_size": len(rows),
            "total_estimate": TOTAL_INVOICES}


@app.get("/v1/shipment_events")
def shipment_events(invoice_id: str, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    c = _chaos()
    if c is not None:
        return c
    n = _rand(("ev", invoice_id), 3, 8)
    stages = ["BOOKED", "PICKED_UP", "IN_TRANSIT", "HUB_SCAN", "OUT_FOR_DELIVERY", "DELIVERED", "POD_UPLOADED"]
    base = dt.datetime(2025, 6, 1) + dt.timedelta(hours=_rand(("eb", invoice_id), 0, 9000))
    return {"invoice_id": invoice_id, "events": [
        {"seq": k + 1, "event": stages[k % len(stages)],
         "timestamp_utc": (base + dt.timedelta(hours=k * 6)).isoformat() + "Z",
         "location": ["Bhiwandi", "Pune", "Hoskote", "Bhiwadi", "Dankuni"][_rand(("el", invoice_id, k), 0, 5)],
         "temperature_celsius": round(_rand(("et", invoice_id, k), -20, 120) / 10, 1)}
        for k in range(n)]}


@app.get("/v1/fuel_surcharge")
def fuel_surcharge(month: str, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    return {"month": month,
            "surcharge_pct": round(_rand(("fs", month), 300, 1500) / 100, 2),
            "diesel_index": round(_rand(("di", month), 8900, 10600) / 100, 2),
            "source": "internal_index"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8088)
