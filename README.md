# Sample repository

Sample content used to exercise the automated release promotion pipeline.

## Promotion flow

```
dev_collaboration  ->  master  ->  psup  ->  prod
```

Dispatch the `code_promotion` workflow from Actions, pick `PSUP` or `PROD`, and
select the user-created temporary branch. Before dispatching, create that
branch and commit a root-level `promotion.txt` file with one repository-relative
path per line. `DELETE|<path>` remains available for deletions.

```
reltest_30_08_2026/promotion.txt

config/application.yml
workflows/customer_sync.json
DELETE|workflows/legacy_cleanup.json
```

For a MASTER promotion, files named in `promotion.txt` are read from
`dev_collaboration`, applied to the supplied temporary branch, and proposed by
a Pull Request from that branch directly to `master`. No release branch is
created for MASTER.

PSUP and PROD keep the release-branch path: their requested files are read from
`master` and `psup` respectively, applied to the temporary branch, then the
pipeline creates `release/<timestamp>_psup` or `release/<timestamp>_prod` from
the target and opens a Pull Request into that generated release branch.
`dev_collaboration`, `master`, `psup`, and `prod` are never written to by the
automation.

The `promotion.txt` file must be at the repository root of the selected staging
branch. Blank lines and surrounding whitespace are ignored; invalid or duplicate
paths fail the run without a push or Pull Request. If the temporary branch
already has the requested `master` versions, the automation does not create another
commit; it still creates the release branch and Pull Request after verifying the
full PR diff contains only approved paths.

## Layout

| Path | Contents |
| --- | --- |
| `config/` | Application, database and feature-flag configuration. |
| `Notebooks/` | Jupyter notebooks. |
| `workflows/` | Job definitions. Drives the `workflows_list.txt` rebuild. |
| `test/` | Tests for the sample content. |
| `workflows_list.txt` | Normalized automatically for workflow paths in `promotion.txt` or staging; stores paths relative to `workflows/` (for example, `team/job.json`). |
| `promotion/` | The promotion pipeline itself. Standard library only. |
