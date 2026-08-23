"""The LLM boundary.

Everything that talks to a language model lives here, behind one small provider-neutral
interface. The rest of the pipeline never constructs a request, never reads a response and
never sees a credential.
"""
