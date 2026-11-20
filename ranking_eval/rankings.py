"""The rankings under test, each usable as the example score, the evidence score, or both.

Every ranking has the interface app.core.examples expects (see WordOverlap there):
  example_scores(sentence, candidates)           -> [(score, example)]   which past rules the model sees
  in_evidence_pool(example, query)               -> bool                 which past rules may suggest a value
  evidence_scores(sentence, candidates, query)   -> [(score, example)]   how close each one is; 0 drops it
The query carries the sentence, the rule type the model picked, the open slot and the model's understanding.

Evidence pool: every ranking keeps only past rules with the same action and the same prior action as the
open rule (structure.same_frame), which never looks at a rule type. The one exception is the baseline,
jaccard, which keeps today's pool, the same rule type. jaccard-action is the same word overlap with the
same-action pool, so jaccard vs jaccard-action shows what the pool change alone does, and every other row
differs from jaccard-action only in its score.
"""

import hashlib
import json
import math
import random
import re
import time
import urllib.request
from collections import Counter
from copy import deepcopy
from pathlib import Path

from app.core import config, examples, generate
from app.core.examples import WordOverlap
from ranking_eval import replay, structure

CACHE_DIR = Path(__file__).parent / ".cache"
_WORD = re.compile(r"[a-z]+")


def tokens(text):
    """Words as a list (repeats kept), stemmed and without stop words, the same way word overlap reads them."""
    found = (examples._stem(w) for w in _WORD.findall(text.lower()))
    return [w for w in found if w not in examples._STOP_WORDS]


SAME_TYPE, SAME_ACTION = "same rule type", "same action and prior action"


class Ranking(WordOverlap):
    """A ranking with a name, the slots it can fill, its evidence pool, and a clock for the time it spends."""

    name = "base"
    slots = ("example", "evidence")
    reference = False  # a reference row (oracle, random, none), not a candidate
    pool = SAME_ACTION

    def __init__(self):
        self.seconds = 0.0

    def in_evidence_pool(self, example, query):
        if self.pool == SAME_TYPE:
            return super().in_evidence_pool(example, query)
        return structure.same_frame(query_profile(query), structure.library_profile(example["id"]))

    def set_case(self, case, library, mode):
        """Called before each quiz case; only the oracle needs it."""


class Jaccard(Ranking):
    """Today's ranking, unchanged: word overlap, evidence from the same rule type. The baseline."""

    name = "jaccard"
    pool = SAME_TYPE


class JaccardAction(Ranking):
    """Word overlap with the same-action pool: the pool change on its own.

    Its example score is jaccard's, so it only runs in the evidence score.
    """

    name = "jaccard-action"
    slots = ("evidence",)


class BM25(Ranking):
    """Okapi BM25 over the library's sentences: rare shared words count more, so 'refund' stops dominating."""

    name = "bm25"
    K1, B = 1.5, 0.75

    def _corpus(self, candidates):
        docs = [tokens(e["sentence"]) for e in examples.LIBRARY if e["type"] != examples.BASELINE]
        df = Counter(w for d in docs for w in set(d))
        n = len(docs)
        idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        avg = sum(len(d) for d in docs) / n if n else 1
        return idf, avg

    def score(self, sentence, other, idf, avg):
        query = set(tokens(sentence))
        doc = Counter(tokens(other))
        length = sum(doc.values())
        total = 0.0
        for w in query:
            f = doc.get(w, 0)
            if f:
                total += idf.get(w, 0) * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * length / avg))
        return total

    def example_scores(self, sentence, candidates):
        idf, avg = self._corpus(candidates)
        scored = [(self.score(sentence, e["sentence"], idf, avg), e) for e in candidates]
        return [(s, e) for s, e in scored if s > 0]

    def evidence_scores(self, sentence, candidates, query):
        idf, avg = self._corpus(candidates)
        return [(self.score(sentence, e["sentence"], idf, avg), e) for e in candidates]


def query_profile(query):
    """The open rule's profile: from the model's understanding, else from its rule type alone."""
    return structure.understanding_profile(query.get("understanding")) or structure.type_profile(query["rule_type"])


class Structural(Ranking):
    """Same action, prior action, form kind, matching fields and values set. Ties go to word overlap, a little."""

    name = "structural"
    TIE_BREAK = 0.001

    def example_scores(self, sentence, candidates):
        query = structure.sentence_profile(sentence)
        scored = []
        for e in candidates:
            s = structure.similarity(query, structure.library_profile(e["id"]))
            if s > 0:
                scored.append((s + self.TIE_BREAK * examples.overlap(sentence, e["sentence"])[0], e))
        return scored

    def evidence_scores(self, sentence, candidates, query):
        q = query_profile(query)
        return [
            (
                structure.similarity(q, structure.library_profile(e["id"]))
                + self.TIE_BREAK * examples.overlap(sentence, e["sentence"])[0],
                e,
            )
            for e in candidates
        ]


class Hybrid(BM25):
    """Filter by structure (same action and prior action), then rank by BM25."""

    name = "hybrid"

    def example_scores(self, sentence, candidates):
        query = structure.sentence_profile(sentence)
        if query["action"] is not None:
            kept = [e for e in candidates if structure.library_profile(e["id"])["action"] == query["action"]]
            if query["prior_known"]:
                kept = [e for e in kept if structure.library_profile(e["id"])["prior"] == query["prior"]]
            candidates = kept
        return super().example_scores(sentence, candidates)


class Embeddings(Ranking):
    """Cosine similarity of a local embedding model's vectors, to catch paraphrases with no shared words."""

    name = "embeddings"
    MODEL = "nomic-embed-text"

    def __init__(self, model=MODEL, url=config.OLLAMA_URL):
        super().__init__()
        self.model, self.url = model, url.rstrip("/") + "/api/embed"
        self.path = CACHE_DIR / f"embeddings-{re.sub(r'[^a-z0-9]+', '-', model)}.json"
        self.cache = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.digest = None

    def _embed(self, texts):
        missing = [t for t in texts if t not in self.cache]
        if missing:
            body = json.dumps({"model": self.model, "input": missing}).encode()
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=config.CALL_TIMEOUT_SECONDS) as resp:
                vectors = json.loads(resp.read())["embeddings"]
            self.cache.update(zip(missing, vectors, strict=True))
            CACHE_DIR.mkdir(exist_ok=True)
            self.path.write_text(json.dumps(self.cache), encoding="utf-8")
        return [self.cache[t] for t in texts]

    @staticmethod
    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        return dot / norm if norm else 0.0

    def _scores(self, sentence, candidates):
        # nomic-embed-text expects these task prefixes.
        texts = [f"search_query: {sentence}"] + [f"search_document: {e['sentence']}" for e in candidates]
        query, *docs = self._embed(texts)
        return [(max(self.cosine(query, d), 0.0), e) for d, e in zip(docs, candidates, strict=True)]

    def example_scores(self, sentence, candidates):
        return [(s, e) for s, e in self._scores(sentence, candidates) if s > 0]

    def evidence_scores(self, sentence, candidates, query):
        return self._scores(sentence, candidates) if candidates else []


# A value both policies get for the open slot, so their histories can't depend on it.
PLACEHOLDER = {
    "window": "24h",
    "max_amount": 10_000,
    "approver_role": "manager",
    "services": ["payments", "search", "web", "checkout"],
}
_WITHIN = re.compile(r"\bwithin\s+\d+[smhd]\b")


def _fill_dogwood(text, slot):
    if slot == "window":
        return _WITHIN.sub(f"within {PLACEHOLDER['window']}", text)
    return text


def _fill_form(form, slot):
    form = deepcopy(form)
    if slot == "window" and "requires" in form:
        form["requires"]["within"] = PLACEHOLDER["window"]
    elif slot == "max_amount" and form.get("kind") == "limit":
        form["max"] = PLACEHOLDER["max_amount"]
    elif slot == "services" and form.get("kind") == "allowlist":
        form["allowed"] = PLACEHOLDER["services"]
    elif slot == "approver_role" and "requires" in form:
        form["requires"]["approver_role"] = PLACEHOLDER["approver_role"]
    return form


class Behavioral(Structural):
    """Rerank the structural top few by how alike the two policies decide the same histories.

    Evidence only: it needs the open rule drafted as a policy, which exists only after the model has read it.
    The open slot is set to the same placeholder in both policies and only histories that don't depend on
    how long ago something happened are replayed, so a candidate can't agree with the draft just because
    it was filled with the candidate's own value.
    """

    name = "behavioral"
    slots = ("evidence",)
    TOP = 5

    def __init__(self):
        super().__init__()
        self._policies = {}

    def example_scores(self, sentence, candidates):  # not this ranking's job: today's example score
        return Jaccard().example_scores(sentence, candidates)

    def _draft(self, query):
        u = query.get("understanding")
        if not u or not u.get("rule_type"):
            return None
        u = deepcopy(u)
        slot = query["slot"]
        for name, s in u["slots"].items():
            if name == slot or (s["source"] == "missing" and s["required"]):
                s.update(source="stated", value=PLACEHOLDER.get(name, s["value"]))
        try:
            forms = [_fill_form(f, slot) for f in generate.build_forms(u)]
            return self._policy(("draft", json.dumps(forms, sort_keys=True)), forms=forms)
        except (
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            OSError,
        ):  # a draft that can't be built has no behavior to compare
            return None

    def _policy(self, key, forms=None, dogwood=None):
        digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]
        if digest not in self._policies:
            self._policies[digest] = replay.Policy(forms=forms, dogwood=dogwood)
        return self._policies[digest]

    def _library_policy(self, example, slot):
        if example.get("form"):
            form = _fill_form(example["form"], slot)
            return self._policy(("form", example["id"], slot), forms=[form])
        if example.get("dogwood") and "..." not in example["dogwood"]:
            return self._policy(("dw", example["id"], slot), dogwood=_fill_dogwood(example["dogwood"], slot))
        return None

    def evidence_scores(self, sentence, candidates, query):
        structural = sorted(super().evidence_scores(sentence, candidates, query), key=lambda s: -s[0])
        draft = self._draft(query)
        profile = query_profile(query)
        found = replay.histories(profile["action"] if profile else None, gap_free=True)
        if draft is None or not found:
            return structural
        out = []
        for i, (score, example) in enumerate(structural):
            if i >= self.TOP:
                out.append((0.0, example))
                continue
            theirs = self._library_policy(example, query["slot"])
            agree = replay.agreement(draft, theirs, found) if theirs else None
            out.append(((agree or 0.0) + Structural.TIE_BREAK * score, example))
        return out


class NoExamples(Ranking):
    """Reference: the model sees no past rules and no value is ever suggested."""

    name = "none"
    reference = True
    pool = None

    def in_evidence_pool(self, example, query):
        return False

    def example_scores(self, sentence, candidates):
        return []

    def evidence_scores(self, sentence, candidates, query):
        return []


class RandomRanking(Ranking):
    """Reference: past rules in a random order (seeded by the sentence, so a rerun gives the same order)."""

    name = "random"
    reference = True

    def _scores(self, sentence, candidates):
        rng = random.Random(hashlib.sha256(sentence.encode()).hexdigest())
        return [(rng.random() + 1e-6, e) for e in candidates]

    def example_scores(self, sentence, candidates):
        return self._scores(sentence, candidates)

    def evidence_scores(self, sentence, candidates, query):
        return self._scores(sentence, candidates)


class Oracle(Ranking):
    """Reference: exactly the past rules the answer key marks relevant. The best any ranking could do."""

    name = "oracle"
    reference = True
    pool = "the answer key's relevant rules"

    def __init__(self):
        super().__init__()
        self.relevant_examples, self.relevant_evidence = set(), {}

    def set_case(self, case, library, mode):
        from ranking_eval import keys

        self.relevant_examples = set(case.get("relevant", []))
        self.relevant_evidence = {
            field: set(keys.evidence_relevant(case, field, library, mode)) for field in case["values"]
        }

    def example_scores(self, sentence, candidates):
        return [(1.0, e) for e in candidates if e["id"] in self.relevant_examples]

    def in_evidence_pool(self, example, query):
        return example["id"] in self.relevant_evidence.get(query["slot"], set())

    def evidence_scores(self, sentence, candidates, query):
        return [(1.0, e) for e in candidates]


CANDIDATES = [Jaccard, JaccardAction, BM25, Structural, Hybrid, Embeddings, Behavioral]
REFERENCES = [Oracle, RandomRanking, NoExamples]
BY_NAME = {r.name: r for r in CANDIDATES + REFERENCES}


class Pair(WordOverlap):
    """One ranking for the example score and another for the evidence score, each timed."""

    def __init__(self, example, evidence):
        self.example, self.evidence = example, evidence
        self.name = f"{example.name}+{evidence.name}"

    def _timed(self, ranking, method, *args):
        start = time.perf_counter()
        try:
            return getattr(ranking, method)(*args)
        finally:
            ranking.seconds += time.perf_counter() - start

    def example_scores(self, sentence, candidates):
        return self._timed(self.example, "example_scores", sentence, candidates)

    def in_evidence_pool(self, example, query):
        return self._timed(self.evidence, "in_evidence_pool", example, query)

    def evidence_scores(self, sentence, candidates, query):
        return self._timed(self.evidence, "evidence_scores", sentence, candidates, query)
