"""What every agent is built from.

The linter found this one. Three builders took the same four arguments in the
same order, and adding a fifth to one of them tripped the argument-count rule —
which is what that rule is for: a parameter list that long is usually a concept
nobody has named yet.

Naming it pays twice. The registry's builder type becomes one argument instead
of four, so adding a collaborator later is one edit rather than one per agent;
and an agent's own options stay keyword-only and visibly separate from the
things it collaborates with.
"""

from dataclasses import dataclass

from paimon.agents.tools import CorpusAccess
from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.ports import ChatModel, DocumentRepository, TokenCounter, VectorStore


@dataclass(frozen=True, slots=True)
class AgentCollaborators:
    """The ports and use cases an agent's nodes call.

    Attributes:
        retrieve: Retrieval, already configured with a store and an embedding
            model.
        chat_model: Generation. Called at the few nodes that need judgement.
        repository: Loads the documents behind cited chunks, so a citation
            resolves to a span of the source rather than to a chunk id.
        token_counter: Enforces the context budget.
        store: Where a document's indexed chunks are read from. Used only by the
            agent that lets a model ask to read one, and held here rather than
            passed to that builder alone so every builder keeps the same
            signature — which is the whole reason this type exists.
    """

    retrieve: RetrieveChunks
    chat_model: ChatModel
    repository: DocumentRepository
    token_counter: TokenCounter
    store: VectorStore

    @property
    def corpus(self) -> CorpusAccess:
        """What the tools run against, not yet bound to a tenant.

        A property rather than a fifth field, because it is a view of three
        fields that are already here and a run binds it to its own tenant.
        """
        return CorpusAccess(retrieve=self.retrieve, repository=self.repository, store=self.store)
