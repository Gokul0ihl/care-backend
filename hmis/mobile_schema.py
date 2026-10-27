"""Publish the mobile wire contract in the shared Care Swagger document.

Mobile APIViews return an envelope rather than Care's Pydantic viewset shapes,
so the default AutoSchema cannot infer their requests/responses. Keep the
explicit contract here and export it to the frontend docs/api/mobile.openapi.json.
"""

import json
from pathlib import Path

MOBILE_PREFIX = "/api/hmis/mobile/api/v1"


def mobile_schema(result, generator, request, public):
    contract = json.loads(Path(__file__).with_name("mobile.openapi.json").read_text())
    # Replace inferred mobile operations, including optional-slash variants.
    documented_paths = {f"{MOBILE_PREFIX}{path}" for path in contract["paths"]}
    for path in list(result["paths"]):
        if path.rstrip("/") in documented_paths:
            del result["paths"][path]
    for path, operations in contract["paths"].items():
        result["paths"][f"{MOBILE_PREFIX}{path}"] = operations
    for group, components in contract["components"].items():
        result.setdefault("components", {}).setdefault(group, {}).update(components)
    tags = result.setdefault("tags", [])
    existing = {tag["name"] for tag in tags}
    tags.extend(tag for tag in contract["tags"] if tag["name"] not in existing)
    return result
