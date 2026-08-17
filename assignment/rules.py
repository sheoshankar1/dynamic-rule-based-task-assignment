"""The rule engine (README section 3).

Two evaluators over the same canonical JSON, because the problem runs in two
directions with opposite shapes:

    one rule  -> 100k users   : a database scan   -> to_sql()
    one user  -> ~1k rules    : an in-memory loop -> matches()

They must agree. R-5 is the property test that asserts they do.

A rule is a flat AND of at most three stable predicates plus one volatile cap.
Deliberately not a DSL: the attribute set is closed, and arbitrary expressions
would have unbounded cardinality, which breaks the deduplication the whole
design rests on.
"""

import hashlib
import json

# Stable predicates are fingerprinted and materialised.
STABLE_KEYS = ("department", "experience_years", "location")
# Volatile: stored beside the rule, applied at query time, never fingerprinted.
VOLATILE_KEYS = ("max_active_tasks",)

# Single-valued. A rule names one department and at most one location;
# there is no OR across departments or cities.
_SCALAR_KEYS = ("department", "location")
_RANGE_KEYS = ("experience_years",)
_RANGE_OPS = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}


class InvalidRule(ValueError):
    """Raised on a malformed rule. Rules arrive from the API, so this is a
    trust boundary and gets explicit validation rather than a KeyError."""


def split(raw):
    """Split an API-shaped rule into (stable_predicates, volatile_dict)."""
    if not isinstance(raw, dict):
        raise InvalidRule("rule must be an object")
    unknown = set(raw) - set(STABLE_KEYS) - set(VOLATILE_KEYS)
    if unknown:
        raise InvalidRule(f"unknown predicate(s): {sorted(unknown)}")

    volatile = {}
    cap = raw.get("max_active_tasks")
    if cap is not None:
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            raise InvalidRule("max_active_tasks must be a positive integer")
        volatile["max_active_tasks"] = cap

    return canonicalize({k: v for k, v in raw.items() if k in STABLE_KEYS}), volatile


def canonicalize(predicates):
    """Normalise so that logically identical rules hash identically.

    Sorts keys, flattens single-element lists to scalars, drops empty and null
    predicates, and coerces range bounds to int. Without this,
    {"department": "Finance"} and {"department": "Finance", "location": ""}
    would produce different fingerprints and deduplication would silently
    degrade toward per-task materialisation -- with nothing failing loudly.
    """
    out = {}
    for key in STABLE_KEYS:
        if key not in predicates:
            continue
        value = predicates[key]
        if value is None:
            continue

        if key in _SCALAR_KEYS:
            # A one-element list is accepted and flattened, because that is what
            # earlier clients sent. More than one is rejected rather than
            # silently truncated -- a rule that quietly dropped a department
            # would route work to the wrong team with nothing to show for it.
            if isinstance(value, (list, tuple)):
                # Dedupe first: ["HR", "HR"] names one department and is fine.
                items = sorted({str(v).strip() for v in value if str(v).strip()})
                if len(items) > 1:
                    raise InvalidRule(
                        f"{key} takes a single value, got {len(items)}: "
                        f"{items}. Multi-{key} rules are not supported."
                    )
                value = items[0] if items else ""
            if not isinstance(value, str):
                raise InvalidRule(f"{key} must be a string")
            value = value.strip()
            if value:
                out[key] = value

        elif key in _RANGE_KEYS:
            if isinstance(value, int) and not isinstance(value, bool):
                value = {"gte": value}
            if not isinstance(value, dict):
                raise InvalidRule(f"{key} must be an object or an integer")
            bounds = {}
            for op, bound in value.items():
                if op not in _RANGE_OPS:
                    raise InvalidRule(f"unknown operator {op!r} on {key}")
                if not isinstance(bound, int) or isinstance(bound, bool):
                    raise InvalidRule(f"{key}.{op} must be an integer")
                bounds[op] = bound
            if bounds:
                out[key] = {op: bounds[op] for op in sorted(bounds)}
    return out


def fingerprint(canonical_predicates):
    """sha256 over the canonical form. Content addressing means two tasks with
    the same rule share one materialised eligible set."""
    blob = json.dumps(canonical_predicates, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def to_sql(predicates, alias="u"):
    """Rule -> (where_sql, params) against the users table.

    Used to materialise one rule over every user. Always parameterised: rule
    content originates from an API caller.
    """
    clauses, params = [], []
    for key, value in canonicalize(predicates).items():
        col = f'{alias}."{key}"'
        if key in _SCALAR_KEYS:
            clauses.append(f"{col} = %s")
            params.append(value)
        else:
            for op in sorted(value):
                clauses.append(f"{col} {_RANGE_OPS[op]} %s")
                params.append(value[op])
    return (" AND ".join(clauses) if clauses else "TRUE"), params


def matches(predicates, user):
    """Rule -> bool for one user. Used to test one user against every cached
    rule when their stable attributes change.

    `user` may be a User instance or a plain dict.
    """
    get = user.get if isinstance(user, dict) else lambda k: getattr(user, k)
    for key, value in canonicalize(predicates).items():
        actual = get(key)
        if key in _SCALAR_KEYS:
            if str(actual) != value:
                return False
        else:
            if actual is None:
                return False
            for op, bound in value.items():
                if op == "gte" and not actual >= bound:
                    return False
                if op == "lte" and not actual <= bound:
                    return False
                if op == "gt" and not actual > bound:
                    return False
                if op == "lt" and not actual < bound:
                    return False
    return True
