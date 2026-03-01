"""API routes for InsightForge."""

import os
import secrets
import time
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.core.analyzer import AnalysisEngine, AnalysisRequest

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Simple in-memory stores (replace with DB later)
reports_store: dict[str, dict] = {}
orders_store: dict[str, dict] = {}
usage_stats = {"total_reports": 0, "total_tokens": 0, "revenue_cents": 0}
waitlist: list[dict] = []


def get_engine() -> AnalysisEngine:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    return AnalysisEngine(api_key=api_key)


def require_admin(x_admin_key: str = Header(None)):
    """Verify the admin API key for protected endpoints."""
    admin_key = os.environ.get("ADMIN_API_KEY", "")
    if not admin_key:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
    if not x_admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")


def require_order_key(order_id: str, key: str):
    """Verify an order access key."""
    order = orders_store.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("access_key") != key:
        raise HTTPException(status_code=401, detail="Invalid access key")
    return order


# ---------- Pages ----------

@router.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/order", response_class=HTMLResponse)
async def order_page(request: Request):
    """Public order intake form — linked from Fiverr/Upwork gig descriptions."""
    return templates.TemplateResponse("order.html", {"request": request})


@router.get("/order/{order_id}", response_class=HTMLResponse)
async def order_status_page(request: Request, order_id: str, key: str = ""):
    """Order status page — customer sees progress + report when ready."""
    order = orders_store.get(order_id)
    if not order or order.get("access_key") != key:
        raise HTTPException(status_code=404, detail="Order not found")
    report = reports_store.get(order.get("report_id", ""))
    return templates.TemplateResponse(
        "order_status.html", {"request": request, "order": order, "report": report}
    )


@router.get("/report/{report_id}", response_class=HTMLResponse)
async def view_report(request: Request, report_id: str):
    report = reports_store.get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return templates.TemplateResponse(
        "report.html", {"request": request, "report": report}
    )


# ---------- Order Pipeline ----------

class OrderRequest(BaseModel):
    client_name: str
    client_email: str
    company_name: str
    industry: str
    analysis_type: str = "comprehensive"
    question: str
    source: str = "direct"  # fiverr, upwork, direct, website


def _generate_report_sync(order_id: str):
    """Background task: generate the report for an order."""
    order = orders_store.get(order_id)
    if not order:
        return

    order["status"] = "generating"
    engine = get_engine()

    try:
        import asyncio
        loop = asyncio.new_event_loop()
        report = loop.run_until_complete(engine.generate_report(AnalysisRequest(
            company_name=order["company_name"],
            industry=order["industry"],
            analysis_type=order["analysis_type"],
            question=order["question"],
        )))
        loop.close()

        report_id = f"rpt_{int(time.time())}_{hash(order['company_name']) % 10000:04d}"
        report_data = {
            "id": report_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_time_seconds": 0,
            **report.model_dump(),
        }

        reports_store[report_id] = report_data
        order["status"] = "completed"
        order["report_id"] = report_id
        order["completed_at"] = datetime.now(timezone.utc).isoformat()

        usage_stats["total_reports"] += 1
        usage_stats["total_tokens"] += report.estimated_tokens_used

    except Exception as e:
        order["status"] = "failed"
        order["error"] = str(e)


@router.post("/api/v1/orders")
async def create_order(order_req: OrderRequest, background_tasks: BackgroundTasks):
    """Create a new order — auto-generates report in background."""
    order_id = f"ord_{int(time.time())}_{secrets.token_hex(4)}"
    access_key = secrets.token_urlsafe(16)

    order = {
        "id": order_id,
        "access_key": access_key,
        "status": "queued",  # queued → generating → completed / failed
        "client_name": order_req.client_name,
        "client_email": order_req.client_email,
        "company_name": order_req.company_name,
        "industry": order_req.industry,
        "analysis_type": order_req.analysis_type,
        "question": order_req.question,
        "source": order_req.source,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "report_id": None,
        "completed_at": None,
    }

    orders_store[order_id] = order

    # Auto-generate report in background
    background_tasks.add_task(_generate_report_sync, order_id)

    base_url = os.environ.get("BASE_URL", "https://insightforge.azriel.io")
    status_url = f"{base_url}/order/{order_id}?key={access_key}"

    return {
        "order_id": order_id,
        "status": "queued",
        "status_url": status_url,
        "message": "Your report is being generated. Check the status URL in ~90 seconds.",
    }


@router.get("/api/v1/orders/{order_id}")
async def get_order_status(order_id: str, key: str = ""):
    """Check order status (public with access key)."""
    order = orders_store.get(order_id)
    if not order or order.get("access_key") != key:
        raise HTTPException(status_code=404, detail="Order not found")

    result = {
        "order_id": order["id"],
        "status": order["status"],
        "company_name": order["company_name"],
        "analysis_type": order["analysis_type"],
        "created_at": order["created_at"],
    }

    if order["status"] == "completed" and order.get("report_id"):
        base_url = os.environ.get("BASE_URL", "https://insightforge.azriel.io")
        result["report_url"] = f"{base_url}/report/{order['report_id']}"
        result["completed_at"] = order["completed_at"]

    return result


@router.get("/api/v1/orders")
async def list_orders(x_admin_key: str = Header(None)):
    """List all orders (admin only) — your fulfillment dashboard."""
    require_admin(x_admin_key)
    return {
        "orders": [
            {
                "id": o["id"],
                "status": o["status"],
                "client_name": o["client_name"],
                "client_email": o["client_email"],
                "company_name": o["company_name"],
                "analysis_type": o["analysis_type"],
                "source": o["source"],
                "created_at": o["created_at"],
                "completed_at": o.get("completed_at"),
                "report_id": o.get("report_id"),
            }
            for o in orders_store.values()
        ],
        "total": len(orders_store),
        "pending": sum(1 for o in orders_store.values() if o["status"] in ("queued", "generating")),
        "completed": sum(1 for o in orders_store.values() if o["status"] == "completed"),
    }


# ---------- Direct Admin Analyze (CLI) ----------

@router.post("/api/v1/analyze")
async def analyze(request: AnalysisRequest, x_admin_key: str = Header(None)):
    """Generate a market research report directly. Admin-only."""
    require_admin(x_admin_key)
    engine = get_engine()

    start = time.time()
    report = await engine.generate_report(request)
    elapsed = time.time() - start

    report_id = f"rpt_{int(time.time())}_{hash(request.company_name) % 10000:04d}"
    report_data = {
        "id": report_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generation_time_seconds": round(elapsed, 2),
        **report.model_dump(),
    }

    reports_store[report_id] = report_data
    usage_stats["total_reports"] += 1
    usage_stats["total_tokens"] += report.estimated_tokens_used

    return report_data


# ---------- Waitlist ----------

class WaitlistEntry(BaseModel):
    email: str


@router.post("/api/v1/waitlist")
async def join_waitlist(entry: WaitlistEntry):
    email = entry.email.strip().lower()
    if any(w["email"] == email for w in waitlist):
        return {"status": "already_registered", "message": "You're already on the list!"}
    waitlist.append({
        "email": email,
        "joined_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "success", "message": "You're on the list! We'll notify you when we launch."}


@router.get("/api/v1/waitlist")
async def get_waitlist(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    return {"waitlist": waitlist, "total": len(waitlist)}


# ---------- Reports + Stats ----------

@router.get("/api/v1/reports")
async def list_reports(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    return {
        "reports": [
            {
                "id": r["id"],
                "company_name": r["company_name"],
                "industry": r["industry"],
                "analysis_type": r["analysis_type"],
                "created_at": r["created_at"],
            }
            for r in reports_store.values()
        ],
        "total": len(reports_store),
    }


@router.get("/api/v1/reports/{report_id}")
async def get_report(report_id: str):
    report = reports_store.get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/api/v1/stats")
async def get_stats(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    estimated_cost = usage_stats["total_tokens"] * 0.000003
    estimated_revenue = usage_stats["total_reports"] * 49.0
    return {
        **usage_stats,
        "estimated_api_cost_usd": round(estimated_cost, 4),
        "estimated_revenue_usd": round(estimated_revenue, 2),
        "estimated_profit_usd": round(estimated_revenue - estimated_cost, 2),
        "avg_tokens_per_report": (
            round(usage_stats["total_tokens"] / usage_stats["total_reports"])
            if usage_stats["total_reports"] > 0
            else 0
        ),
        "waitlist_signups": len(waitlist),
        "total_orders": len(orders_store),
    }


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "insightforge",
        "version": "0.3.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
