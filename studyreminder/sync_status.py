"""Read-only views of persisted platform verification times."""
from __future__ import annotations

import json

FIELDS = {"chaoxing": ("chaoxing_homework", "chaoxing_exams"), "xiji": ("xiji",)}


def sync_signature(collection, source=None):
    scope = collection.get("course_scope", {})
    keys = FIELDS[source] if source else tuple(key for values in FIELDS.values() for key in values)
    value = {"course_scope": {key: scope.get(key, []) for key in keys}}
    if source != "xiji":
        value["homework_year"] = collection.get("homework_year")
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def sync_info(store):
    """A login probe, application launch or task edit never produces a time."""
    record = store.get_value("collection_sync", {})
    collection = store.settings()["collection"]
    scope = collection.get("course_scope", {})

    def visible(item, signature):
        basis = item.get("success_scope") if item.get("last_success_at") else item.get("attempt_scope")
        changed = bool((item.get("last_success_at") or item.get("last_attempt_at")) and basis != signature)
        return {"last_attempt_at": item.get("last_attempt_at"), "last_success_at": item.get("last_success_at"),
                "last_result": item.get("last_result", "never"), "scope_changed": changed}

    public = visible(record, sync_signature(collection))
    public["sources"] = {}
    for source, keys in FIELDS.items():
        item = visible(record.get("sources", {}).get(source, {}), sync_signature(collection, source))
        item["configured"] = any(scope.get(key) for key in keys)
        if not item["configured"]:
            item["last_result"] = "disabled"
        public["sources"][source] = item
    return public


def platform_synced_at(store):
    info = sync_info(store)
    return info["last_success_at"] if not info["scope_changed"] else None
