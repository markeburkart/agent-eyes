# Project slash commands

Markdown files dropped in this directory become slash commands available to
every persona on this project's team — they're loaded via
`settingSources: ["project"]` in `apps/server/src/agent-session.ts`, since
every persona session's `cwd` is this project's root.

`orchestra init` copies this file here as a starting point; it's your
project's own `.claude/commands`, safe to edit or delete. Personal,
cross-project commands belong in `~/.claude` instead (loaded via
`settingSources: ["user"]`).

See the Claude Agent SDK docs for the command file format (frontmatter +
prompt body, `$ARGUMENTS` placeholder, etc).
