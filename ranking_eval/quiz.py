"""The rule quiz: every case through the tool under each ranking, graded on five checks, in two modes.

  uv run python -m ranking_eval.quiz --smoke                        a few cases per ranking, to prove the plumbing
  uv run python -m ranking_eval.quiz --pass evidence                pass 1: every ranking in the evidence score
  uv run python -m ranking_eval.quiz --pass example --evidence bm25 pass 2: the example score, best evidence fixed
  uv run python -m ranking_eval.quiz --final                        also run the hidden third (final comparison only)

Modes:
  isolated   tests the ranking alone: the case's own policy is left out of the library, the glossary's
             gap fills are off and there are no saved answers, so past rules are the only possible source.
  realistic  everything on, as the tool is used (saved answers come from saved_answers.json, not your own file).

Checks (see the proposal): 1 understood, 2 works, 3 right source, 4 right suggestion, 5 asks only when needed.
Model answers are cached by prompt, so rankings that only change the evidence score share one model call per case.
"""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from app.core import answers, config, edge_examples, examples, generate, glossary, saved_cases
from app.core.model import ModelError, OllamaModel
from app.core.resolve import LIMIT_SCOPE, AnswerError, resolve
from app.core.status import decide
from ranking_eval import keys, replay, report
from ranking_eval.rankings import BY_NAME, CANDIDATES, REFERENCES, Pair

HERE = Path(__file__).parent
SAVED_ANSWERS = HERE / "saved_answers.json"
MODEL_CACHE = HERE / ".cache" / "model.jsonl"
RESULTS = config.REPO / "results" / "ranking-eval"
BASELINE = "jaccard+jaccard"
CORRECT, CORRECT_NONE, WRONG, ABSENT, NOT_ASKED = "correct", "correct (none)", "wrong", "absent", "not asked"


class CachingModel:
    """The tool's model, with answers kept by prompt. Temperature 0 and a fixed seed make a cached answer the answer."""

    def __init__(self, inner, path=MODEL_CACHE):
        self.inner, self.path = inner, Path(path)
        self.cache = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.cache[row["key"]] = row["answer"]
        self.hits = self.misses = 0
        self.seconds = 0.0
        self.last_seconds, self.last_timing = None, None

    def info(self):
        return self.inner.info()

    def _key(self, prompt):
        body = json.dumps([self.inner.info(), prompt["system"], prompt["user"], prompt["format"]], sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    def complete(self, prompt):
        key = self._key(prompt)
        if key in self.cache:
            self.hits += 1
            self.last_seconds, self.last_timing = 0.0, None
            return deepcopy(self.cache[key])
        answer = self.inner.complete(prompt)
        self.misses += 1
        self.last_seconds = getattr(self.inner, "last_seconds", None)
        self.last_timing = getattr(self.inner, "last_timing", None)
        self.seconds += self.last_seconds or 0.0
        self.cache[key] = deepcopy(answer)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "answer": answer}) + "\n")
        return answer


@contextmanager
def environment(case, mode, workdir):
    """The tool's sources as the mode wants them, put back afterwards. Nothing touches your own saved answers."""
    workdir = Path(workdir)
    library = examples.LIBRARY
    if mode == "isolated":
        library = [e for e in library if e["id"] not in set(case.get("leave_out", []))]
    gloss = deepcopy(glossary.GLOSSARY)
    if mode == "isolated":
        gloss["gaps"] = []
    confirmed = workdir / "confirmed"
    confirmed.mkdir(parents=True, exist_ok=True)
    patches = [
        mock.patch.object(config, "ANSWERS_PATH", workdir / "answers.sqlite"),
        mock.patch.object(examples, "LIBRARY", library),
        mock.patch.object(glossary, "GLOSSARY", gloss),
        mock.patch.object(generate, "POLICIES_ROOT", workdir / "policies"),
        mock.patch.object(saved_cases, "CASES_DIR", confirmed if mode == "isolated" else saved_cases.CASES_DIR),
    ]
    for p in patches:
        p.start()
    try:
        if mode == "realistic" and SAVED_ANSWERS.exists():
            for row in json.loads(SAVED_ANSWERS.read_text(encoding="utf-8"))["answers"]:
                answers.save(row["rule_type"], row["field"], row["term"], row["value"], row["sentence"])
        yield library
    finally:
        for p in reversed(patches):
            p.stop()


# ---------------------------------------------------------------- reading what the tool did


def actual_source(u, field):
    if field == LIMIT_SCOPE:
        how = (u.get("limit_scope") or {}).get("how")
        return {"saved_answer": keys.SAVED, "glossary": keys.GLOSSARY, "asked": keys.ASK}.get(how, keys.UNFILLED)
    slot = u.get("slots", {}).get(field)
    if not slot:
        return keys.UNFILLED
    src = slot["source"]
    if src == "stated" or (src == "glossary" and slot.get("value_at")):
        return keys.SENTENCE
    return {"glossary": keys.GLOSSARY, "saved_answer": keys.SAVED, "asked": keys.ASK}.get(src, keys.UNFILLED)


def actual_value(u, field):
    slot = u.get("slots", {}).get(field)
    if not slot or slot["source"] == "missing" or slot["value"] in (None, []):
        return None
    return slot["value"]


def form_kinds(u):
    from app.core import rule_types

    if not u.get("rule_type") or u["rule_type"] not in rule_types.BY_ID:
        return None
    return sorted({f["kind"] for f in rule_types.BY_ID[u["rule_type"]]["forms"]})


def _ids(items):
    return [e["id"] for e in items]


# ---------------------------------------------------------------- one case


def _policy_stage(u, case, workdir):
    """Generate, validate and replay the tool's policy, and compare it with the key's reference policy."""
    out = {"generated": False, "valid": None, "edge_examples_agree": None, "reference_rows": [], "status": None}
    problems = generate.readiness_problems(u)
    try:
        forms = generate.build_forms(u)
        policy_dir = generate.new_policy_dir()
        policy_path = generate.write_policy(forms, policy_dir)
    except (
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        OSError,
    ) as e:  # a rule the converter can't build is a failure of check 2, not of the quiz
        out["error"] = f"could not build a policy: {e}"[:300]
        out["status"] = decide(u, problems)["status"]
        return out
    out["generated"] = True
    out["forms"] = forms
    validation = generate.validate(policy_path, policy_dir)
    out["valid"] = validation[0]
    if not validation[0]:
        out["status"] = decide(u, validation=validation)["status"]
        out["error"] = validation[1][:300]
        return out
    gate = generate.gate_for(forms, policy_dir, policy_path)
    try:
        edges = edge_examples.run_all(u, gate)
        out["edge_examples_agree"] = all(e["agrees"] for e in edges)
        out["status"] = decide(u, problems, validation, edges)["status"]
    except (KeyError, ValueError, RuntimeError) as e:
        # The tool's own edge examples can't be built for this rule (e.g. an approval with no role, which the
        # glossary always fills in the app). That is a check 2 failure, recorded with its reason.
        out["edge_examples_agree"] = False
        out["edge_error"] = f"{type(e).__name__}: {e}"[:200]
        out["status"] = decide(u, problems, validation)["status"]
    ref = case.get("reference")
    if ref:
        reference = replay.Policy(
            forms=[{**f, "id": f"ref_{i}", "sentence": case["sentence"]} for i, f in enumerate(ref["forms"])]
            if "forms" in ref
            else None,
            dogwood=ref.get("dogwood"),
            workdir=workdir / "reference",
        )
        candidate = replay.Policy(forms=forms, workdir=workdir / "candidate")
        action = case["expect"].get("action")
        out["reference_rows"] = replay.compare(reference, candidate, replay.histories(action))
    return out


def run_case(case, mode, pair, model, workdir=None):
    """Everything the checks need about one case under one ranking pair and mode."""
    workdir = Path(workdir or tempfile.mkdtemp(prefix="quiz-"))
    record = {"case": case["id"], "kind": case["kind"], "mode": mode, "ranking": pair.name, "hidden": case["hidden"]}
    start = time.perf_counter()
    with environment(case, mode, workdir) as library, examples.use_ranking(pair):
        record["library"] = _ids(library)
        for ranking in {id(pair.example): pair.example, id(pair.evidence): pair.evidence}.values():
            ranking.set_case(case, library, mode)
        try:
            first = resolve(case["sentence"], model)
        except ModelError as e:
            record["error"] = f"model: {e}"
            record["seconds"] = round(time.perf_counter() - start, 2)
            return record
        u1 = first["understanding"]
        record["understood"] = {
            "verdict": u1["verdict"],
            "rule_type": u1["rule_type"],
            "action": u1["action"],
            "form_kinds": form_kinds(u1),
            "values": {f: actual_value(u1, f) for f in case["values"]},
            "sources": {f: actual_source(u1, f) for f in case["values"]},
        }
        record["examples_shown"] = _ids(u1.get("examples_used", []))
        record["questions"] = [
            {"field": q["field"], "term": q["term"], "proposal": q["proposal"], "evidence": _ids(q["evidence"])}
            for q in first["questions"]
        ]
        given = {
            q["field"]: keys.raw_answer(q["field"], case["values"][q["field"]]["value"])
            for q in first["questions"]
            if q["field"] in case["values"]
        }
        final = first
        if given and len(given) == len(first["questions"]):
            try:
                final = resolve(case["sentence"], model, given)
            except (AnswerError, ModelError) as e:
                record["answer_error"] = str(e)
        u2 = final["understanding"]
        record["final"] = {
            "verdict": u2["verdict"],
            "values": {f: actual_value(u2, f) for f in case["values"]},
            "sources": {f: actual_source(u2, f) for f in case["values"]},
            "open_questions": [q["field"] for q in final["questions"]],
            "conflicts": len(u2.get("conflicts", [])),
        }
        ready_to_build = (
            u2["verdict"] != "invalid" and u2.get("rule_type") and not final["questions"] and not u2.get("conflicts")
        )
        record["policy"] = _policy_stage(u2, case, workdir) if ready_to_build else {"generated": False}
        if not ready_to_build:
            record["policy"]["status"] = decide(u2)["status"]
    record["seconds"] = round(time.perf_counter() - start, 2)
    return record


# ---------------------------------------------------------------- grading


def grade(case, record):
    """The five checks for one record. Each is True, False or None (does not apply)."""
    mode = record["mode"]
    if "error" in record:
        return {"error": record["error"]}
    u, fin, pol = record["understood"], record["final"], record["policy"]
    expect = case["expect"]
    g = {}

    # 1 understood: the action, the form kind and every stated value. The rule type is recorded, not required.
    if expect.get("refuse"):
        g["understood"] = u["verdict"] == "invalid"
        g["understood_detail"] = "refused" if g["understood"] else f"read as {u['rule_type']}"
    else:
        misses = []
        if u["verdict"] == "invalid":
            misses.append("refused")
        if u["action"] != expect["action"]:
            misses.append(f"action {u['action']}")
        if expect.get("kinds") is not None and u["form_kinds"] != sorted(expect["kinds"]):
            misses.append(f"kinds {u['form_kinds']}")
        for f, vk in case["values"].items():
            if keys.source(vk, mode) == keys.SENTENCE and not keys.same_value(f, u["values"].get(f), vk["value"]):
                misses.append(f"{f}={u['values'].get(f)!r}")
        g["understood"] = not misses
        g["understood_detail"] = ", ".join(misses)
    g["rule_type"] = u["rule_type"]

    # 2 works: validates, edge examples agree, and the reference histories give the reference's decisions.
    if case.get("reference") is None:
        g["works"] = None
    else:
        rows = pol.get("reference_rows", [])
        wrong_allows = sum(r["result"] == "wrong allow" for r in rows)
        wrong_blocks = sum(r["result"] == "wrong block" for r in rows)
        errors = sum(r["result"] == "error" for r in rows)
        g["works"] = bool(
            pol.get("generated")
            and pol.get("valid")
            and pol.get("edge_examples_agree")
            and rows
            and wrong_allows == wrong_blocks == errors == 0
        )
        g["wrong_allows"], g["wrong_blocks"], g["decisions"] = wrong_allows, wrong_blocks, len(rows)
        if not pol.get("generated"):
            g["works_detail"] = pol.get("error") or "no policy (questions left open or refused)"
        elif not pol.get("valid"):
            g["works_detail"] = "dogwood validate failed"
        elif pol.get("edge_error"):
            g["works_detail"] = f"the tool's edge examples could not be built ({pol['edge_error']})"
        elif not pol.get("edge_examples_agree"):
            g["works_detail"] = "edge examples disagree with the summary"
        else:
            g["works_detail"] = f"{wrong_allows} wrong allows, {wrong_blocks} wrong blocks of {len(rows)}"

    # 3 right source: each value came from where the key expects.
    if not case["values"]:
        g["right_source"] = None
    else:
        wrong = {
            f: fin["sources"].get(f)
            for f, vk in case["values"].items()
            if fin["sources"].get(f) != keys.source(vk, mode)
        }
        g["right_source"] = not wrong
        g["source_detail"] = ", ".join(f"{f} from {s}" for f, s in wrong.items())

    # 4 right suggestion: for each question the key needs, the suggestion was correct, wrong or absent.
    asked = {q["field"]: q for q in record["questions"]}
    needed = keys.needed_questions(case, mode)
    suggestions = {}
    for f in needed:
        want = keys.suggestion(case["values"][f], mode)
        q = asked.get(f)
        if q is None:
            suggestions[f] = NOT_ASKED
        elif want is None:
            suggestions[f] = CORRECT_NONE if q["proposal"] is None else WRONG
        elif q["proposal"] is None:
            suggestions[f] = ABSENT
        else:
            suggestions[f] = CORRECT if keys.same_value(f, q["proposal"], want) else WRONG
    for f, q in asked.items():  # a suggestion on a question nobody needed is a wrong suggestion too
        if f not in needed and q["proposal"] is not None:
            suggestions[f or "(reword)"] = WRONG
    g["suggestions"] = suggestions

    # 5 asks only when needed.
    asked_fields = [q["field"] or "(reword)" for q in record["questions"]]
    g["needed"], g["asked"] = needed, asked_fields
    g["asks_right"] = sorted(set(asked_fields)) == needed and len(asked_fields) == len(set(asked_fields))

    # Recall@3, by sentence (a converter form and its Dogwood twin are one sentence).
    by_id = {e["id"]: e["sentence"] for e in examples.LIBRARY}
    present = set(record["library"])
    rel_ex = {by_id[i] for i in case.get("relevant", []) if i in present}
    shown = {by_id[i] for i in record["examples_shown"] if i in by_id}
    g["recall_example"] = len(shown & rel_ex) / len(rel_ex) if rel_ex else None
    present_library = [e for e in examples.LIBRARY if e["id"] in present]
    recalls = []
    found_any = {}
    for f in needed:
        rel = {by_id[i] for i in keys.evidence_relevant(case, f, present_library, mode)}
        q = asked.get(f)
        if rel and q is not None:
            got = {by_id[i] for i in q["evidence"] if i in by_id}
            recalls.append(len(got & rel) / len(rel))
            found_any[f] = bool(got & rel)
    g["recall_evidence"] = sum(recalls) / len(recalls) if recalls else None
    g["evidence_found"] = found_any

    # Wrong values in a policy that reached Ready.
    g["status"] = pol.get("status")
    g["wrong_ready"] = bool(
        pol.get("status") == "ready"
        and any(
            not keys.same_value(f, fin["values"].get(f), vk["value"])
            for f, vk in case["values"].items()
            if keys.source(vk, mode) != keys.UNFILLED
        )
    )
    return g


# ---------------------------------------------------------------- runs


def pairs_for(args):
    def names(text, default):
        if text == "all":
            return [r.name for r in default]
        return [n.strip() for n in text.split(",") if n.strip()]

    if args.pass_ == "example":
        example = [r.name for r in CANDIDATES if "example" in r.slots] + [r.name for r in REFERENCES]
        evidence = names(args.evidence or "jaccard", CANDIDATES)
    else:
        example = names(args.example or "jaccard", CANDIDATES)
        evidence = names(args.evidence or "all", CANDIDATES + REFERENCES)
    out = [("jaccard", "jaccard")]
    for ex in example:
        for ev in evidence:
            if (ex, ev) not in out:
                out.append((ex, ev))
    for ex, ev in out:
        for n in (ex, ev):
            if n not in BY_NAME:
                sys.exit(f"unknown ranking {n!r}; known: {', '.join(BY_NAME)}")
        if "example" not in BY_NAME[ex].slots:
            sys.exit(f"{ex} can't fill the example score (it needs the model's reading first)")
    return out


def select_cases(all_cases, args):
    chosen = [c for c in all_cases if args.final or not c["hidden"]]
    if args.cases:
        wanted = set(args.cases.split(","))
        chosen = [c for c in all_cases if c["id"] in wanted]
    elif args.smoke:
        picked = []
        for kind in ("masked", "paraphrase", "near_miss", "should_ask"):
            picked += [c for c in chosen if c["kind"] == kind][: args.smoke]
        chosen = picked
    return chosen


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pass", dest="pass_", choices=["evidence", "example"], default="evidence")
    ap.add_argument("--example", help="rankings for the example score, comma-separated or 'all' (default jaccard)")
    ap.add_argument("--evidence", help="rankings for the evidence score, comma-separated or 'all' (default all)")
    ap.add_argument("--modes", default="isolated,realistic")
    ap.add_argument("--cases", help="only these case ids, comma-separated")
    ap.add_argument("--smoke", type=int, nargs="?", const=1, default=0, help="only N cases of each kind (default 1)")
    ap.add_argument("--final", action="store_true", help="include the hidden third (final comparison only)")
    ap.add_argument("--out", help="results folder (default results/ranking-eval/<time>)")
    ap.add_argument("--keep-policies", action="store_true", help="keep each case's generated policies")
    a = ap.parse_args(argv)

    all_cases = keys.load()
    cases = select_cases(all_cases, a)
    modes = [m.strip() for m in a.modes.split(",")]
    pairs = pairs_for(a)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) if a.out else RESULTS / stamp
    out.mkdir(parents=True, exist_ok=True)
    scratch = config.REPO / "pipeline-runs" / "ranking-eval" / stamp
    model = CachingModel(OllamaModel())
    print(f"{len(cases)} cases x {len(pairs)} ranking pairs x {len(modes)} modes -> {out}", flush=True)

    runs = []
    with (out / "records.jsonl").open("w", encoding="utf-8") as f:
        for ex_name, ev_name in pairs:
            example_r = BY_NAME[ex_name]()
            evidence_r = example_r if ev_name == ex_name else BY_NAME[ev_name]()
            pair = Pair(example_r, evidence_r)
            for mode in modes:
                hits, misses, model_seconds = model.hits, model.misses, model.seconds
                example_r.seconds = evidence_r.seconds = 0.0
                start = time.perf_counter()
                for case in cases:
                    workdir = scratch / pair.name / mode / case["id"]
                    try:
                        record = run_case(case, mode, pair, model, workdir)
                        record["grade"] = grade(case, record)
                    except Exception as e:  # noqa: BLE001 - one broken case must not end a long run; it is reported
                        record = {
                            "case": case["id"],
                            "kind": case["kind"],
                            "mode": mode,
                            "ranking": pair.name,
                            "hidden": case["hidden"],
                            "error": f"{type(e).__name__}: {e}",
                        }
                        record["grade"] = {"error": record["error"]}
                    f.write(json.dumps(record, default=str) + "\n")
                    f.flush()
                    print(f"  {pair.name:<24} {mode:<9} {case['id']:<38} {report.one_line(record)}", flush=True)
                runs.append(
                    {
                        "ranking": pair.name,
                        "example": ex_name,
                        "evidence": ev_name,
                        "mode": mode,
                        "cases": len(cases),
                        "seconds": round(time.perf_counter() - start, 1),
                        "ranking_seconds": round(
                            example_r.seconds + (0 if evidence_r is example_r else evidence_r.seconds), 2
                        ),
                        "model_calls": model.misses - misses,
                        "model_cached": model.hits - hits,
                        "model_seconds": round(model.seconds - model_seconds, 1),
                    }
                )
    if not a.keep_policies:
        shutil.rmtree(scratch, ignore_errors=True)
    meta = {
        "stamp": stamp,
        "model": OllamaModel().info(),
        "embedding_model": BY_NAME["embeddings"].MODEL,
        "cases_file": str(keys.CASES_PATH.relative_to(config.REPO)),
        "final": a.final,
        "modes": modes,
        "runs": runs,
        "argv": argv if argv is not None else sys.argv[1:],
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    report.write(out, all_cases)
    print(f"\nSaved {out / 'leaderboard.md'}")


if __name__ == "__main__":
    main()
