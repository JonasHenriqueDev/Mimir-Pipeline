"""Cross-arm baseline identities and stable per-run occurrence tracking."""

import hashlib
import json
from collections import Counter

from .models import Snapshot


class IssueTracker:
    """Use Sonar's stable key within a run; baseline positions pair independent arms.

    New server keys are conservatively considered new diagnostics. A scanner that
    rekeys unchanged findings may therefore reject a candidate rather than credit
    a spurious repair. The raw key and current source location remain available.
    """

    def __init__(self):
        self.identities: dict[str, tuple[str, str, str]] = {}
        self.baseline_registered = False
        self.sequence = 0

    def register(self, snapshot: Snapshot) -> Snapshot:
        occurrences: Counter = Counter()
        keys = [issue.key for issue in snapshot.issues]
        if len(keys) != len(set(keys)):
            raise ValueError("Snapshot contém chaves de apontamento duplicadas")
        for issue in sorted(
            snapshot.issues,
            key=lambda item: (item.rule, item.path, item.line, item.message, item.anchor, item.key),
        ):
            known = self.identities.get(issue.key)
            if known is not None:
                identity, rule, path = known
                if issue.rule != rule or issue.path != path:
                    raise ValueError("O scanner reutilizou uma chave para outra regra ou arquivo")
                issue.tracking_id = identity
                continue
            signature = (issue.rule, issue.path, issue.line, issue.message, issue.anchor)
            occurrence = occurrences[signature]
            occurrences[signature] += 1
            payload = json.dumps([*signature, occurrence], ensure_ascii=False)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
            if not self.baseline_registered:
                identity = f"baseline:{digest}"
            else:
                self.sequence += 1
                identity = f"new:{self.sequence}:{digest}"
            issue.tracking_id = identity
            self.identities[issue.key] = (identity, issue.rule, issue.path)
        self.baseline_registered = True
        return snapshot
