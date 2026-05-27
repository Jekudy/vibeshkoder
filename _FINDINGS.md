# T12-06 CI Findings

## action_id threading gap (T12-07 / orchestrator follow-up)

`SuggestCardCreationTool.execute()` accepts `action_id: int = 0` as a default parameter
because `ButlerService.execute_action` (in `bot/services/butler.py`) does not currently
thread the `butler_actions.id` through to the tool's `execute()` call. In tests this
default is acceptable (test path is explicit in the docstring), but in production every
`butler_card_suggestions` row written via `suggest_card_creation` will have
`butler_action_id=0`, which will FK-violate against `butler_actions` (no row with id=0
exists). This must be fixed in T12-07 when the orchestrator wires `action_id` through
`ButlerService.execute_action → tool.execute(action_id=action.id)`.
