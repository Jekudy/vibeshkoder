- source_spec: `spec-gh-478-qa-reply-trigger.md`
  summary: Replace PostgreSQL headline tags with collision-free sentinels in a dedicated search-contract change.
  evidence: `search_messages` uses PostgreSQL's default `<b>` markers, which are indistinguishable from literal source text; changing the marker contract is outside issue #478 and its frozen no-SQL boundary.
