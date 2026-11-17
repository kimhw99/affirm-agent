# affirm-agent



## Sub-agents
### Input and Output Data Shape for Sub-agents
#### 1. Records worker input
```
{
  "task_id": "t1",
  "worker": "records",
  "question": "Who does the submitted TIN belong to: the business name or the owner? Is 'Sunshine Cafe' registered as an entity or as a DBA in NY or Nassau County?",
  "focus_fields": ["tin_match", "irs_name_control", "legal_name", "dba_name", "entity_status", "kyc_status"],
  "hypotheses": [
    {"hypothesis_id": "h1", "text": "Sole proprietor using a trade name"},
    {"hypothesis_id": "h2", "text": "DBA of a registered LLC"},
    {"hypothesis_id": "h3", "text": "Wrong or fake TIN"}
  ],
  "context": {
    "case_id": "case_0012",
    "ec_flags": ["EC-1", "EC-2", "EC-4"],
    "submitted_legal_name": "Sunshine Cafe",
    "entity_type": "sole_prop",
    "formation_state": "NY",
    "address": "12 Main St, Great Neck, NY 11021",
    "tin_ref": "tin_ref_8841",
    "tin_last4": "4417",
    "owners": [{"owner_id": "o1", "name": "Mark Hemlock", "role": "owner", "kyc_ref": "kyc_77"}],
    "documents": [],
    "website": null
  },
  "prior_findings": [],
  "follow_up_of": null,
  "max_tool_calls": 6
}
```

#### 2. Evidence worker input
```
{
  "task_id": "t2",
  "worker": "evidence",
  "question": "What legal name, owner, EIN and address do the merchant's documents and website show? Do they name 'Sunshine Cafe' as a trade name of Mark Hemlock?",
  "focus_fields": ["legal_name", "dba_name", "owner_name", "ein_masked", "operating_address", "bank_account_holder", "website_legal_name", "document_issue_date"],
  "hypotheses": [
    {"hypothesis_id": "h1", "text": "Sole proprietor using a trade name"},
    {"hypothesis_id": "h2", "text": "DBA of a registered LLC"}
  ],
  "context": {
    "case_id": "case_0012",
    "ec_flags": ["EC-1", "EC-2", "EC-4"],
    "submitted_legal_name": "Sunshine Cafe",
    "entity_type": "sole_prop",
    "formation_state": "NY",
    "address": "12 Main St, Great Neck, NY 11021",
    "tin_ref": "redacted",
    "tin_last4": "4417",
    "owners": [{"owner_id": "o1", "name": "Mark Hemlock", "role": "owner", "kyc_ref": null}],
    "documents": [
      {"doc_id": "d1", "doc_type": "ein_letter_147c", "file_ref": "docs/case_0012_147c.pdf", "uploaded_at": "2026-11-14T10:00:00"},
      {"doc_id": "d2", "doc_type": "dba_certificate", "file_ref": "docs/case_0012_dba.pdf", "uploaded_at": "2026-11-14T10:01:00"},
      {"doc_id": "d3", "doc_type": "voided_check", "file_ref": "docs/case_0012_check.pdf", "uploaded_at": "2026-11-14T10:02:00"}
    ],
    "website": "https://sunshinecafe.example"
  },
  "prior_findings": [],
  "follow_up_of": null,
  "max_tool_calls": 6
}
```

#### 3. Records worker result
```
{
  "task_id": "t2",
  "worker": "evidence",
  "question": "What legal name, owner, EIN and address do the merchant's documents and website show? Do they name 'Sunshine Cafe' as a trade name of Mark Hemlock?",
  "focus_fields": ["legal_name", "dba_name", "owner_name", "ein_masked", "operating_address", "bank_account_holder", "website_legal_name", "document_issue_date"],
  "hypotheses": [
    {"hypothesis_id": "h1", "text": "Sole proprietor using a trade name"},
    {"hypothesis_id": "h2", "text": "DBA of a registered LLC"}
  ],
  "context": {
    "case_id": "case_0012",
    "ec_flags": ["EC-1", "EC-2", "EC-4"],
    "submitted_legal_name": "Sunshine Cafe",
    "entity_type": "sole_prop",
    "formation_state": "NY",
    "address": "12 Main St, Great Neck, NY 11021",
    "tin_ref": "redacted",
    "tin_last4": "4417",
    "owners": [{"owner_id": "o1", "name": "Mark Hemlock", "role": "owner", "kyc_ref": null}],
    "documents": [
      {"doc_id": "d1", "doc_type": "ein_letter_147c", "file_ref": "docs/case_0012_147c.pdf", "uploaded_at": "2026-11-14T10:00:00"},
      {"doc_id": "d2", "doc_type": "dba_certificate", "file_ref": "docs/case_0012_dba.pdf", "uploaded_at": "2026-11-14T10:01:00"},
      {"doc_id": "d3", "doc_type": "voided_check", "file_ref": "docs/case_0012_check.pdf", "uploaded_at": "2026-11-14T10:02:00"}
    ],
    "website": "https://sunshinecafe.example"
  },
  "prior_findings": [],
  "follow_up_of": null,
  "max_tool_calls": 6
}
```

#### 4. Evidence worker input
```
{
  "task_id": "t1", "case_id": "case_0012", "worker": "records",
  "status": "completed",
  "findings": [
    {"finding_id": "f_rec_1", "subject": "business", "field": "tin_match", "value": "3",
     "source_id": "src_irs_1", "extraction_method": "structured_record", "confidence": "high",
     "citation": {"source_id": "src_irs_1", "locator": "tin_match:req_551",
                  "excerpt": "code 3: name/TIN combination does not match ('Sunshine Cafe')"},
     "note": "Checked against the business name"},
    {"finding_id": "f_rec_2", "subject": "business", "field": "tin_match", "value": "6",
     "source_id": "src_irs_2", "extraction_method": "structured_record", "confidence": "high",
     "citation": {"source_id": "src_irs_2", "locator": "tin_match:req_552",
                  "excerpt": "code 6: matched on SSN with name 'Mark Hemlock'"},
     "note": "Checked against the owner's name"},
    {"finding_id": "f_rec_3", "subject": "business", "field": "irs_name_control", "value": "HEML",
     "source_id": "src_irs_2", "extraction_method": "structured_record", "confidence": "high",
     "citation": {"source_id": "src_irs_2", "locator": "tin_match:req_552", "excerpt": "name control HEML"}},
    {"finding_id": "f_rec_4", "subject": "owner:o1", "field": "kyc_status", "value": "verified",
     "source_id": "src_kyc_1", "extraction_method": "structured_record", "confidence": "high",
     "citation": {"source_id": "src_kyc_1", "locator": "kyc_77", "excerpt": "name and address verified"}}
  ],
  "sources": [
    {"source_id": "src_irs_1", "source_type": "irs_tin_match", "authority": "official",
     "locator": "tin_match:req_551", "retrieved_at": "2026-11-17T09:00:00"},
    {"source_id": "src_irs_2", "source_type": "irs_tin_match", "authority": "official",
     "locator": "tin_match:req_552", "retrieved_at": "2026-11-17T09:00:05"},
    {"source_id": "src_kyc_1", "source_type": "kyc_vendor", "authority": "third_party",
     "locator": "kyc_77", "retrieved_at": "2026-11-17T09:00:09"}
  ],
  "hypothesis_signals": [
    {"hypothesis_id": "h1", "direction": "supports", "finding_ids": ["f_rec_2", "f_rec_4"]},
    {"hypothesis_id": "h3", "direction": "contradicts", "finding_ids": ["f_rec_2"]}
  ],
  "not_found": ["legal_name", "dba_name"],
  "unresolved": ["No NY entity or Nassau County DBA found for 'Sunshine Cafe' or for owner Mark Hemlock"],
  "tool_calls_used": 5,
  "error": null
}
```