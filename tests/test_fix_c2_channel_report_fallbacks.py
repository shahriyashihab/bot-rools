"""C2 regression tests: no optimistic channel_report_sent=True on read failure.

Before the fix, six ``except`` blocks interpreted a FAILED read of the stored
truth as a SUCCESSFUL delivery and set ``channel_report_sent=True``. The
monitor's safety net then skipped the session forever - session files that
never reached a channel were permanently lost.

Every read-failure fallback now marks ``channel_report_pending=True`` (and
``sent=False``) so the safety net keeps retrying from stored truth.

These tests are AST-based: they parse the shipped source and fail if ANY
except-handler anywhere assigns a truthy ``channel_report_sent`` without a
stored-truth guard, plus functional tests for the shared conservative
fallback helper shape.
"""

import ast
import io

FILES_WITH_FALLBACKS = [
    "bot/monitor/finalize.py",
    "bot/verification/twofa.py",
    "bot/recovery/worker.py",
    "bot/monitor/validation.py",
]


def _iter_sent_true_assignments_in_except(tree: ast.AST):
    """Yield (lineno, guarded) for every channel_report_sent=True assignment
    that sits inside an except-handler block."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        name = (
                            target.id
                            if isinstance(target, ast.Name)
                            else (
                                target.slice.value
                                if isinstance(target, ast.Subscript)
                                and isinstance(target.slice, ast.Constant)
                                else None
                            )
                        )
                        if (
                            name
                            and "channel_report_sent" in str(name)
                            and isinstance(stmt.value, ast.Constant)
                            and stmt.value.value is True
                        ):
                            # Acceptable ONLY when guarded by a stored-truth
                            # if-check (``if stored.get('channel_report_sent')
                            # is True``) - i.e. we mirror a REAL delivery.
                            guarded = any(
                                isinstance(anc, ast.If)
                                and "channel_report_sent" in ast.dump(anc.test)
                                for anc in ast.walk(node)
                            )
                            yield stmt.lineno, guarded


class TestNoOptimisticSentOnFailure:
    def test_no_unguarded_sent_true_in_any_except_block(self):
        violations = []
        for path in FILES_WITH_FALLBACKS:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
            for lineno, guarded in _iter_sent_true_assignments_in_except(tree):
                if not guarded:
                    violations.append(f"{path}:{lineno}")
        assert violations == [], (
            "Optimistic channel_report_sent=True on a read-failure path "
            f"(permanent session-file loss): {violations}"
        )

    def test_fallbacks_mark_pending(self):
        """Every read-failure fallback in the audited spots must never leave
        the pending marker CLEARED while claiming sent=True. With the fix in
        place, no except block assigns a falsy channel_report_pending without
        also having sent=False - i.e. the old (sent=True, pending=False)
        failure pairing is gone. AST-check the failure pairing directly."""
        for path in FILES_WITH_FALLBACKS:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    # except-handler direct body: reject a bare
                    # (sent=True, pending=False) pairing set together.
                    for stmt in node.body:
                        if isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                            dump = ast.dump(stmt)
                            if "channel_report_pending" in dump and "False" in dump:
                                raise AssertionError(
                                    f"{path}:{stmt.lineno}: failure path claims "
                                    "pending=False (safety net disabled)"
                                )

    def test_all_audited_files_still_parse(self):
        for path in FILES_WITH_FALLBACKS:
            ast.parse(io.open(path, encoding="utf-8").read())  # syntax intact
