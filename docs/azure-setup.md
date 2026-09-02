# Provisioning the Azure backend

The platform runs entirely on local adapters by default. This describes what to create when
you want it running on Azure, and what each value maps to in configuration.

Nothing here is required to develop, test or benchmark. It is required to reproduce the
cloud-backed numbers.

## What is needed

| Resource | Why |
|---|---|
| Azure OpenAI, with an embedding deployment | Vectors for indexing and querying |
| Azure OpenAI, with a chat deployment | Grounded answers |
| Azure AI Search | Retrieval, including its native hybrid ranker |

Azure OpenAI requires access approval on some subscriptions, and it is granted per
subscription rather than per resource. Request it before planning around it.

## Creating them

```bash
LOCATION=swedencentral          # a region where the models you want are available
GROUP=paimon
az group create --name "$GROUP" --location "$LOCATION"

# --- Azure OpenAI -----------------------------------------------------------
az cognitiveservices account create \
    --name paimon-openai --resource-group "$GROUP" --location "$LOCATION" \
    --kind OpenAI --sku S0 --custom-domain paimon-openai

# The deployment name is what the platform addresses. It need not match the
# model, and the URL uses this, so choose something you will recognise.
az cognitiveservices account deployment create \
    --name paimon-openai --resource-group "$GROUP" \
    --deployment-name paimon-embed \
    --model-name text-embedding-3-large --model-version 1 --model-format OpenAI \
    --sku-name Standard --sku-capacity 20

az cognitiveservices account deployment create \
    --name paimon-openai --resource-group "$GROUP" \
    --deployment-name paimon-chat \
    --model-name gpt-4o-mini --model-version 2024-07-18 --model-format OpenAI \
    --sku-name Standard --sku-capacity 20

# --- Azure AI Search --------------------------------------------------------
# The free tier is roughly 50 MB and three indexes: enough for the sample corpus,
# not enough for the full benchmark. Use basic when you want the real numbers.
az search service create \
    --name paimon-search --resource-group "$GROUP" --location "$LOCATION" --sku free
```

## Pointing the platform at them

```bash
PAIMON_EMBEDDING__PROVIDER=azure
PAIMON_CHAT__PROVIDER=azure
PAIMON_RETRIEVAL__STORE=azure_search

PAIMON_AZURE_OPENAI__ENDPOINT=https://paimon-openai.openai.azure.com
PAIMON_AZURE_OPENAI__EMBEDDING_DEPLOYMENT=paimon-embed
PAIMON_AZURE_OPENAI__CHAT_DEPLOYMENT=paimon-chat
PAIMON_AZURE_SEARCH__ENDPOINT=https://paimon-search.search.windows.net
```

Then create the index, whose schema the adapter declares:

```bash
cd backend
uv run python -c "
import asyncio
from paimon.config import get_settings
from paimon.interfaces.api.dependencies import build_resources

async def main():
    async with build_resources(get_settings()) as resources:
        await resources.vector_store.ensure_index()

asyncio.run(main())
"
```

## Authentication

Two ways, and the platform picks by whether a key is present (ADR-0014).

**Service keys**, simplest to start with:

```bash
PAIMON_AZURE_OPENAI__API_KEY=$(az cognitiveservices account keys list \
    --name paimon-openai --resource-group "$GROUP" --query key1 -o tsv)
PAIMON_AZURE_SEARCH__API_KEY=$(az search admin-key show \
    --service-name paimon-search --resource-group "$GROUP" --query primaryKey -o tsv)
```

**Entra ID**, with no keys in configuration at all. Leave both key variables unset, install
the extra, and assign the roles — without them every call returns 403, which is the usual
first surprise:

```bash
cd backend && uv sync --extra azure

SUBSCRIPTION=$(az account show --query id -o tsv)
ME=$(az ad signed-in-user show --query id -o tsv)

az role assignment create --assignee "$ME" \
    --role "Cognitive Services OpenAI User" \
    --scope "/subscriptions/$SUBSCRIPTION/resourceGroups/$GROUP/providers/Microsoft.CognitiveServices/accounts/paimon-openai"

az role assignment create --assignee "$ME" \
    --role "Search Index Data Contributor" \
    --scope "/subscriptions/$SUBSCRIPTION/resourceGroups/$GROUP/providers/Microsoft.Search/searchServices/paimon-search"
```

`az login` locally and a managed identity in Azure then take the same code path.

## Cost

Embeddings and generation are billed per token; Azure AI Search is billed per hour the
service exists, whether or not anything queries it. A search service left running is the
expensive mistake here, not a benchmark run.

```bash
az group delete --name "$GROUP" --yes --no-wait
```
