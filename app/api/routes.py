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
from app.core import database as db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

REPORT_PRICE_CENTS = 4900  # $49


def _stripe():
    """Lazy-load stripe module (only when keys are configured)."""
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        return None
    import stripe
    stripe.api_key = key
    return stripe


def get_engine() -> AnalysisEngine:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    return AnalysisEngine(api_key=api_key)


def require_admin(x_admin_key: str = Header(None)):
    admin_key = os.environ.get("ADMIN_API_KEY", "")
    if not admin_key:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
    if not x_admin_key or x_admin_key != admin_key:
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")


# ---------- Pages ----------

@router.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    stripe_enabled = bool(os.environ.get("STRIPE_SECRET_KEY"))
    return templates.TemplateResponse(
        "index.html", {"request": request, "stripe_enabled": stripe_enabled}
    )


@router.get("/order", response_class=HTMLResponse)
async def order_page(request: Request):
    stripe_enabled = bool(os.environ.get("STRIPE_SECRET_KEY"))
    return templates.TemplateResponse(
        "order.html", {"request": request, "stripe_enabled": stripe_enabled}
    )


@router.get("/order/{order_id}", response_class=HTMLResponse)
async def order_status_page(request: Request, order_id: str, key: str = ""):
    order = db.get_order(order_id)
    if not order or order.get("access_key") != key:
        raise HTTPException(status_code=404, detail="Order not found")
    report = db.get_report(order.get("report_id") or "")
    return templates.TemplateResponse(
        "order_status.html", {"request": request, "order": order, "report": report}
    )


@router.get("/report/{report_id}", response_class=HTMLResponse)
async def view_report(request: Request, report_id: str):
    report = db.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return templates.TemplateResponse(
        "report.html", {"request": request, "report": report}
    )


# ---------- Stripe Checkout ----------

class CheckoutRequest(BaseModel):
    client_name: str
    client_email: str
    company_name: str
    industry: str
    analysis_type: str = "comprehensive"
    question: str
    source: str = "website"


@router.post("/api/v1/checkout")
async def create_checkout(req: CheckoutRequest):
    """Create a Stripe Checkout Session for a report purchase."""
    stripe = _stripe()
    if not stripe:
        raise HTTPException(status_code=503, detail="Payments not configured yet")

    order_id = f"ord_{int(time.time())}_{secrets.token_hex(4)}"
    access_key = secrets.token_urlsafe(16)
    base_url = os.environ.get("BASE_URL", "https://insightforge.azriel.io")

    # Save order as pending_payment
    db.save_order({
        "id": order_id,
        "access_key": access_key,
        "status": "pending_payment",
        "client_name": req.client_name,
        "client_email": req.client_email,
        "company_name": req.company_name,
        "industry": req.industry,
        "analysis_type": req.analysis_type,
        "question": req.question,
        "source": req.source,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    analysis_labels = {
        "comprehensive": "Comprehensive Market Analysis",
        "competitive": "Competitive Intelligence Report",
        "swot": "SWOT Analysis",
        "market_sizing": "Market Sizing (TAM/SAM/SOM)",
    }

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": REPORT_PRICE_CENTS,
                "product_data": {
                    "name": f"{analysis_labels.get(req.analysis_type, 'Market Research Report')}: {req.company_name}",
                    "description": f"AI-powered {req.analysis_type} report for {req.company_name} ({req.industry})",
                },
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{base_url}/order/{order_id}?key={access_key}",
        cancel_url=f"{base_url}/order?cancelled=true",
        customer_email=req.client_email,
        metadata={"order_id": order_id},
    )

    db.update_order(order_id, stripe_session_id=session.id)

    return {"checkout_url": session.url, "order_id": order_id}


@router.post("/api/v1/webhooks/stripe")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Stripe webhook events."""
    stripe = _stripe()
    if not stripe:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if webhook_secret:
        try:
            event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
        except (ValueError, stripe.error.SignatureVerificationError):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    else:
        import json
        event = json.loads(payload)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = session.get("metadata", {}).get("order_id")
        if not order_id:
            return {"status": "ignored", "reason": "no order_id in metadata"}

        order = db.get_order(order_id)
        if order and order["status"] == "pending_payment":
            db.update_order(
                order_id,
                status="queued",
                stripe_payment_id=session.get("payment_intent", ""),
                amount_cents=session.get("amount_total", REPORT_PRICE_CENTS),
            )
            db.increment_stats(revenue_cents=session.get("amount_total", REPORT_PRICE_CENTS))
            background_tasks.add_task(_generate_report_sync, order_id)

    return {"status": "ok"}


# ---------- Order Pipeline ----------

class OrderRequest(BaseModel):
    client_name: str
    client_email: str
    company_name: str
    industry: str
    analysis_type: str = "comprehensive"
    question: str
    source: str = "direct"


def _generate_report_sync(order_id: str):
    """Background task: generate the report for an order."""
    order = db.get_order(order_id)
    if not order:
        return

    db.update_order(order_id, status="generating")
    engine = get_engine()

    try:
        loop = asyncio.new_event_loop()
        start = time.time()
        report = loop.run_until_complete(engine.generate_report(AnalysisRequest(
            company_name=order["company_name"],
            industry=order["industry"],
            analysis_type=order["analysis_type"],
            question=order["question"],
        )))
        elapsed = time.time() - start
        loop.close()

        report_id = f"rpt_{int(time.time())}_{secrets.token_hex(4)}"
        report_data = {
            "id": report_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_time_seconds": round(elapsed, 2),
            **report.model_dump(),
        }

        db.save_report(report_data)
        db.update_order(
            order_id,
            status="completed",
            report_id=report_id,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        db.increment_stats(reports=1, tokens=report.estimated_tokens_used)

    except Exception as e:
        db.update_order(order_id, status="failed", error=str(e))


@router.post("/api/v1/orders")
async def create_order(
    order_req: OrderRequest,
    background_tasks: BackgroundTasks,
    x_admin_key: str = Header(None),
):
    """Create order directly (admin-only, for Fiverr/Upwork fulfillment)."""
    require_admin(x_admin_key)

    order_id = f"ord_{int(time.time())}_{secrets.token_hex(4)}"
    access_key = secrets.token_urlsafe(16)

    order = {
        "id": order_id,
        "access_key": access_key,
        "status": "queued",
        "client_name": order_req.client_name,
        "client_email": order_req.client_email,
        "company_name": order_req.company_name,
        "industry": order_req.industry,
        "analysis_type": order_req.analysis_type,
        "question": order_req.question,
        "source": order_req.source,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    db.save_order(order)
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
    order = db.get_order(order_id)
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
async def list_orders_endpoint(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    orders = db.list_orders()
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
                "amount_cents": o.get("amount_cents", 0),
            }
            for o in orders
        ],
        "total": len(orders),
        "pending": sum(1 for o in orders if o["status"] in ("queued", "generating", "pending_payment")),
        "completed": sum(1 for o in orders if o["status"] == "completed"),
    }


# ---------- Direct Admin Analyze ----------

@router.post("/api/v1/analyze")
async def analyze(request: AnalysisRequest, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    engine = get_engine()

    start = time.time()
    report = await engine.generate_report(request)
    elapsed = time.time() - start

    report_id = f"rpt_{int(time.time())}_{secrets.token_hex(4)}"
    report_data = {
        "id": report_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generation_time_seconds": round(elapsed, 2),
        **report.model_dump(),
    }

    db.save_report(report_data)
    db.increment_stats(reports=1, tokens=report.estimated_tokens_used)

    return report_data


# ---------- Waitlist ----------

class WaitlistEntry(BaseModel):
    email: str


@router.post("/api/v1/waitlist")
async def join_waitlist(entry: WaitlistEntry):
    email = entry.email.strip().lower()
    added = db.add_to_waitlist(email, datetime.now(timezone.utc).isoformat())
    if not added:
        return {"status": "already_registered", "message": "You're already on the list!"}
    return {"status": "success", "message": "You're on the list! We'll notify you when we launch."}


@router.get("/api/v1/waitlist")
async def get_waitlist(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    wl = db.get_waitlist()
    return {"waitlist": wl, "total": len(wl)}


# ---------- Reports + Stats ----------

@router.get("/api/v1/reports")
async def list_reports_endpoint(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    reports = db.list_reports()
    return {"reports": reports, "total": len(reports)}


@router.get("/api/v1/reports/{report_id}")
async def get_report(report_id: str):
    report = db.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/api/v1/stats")
async def get_stats(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    stats = db.get_stats()
    total_reports = stats["total_reports"]
    total_tokens = stats["total_tokens"]
    revenue_cents = stats["revenue_cents"]

    estimated_cost = total_tokens * 0.000003
    revenue_usd = revenue_cents / 100.0

    wl = db.get_waitlist()
    orders = db.list_orders()

    return {
        "total_reports": total_reports,
        "total_tokens": total_tokens,
        "revenue_cents": revenue_cents,
        "revenue_usd": round(revenue_usd, 2),
        "estimated_api_cost_usd": round(estimated_cost, 4),
        "estimated_profit_usd": round(revenue_usd - estimated_cost, 2),
        "avg_tokens_per_report": (
            round(total_tokens / total_reports) if total_reports > 0 else 0
        ),
        "waitlist_signups": len(wl),
        "total_orders": len(orders),
    }


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "insightforge",
        "version": "0.4.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
