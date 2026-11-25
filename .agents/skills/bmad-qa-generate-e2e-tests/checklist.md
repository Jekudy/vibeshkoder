# QA Automate - Validation Checklist

## Test Generation

- [ ] API tests generated (if applicable)
- [ ] E2E tests generated (if UI exists)
- [ ] Browser tests use `ego-browser`. Other tests use standard test framework APIs.
- [ ] Tests cover happy path
- [ ] Tests cover 1-2 critical error cases

## Test Quality

- [ ] All generated tests run successfully
- [ ] Tests use proper locators (semantic, accessible)
- [ ] Tests have clear descriptions
- [ ] No hardcoded waits or sleeps
- [ ] Tests are independent (no order dependency)

## Output

- [ ] Test summary created
- [ ] Tests saved to appropriate directories
- [ ] Summary includes coverage metrics

## Validation

Run API and non-browser tests with the project's test command. If that command also starts a legacy browser suite, run only the API and non-browser suite. Run browser tests with `ego-browser`.

**Expected**: All tests pass ✅

---

**Need more comprehensive testing?** Install [Test Architect (TEA)](https://bmad-code-org.github.io/bmad-method-test-architecture-enterprise/) for advanced workflows.
