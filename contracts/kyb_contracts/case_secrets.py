"""CaseSecrets: secrets.json, the private part of one golden case.

Holds the numbers that CaseInput and world.json carry only as last 4 digits: the full
TIN the merchant submitted, and each owner's SSN. It sits next to case.json and
world.json in data/golden/<ec>/<case_id>/.

The case service loads this file into its private tables only. No endpoint returns
these values, and they never reach the agent or the model. Endpoint 6 (tin-record)
uses the full TIN inside the case service to find the IRS record, then returns only
the last 4 digits.

Every number here is fake. Following the test data convention in
case_input_guide.md, fake EINs start with 00 and fake SSNs with 000; neither is ever
issued. This model rejects any other number, so a real one can't slip in, and it
keeps the numbers out of validation errors and repr().
"""

import re

from pydantic import ConfigDict, Field, model_validator

from .case_input import CASE_ID, OWNER_REF, Contract, TinType

FAKE_EIN = re.compile(r"00-\d{7}")          # e.g. 00-1234567
FAKE_SSN = re.compile(r"000-\d{2}-\d{4}")   # e.g. 000-12-3456


def _check_fake(kind: str, number: str) -> None:
    pattern, example = (FAKE_EIN, "00-1234567") if kind == "EIN" else (FAKE_SSN, "000-12-3456")
    if not pattern.fullmatch(number):
        prefix = example.split("-")[0]
        raise ValueError(
            f"a fake {kind} is written like {example}: it must start with {prefix}, "
            "which is never issued, so no real number gets in"
        )


class _Private(Contract):
    """Base for the models here: a validation error never echoes the input."""

    model_config = ConfigDict(hide_input_in_errors=True)


class OwnerSecret(_Private):
    ref: str = Field(pattern=OWNER_REF)           # an owner in case.json's application.owners
    ssn: str = Field(repr=False)

    @model_validator(mode="after")
    def ssn_is_fake(self) -> "OwnerSecret":
        _check_fake("SSN", self.ssn)
        return self


class CaseSecrets(_Private):
    case_id: str = Field(pattern=CASE_ID)         # the golden case this file belongs to
    tin_type: TinType | None = None               # the same as application.tin_type
    tin: str | None = Field(default=None, repr=False)  # the full submitted TIN, with dashes
    owners: list[OwnerSecret] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_integrity(self) -> "CaseSecrets":
        if (self.tin_type is None) != (self.tin is None):
            raise ValueError("tin_type and tin must be given together")
        if self.tin is not None:
            _check_fake(self.tin_type, self.tin)

        refs = [o.ref for o in self.owners]
        duplicates = sorted({r for r in refs if refs.count(r) > 1})
        if duplicates:
            raise ValueError(f"duplicate owner refs: {duplicates}")
        return self
