"""CSV contact import: parse, normalise numbers, dedup, and map columns to
the agent's prompt variables.

The column mapping is the point of this module. A prompt written as "Customer
name: $customer_name ... Shortfall: $shortfall_amount" already declares exactly
what each call needs (see `prompts.declared_variables`), and the customer
already has those values in a spreadsheet. Import is therefore mostly the act
of pointing one at the other.

Numbers go through `phonenumbers` rather than a hand-rolled +91 rule because a
malformed row here is not a validation nicety — it is a real dial attempt that
costs money and rings a stranger.
"""

import csv
import io
import json
import uuid
from typing import Any

import phonenumbers

# Numbers in these lists are Indian and are usually written bare ("9876543210")
# or with a domestic trunk prefix ("09876543210"), neither of which parses
# without a default region to interpret them against.
DEFAULT_REGION = "IN"


def normalise_phone(raw: str, region: str = DEFAULT_REGION) -> str | None:
    """E.164, or None when the value is not a dialable number."""
    try:
        parsed = phonenumbers.parse((raw or "").strip(), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def parse_csv(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Headers and rows. utf-8-sig because Excel writes a BOM that would
    otherwise become part of the first column's name."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    headers = list(reader.fieldnames or [])
    return headers, [dict(row) for row in reader]


def suggest_mapping(headers: list[str], variables: list[str]) -> dict[str, str]:
    """Best guess at variable -> column, so the UI opens with the obvious
    answer already filled in. Matches on name ignoring case, spaces and
    underscores: "Customer Name" and "customer_name" are the same thing.
    """
    def key(text: str) -> str:
        return "".join(ch for ch in text.lower() if ch.isalnum())

    by_key = {key(h): h for h in headers}
    return {v: by_key[key(v)] for v in variables if key(v) in by_key}


def guess_phone_column(headers: list[str]) -> str:
    """The column most likely to hold the number, for the same reason."""
    for candidate in ("phone", "mobile", "number", "contact", "msisdn", "phonenumber"):
        for header in headers:
            if candidate in header.lower().replace("_", "").replace(" ", ""):
                return header
    return headers[0] if headers else ""


def build_contacts(
    rows: list[dict[str, str]],
    phone_column: str,
    name_column: str = "",
    variable_map: dict[str, str] | None = None,
    region: str = DEFAULT_REGION,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn raw CSV rows into contacts, reporting what could not be used.

    Rejects are returned rather than silently dropped: a customer who uploads
    500 rows and gets 480 contacts needs to see which 20 failed and why.
    Duplicates are resolved here as well as by the unique index, so the count
    shown back to the user matches what was actually stored.
    """
    variable_map = variable_map or {}
    contacts: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    seen: set[str] = set()

    for line, row in enumerate(rows, start=2):  # start=2: line 1 is the header
        phone = normalise_phone(row.get(phone_column, ""), region)
        if phone is None:
            rejects.append({
                "line": line,
                "value": row.get(phone_column, ""),
                "reason": "not a valid phone number",
            })
            continue
        if phone in seen:
            rejects.append({"line": line, "value": phone, "reason": "duplicate"})
            continue
        seen.add(phone)
        contacts.append({
            "phone_e164": phone,
            "name": (row.get(name_column) or "").strip() if name_column else "",
            "variables": {
                var: (row.get(column) or "").strip()
                for var, column in variable_map.items()
            },
        })
    return contacts, rejects


def to_rows(contacts: list[dict[str, Any]], list_id: str, org_id: str) -> list[tuple]:
    """Contacts as insertable tuples, in `db.insert_contacts` column order."""
    return [
        (
            uuid.uuid4().hex[:16],
            list_id,
            org_id,
            c["phone_e164"],
            c["name"],
            json.dumps(c["variables"], ensure_ascii=False),
        )
        for c in contacts
    ]
