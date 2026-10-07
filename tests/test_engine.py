"""Code tests for hygienic macro expansion scenarios.

Run:  python -m unittest discover -s tests -v
"""

import unittest

from app.engine import (
    EXPANSION_LIMIT, MAX_EXPANSION_OUTPUT_NODES, MAX_EXPANSION_STEPS,
    MAX_MACROS, MAX_RULES, review,
)


def norm(src):
    r = review(src)
    assert r.ok, r.error
    return r.normalized


class HygieneTests(unittest.TestCase):
    def test_template_temporary_does_not_capture_call_site_x(self):
        src = """
        (define-syntax with-x
          (syntax-rules ()
            ((with-x body) (let ((x 99)) body))))
        (let ((x 1)) (with-x x))
        """
        r = review(src)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len(r.steps), 1)
        # inner body x resolves to the outer (call-site) binder B1, not B2
        self.assertIn("(let ((x⁽B2⁾ 99)) x⁽B1⁾)", r.normalized)
        self.assertTrue(any(c["name"] == "x" and c["verdict"] == "distinct"
                            for c in r.hygiene_checks))
        check = next(c for c in r.hygiene_checks if c["name"] == "x")
        origins = {b["origin"] for b in check["bindings"]}
        self.assertEqual(origins, {"macro-template", "source"})

    def test_nested_macro_calls_distinct_x_identities(self):
        src = """
        (define-syntax swap-set!
          (syntax-rules ()
            ((swap-set! a b) (let ((x a)) (set! a b) (set! b x)))))
        (define-syntax call-swap
          (syntax-rules ()
            ((call-swap p q) (swap-set! p q))))
        (let ((x 10) (y 20)) (call-swap x y))
        """
        r = review(src)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len(r.steps), 2)
        # call-site x (B1) flows into template and is never confused with
        # the template's own x binder (B3).
        self.assertIn("(let ((x⁽B3⁾ x⁽B1⁾))", r.normalized)
        self.assertIn("(set!⁽", r.normalized)
        # second call was introduced by the first macro's template
        self.assertEqual(r.steps[1].call_origin, "macro-template")
        self.assertEqual(r.steps[0].call_origin, "call-site")

    def test_pattern_variable_keeps_call_site_binding(self):
        src = """
        (define-syntax id-m (syntax-rules () ((id-m v) v)))
        (lambda (x) (id-m x))
        """
        r = review(src)
        self.assertTrue(r.ok, r.error)
        self.assertIn("(x⁽B1⁾)", r.normalized)

    def test_template_free_identifier_not_captured(self):
        # The template refers to a free x that must resolve globally even
        # though the call site sits under a local x binder.
        src = """
        (define-syntax get-x (syntax-rules () ((get-x) x)))
        (lambda (x) (get-x))
        """
        r = review(src)
        self.assertTrue(r.ok, r.error)
        # output x carries the introduction scope, so the local B1 does not bind it
        self.assertIn("x⁽F", r.normalized)
        self.assertTrue(any(c["name"] == "x" for c in r.hygiene_checks))

    def test_stable_new_scope_for_each_expansion(self):
        src = """
        (define-syntax bind-x (syntax-rules () ((bind-x b) (let ((x 0)) b))))
        (bind-x (bind-x 1))
        """
        r = review(src)
        self.assertTrue(r.ok, r.error)
        # two template x binders => distinct tags B1 and B2
        self.assertIn("x⁽B1⁾", r.normalized)
        self.assertIn("x⁽B2⁾", r.normalized)


class LiteralTests(unittest.TestCase):
    def test_literal_matches_by_lexical_binding(self):
        src = """
        (define-syntax my-if
          (syntax-rules (if)
            ((my-if if c t e) (list c t e))
            ((my-if c t e) (list c t e))))
        (my-if if 1 2 3)
        """
        r = review(src)
        self.assertTrue(r.ok, r.error)
        # the 4-arg rule (with literal if) is the one that fires
        self.assertEqual(r.steps[0].rule_index, 1)

    def test_spelling_match_under_local_binder_is_not_literal(self):
        src = """
        (define-syntax my-if
          (syntax-rules (if)
            ((my-if if c t e) (list c t e))
            ((my-if q c t e) (list q c t e))))
        (lambda (if) (my-if if 1 2 3))
        """
        r = review(src)
        self.assertTrue(r.ok, r.error)
        # local `if` does NOT match the literal; second rule fires
        self.assertEqual(r.steps[0].rule_index, 2)

    def test_unbound_literal_rejected_at_definition(self):
        src = """
        (define-syntax bar (syntax-rules (if*) ((bar if* e) e)))
        (bar 1 2)
        """
        r = review(src)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "unbound-literal")
        self.assertGreaterEqual(r.error["line"], 1)
        self.assertTrue(r.error["evidence"].startswith("EV-"))


class EllipsisTests(unittest.TestCase):
    def test_one_level_repetition_expands(self):
        src = """
        (define-syntax list-of
          (syntax-rules () ((list-of e ...) (list e ...))))
        (list-of 1 2 3)
        """
        self.assertIn("(list⁽", norm(src))
        self.assertIn("1 2 3", norm(src))

    def test_repetition_mismatch_locates_template(self):
        src = """
        (define-syntax pair-up
          (syntax-rules ()
            ((pair-up (a ...) (b ...)) (list (cons a b) ...))))
        (pair-up (1 2) (3 4 5))
        """
        r = review(src)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "repetition-mismatch")
        self.assertIn("a=2", r.error["message"])
        self.assertIn("b=3", r.error["message"])
        self.assertTrue(r.error["evidence"].startswith("EV-"))

    def test_nested_ellipsis_rejected(self):
        src = """
        (define-syntax bad
          (syntax-rules () ((bad ((x ...) ...)) x)))
        (bad ((1) (2)))
        """
        r = review(src)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "incomplete-syntax")


class ErrorAndIsolationTests(unittest.TestCase):
    def test_no_rule_match_points_at_call(self):
        src = """
        (define-syntax foo (syntax-rules () ((foo a b) (list a b))))
        (foo 1)
        """
        r = review(src)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "no-rule-match")
        self.assertIn("(foo 1)", r.error["snippet"])

    def test_recursion_limit_reported(self):
        src = """
        (define-syntax loop (syntax-rules () ((loop) (loop))))
        (loop)
        """
        r = review(src)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "recursion-limit")
        self.assertIn(str(EXPANSION_LIMIT), r.error["message"])

    def test_recursion_failure_does_not_block_later_review(self):
        bad = """
        (define-syntax loop (syntax-rules () ((loop) (loop))))
        (loop)
        """
        good = "(let ((x 1)) (with-x x))"
        review(bad)  # must not leave any global state
        r2 = review(good)
        # even though with-x is undefined in good, parsing/expansion is
        # well-defined; ensure no stale macros leaked: with-x stays free
        self.assertTrue(r2.ok)
        r3 = review("(define-syntax z (syntax-rules () ((z) 1))) (z)")
        self.assertTrue(r3.ok)
        self.assertIn("1", r3.normalized)

    def test_incomplete_input(self):
        src = """
        (define-syntax baz (syntax-rules () ((baz a) (list a)))
        (let ((x 1))
        """
        r = review(src)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "incomplete-syntax")
        self.assertTrue(r.error["evidence"].startswith("EV-"))

    def test_evidence_is_stable(self):
        src = "(foo 1)"
        r1 = review("(define-syntax foo (syntax-rules () ((foo a b) a)))\n" + src)
        r2 = review("(define-syntax foo (syntax-rules () ((foo a b) a)))\n" + src)
        self.assertEqual(r1.error["evidence"], r2.error["evidence"])

    def test_error_clears_previous_conclusions_in_result(self):
        # each review returns a fresh result object
        good = review("(define-syntax z (syntax-rules () ((z) 1))) (z)")
        bad = review("(define-syntax z (syntax-rules () ((z) 1))) (z 1 2 3)")
        self.assertTrue(good.ok)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error["kind"], "no-rule-match")
        self.assertEqual(bad.steps, [])
        self.assertEqual(bad.normalized, "")


class LimitsTests(unittest.TestCase):
    def test_too_many_macros_rejected(self):
        defs = "\n".join(
            f"(define-syntax m{i} (syntax-rules () ((m{i}) {i})))"
            for i in range(MAX_MACROS + 1))
        r = review(defs + "\n(m0)")
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "incomplete-syntax")

    def test_too_many_rules_rejected(self):
        rules = " ".join(f"((f a{i}) a{i})" for i in range(MAX_RULES + 1))
        r = review(f"(define-syntax f (syntax-rules () {rules})) (f 1)")
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "incomplete-syntax")


def _duplicating_module(depth: int) -> str:
    """One macro, one rule, template copies its argument: exponential fan-out."""
    expr = "1"
    for _ in range(depth):
        expr = f"(dup {expr})"
    return ("(define-syntax dup (syntax-rules () ((dup e) (pair e e))))\n"
            + expr + "\n")


class ExpansionBudgetTests(unittest.TestCase):
    def test_budgets_are_below_the_pathological_scale(self):
        # The reported abuse produced >60_000 steps and >800_000 normalized
        # chars; the configured cumulative boundary must stop well before it.
        self.assertLess(MAX_EXPANSION_STEPS, 60_000)
        self.assertLess(MAX_EXPANSION_OUTPUT_NODES, 800_000)

    def test_16_level_duplicating_macro_fails_before_abnormal_size(self):
        src = _duplicating_module(16)
        self.assertLess(len(src), 256 * 1024)  # still inside the request limit
        r = review(src)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "expansion-budget")
        # depth 16 is far below the single-chain limit, so this must be the
        # *cumulative* boundary firing, not the recursion-depth guard
        self.assertNotIn(str(EXPANSION_LIMIT), r.error["message"])
        # located at the original (outermost) call site: line 2, column 1
        self.assertEqual((r.error["line"], r.error["column"]), (2, 1))
        self.assertTrue(r.error["snippet"].lstrip().startswith("(dup"))
        self.assertIsNotNone(r.error["span"])
        self.assertTrue(r.error["evidence"].startswith("EV-"))
        # controlled failure carries no partial success conclusion at all
        self.assertEqual(r.steps, [])
        self.assertEqual(r.normalized, "")
        self.assertEqual(r.identities, [])
        self.assertEqual(r.hygiene_checks, [])

    def test_budget_evidence_is_stable_across_reviews(self):
        src = _duplicating_module(16)
        self.assertEqual(review(src).error["evidence"],
                         review(src).error["evidence"])

    def test_budget_failure_does_not_block_later_legitimate_review(self):
        review(_duplicating_module(16))  # exhausts a review-scoped budget
        good = review("(define-syntax z (syntax-rules () ((z) 1))) (z)")
        self.assertTrue(good.ok, good.error)
        self.assertIn("1", good.normalized)
        self.assertEqual(len(good.steps), 1)

    def test_normal_deep_nesting_without_duplication_still_completes(self):
        # Single (non-duplicating) copy per level: 16 nested levels are fine.
        expr = "1"
        for _ in range(16):
            expr = f"(wrap {expr})"
        src = ("(define-syntax wrap (syntax-rules () ((wrap e) (pair e))))\n"
               + expr)
        r = review(src)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len(r.steps), 16)
        self.assertIn("1", r.normalized)

    def test_single_ellipsis_rule_cannot_spike_past_budget(self):
        # One rule with a long one-level repetition could otherwise emit a huge
        # output in a single invocation; the budget is reserved pre-build.
        args = " ".join("1" for _ in range(100_000))
        src = ("(define-syntax big (syntax-rules () "
               "((big e ...) (list e ...))))\n(big " + args + ")\n")
        r = review(src)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "expansion-budget")
        self.assertEqual(r.error["line"], 2)
        self.assertEqual(r.steps, [])
        self.assertEqual(r.normalized, "")


if __name__ == "__main__":
    unittest.main()
