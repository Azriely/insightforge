"""Core analysis engine - generates market research reports using Claude."""

import anthropic
from pydantic import BaseModel


class AnalysisRequest(BaseModel):
    company_name: str
    industry: str
    question: str
    analysis_type: str = "comprehensive"  # comprehensive, competitive, swot, market_sizing


class AnalysisReport(BaseModel):
    company_name: str
    industry: str
    question: str
    analysis_type: str
    executive_summary: str
    full_report: str
    sections: dict[str, str]
    key_insights: list[str]
    recommendations: list[str]
    estimated_tokens_used: int


ANALYSIS_PROMPTS = {
    "comprehensive": """You are a senior business analyst at a top-tier consulting firm (McKinsey/BCG/Bain level).
Generate a comprehensive market research report for the following request.

Company: {company_name}
Industry: {industry}
Research Question: {question}

Produce a detailed, actionable report with the following sections:

## Executive Summary
A concise 2-3 paragraph overview of the key findings.

## Market Overview
- Market size and growth trajectory
- Key trends shaping the industry
- Regulatory environment

## Competitive Landscape
- Major players and their positioning
- Market share distribution (estimated)
- Competitive advantages and moats

## SWOT Analysis
- Strengths (internal)
- Weaknesses (internal)
- Opportunities (external)
- Threats (external)

## Target Customer Analysis
- Customer segments
- Pain points and needs
- Buying behavior and decision criteria

## Strategic Recommendations
- Short-term actions (0-6 months)
- Medium-term strategy (6-18 months)
- Long-term positioning (18+ months)

## Key Risks & Mitigation
- Top 3-5 risks with mitigation strategies

## Financial Indicators
- Revenue potential estimates
- Cost structure considerations
- Unit economics framework

Format the report in clean Markdown. Be specific with data points, percentages, and concrete recommendations.
Do NOT use placeholder data - provide your best estimates based on your knowledge, clearly marking estimates as such.""",

    "competitive": """You are a competitive intelligence analyst at a Fortune 500 company.
Generate a detailed competitive analysis report.

Company: {company_name}
Industry: {industry}
Research Question: {question}

Produce a competitive analysis with these sections:

## Executive Summary

## Competitor Identification
- Direct competitors (5-8)
- Indirect competitors (3-5)
- Emerging threats (2-3)

## Competitive Matrix
For each major competitor, analyze:
- Product/service offerings
- Pricing strategy
- Market positioning
- Key differentiators
- Estimated market share
- Strengths and weaknesses

## Competitive Advantages Assessment
- Where {company_name} wins
- Where {company_name} loses
- White space opportunities

## Strategic Positioning Recommendations
- Differentiation strategy
- Pricing recommendations
- Feature/product gaps to address

## Threat Assessment
- Likelihood and impact of each competitive threat
- Recommended defensive strategies

Format in clean Markdown with specific, actionable insights.""",

    "swot": """You are a strategic planning consultant.
Generate a thorough SWOT analysis for the following:

Company: {company_name}
Industry: {industry}
Research Question: {question}

## Executive Summary

## Strengths (Internal Positive)
List 8-12 strengths with detailed explanation for each.

## Weaknesses (Internal Negative)
List 8-12 weaknesses with detailed explanation for each.

## Opportunities (External Positive)
List 8-12 opportunities with market sizing where possible.

## Threats (External Negative)
List 8-12 threats with probability and impact assessment.

## Cross-Analysis Matrix
- SO Strategies (leverage strengths to capture opportunities)
- WO Strategies (address weaknesses to capture opportunities)
- ST Strategies (use strengths to mitigate threats)
- WT Strategies (minimize weaknesses to avoid threats)

## Priority Actions
Ranked list of 5-7 immediate strategic priorities.

Format in clean Markdown. Be specific and actionable.""",

    "market_sizing": """You are a market sizing expert at a management consulting firm.
Generate a detailed market sizing analysis.

Company: {company_name}
Industry: {industry}
Research Question: {question}

## Executive Summary

## Total Addressable Market (TAM)
- Top-down estimation with methodology
- Bottom-up estimation with methodology
- Reconciliation of estimates

## Serviceable Addressable Market (SAM)
- Geographic constraints
- Segment focus
- Channel constraints

## Serviceable Obtainable Market (SOM)
- Realistic market capture estimates
- Year 1, 3, and 5 projections
- Key assumptions

## Market Segmentation
- By geography
- By customer type
- By product/service line
- By price point

## Growth Projections
- Historical growth rate
- Projected CAGR (5-year)
- Growth drivers
- Growth inhibitors

## Revenue Model Analysis
- Pricing strategies in the market
- Average deal size / ARPU
- Customer lifetime value estimates
- Unit economics framework

## Entry Strategy Recommendations
- Best segments to target first
- Go-to-market approach
- Pricing strategy

Format in clean Markdown with specific numbers and estimates (clearly marked as estimates).""",
}


class AnalysisEngine:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    async def generate_report(self, request: AnalysisRequest) -> AnalysisReport:
        prompt_template = ANALYSIS_PROMPTS.get(
            request.analysis_type, ANALYSIS_PROMPTS["comprehensive"]
        )
        prompt = prompt_template.format(
            company_name=request.company_name,
            industry=request.industry,
            question=request.question,
        )

        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        full_report = message.content[0].text
        tokens_used = message.usage.input_tokens + message.usage.output_tokens

        # Parse sections from the markdown
        sections = self._parse_sections(full_report)
        executive_summary = sections.get("Executive Summary", "See full report.")
        key_insights = self._extract_insights(full_report)
        recommendations = self._extract_recommendations(full_report, request.analysis_type)

        return AnalysisReport(
            company_name=request.company_name,
            industry=request.industry,
            question=request.question,
            analysis_type=request.analysis_type,
            executive_summary=executive_summary,
            full_report=full_report,
            sections=sections,
            key_insights=key_insights,
            recommendations=recommendations,
            estimated_tokens_used=tokens_used,
        )

    def _parse_sections(self, report: str) -> dict[str, str]:
        sections: dict[str, str] = {}
        current_section = ""
        current_content: list[str] = []

        for line in report.split("\n"):
            if line.startswith("## "):
                if current_section:
                    sections[current_section] = "\n".join(current_content).strip()
                current_section = line[3:].strip()
                current_content = []
            else:
                current_content.append(line)

        if current_section:
            sections[current_section] = "\n".join(current_content).strip()

        return sections

    def _extract_insights(self, report: str) -> list[str]:
        insights = []
        for line in report.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- ") and len(stripped) > 20 and len(stripped) < 200:
                insights.append(stripped[2:])
            if len(insights) >= 5:
                break
        return insights if insights else ["See full report for detailed insights."]

    def _extract_recommendations(self, report: str, analysis_type: str) -> list[str]:
        recs = []
        in_recs = False
        for line in report.split("\n"):
            if "recommend" in line.lower() or "strateg" in line.lower():
                in_recs = True
            if in_recs and line.strip().startswith("- "):
                recs.append(line.strip()[2:])
            if len(recs) >= 5:
                break
        return recs if recs else ["See full report for strategic recommendations."]
