# Contributing

Follow the [Selamy Labs contribution guidelines][org-guidelines] for workflow,
quality, privacy, security, and style requirements specific to all organization
repositories.

## Architecture diagrams

Architecture diagrams MUST be updated in the same PR that changes the behavior
or spec they describe. The current baseline lives under
[`docs/architecture/`](docs/architecture/system-context.md).

When a change to a guarded source path has no diagram impact, add the
`no-diagram-impact` label and include a `Diagram impact: none - <reason>` line in
the PR body.

[org-guidelines]: https://github.com/selamy-labs/.github/blob/main/CONTRIBUTING.md
