# InsightForge

AI-powered market research & business analysis platform. Get consulting-quality reports in 60 seconds.

**Product of [Autonomous AI Corporation](https://github.com/Azriely)**

## What It Does

InsightForge generates detailed market research reports using AI. Input a company name, industry, and research question — get a professional report with executive summary, competitive analysis, strategic recommendations, and more.

### Report Types
- **Comprehensive Market Analysis** — Full market overview with SWOT, competitors, and strategy
- **Competitive Intelligence** — Deep dive on competitors, positioning, and market share
- **SWOT Analysis** — Detailed strengths, weaknesses, opportunities, threats with cross-analysis
- **Market Sizing (TAM/SAM/SOM)** — Bottom-up and top-down market size estimates

### Unit Economics
- API cost per report: ~$0.01
- Selling price: $49/report or $199/mo
- Gross margin: **99%+**

## Quick Start (Local)

```bash
# Clone
git clone https://github.com/Azriely/insightforge.git
cd insightforge

# Install dependencies
uv sync

# Configure
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# Run
uv run python main.py
# Visit http://localhost:8000
```

## Deploy on Unraid (Docker)

### Option A: Docker Compose (Recommended)

1. SSH into your Unraid server
2. Clone the repo:
   ```bash
   cd /mnt/user/appdata
   git clone https://github.com/Azriely/insightforge.git
   cd insightforge
   ```
3. Create `.env` file:
   ```bash
   echo "ANTHROPIC_API_KEY=your-key-here" > .env
   echo "PORT=8000" >> .env
   ```
4. Start:
   ```bash
   docker compose up -d
   ```
5. Visit `http://your-unraid-ip:8000`

### Option B: Unraid Docker UI

1. Go to **Docker** tab in Unraid
2. Click **Add Container**
3. Configure:
   - **Name**: `insightforge`
   - **Repository**: Build from Dockerfile (or use the compose method above)
   - **Port**: `8000` → `8000`
   - **Variable**: `ANTHROPIC_API_KEY` = `your-key-here`
4. Click **Apply**

### Reverse Proxy (Optional)

To expose with a domain name, add to your Nginx Proxy Manager or Tailscale:

```nginx
server {
    listen 443 ssl;
    server_name insightforge.yourdomain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;  # Reports take ~60s to generate
    }
}
```

## API Usage

```bash
# Generate a report
curl -X POST http://localhost:8000/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "Stripe",
    "industry": "FinTech",
    "question": "What is the competitive landscape for payment processing?",
    "analysis_type": "competitive"
  }'

# List reports
curl http://localhost:8000/api/v1/reports

# Get stats
curl http://localhost:8000/api/v1/stats
```

## Tech Stack

- **Runtime**: Python 3.12 + FastAPI
- **AI**: Claude Sonnet 4 (via Anthropic API)
- **Templates**: Jinja2 (server-side rendering)
- **Package Manager**: uv
- **Deployment**: Docker

## License

Proprietary — Autonomous AI Corporation
