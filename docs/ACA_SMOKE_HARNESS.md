# ACA Smoke Harness Contract

The ACA smoke harness is a documentation-focused check that confirms workers can
make small, isolated repository changes in the expected task order without
touching unrelated runtime files. It exists to exercise the ACA worker handoff
contract: inspect the assigned targets, make the narrowest acceptable edit,
verify the edited documentation, and report exactly what changed.

## Task order

Workers running this smoke task should follow this order:

1. Read each existing target document before editing it.
2. Check each requested target path exactly, including paths that may not exist
   yet.
3. Create or update only the requested documentation files.
4. Keep the diff limited to docs unless verification fails for an obvious
   documentation-adjacent reason.
5. Verify the resulting documentation with a narrow readback or grep.
6. Report the changed files, verification command and result, and any blockers.

## Expected files

This smoke harness contract is expected to touch only these documentation files:

- `docs/ACA_SMOKE_HARNESS.md` - describes the smoke harness purpose, task order,
  expected files, and verification command.
- `docs/README.md` - links to this contract from the documentation index.

The harness should not modify source, test, runtime, configuration, lock, or
temporary files for this docs-only task.

## Verification command (from repository root)

Run a narrow grep/readback against the changed docs, for example:

```sh
grep -R "ACA Smoke Harness Contract\|ACA_SMOKE_HARNESS" docs/ACA_SMOKE_HARNESS.md docs/README.md
```

Passing verification means the new contract exists and the docs index links to
it. If verification is not applicable or cannot be run, the worker report should
state why.
