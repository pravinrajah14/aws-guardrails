"""The checker's planted errors, adapted to the current schema as changes to a policy's forms."""

import copy

WIDER_WINDOW = "24h"
NARROWER_WINDOW = "1h"
CHECKER = {
    "wrong_window": ["P1_window", "P3_window"],
    "missing_amount_check": [],
    "any_approver": ["P1_person"],
    "boundary": ["P1_boundary"],
    "approval_asked_not_granted": ["P1_attempt", "P3_attempt"],
    "other_order": ["P1_other_refund", "P3_other_refund"],
}


def _requires(forms):
    return next((f["requires"] for f in forms if f["kind"] == "requires_prior"), None)


def _wrong_window(forms):
    r = _requires(forms)
    if r:
        r["within"] = NARROWER_WINDOW if r["within"] == WIDER_WINDOW else WIDER_WINDOW
    return bool(r)


def _missing_amount_check(forms):
    r = _requires(forms)
    if not r or "at_most" not in r:
        return False
    del r["at_most"]
    r.pop("spent_so_far", None)
    return True


def _any_approver(forms):
    r = _requires(forms)
    if not r or "approver_role" not in r:
        return False
    del r["approver_role"]
    return True


def _boundary(forms):
    limits = [f for f in forms if f["kind"] == "limit"]
    for f in limits:
        f["op"] = ">" if f.get("op", ">") == ">=" else ">="
    return bool(limits)


def _asked_not_granted(forms):
    r = _requires(forms)
    if not r:
        return False
    r["event"] = "request"
    for key in ("output", "at_most", "spent_so_far", "approver_role"):
        r.pop(key, None)
    return True


def _other_order(forms):
    """With the amount and role checks in place the engine still pins the order, so those go too."""
    r = _requires(forms)
    if not r:
        return False
    if "version" in r["same"]:
        r["same"] = ["service"]
        return True
    r["same"] = []
    for key in ("at_most", "spent_so_far", "approver_role"):
        r.pop(key, None)
    return True


BUGS = {
    "wrong_window": ("Wrong time window", _wrong_window),
    "missing_amount_check": ("Missing amount check", _missing_amount_check),
    "any_approver": ("Any approver accepted", _any_approver),
    "boundary": ("> vs >=", _boundary),
    "approval_asked_not_granted": ("Approval asked for, not granted", _asked_not_granted),
    "other_order": ("Approval for a different item", _other_order),
}
DEMO_BUGS = ["wrong_window", "missing_amount_check", "any_approver", "boundary"]


def plant(forms, bug):
    """A broken copy of the forms, or None when this bug doesn't apply to this kind of rule."""
    broken = copy.deepcopy(forms)
    return broken if BUGS[bug][1](broken) else None


def applicable(forms):
    return [{"id": b, "label": BUGS[b][0]} for b in DEMO_BUGS if plant(forms, b) is not None]
