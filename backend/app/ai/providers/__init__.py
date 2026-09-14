"""Provider adapters.

One module per vendor, each implementing ``LLMProvider``. **Only modules in
this package may import a provider SDK** - that boundary is what keeps the rest
of the application provider-independent, and a test enforces it.

Nothing is re-exported here on purpose. Importing a submodule runs this file,
so re-exporting the adapters would mean that importing ``app.ai`` - which
imports ``app.ai.providers.base`` for the interface - pulled both vendor SDKs
into every process. Import what you need directly:

    from app.ai.providers.registry import create_provider
    from app.ai.providers.anthropic import AnthropicProvider
"""
