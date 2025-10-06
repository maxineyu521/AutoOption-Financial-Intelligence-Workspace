# data_models.py
from pydantic import BaseModel, Field
from typing import List

class FinalReport(BaseModel):
    summary: str = Field(..., description="Core answer summary to the user's question")
    key_findings: List[str] = Field(..., description="List of main findings")
    counter_arguments: List[str] = Field(..., description="Main counter-arguments or risks")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Overall confidence score for conclusions")
    uncertainty_notes: str = Field(..., description="Explanation of confidence score reasoning and uncertainties in the report")

    def to_markdown(self) -> str:
        """Convert report to Markdown format"""
        findings_md = "\n".join(f"- {item}" for item in self.key_findings)
        counters_md = "\n".join(f"- {item}" for item in self.counter_arguments)

        return f"""
# Financial Analysis Report

## Core Summary
{self.summary}

---

### Key Findings
{findings_md}

---

### Main Risks & Counter-Arguments
{counters_md}

---

### Conclusion Confidence
- **Confidence Score**: **{self.confidence_score * 100:.1f}%**
- **Uncertainty Notes**: {self.uncertainty_notes}
"""
