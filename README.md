# affirm-agent

## Repository layout

```text
affirm-agent/
├── README.md                     # Overview, how to run, folder guide
├── docker-compose.yml            # Runs all services together
├── .env.example                  # Lists every environment variable, no real values
├── .gitignore                    # .env, node_modules, pycache
├── docs/
│   ├── proposal/                 # Project proposal
│   └── workflow-handoffs.md      # Workflow steps, owners and handoffs
├── contracts/                    # Shared data formats (Pydantic); each producer maintains its own
│   ├── kyb_contracts/
│   │   ├── case_input.py         # CaseInput
│   │   ├── worker_io.py          # WorkerTask, FollowUp, RecordsFinding, EvidenceFinding
│   │   ├── memo.py               # Memo, CheckResult, evidence_index
│   │   └── decision.py           # DecisionInput, DecisionRecord
│   └── examples/                 # One valid JSON example per format
├── services/
│   ├── gateway/                  # FastAPI; verifies the JWT and routes by path
│   ├── auth/                     # Login, JWT, roles (auth_db)
│   ├── case/                     # Cases, decisions, audit log (case_db); seed script
│   └── agent/                    # LangGraph workflow and its API; see below
├── frontend/                     # React + React Router; nginx serves the pages and forwards /api to the gateway
├── db/init.sql                   # Creates the three databases and their accounts
├── data/golden/
│   ├── ec1/                      # EC-1 cases, one folder per case
│   ├── ec2/                      # EC-2 cases
│   ├── ec4/                      # EC-4 cases
│   └── combo/                    # Cases that combine edge cases
├── eval/                         # Evaluation runner, single-agent baseline, results
└── scripts/validate_golden.py    # Checks that each golden case’s files agree with each other
```

### `services/agent/app/`

```text
services/agent/app/
├── main.py              # FastAPI entry point
├── api/                 # POST /reviews, GET /reviews/{id}, POST /reviews/{id}/decision
├── graph/               # State, orchestration, lead nodes, reconcile, checks
├── workers/             # Records worker, evidence worker
├── tools/               # sos_search, dba_lookup, tin_match, etc., plus the world.json loader
├── clients/             # HTTP client for calling the case service
└── observability/       # LangFuse
```

## Communiation Schema
### Agent and Backend
```
class CaseStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class FlagReason(str, Enum):
    NAME_SIMILAR_MATCH = "NAME_SIMILAR_MATCH"
    DBA_UNVERIFIED = "DBA_UNVERIFIED"
    TIN_MISMATCH = "TIN_MISMATCH"
    PRODUCT_CATEGORY_REVIEW = "PRODUCT_CATEGORY_REVIEW"


class Owner(BaseModel):
    ref: str
    name: str
    role: str


class Application(BaseModel):
    legal_name: str
    dba: str | None = None
    ein: str = Field(pattern=r"^\d{2}-\d{7}$")
    state: str = Field(min_length=2, max_length=2)
    state_entity_id: str
    entity_type: str
    formation_date: date
    owners: list[Owner]


class CheckResult(BaseModel):
    result: str
    matched_name: str | None = None


class Checks(BaseModel):
    sos_name: CheckResult
    dba: CheckResult
    tin: CheckResult


class Document(BaseModel):
    id: str
    type: str
    # Using dict[str, Any] here so different document types can have different extracted schemas
    extracted: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)


class BankVerification(BaseModel):
    id: str
    account_holder: str
    status: Literal["verified", "unverified", "pending", "failed"]
    verified_at: date


class WebSnapshot(BaseModel):
    id: str
    url: HttpUrl
    captured_at: date
    text: str


class OwnerKYC(BaseModel):
    id: str
    owner_ref: str
    result: Literal["pass", "fail", "pending"]

# ==========================================
# Top-Level Sub-Agent Schema
# ==========================================

class CaseReview(BaseModel):
    case_id: str
    status: CaseStatus
    flag_reasons: list[FlagReason]
    application: Application
    checks: Checks
    documents: list[Document]
    bank_verification: BankVerification | None = None
    web_snapshot: list[WebSnapshot] = Field(default_factory=list)
    owner_kyc: list[OwnerKYC] = Field(default_factory=list)
```

#### Lead and Sub-agents
```
# ==========================================
# Shared & Nested Models
# ==========================================

class Hypothesis(BaseModel):
    hypothesis_id: str
    text: str


class Owner(BaseModel):
    owner_id: str
    name: str
    role: str
    kyc_ref: Optional[str] = None


class Document(BaseModel):
    doc_id: str
    doc_type: str
    file_ref: str
    uploaded_at: datetime


class CaseContext(BaseModel):
    case_id: str
    ec_flags: list[str]
    submitted_legal_name: str
    entity_type: str
    formation_state: str
    address: str
    tin_ref: str
    tin_last4: str
    owners: list[Owner]
    documents: list[Document] = Field(default_factory=list)
    website: Optional[str] = None


class Citation(BaseModel):
    source_id: str
    locator: str
    excerpt: str


class Finding(BaseModel):
    finding_id: str
    subject: str
    field: str
    value: Any
    source_id: str
    extraction_method: str
    confidence: Literal["low", "medium", "high"] | str
    citation: Citation
    note: Optional[str] = None


class Source(BaseModel):
    source_id: str
    source_type: str
    authority: Literal["official", "third_party", "merchant"] | str
    locator: str
    retrieved_at: datetime


class HypothesisSignal(BaseModel):
    hypothesis_id: str
    direction: Literal["supports", "contradicts", "neutral"] | str
    finding_ids: list[str]


# ==========================================
# Top-Level Sub-Agent Schemas
# ==========================================

class WorkerInput(BaseModel):
    """Schema for JSON blocks 1, 2, and 3."""
    task_id: str
    worker: Literal["records", "evidence"] | str
    question: str
    focus_fields: list[str]
    hypotheses: list[Hypothesis]
    context: CaseContext
    prior_findings: list[Finding] = Field(default_factory=list)
    follow_up_of: Optional[str] = None
    max_tool_calls: int


class WorkerResult(BaseModel):
    """Schema for JSON block 4."""
    task_id: str
    case_id: str
    worker: Literal["records", "evidence"] | str
    status: Literal["completed", "failed", "partial"] | str
    findings: list[Finding]
    sources: list[Source]
    hypothesis_signals: list[HypothesisSignal]
    not_found: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    tool_calls_used: int
    error: Optional[str] = None
```
