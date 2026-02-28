"""API routes for InsightForge."""

import os
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.analyzer import AnalysisEngine, AnalysisRequest

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Simple in-memory store for reports (replace with DB later)
reports_store: dict[str, dict] = {}
# Simple usage tracking
usage_stats = {"total_reports": 0, "total_tokens": 0, "revenue_cents": 0}


def get_engine() -> AnalysisEngine:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    return AnalysisEngine(api_key=api_key)


@router.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/report/{report_id}", response_class=HTMLResponse)
async def view_report(request: Request, report_id: str):
    report = reports_store.get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return templates.TemplateResponse(
        "report.html", {"request": request, "report": report}
    )


@router.post("/api/v1/analyze")
async def analyze(request: AnalysisRequest):
    """Generate a market research report. This is the core revenue endpoint."""
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


@router.get("/api/v1/reports")
async def list_reports():
    """List all generated reports."""
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
    """Get a specific report by ID."""
    report = reports_store.get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/api/v1/stats")
async def get_stats():
    """Usage statistics for the CEO dashboard."""
    estimated_cost = usage_stats["total_tokens"] * 0.000003  # rough estimate
    estimated_revenue = usage_stats["total_reports"] * 49.0  # $49/report
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
    }


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "insightforge",
        "version": "0.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
