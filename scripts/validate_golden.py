"""Checks that each golden case's files agree with each other.

A golden case is a folder data/golden/<ec>/<case_id>/, where <ec> is ec1, ec2, ec4
or combo. It holds two files, each checked against its model in
contracts/kyb_contracts/ (other files in the folder are ignored):

    case.json   the case the existing system hands over   CaseInput  (case_input.py)
    world.json  the mocked official and vendor records    World      (world.py)

Once each file passes its model, this script checks what no single model can see:
- case_id: the folder name and both files agree.
- TIN: case.json's tin_type and tin_last4 and world.json's tin_record describe the
  same TIN, or neither has one.
- Vendor checks: checks.tin fits the IRS status in tin_record; checks.sos_name's
  matched_name is the legal name of an entity in world.json; checks.dba and
  checks.tin appear only when the application has a DBA or a TIN.
- Flags: each flag has the vendor result behind it, e.g. TIN_MISMATCH needs
  checks.tin = Mismatch. Only this direction is checked, because the existing
  system doesn't raise a flag for every result (case_2001's commercial_db is
  Not Found without NO_COMMERCIAL_RECORD).
- Folder: a case in ec1, ec2 or ec4 has at least one flag of that edge case, and a
  case in combo has flags from at least two.
- Across cases: case IDs and mock record IDs (state_entity_id, event_id, filing_id,
  profile_id) are unique, because every world.json goes into the same tables.

Not checked until the team decides: how NAME_UNVERIFIED and ENTITY_NOT_FOUND differ
(open question 10 in docs/workflow-handoffs.md), and the expected-answer file.

Usage, from the repo root (needs pydantic; `pip install -e ./contracts` installs it):

    python scripts/validate_golden.py                    # every case under data/golden
    python scripts/validate_golden.py data/golden/ec1    # one edge case's cases
    python scripts/validate_golden.py data/golden/ec1/<case_id>

The cross-case checks cover only the cases in that run, so run it with no argument
before committing. It prints each problem and exits with 1 if there are any, 0 if
there are none, and 2 if the path isn't a folder.
"""

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "contracts"))  # use this repo's contracts, installed or not

from pydantic import BaseModel, ValidationError  # noqa: E402

from kyb_contracts.case_input import CaseInput  # noqa: E402
from kyb_contracts.world import World  # noqa: E402

GOLDEN = REPO / "data" / "golden"

FILES: dict[str, type[BaseModel]] = {
    "case.json": CaseInput,
    "world.json": World,
}

# The flags of each edge case, grouped as in FlagReason (case_input.py).
EC_FLAGS: dict[str, set[str]] = {
    "ec1": {"NAME_SIMILAR_MATCH", "NAME_UNVERIFIED", "DBA_UNVERIFIED"},
    "ec2": {"TIN_MISMATCH", "TIN_NOT_FOUND", "TIN_NOT_ISSUED"},
    "ec4": {"ENTITY_NOT_FOUND", "NO_COMMERCIAL_RECORD"},
}
EC_FOLDERS = {*EC_FLAGS, "combo"}

# The vendor result each flag needs: (field of checks, result).
FLAG_NEEDS: dict[str, tuple[str, str]] = {
    "NAME_SIMILAR_MATCH": ("sos_name", "Similar Match"),
    "NAME_UNVERIFIED": ("sos_name", "Unverified"),
    "ENTITY_NOT_FOUND": ("sos_name", "Unverified"),
    "DBA_UNVERIFIED": ("dba", "Unverified"),
    "TIN_MISMATCH": ("tin", "Mismatch"),
    "TIN_NOT_FOUND": ("tin", "Not Found"),
    "TIN_NOT_ISSUED": ("tin", "Not Issued"),
    "NO_COMMERCIAL_RECORD": ("commercial_db", "Not Found"),
}

# The IRS status behind each vendor TIN result.
IRS_STATUS_FOR: dict[str, str] = {
    "Match": "issued",
    "Mismatch": "issued",
    "Not Found": "not_found",
    "Not Issued": "not_issued",
}


@dataclass
class GoldenCase:
    folder: Path
    case: CaseInput | None = None
    world: World | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.folder.parent.name}/{self.folder.name}"


def _subfolders(path: Path) -> list[Path]:
    return sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))


def find_case_folders(path: Path) -> tuple[list[Path], list[str]]:
    """The case folders under path: data/golden, one <ec> folder, or one case folder."""
    if path.parent.name in EC_FOLDERS:
        return [path], []
    if path.name in EC_FOLDERS:
        return _subfolders(path), []
    folders, problems = [], []
    for child in _subfolders(path):
        if child.name in EC_FOLDERS:
            folders += _subfolders(child)
        else:
            problems.append(f"{child.name}/ is not an edge-case folder; use one of {sorted(EC_FOLDERS)}")
    return folders, problems


def _describe(error: dict) -> str:
    where = ".".join(str(part) for part in error["loc"])
    message = error["msg"].removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


def load_case(folder: Path) -> GoldenCase:
    """Read a case folder's two files and check each against its model."""
    golden = GoldenCase(folder)
    for name, model in FILES.items():
        path = folder / name
        if not path.is_file():
            golden.problems.append(f"{name} is missing")
            continue
        try:
            parsed = model.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as error:
            # Location and message only: never echo a value, in case it's a full TIN.
            golden.problems += [f"{name}: {_describe(e)}" for e in error.errors()]
            continue
        setattr(golden, name.removesuffix(".json"), parsed)
    return golden


def check_case(golden: GoldenCase) -> None:
    """Add the problems that show only when a case's files are read together."""
    problems = golden.problems
    for name, parsed in (("case.json", golden.case), ("world.json", golden.world)):
        if parsed is not None and parsed.case_id != golden.folder.name:
            problems.append(f"{name}: case_id is {parsed.case_id}, but the folder is {golden.folder.name}")

    case, world = golden.case, golden.world
    if case is None:
        return  # every other check compares against case.json
    app, checks = case.application, case.checks
    flags = {getattr(flag, "value", flag) for flag in case.flag_reasons}

    if world is not None:
        record = world.tin_record
        if record is None and app.tin_type is not None:
            problems.append("world.json: tin_record is missing, but the case has a TIN")
        elif record is not None and app.tin_type is None:
            problems.append("world.json: has a tin_record, but the case has no TIN")
        elif record is not None and (record.tin_type, record.tin_last4) != (app.tin_type, app.tin_last4):
            problems.append(f"world.json: tin_record is {record.tin_type} ending {record.tin_last4}, "
                            f"but case.json has {app.tin_type} ending {app.tin_last4}")
        if checks.tin is not None and record is not None:
            expected = IRS_STATUS_FOR[checks.tin.result]
            if record.irs_status != expected:
                problems.append(f"world.json: checks.tin is {checks.tin.result}, so tin_record.irs_status "
                                f"should be {expected}, not {record.irs_status}")
        matched = checks.sos_name.matched_name
        if matched is not None and matched not in {e.legal_name for e in world.registry}:
            problems.append(f"world.json: no registry entity has the legal_name {matched!r} "
                            "from checks.sos_name.matched_name")

    if checks.dba is not None and app.dba is None:
        problems.append("case.json: checks.dba is given, but the application has no DBA")
    if checks.tin is not None and app.tin_type is None:
        problems.append("case.json: checks.tin is given, but the application has no TIN")

    for flag in sorted(flags & FLAG_NEEDS.keys()):
        check_name, needed = FLAG_NEEDS[flag]
        check = getattr(checks, check_name)
        actual = check.result if check is not None else "missing"
        if actual != needed:
            problems.append(f"case.json: flag {flag} needs checks.{check_name} = {needed}, but it is {actual}")

    ec = golden.folder.parent.name
    covered = {name for name, members in EC_FLAGS.items() if members & flags}
    if ec in EC_FLAGS and ec not in covered:
        problems.append(f"case.json: a case in {ec}/ needs one of the flags {sorted(EC_FLAGS[ec])}")
    elif ec == "combo" and len(covered) < 2:
        problems.append("case.json: a case in combo/ needs flags from at least two edge cases; "
                        f"it has {sorted(covered) or 'none'}")


def check_across(cases: list[GoldenCase]) -> list[str]:
    """IDs used by more than one case: case IDs, and mock record IDs, which share tables."""
    users: dict[tuple[str, str], list[str]] = defaultdict(list)
    for golden in cases:
        users[("case_id", golden.folder.name)].append(golden.label)
        if golden.world is None:
            continue
        for entity in golden.world.registry:
            users[("registry entity", f"{entity.state}/{entity.state_entity_id}")].append(golden.label)
            for filing in entity.filings:
                users[("filing event_id", filing.event_id)].append(golden.label)
        for dba in golden.world.dba_filings:
            users[("DBA filing_id", dba.filing_id)].append(golden.label)
        for profile in golden.world.commercial:
            users[("commercial profile_id", profile.profile_id)].append(golden.label)
    return [f"{kind} {value} is used by more than one case: {', '.join(labels)}"
            for (kind, value), labels in sorted(users.items()) if len(labels) > 1]


def validate(path: Path) -> tuple[list[GoldenCase], list[str]]:
    """Check every case under path.

    Returns the cases, each with its own problems, and the problems that belong to
    no single case (unknown folders, IDs shared across cases).
    """
    folders, problems = find_case_folders(path)
    cases = [load_case(folder) for folder in folders]
    for golden in cases:
        check_case(golden)
    return cases, problems + check_across(cases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that each golden case's files agree with each other.")
    parser.add_argument("path", nargs="?", type=Path, default=GOLDEN,
                        help="data/golden (the default), one edge case's folder, or one case folder")
    path = parser.parse_args(argv).path
    if not path.is_dir():
        print(f"{path} is not a folder")
        return 2

    cases, problems = validate(path)
    if not cases:
        print(f"No golden cases under {path} yet.")
    for golden in cases:
        if golden.problems:
            print(f"{golden.label}:")
            print("\n".join(f"  - {problem}" for problem in golden.problems))
        else:
            print(f"{golden.label}: ok")
    if problems:
        print("Other problems:")
        print("\n".join(f"  - {problem}" for problem in problems))

    failed = sum(1 for golden in cases if golden.problems)
    summary = f"{len(cases)} case(s) checked: {len(cases) - failed} ok, {failed} with problems"
    if problems:
        summary += f"; {len(problems)} other problem(s)"
    print(f"\n{summary}.")
    return 1 if failed or problems else 0


if __name__ == "__main__":
    sys.exit(main())
