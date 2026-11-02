# Tasks

## 1. Pure validation and splitting functions

- [x] 1.1 Add `agent/validation.py`: `validate_input(text) -> Verdict` (reject empty/blank, reject >4096, strip zero-width and bidi-override control characters from accepted text) and `split_reply(text, limit=4096) -> list[str]` measuring UTF-16 code units (paragraph → sentence → word boundaries, hard-cut fallback for unbreakable tokens, concatenation always equals the input)
- [x] 1.2 Add pure-function tests in `tests/test_input_validation.py` and `tests/test_output_safety.py`: empty/whitespace rejection, 4096/4097 boundary pair, control-char strip list, visible-Unicode passthrough, split ordering, chunk-length cap, content equality, unbreakable-token hard cut, astral-emoji reply split by UTF-16 units (e.g. `"😀"*3000` → ≥2 chunks, each ≤4096 units) — `python -m pytest tests/test_input_validation.py tests/test_output_safety.py -v`

## 2. Handler wiring

- [x] 2.1 Wire `validate_input` into `handle_message` in `bot.py`: after the auth gate and after the document branch (text path only); a rejection sends one friendly constant reply and returns — prove with a handler-level test that `ScriptedLLM.chat_calls == []` and the chat's conversation history is untouched, and that a document with no text is still routed to the document flow
- [x] 2.2 Wire `split_reply` into both agent-reply send paths in `bot.py` (`handle_message` text path and `build_document_handler`'s agent-turn `reply_to`) — prove with a fake TeleBot recorder that a scripted 10,000-char reply yields ordered sends, each ≤4096 UTF-16 units
- [x] 2.3 Add a plain-text contract test: the fake TeleBot records sends, and no send ever carries a `parse_mode` kwarg; a reply containing `_*[]()~>#+-=|{}.!` is delivered unchanged

## 3. Quality gates and archive

- [x] 3.1 `ruff check .` and `ruff format --check .` pass
- [x] 3.2 `mypy .` passes
- [x] 3.3 `python -m pytest` passes offline (gated suites, if any, skip without network)
- [x] 3.4 `npx --yes @fission-ai/openspec@1.14.1 validate --all --strict` passes
- [x] 3.5 Archive this change in the same pull request that closes its last task, so the deltas land in `openspec/specs/`
