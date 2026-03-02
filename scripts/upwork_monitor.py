#!/usr/bin/env python3
"""Upwork Job Monitor — finds market research jobs and drafts proposals.

This script:
1. Searches Upwork for relevant market research jobs via GraphQL API
2. Scores and filters jobs by relevance, budget, and client quality
3. Generates personalized proposal drafts using Claude
4. Saves results to a JSON file and optionally sends alerts

Requirements:
  - Upwork API credentials (OAuth2): UPWORK_CLIENT_ID, UPWORK_CLIENT_SECRET
  - Upwork access token: UPWORK_ACCESS_TOKEN (from OAuth2 flow)
  - Anthropic API key: ANTHROPIC_API_KEY (for proposal drafting)

Usage:
  # First time: run OAuth2 flow to get access token
  python scripts/upwork_monitor.py --auth

  # Monitor for jobs (run on cron every 15-30 min)
  python scripts/upwork_monitor.py --search

  # Draft proposals for top jobs
  python scripts/upwork_monitor.py --draft

  # Full pipeline: search → score → draft
  python scripts/upwork_monitor.py --run
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Directory for storing job data and proposals
DATA_DIR = Path(__file__).parent.parent / "data" / "upwork"
JOBS_FILE = DATA_DIR / "jobs.json"
PROPOSALS_FILE = DATA_DIR / "proposals.json"
SEEN_FILE = DATA_DIR / "seen_jobs.json"


# ---- Upwork API Client ----

UPWORK_GQL_URL = "https://www.upwork.com/api/graphql"

SEARCH_KEYWORDS = [
    "market research",
    "competitive analysis",
    "market sizing",
    "SWOT analysis",
    "industry analysis",
    "competitor research",
    "market analysis report",
    "business analysis report",
    "TAM SAM SOM",
    "feasibility study",
]

JOB_SEARCH_QUERY = """
query SearchJobs($filter: MarketplaceJobPostingSearchFilter, $sort: [MarketplaceJobPostingSortAttribute]) {
  marketplaceJobPostings(
    marketPlaceJobFilter: $filter
    sortAttributes: $sort
    pagination: { first: 20 }
  ) {
    totalCount
    edges {
      node {
        id
        title
        createdDateTime
        description
        duration
        durationLabel
        engagement
        amount { amount currencyCode }
        hourlyBudget { min max }
        skills { name prettyName }
        client {
          totalSpent { amount }
          totalHires
          totalPostedJobs
          verificationStatus
          location { country }
        }
        applicants { totalCount }
        occupationGroup { preferredLabel }
      }
    }
  }
}
"""


def get_upwork_headers():
    token = os.environ.get("UPWORK_ACCESS_TOKEN", "")
    if not token:
        print("ERROR: UPWORK_ACCESS_TOKEN not set. Run with --auth first.")
        sys.exit(1)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def search_upwork_jobs(keyword: str) -> list[dict]:
    """Search Upwork for jobs matching a keyword."""
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx required. Install with: pip install httpx")
        sys.exit(1)

    variables = {
        "filter": {
            "searchTerm_eq": {"andTerms_all": keyword},
        },
        "sort": [{"field": "CREATE_TIME", "sortOrder": "DESC"}],
    }

    resp = httpx.post(
        UPWORK_GQL_URL,
        headers=get_upwork_headers(),
        json={"query": JOB_SEARCH_QUERY, "variables": variables},
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"  API error for '{keyword}': {resp.status_code}")
        return []

    data = resp.json()
    edges = (
        data.get("data", {})
        .get("marketplaceJobPostings", {})
        .get("edges", [])
    )
    return [edge["node"] for edge in edges]


def search_all_keywords() -> list[dict]:
    """Search all keywords and deduplicate results."""
    all_jobs = {}
    for kw in SEARCH_KEYWORDS:
        print(f"  Searching: {kw}")
        jobs = search_upwork_jobs(kw)
        for job in jobs:
            all_jobs[job["id"]] = job
        time.sleep(1)  # Rate limiting courtesy

    print(f"  Found {len(all_jobs)} unique jobs across {len(SEARCH_KEYWORDS)} keywords")
    return list(all_jobs.values())


# ---- Job Scoring ----

def score_job(job: dict) -> dict:
    """Score a job on relevance, budget, and client quality (0-100)."""
    score = 0
    reasons = []

    # Budget score (0-30)
    budget = 0
    if job.get("amount"):
        budget = float(job["amount"].get("amount", 0))
    elif job.get("hourlyBudget"):
        hb = job["hourlyBudget"]
        budget = float(hb.get("max", hb.get("min", 0))) * 10  # Estimate 10hrs

    if budget >= 300:
        score += 30
        reasons.append(f"High budget (${budget:.0f})")
    elif budget >= 100:
        score += 20
        reasons.append(f"Medium budget (${budget:.0f})")
    elif budget >= 50:
        score += 10
        reasons.append(f"Low budget (${budget:.0f})")
    else:
        reasons.append("Budget unclear or very low")

    # Client quality (0-30)
    client = job.get("client", {})
    total_spent = float(client.get("totalSpent", {}).get("amount", 0))
    if total_spent > 10000:
        score += 30
        reasons.append(f"Top client (${total_spent:.0f} spent)")
    elif total_spent > 1000:
        score += 20
        reasons.append(f"Good client (${total_spent:.0f} spent)")
    elif total_spent > 0:
        score += 10
        reasons.append(f"New client (${total_spent:.0f} spent)")

    if client.get("verificationStatus") == "VERIFIED":
        score += 5
        reasons.append("Verified client")

    # Competition (0-15)
    applicants = job.get("applicants", {}).get("totalCount", 0)
    if applicants < 5:
        score += 15
        reasons.append(f"Low competition ({applicants} applicants)")
    elif applicants < 15:
        score += 10
        reasons.append(f"Moderate competition ({applicants} applicants)")
    elif applicants < 30:
        score += 5
        reasons.append(f"High competition ({applicants} applicants)")

    # Relevance (0-20)
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()
    text = title + " " + desc

    high_relevance = ["market research", "competitive analysis", "swot", "market sizing", "tam sam"]
    medium_relevance = ["industry analysis", "competitor", "market report", "business analysis"]

    for term in high_relevance:
        if term in text:
            score += 10
            reasons.append(f"High relevance: '{term}'")
            break

    for term in medium_relevance:
        if term in text:
            score += 5
            reasons.append(f"Medium relevance: '{term}'")
            break

    # Skills match
    skills = [s.get("name", "").lower() for s in job.get("skills", [])]
    our_skills = {"market research", "business analysis", "competitive analysis", "data analysis", "report writing"}
    overlap = our_skills & set(skills)
    if overlap:
        score += 5
        reasons.append(f"Skills match: {', '.join(overlap)}")

    return {
        **job,
        "score": min(score, 100),
        "score_reasons": reasons,
        "estimated_budget": budget,
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }


# ---- Proposal Drafting ----

PROPOSAL_PROMPT = """You are a freelance market research specialist on Upwork. Draft a personalized, compelling proposal for the following job.

JOB TITLE: {title}
JOB DESCRIPTION: {description}
CLIENT BUDGET: ${budget:.0f} (estimated)
CLIENT SPEND HISTORY: ${client_spent:.0f} total spent on Upwork
REQUIRED SKILLS: {skills}

YOUR EXPERTISE:
- AI-powered market research reports delivered in 24 hours
- Report types: Comprehensive Market Analysis, Competitive Intelligence, SWOT Analysis, Market Sizing (TAM/SAM/SOM)
- Reports are 2,000-4,000 words, structured with frameworks used by McKinsey/BCG/Bain
- Sample reports available at insightforge.azriel.io

PROPOSAL GUIDELINES:
- Start with a specific insight or observation about their project (show you read the brief)
- Keep it under 200 words (Upwork proposals should be concise)
- Mention your 24-hour turnaround
- Offer to share a relevant sample report
- End with a question to start a conversation
- Sound human, professional, and confident — NOT salesy or generic
- Suggest a fair bid amount based on the job scope

Write ONLY the proposal text, nothing else."""


def draft_proposal(job: dict) -> str:
    """Use Claude to draft a personalized proposal for a job."""
    try:
        import anthropic
    except ImportError:
        return "[ERROR: anthropic package not installed]"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "[ERROR: ANTHROPIC_API_KEY not set]"

    client_data = job.get("client", {})
    skills = ", ".join(s.get("prettyName", s.get("name", "")) for s in job.get("skills", []))

    prompt = PROPOSAL_PROMPT.format(
        title=job.get("title", "Unknown"),
        description=job.get("description", "No description")[:2000],
        budget=job.get("estimated_budget", 0),
        client_spent=float(client_data.get("totalSpent", {}).get("amount", 0)),
        skills=skills or "Not specified",
    )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---- Data Persistence ----

def load_seen_jobs() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen_jobs(seen: set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(list(seen)))


def save_jobs(jobs: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(jobs, indent=2, default=str))


def save_proposals(proposals: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROPOSALS_FILE.write_text(json.dumps(proposals, indent=2, default=str))


def load_jobs() -> list[dict]:
    if JOBS_FILE.exists():
        return json.loads(JOBS_FILE.read_text())
    return []


# ---- OAuth2 Flow ----

def run_auth_flow():
    """Interactive OAuth2 flow to get Upwork access token."""
    client_id = os.environ.get("UPWORK_CLIENT_ID", "")
    client_secret = os.environ.get("UPWORK_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print("ERROR: Set UPWORK_CLIENT_ID and UPWORK_CLIENT_SECRET environment variables.")
        print("Get these from: https://www.upwork.com/developer/keys/apply")
        sys.exit(1)

    redirect_uri = "https://insightforge.azriel.io/api/v1/upwork/callback"
    auth_url = (
        f"https://www.upwork.com/ab/account-security/oauth2/authorize"
        f"?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
    )

    print(f"\n1. Open this URL in your browser:\n   {auth_url}")
    print(f"\n2. Authorize the application")
    print(f"\n3. You'll be redirected. Copy the 'code' parameter from the URL.")
    code = input("\nPaste the authorization code: ").strip()

    try:
        import httpx
    except ImportError:
        print("ERROR: httpx required. Install with: pip install httpx")
        sys.exit(1)

    resp = httpx.post(
        "https://www.upwork.com/api/v3/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )

    if resp.status_code != 200:
        print(f"ERROR: Token exchange failed: {resp.text}")
        sys.exit(1)

    tokens = resp.json()
    print(f"\nAccess Token: {tokens['access_token']}")
    print(f"Refresh Token: {tokens.get('refresh_token', 'N/A')}")
    print(f"\nAdd to your .env: UPWORK_ACCESS_TOKEN={tokens['access_token']}")


# ---- CLI ----

def cmd_search():
    """Search for jobs and score them."""
    print("Searching Upwork for market research jobs...")
    jobs = search_all_keywords()

    seen = load_seen_jobs()
    new_jobs = [j for j in jobs if j["id"] not in seen]
    print(f"  {len(new_jobs)} new jobs (out of {len(jobs)} total)")

    scored = [score_job(j) for j in new_jobs]
    scored.sort(key=lambda x: x["score"], reverse=True)

    save_jobs(scored)
    seen.update(j["id"] for j in new_jobs)
    save_seen_jobs(seen)

    print(f"\nTop 5 Jobs:")
    for j in scored[:5]:
        print(f"  [{j['score']:3d}] ${j['estimated_budget']:>6.0f} | {j['title'][:60]}")
        print(f"        Reasons: {', '.join(j['score_reasons'][:3])}")
    print(f"\nSaved {len(scored)} scored jobs to {JOBS_FILE}")


def cmd_draft():
    """Draft proposals for top-scored jobs."""
    jobs = load_jobs()
    if not jobs:
        print("No jobs found. Run --search first.")
        return

    top_jobs = [j for j in jobs if j.get("score", 0) >= 40][:5]
    if not top_jobs:
        top_jobs = jobs[:3]

    print(f"Drafting proposals for {len(top_jobs)} top jobs...")
    proposals = []

    for job in top_jobs:
        print(f"  Drafting for: {job['title'][:50]}...")
        proposal_text = draft_proposal(job)
        proposals.append({
            "job_id": job["id"],
            "job_title": job["title"],
            "score": job.get("score", 0),
            "estimated_budget": job.get("estimated_budget", 0),
            "proposal": proposal_text,
            "drafted_at": datetime.now(timezone.utc).isoformat(),
        })

    save_proposals(proposals)
    print(f"\nDrafted {len(proposals)} proposals. Saved to {PROPOSALS_FILE}")
    print("\nReview proposals, then copy-paste to Upwork manually.")

    for p in proposals:
        print(f"\n{'='*60}")
        print(f"JOB: {p['job_title']}")
        print(f"SCORE: {p['score']} | BUDGET: ${p['estimated_budget']:.0f}")
        print(f"{'='*60}")
        print(p["proposal"])


def cmd_run():
    """Full pipeline: search → score → draft."""
    cmd_search()
    print()
    cmd_draft()


def main():
    parser = argparse.ArgumentParser(description="Upwork Job Monitor for InsightForge")
    parser.add_argument("--auth", action="store_true", help="Run OAuth2 authorization flow")
    parser.add_argument("--search", action="store_true", help="Search and score jobs")
    parser.add_argument("--draft", action="store_true", help="Draft proposals for top jobs")
    parser.add_argument("--run", action="store_true", help="Full pipeline: search + draft")
    args = parser.parse_args()

    if args.auth:
        run_auth_flow()
    elif args.search:
        cmd_search()
    elif args.draft:
        cmd_draft()
    elif args.run:
        cmd_run()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
