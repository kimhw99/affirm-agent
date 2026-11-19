"""CaseInput: the case the existing KYB system hands to our review system (step 1).

Served by the case service (GET /cases/{case_id}). Used by intake, the graph and
both workers, so every change goes through a PR that those owners review.

Rules for this contract:
- It checks structure only: types, formats and IDs. It does not check business
  rules such as "an LLC must have a state ID". Missing or odd data is exactly what
  the agent has to handle, so the contract must be able to carry it.
- Everything arrives already parsed: the application, the vendor checks, the fields
  extracted from each document, the bank verification, the website fields and the
  KYC results. The workers parse nothing; they fetch these stored records by ID.
- Full tax IDs never appear here, neither SSNs nor EINs. The application carries the
  TIN type and its last 4 digits; owners are referenced as owner:P1. The case
  service keeps the real numbers in a separate table that the model never sees.
- Raw merchant text (OCR text, page text) stays in case_db and is not part of this
  contract. Extracted values are still the merchant's claims: only the Evidence
  worker reads documents, bank verification and web snapshots.
- Owner KYC comes from a third-party vendor; the Records worker reads it.
- Evidence IDs (D, B, W, K) are fixed when the case arrives and never change.
"""

import re
from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Contract(BaseModel):
    """Base for every contract model: an unknown field is an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


# ---------- ID formats (worker_io.py imports these) ----------

CASE_ID = r"^case_\d+$"
OWNER_REF = r"^owner:P\d+$"
DOC_ID = r"^D\d+$"
BANK_ID = r"^B\d+$"
WEB_ID = r"^W\d+$"
KYC_ID = r"^K\d+$"


# ---------- Enums and fixed values ----------

class CaseStatus(str, Enum):
    PENDING_REVIEW = "pending_review"   # waiting for the agent and the human to review
    APPROVED = "approved"               # human chose approve -> the case is done
    DOCS_REQUESTED = "docs_requested"   # human chose request_docs -> waiting for the merchant to upload more evidences -> after uploaded, [the case goes back to PENDING_REVIEW] OR [create a new case with the new evidences and close this one]
    ESCALATED = "escalated"             # human chose escalate -> hand the case to a senior analyst (human) -> senior analyst can choose approve, deny, or request more evidences
    DENIED = "denied"                   # human chose deny -> the case is done


class FlagReason(str, Enum):
    """
    EC-1: Legal name vs DBA / suffix & punctuation variants
    """
    NAME_SIMILAR_MATCH = "NAME_SIMILAR_MATCH"        # close to the state record, not identical
    NAME_UNVERIFIED = "NAME_UNVERIFIED"              # the applied name matches no state record
    DBA_UNVERIFIED = "DBA_UNVERIFIED"                # the DBA couldn't be tied to this business
    """
    EC-2: EIN/TIN mismatch, including IRS name-control issues
    """
    TIN_MISMATCH = "TIN_MISMATCH"                    # the TIN and the name don't match at the IRS
    TIN_NOT_FOUND = "TIN_NOT_FOUND"                  # the IRS has no record of the TIN
    TIN_NOT_ISSUED = "TIN_NOT_ISSUED"                # not issued yet, common for brand-new EINs
    """
    EC-4: Newly formed businesses and sole proprietors missing from databases
    """
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"            # no state registration found
    NO_COMMERCIAL_RECORD = "NO_COMMERCIAL_RECORD"    # not in commercial databases
    """
    Outside EC-1/2/4: intake passes these to the analyst untouched
    """
    PRODUCT_CATEGORY_REVIEW = "PRODUCT_CATEGORY_REVIEW"
    OFAC_POTENTIAL_MATCH = "OFAC_POTENTIAL_MATCH"


EntityType = Literal["LLC", "CORPORATION", "PARTNERSHIP", "SOLE_PROPRIETORSHIP"]

TinType = Literal["EIN", "SSN"]

DocumentType = Literal[
    "irs_cp575",              # IRS letter confirming a new EIN
    "irs_147c",               # IRS letter confirming the EIN and the name on file now
    "articles",               # articles of organization or incorporation
    "articles_of_amendment",  # a change such as a new name
    "dba_certificate",        # assumed name (DBA) registration
    "w9",                     # the merchant's own tax ID form
    "good_standing",          # state certificate that the entity is active
    "business_license",       # city, county or state license to operate
]

# Fields the existing system extracts from each document type. A document may miss
# some of them (extraction can fail), but it can't have a field outside its list.
# Tax IDs are extracted as their last 4 digits only.
DOCUMENT_FIELDS: dict[str, set[str]] = {
    "irs_cp575": {"name", "ein_last4", "name_control", "address", "date"},
    "irs_147c": {"name", "ein_last4", "name_control", "address", "date"},
    "articles": {"legal_name", "state", "state_entity_id", "formation_date"},
    "articles_of_amendment": {"old_name", "new_name", "state_entity_id", "date"},
    "dba_certificate": {"dba", "registrant", "jurisdiction", "filed", "expires"},
    "w9": {"legal_name", "business_name", "tin_type", "tin_last4"},
    "good_standing": {"legal_name", "status", "date"},
    "business_license": {"holder", "status", "address", "issued", "authority"},
}

# Fields the existing system extracts from a website snapshot.
WEB_FIELDS: set[str] = {"legal_name", "dba", "address"}

# A full SSN or EIN, with or without dashes.
TIN_PATTERN = re.compile(r"(?<!\d)(?:\d{3}-\d{2}-\d{4}|\d{2}-\d{7}|\d{9})(?!\d)")
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")  # kept for existing imports; TIN_PATTERN covers it


def _check_keys(kind: str, extracted: dict[str, str], allowed: set[str]) -> None:
    unknown = set(extracted) - allowed
    if unknown:
        raise ValueError(f"{kind} has unknown extracted fields: {sorted(unknown)}")


# ---------- Application: what the merchant entered ----------

class Owner(Contract):
    ref: str = Field(pattern=OWNER_REF)          # a reference, never the SSN itself
    name: str
    role: str


class Application(Contract):
    legal_name: str                              # for a sole proprietor, the owner's own name
    dba: str | None = None
    address: str                                 # the business address the merchant entered
    tin_type: TinType | None = None              # sole proprietors may use the owner's SSN
    tin_last4: str | None = Field(default=None, pattern=r"^\d{4}$")  # full number stays in case_db
    state: str = Field(pattern=r"^[A-Z]{2}$")
    state_entity_id: str | None = None           # sole proprietors usually have none
    entity_type: EntityType
    formation_date: date | None = None
    owners: list[Owner] = Field(min_length=1)

    @model_validator(mode="after")
    def tin_type_and_last4_together(self) -> "Application":
        if (self.tin_type is None) != (self.tin_last4 is None):
            raise ValueError("tin_type and tin_last4 must be given together")
        return self


# ---------- Checks: what the existing system's KYB vendor already ran ----------

class NameCheck(Contract):
    result: Literal["Verified", "Similar Match", "Unverified"]
    matched_name: str | None = None              # the name exactly as the state record shows it


class DbaCheck(Contract):
    result: Literal["Verified", "Unverified"]


class TinCheck(Contract):
    result: Literal["Match", "Mismatch", "Not Found", "Not Issued"]


class CommercialCheck(Contract):
    result: Literal["Found", "Not Found"]


class Checks(Contract):
    sos_name: NameCheck
    dba: DbaCheck | None = None                  # only when the application lists a DBA
    tin: TinCheck | None = None                  # missing when no TIN was given
    commercial_db: CommercialCheck | None = None


# ---------- Merchant evidence (only the Evidence worker reads it) ----------

class Document(Contract):
    id: str = Field(pattern=DOC_ID)
    type: DocumentType
    extracted: dict[str, str]                    # extracted upstream; keys depend on type
    confidence: float = Field(ge=0.0, le=1.0)    # how sure the extraction is

    @model_validator(mode="after")
    def known_fields_only(self) -> "Document":
        _check_keys(self.type, self.extracted, DOCUMENT_FIELDS[self.type])
        return self


class BankVerification(Contract):
    id: str = Field(pattern=BANK_ID)
    account_holder: str
    status: Literal["verified", "unverified", "pending", "failed"]
    verified_at: date | None = None


class WebSnapshot(Contract):
    id: str = Field(pattern=WEB_ID)
    url: HttpUrl
    captured_at: date
    extracted: dict[str, str] = Field(default_factory=dict)  # keys in WEB_FIELDS; still the merchant's claim

    @model_validator(mode="after")
    def known_fields_only(self) -> "WebSnapshot":
        _check_keys("web_snapshot", self.extracted, WEB_FIELDS)
        return self


# ---------- Owner KYC (third-party; the Records worker reads it) ----------

class OwnerKYC(Contract):
    id: str = Field(pattern=KYC_ID)
    owner_ref: str = Field(pattern=OWNER_REF)    # must match an owner in application.owners
    result: Literal["pass", "fail", "pending"]


# ---------- Top level ----------

class CaseInput(Contract):
    case_id: str = Field(pattern=CASE_ID)        # “case_” prefix + digits
    status: CaseStatus
    flag_reasons: list[FlagReason] = Field(min_length=1)
    application: Application
    checks: Checks
    documents: list[Document] = Field(default_factory=list)
    bank_verification: BankVerification | None = None
    web_snapshot: list[WebSnapshot] = Field(default_factory=list)
    owner_kyc: list[OwnerKYC] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_integrity(self) -> "CaseInput":
        ids = [d.id for d in self.documents] + [w.id for w in self.web_snapshot]
        ids += [k.id for k in self.owner_kyc]
        if self.bank_verification:
            ids.append(self.bank_verification.id)
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate evidence IDs: {duplicates}")

        owner_refs = {o.ref for o in self.application.owners}
        unknown = sorted({k.owner_ref for k in self.owner_kyc} - owner_refs)
        if unknown:
            raise ValueError(f"owner_kyc refers to unknown owners: {unknown}")

        # state_entity_id is skipped: some states issue 9-digit entity IDs.
        if TIN_PATTERN.search(self.model_dump_json(exclude={"application": {"state_entity_id"}})):
            raise ValueError("a full SSN or EIN appears in the case; keep only the last 4 digits")
        return self
