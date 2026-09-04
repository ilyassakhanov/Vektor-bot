----First prompt---
## Task: Add an LLM Integration Layer

Add an LLM feature to the existing Telegram bot.

### Requirements

1. **Message flow**

   * Telegram user messages must be forwarded to an LLM.
   * The LLM response must be returned to the user through Telegram.
   * Keep Telegram-specific logic separate from LLM integration logic.
   * The Telegram bot should depend on an abstract LLM interface, not on a specific LLM provider.

2. **LLM module**

   * Create a dedicated `llm/` directory for all LLM-related functionality.
   * Implement the LLM integration using an OOP approach.
   * Define an abstract interface/base class for LLM providers.
   * The interface should expose a simple method for sending a user message/prompt and receiving the generated response.
   * Provider-specific implementation details must remain inside their respective classes.

3. **Provider interchangeability**

   * Implement the design so that one LLM provider can be replaced with another without modifying the Telegram bot's business logic.
   * Use dependency injection where appropriate.
   * The Telegram bot should interact only with the abstract LLM interface.
   * For example, the architecture should allow:

     * `OllamaLLM`
     * future `OpenAILLM`
     * future `AnthropicLLM`
     * future providers
   * Adding a new provider should require implementing the interface rather than changing existing Telegram message-handling logic.

4. **First implementation: Ollama**

   * Implement the first concrete provider using a locally running Ollama instance.
   * The Ollama connection details must be configurable rather than hardcoded.
   * At minimum, make the Ollama host/base URL and model configurable through the application's existing configuration mechanism or environment variables.
   * Do not introduce unnecessary provider-specific dependencies into the Telegram bot layer.

5. **Project structure**

   * Keep the implementation clean and modular.
   * A reasonable structure would be similar to:

   ```text
   project/
   ├── llm/
   │   ├── __init__.py
   │   ├── base.py          # Abstract LLM interface
   │   └── ollama.py        # Ollama implementation
   ├── bot/
   │   └── ...
   └── ...
   ```

   Adapt this structure to the existing project rather than restructuring unrelated parts of the application.

6. **Error handling**

   * Handle LLM connection failures and API errors gracefully.
   * The Telegram bot must not crash if Ollama is unavailable or returns an error.
   * Return an appropriate user-friendly error message when the LLM cannot be reached.
   * Keep provider-specific exceptions and implementation details inside the LLM layer where possible.

7. **Configuration**

   * Do not hardcode the Ollama URL or model name.
   * Use environment variables or the project's existing configuration mechanism.
   * Provide sensible defaults for local development where appropriate.

8. **Testing**

   * Make the LLM abstraction easy to mock.
   * Add tests for the Telegram-to-LLM integration using a mock/fake LLM implementation.
   * Tests should not require a running Ollama instance.
   * Add provider-specific tests for `OllamaLLM` where practical.

### Important design constraint

Do **not** implement the Telegram bot directly against Ollama's API.

The intended dependency direction is:

```text
Telegram Handler
       │
       ▼
   LLM Interface
       │
       ▼
   OllamaLLM
       │
       ▼
   Ollama API
```

Later, `OllamaLLM` should be replaceable with another implementation without changing the Telegram handler.

### Implementation approach

First inspect the existing project structure and identify:

* where Telegram messages are currently handled;
* how configuration is currently managed;
* how dependencies are managed;
* the existing testing structure.

Then implement the smallest clean change that integrates the LLM feature while preserving the existing architecture and behavior.

Avoid unnecessary refactoring or introducing abstractions that are not required for provider interchangeability.
--------First prompt end(First version)------------