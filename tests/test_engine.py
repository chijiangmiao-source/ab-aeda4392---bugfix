"""Code tests for hygienic macro expansion scenarios.

Run:  python -m unittest discover -s tests -v
"""

import re
import unittest

from app.engine import (
    EXPANSION_LIMIT, MAX_EXPANSIONS, MAX_MACROS, MAX_OUTPUT_CHARS, MAX_RULES, review,
)


def dup_module(levels: int) -> str:
    """One macro, one rule whose template duplicates its argument; the
    innermost call is a constant.  ``levels`` nested uses produce 2**levels-1
    macro invocations while the nesting path is only ``levels`` deep."""
    call = "1"
    for _ in range(levels):
        call = f"(dup {call})"
    return ("(define-syntax dup (syntax-rules () ((dup e) (list e e))))\n"
            + call + "\n")


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


class ExpansionBudgetTests(unittest.TestCase):
    """Cumulative (not single-path-depth) resource boundaries.

    A single macro with one duplicating rule, nested 16 levels, stays far
    below the 64-level depth limit yet would otherwise perform 65,535
    expansions and render 800k+ characters.
    """

    def test_16_level_duplicating_macro_fails_within_budget(self):
        r = review(dup_module(16))
        # controlled failure, never a success with a giant payload
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "expansion-budget")
        # no partial conclusions and no tens-of-thousands of step records
        self.assertEqual(r.steps, [])
        self.assertEqual(r.normalized, "")
        self.assertEqual(r.identities, [])
        self.assertEqual(r.hygiene_checks, [])
        # stopped well before the pathological size (65,535 steps / ~850k chars)
        self.assertLessEqual(r.expansions, MAX_EXPANSIONS + 1)
        self.assertLessEqual(r.expansions, 5000)
        self.assertLessEqual(r.output_chars, MAX_OUTPUT_CHARS + 10000)
        self.assertLess(r.output_chars, 100000)

    def test_budget_error_locates_original_call_site(self):
        r = review(dup_module(16))
        # the outer user-written call lives on line 2; template-generated
        # inner uses must not be reported as the origin
        self.assertEqual(r.error["line"], 2)
        self.assertEqual(r.error["column"], 1)
        self.assertIn("(dup (dup", r.error["snippet"])
        self.assertTrue(r.error["span"])

    def test_budget_evidence_is_stable(self):
        a = review(dup_module(16))
        b = review(dup_module(16))
        self.assertTrue(a.error["evidence"].startswith("EV-"))
        self.assertEqual(a.error["evidence"], b.error["evidence"])

    def test_budget_error_carries_usage_detail(self):
        r = review(dup_module(16))
        self.assertEqual(r.error["budget"], "expansions")
        self.assertEqual(r.error["limit"], MAX_EXPANSIONS)
        self.assertGreaterEqual(r.error["used"], 1)

    def test_normal_nested_dup_macro_still_expands(self):
        # 10 levels => 1,023 expansions, within the cumulative budget
        r = review(dup_module(10))
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len(r.steps), 1023)
        # 1,024 leaf constants (identity tags such as ⁽F1⁾ must not be counted)
        leaves = re.findall(r"(?<![\w⁽])1(?![\w⁾])", r.normalized)
        self.assertEqual(len(leaves), 1024)

    def test_normal_named_macro_scenario_still_expands(self):
        src = """
        (define-syntax with-x
          (syntax-rules ()
            ((with-x body) (let ((x 99)) body))))
        (define-syntax call-with-x
          (syntax-rules ()
            ((call-with-x v) (with-x v))))
        (let ((x 1)) (call-with-x x))
        """
        r = review(src)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len(r.steps), 2)
        self.assertIn("(let ((x⁽B2⁾ 99)) x⁽B1⁾)", r.normalized)

    def test_budget_failure_does_not_block_later_review(self):
        review(dup_module(16))  # must not leave any global state
        r2 = review("(define-syntax z (syntax-rules () ((z) 1))) (z)")
        self.assertTrue(r2.ok, r2.error)
        self.assertIn("1", r2.normalized)
        r3 = review(dup_module(5))
        self.assertTrue(r3.ok, r3.error)
        self.assertEqual(len(r3.steps), 31)

    def test_configurable_expansion_budget(self):
        tight = review(dup_module(16), max_expansions=10)
        self.assertFalse(tight.ok)
        self.assertEqual(tight.error["kind"], "expansion-budget")
        self.assertEqual(tight.error["limit"], 10)
        self.assertLessEqual(tight.error["used"], 11)
        # raising both budgets lets the full 16-level tree complete
        wide = review(dup_module(16), max_expansions=100_000,
                      max_output_chars=10_000_000)
        self.assertTrue(wide.ok, wide.error)
        self.assertEqual(len(wide.steps), 65_535)

    def test_output_size_budget_independent_of_expansion_count(self):
        # few expansions are allowed, but output must not balloon
        r = review(dup_module(8), max_expansions=10 ** 9, max_output_chars=50)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "expansion-budget")
        self.assertEqual(r.error["budget"], "output-chars")
        self.assertEqual(r.steps, [])

    def test_single_path_depth_limit_still_enforced(self):
        # linear (non-duplicating) recursion must still hit the depth limit,
        # which remains a separate defence from the cumulative budget
        src = """
        (define-syntax loop (syntax-rules () ((loop) (loop))))
        (loop)
        """
        r = review(src, max_expansions=10 ** 9, max_output_chars=10 ** 9)
        self.assertFalse(r.ok)
        self.assertEqual(r.error["kind"], "recursion-limit")
        self.assertIn(str(EXPANSION_LIMIT), r.error["message"])


if __name__ == "__main__":
    unittest.main()
