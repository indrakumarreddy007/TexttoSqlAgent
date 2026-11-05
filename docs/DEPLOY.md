# Deploying the simple UI

The simple UI is `apps/streamlit_app.py`: one question box, the answer table, and the SQL and
assumptions behind it. The agent runs in the same process. On first start it builds the
warehouse from the verified Kaggle mirror (about 20 seconds), so a fresh host needs no setup.

## 1. Pick the LLM

Free hosting gives you a small CPU, which cannot run a 7B model at a usable speed. Use a hosted
model there and keep Ollama for local use.

| Option | Cost | Settings |
|---|---|---|
| Ollama on your machine | Free | `SQLAGENT_LLM_PROVIDER=ollama` (default), `SQLAGENT_LLM_MODEL=qwen2.5-coder:7b` |
| Groq | Free tier with rate limits | `SQLAGENT_LLM_PROVIDER=openai`, `SQLAGENT_LLM_BASE_URL=https://api.groq.com/openai/v1`, `SQLAGENT_LLM_MODEL=llama-3.3-70b-versatile`, `OPENAI_API_KEY=<groq key>` |
| Google Gemini | Free tier with rate limits | `SQLAGENT_LLM_PROVIDER=openai`, `SQLAGENT_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`, `SQLAGENT_LLM_MODEL=gemini-2.5-flash`, `OPENAI_API_KEY=<gemini key>` |
| Claude (Anthropic API) | Pay as you go | `SQLAGENT_LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=<key>`; model defaults to `claude-opus-5-5`, or set `SQLAGENT_LLM_MODEL=claude-sonnet-5-5` / `claude-haiku-5-5` for lower cost |

Check each provider's model list for current model ids before deploying. A claude.ai Pro or Max
subscription does not include API access; an API key comes from the Claude Console.

## 2. Run locally

```bash
pip install -e ".[ui]"
```

```bash
streamlit run apps/streamlit_app.py
```

Open http://localhost:8501.

## 3. Streamlit Community Cloud (free)

1. Go to https://share.streamlit.io and sign in with GitHub.
2. Create an app from this repository, branch `main`, main file `apps/streamlit_app.py`.
3. Under **Advanced settings → Secrets**, paste the LLM settings as TOML, for example:
   ```toml
   SQLAGENT_LLM_PROVIDER = "openai"
   SQLAGENT_LLM_BASE_URL = "https://api.groq.com/openai/v1"
   SQLAGENT_LLM_MODEL = "llama-3.3-70b-versatile"
   OPENAI_API_KEY = "gsk_..."
   ```
4. Deploy. Dependencies come from `requirements.txt`.

## 4. Hugging Face Spaces (free CPU)

1. Create a Space with SDK **Docker**.
2. Push this repository to the Space, with `deploy/Dockerfile.streamlit` copied to `Dockerfile` at
   the root and this front matter at the top of the Space's `README.md`:
   ```yaml
   ---
   title: Halcyon SQL Copilot
   sdk: docker
   app_port: 8501
   ---
   ```
3. Add the LLM settings under **Settings → Variables and secrets**.

## 5. Azure Container Apps (monthly free grant)

Container Apps includes a monthly free grant of vCPU-seconds, GiB-seconds and requests; a demo
with scale-to-zero usually stays inside it. Avoid Azure Container Registry for a free setup: the
`publish-ui-image` GitHub workflow pushes the image to GitHub Container Registry instead. Make the
package public on GitHub (Packages → package settings → visibility), then:

```bash
az group create --name halcyon-rg --location eastus
```

```bash
az containerapp env create --name halcyon-env --resource-group halcyon-rg --location eastus
```

```bash
az containerapp create --name halcyon-ui --resource-group halcyon-rg --environment halcyon-env --image ghcr.io/indrakumarreddy007/texttosqlagent-ui:latest --target-port 8501 --ingress external --min-replicas 0 --max-replicas 1 --cpu 1 --memory 2Gi --secrets llm-key=<your key> --env-vars SQLAGENT_LLM_PROVIDER=openai SQLAGENT_LLM_BASE_URL=https://api.groq.com/openai/v1 SQLAGENT_LLM_MODEL=llama-3.3-70b-versatile OPENAI_API_KEY=secretref:llm-key
```

The command prints the app's URL. With `--min-replicas 0` the first request after idle takes a few
seconds longer while the container starts and the warehouse is built.

Azure's free credits and the always-free services do not cover Claude in Microsoft Foundry: Claude
there is billed separately through the Microsoft Marketplace at Anthropic's API prices.

## Access control in the demo

The sidebar user picker uses the demo identities in `config/users.yaml`. That is fine for a demo.
For real users, put the app behind your identity provider and map each signed-in user to a
`UserContext` (role and regions) instead of letting people pick one.
