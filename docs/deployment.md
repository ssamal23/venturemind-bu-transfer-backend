# Deploying to Azure

**Status:** the code and configuration for this are written and the parts that can run without Azure are tested. The Docker images and every `az` command below have **not been run yet** (no Docker or Azure CLI on the build machine). Expect to fix small things on the first pass.

## 1. The two repositories

The project is two repositories that meet only at the HTTP API (`/api/...`). The frontend image proxies to the API at runtime, so neither build depends on the other.

| Repository | Holds |
|---|---|
| [backend](https://github.com/ssamal23/venturemind-bu-transfer-backend) (this one) | API, worker, migrations, tests, scripts, and these docs |
| [frontend](https://github.com/ssamal23/venturemind-bu-transfer-frontend) | The React app and its nginx image |

- Each repository builds and deploys on its own.
- An API change that touches the screens needs a pull request in each. Merge the backend first.
- Keep both **private**, and never commit `.env`. Both `.gitignore` files exclude it, and `.env.example` shows the names.
- **The sponsor's workbook is not in either repository.** Put it at `data/BU_Transfer_Program_Course_Match.xlsx` before running the seed.

## 2. What gets deployed

```
Browser --HTTPS--> bu-web (nginx + React)          public
                      | /api  (internal only)
                      v
                   bu-api (FastAPI)                 no public address
                   bu-worker (same image)           no address at all
                      |
   Azure PostgreSQL (+pgvector)      Azure Blob Storage (private)      Claude / Gemini
```

- **bu-web** is the only public address. nginx serves the React build and forwards `/api` to the API, so the browser talks to one origin and the API needs no CORS or public URL.
- **bu-api** and **bu-worker** are the same image with different commands.
- Files go to Blob Storage because the API and the worker are separate containers; a container's own disk is private to it and lost on restart.
- Containers sign in to Storage and the registry with a **managed identity**, so no storage key exists anywhere.

## 3. Before you start

1. **Allow your current IP** on the PostgreSQL server (portal, server, Networking, "Add current client IP address"). Your address changes; if the database stops answering, this is the first thing to check. Also confirm the server is **running** (Overview).
2. Install the Azure CLI so you can build images with no local Docker: `winget install Microsoft.AzureCLI`, then `az login`. (Azure Cloud Shell in the portal also works.)
3. Pick names. Replace the values in **bold** below.

```bash
RG=rg-bu-transfer-hackathon          # your existing resource group
LOC=<the region of your PostgreSQL server>
ACR=<unique lowercase letters and digits>       # for example butransfer12345
STORAGE=<unique lowercase letters and digits>   # for example butransferfiles12345
ENVNAME=bu-transfer-env
```

## 4. Database

The database already has the schema (migration 0004), the catalog, and the demo users. Two things remain, both from the repository root, on a machine that can reach the server:

```bash
python -m app.workflows.checkpointer          # creates the workflow tables (admin, once)
python scripts/create_app_user.py             # creates the limited user bu_app (hidden password prompt)
```

`bu_app` can read and write the tables but cannot create, drop or truncate them, and cannot update or delete the audit log, decisions or postings. Use a password of letters and digits only, 20 or more characters. **The running app uses `bu_app`; the admin account is for migrations only.**

Future migrations, as admin: `alembic upgrade head`.

## 5. Storage and registry

```bash
az storage account create -n $STORAGE -g $RG -l $LOC --sku Standard_LRS --kind StorageV2 \
  --allow-blob-public-access false --min-tls-version TLS1_2
az acr create -n $ACR -g $RG --sku Basic
```

Build the images in Azure (no local Docker needed). From a checkout of each repository, with the Azure CLI installed:

```bash
az acr build -r $ACR -t bu-api:v1 .      # run inside the backend repository
az acr build -r $ACR -t bu-web:v1 .      # run inside the frontend repository
```

Or, with no checkout at all (for example from Azure Cloud Shell), build straight from GitHub. This works for public repositories; a private one needs `--git-access-token`:

```bash
az acr build -r $ACR -t bu-api:v1 {BACKEND}.git#main
az acr build -r $ACR -t bu-web:v1 {FRONTEND}.git#main
```

The backend build downloads the embedding model into the image, so allow several minutes.

## 6. An identity for the apps

```bash
az identity create -g $RG -n id-bu-apps
ID_ID=$(az identity show -g $RG -n id-bu-apps --query id -o tsv)
ID_CLIENT=$(az identity show -g $RG -n id-bu-apps --query clientId -o tsv)
ID_PRINCIPAL=$(az identity show -g $RG -n id-bu-apps --query principalId -o tsv)

az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope $(az acr show -n $ACR --query id -o tsv)
az role assignment create --assignee-object-id $ID_PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope $(az storage account show -n $STORAGE -g $RG --query id -o tsv)
```

Wait two minutes for the roles to take effect before creating the apps.

## 7. Container Apps

```bash
az containerapp env create -n $ENVNAME -g $RG -l $LOC
```

### Settings

| Name | Value | Where |
|---|---|---|
| `ENVIRONMENT` | `production` | API and worker. Makes the API refuse an unsafe configuration |
| `DATABASE_URL` | `postgresql+psycopg://bu_app:<password>@<server>.postgres.database.azure.com:5432/transfer?sslmode=require` | **Secret**, API and worker |
| `JWT_SECRET` | a random value of 32+ characters | **Secret**, API only |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | your keys | **Secret**, worker (the API does not call the models) |
| `STORAGE_BACKEND` | `azure` | API and worker |
| `AZURE_STORAGE_ACCOUNT_URL` | `https://<STORAGE>.blob.core.windows.net` | API and worker |
| `AZURE_CLIENT_ID` | the identity's client id (`$ID_CLIENT`) | API and worker. Tells the SDK which identity to use |
| `CHECKPOINT_AUTO_SETUP` | `false` | API and worker. `bu_app` cannot create tables |
| `LLM_CHAIN` | `["claude","gemini"]` | worker |

Enter secrets in the portal (Container App, **Secrets**, then reference them in environment variables) so they never appear in your shell history. Generate the signing key with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

### Create the apps

```bash
# API: internal only, no public address
az containerapp create -n bu-api -g $RG --environment $ENVNAME \
  --image $ACR.azurecr.io/bu-api:v1 --registry-server $ACR.azurecr.io --registry-identity $ID_ID \
  --user-assigned $ID_ID --target-port 8000 --ingress internal \
  --cpu 1.0 --memory 2.0Gi --min-replicas 1 --max-replicas 2 \
  --env-vars ENVIRONMENT=production STORAGE_BACKEND=azure AZURE_STORAGE_ACCOUNT_URL=https://$STORAGE.blob.core.windows.net \
             AZURE_CLIENT_ID=$ID_CLIENT CHECKPOINT_AUTO_SETUP=false

# Worker: same image, runs the background jobs, no ingress
az containerapp create -n bu-worker -g $RG --environment $ENVNAME \
  --image $ACR.azurecr.io/bu-api:v1 --registry-server $ACR.azurecr.io --registry-identity $ID_ID \
  --user-assigned $ID_ID --cpu 1.0 --memory 2.0Gi --min-replicas 1 --max-replicas 1 \
  --command python --args "-m" "app.workers.evaluation_worker" \
  --env-vars ENVIRONMENT=production STORAGE_BACKEND=azure AZURE_STORAGE_ACCOUNT_URL=https://$STORAGE.blob.core.windows.net \
             AZURE_CLIENT_ID=$ID_CLIENT CHECKPOINT_AUTO_SETUP=false
```

Then add the secrets and the secret-backed variables (portal), and restart both. The API fails to start with a clear message if something unsafe is missing.

Find the API's internal address and give it to the web app:

```bash
API_FQDN=$(az containerapp show -n bu-api -g $RG --query properties.configuration.ingress.fqdn -o tsv)

az containerapp create -n bu-web -g $RG --environment $ENVNAME \
  --image $ACR.azurecr.io/bu-web:v1 --registry-server $ACR.azurecr.io --registry-identity $ID_ID \
  --user-assigned $ID_ID --target-port 8080 --ingress external \
  --cpu 0.5 --memory 1.0Gi --min-replicas 1 --max-replicas 2 \
  --env-vars API_UPSTREAM=https://$API_FQDN

az containerapp show -n bu-web -g $RG --query properties.configuration.ingress.fqdn -o tsv   # the public address
```

## 8. Lock it down before anyone else sees it

The demo accounts (`student@demo.edu`, `evaluator@demo.edu`, `office@demo.edu`) all use the password `demo1234`. On a public address, **anyone could sign in as the credit office.** The data is synthetic, but do one of these before sharing the link:

- **Restrict who can reach the site** (simplest): allow only your IP or your office's range.
  ```bash
  az containerapp ingress access-restriction set -n bu-web -g $RG --rule-name team --ip-address <your-ip>/32 --action Allow
  ```
- Or change the demo passwords, or switch to Microsoft Entra sign-in (not built).

## 9. Check it

1. Open the web address. Sign in as the student: six courses.
2. Sign in as the evaluator: four cases. Open one.
3. Logs: `az containerapp logs show -n bu-api -g $RG --follow` and the same for `bu-worker`.
4. Upload a syllabus and watch the worker log pick it up. The file should appear in the Storage account's `syllabi` container.

## 10. Updating

```bash
az acr build -r $ACR -t bu-api:v2 .      # from the backend repository
az containerapp update -n bu-api    -g $RG --image $ACR.azurecr.io/bu-api:v2
az containerapp update -n bu-worker -g $RG --image $ACR.azurecr.io/bu-api:v2
```

Run `alembic upgrade head` as the admin **before** deploying code that needs a new column.

## 11. Cost

Rough drivers, in order: the PostgreSQL server, the always-on replicas, then everything else (small). Check the portal's cost estimate for your region.

- **Stop the PostgreSQL server** when not in use (Overview, Stop). Azure restarts it after 7 days.
- Set the apps' minimum replicas to 0 when idle (`--min-replicas 0`); the first request then takes a few seconds. Keep `bu-worker` at 1 while jobs are waiting.
- When the hackathon ends, delete the resource group: `az group delete -n $RG`.

## 12. Known gaps

- **Untested here:** the Docker builds, the nginx templating, every `az` command, and the managed-identity sign-in to Storage. The Blob Storage code is tested against a fake client only.
- **No Key Vault.** Secrets live in Container Apps' encrypted secret store. Moving them to Key Vault with a reference is the next step for real data.
- **No custom domain or sign-in service.** The address is the generated `*.azurecontainerapps.io` one, with password sign-in.
- **No monitoring set up.** Container Apps writes logs to Log Analytics; add alerts on `bu-worker` restarts and on the job queue length.
- **Single worker.** More than one is safe (jobs are claimed with `FOR UPDATE SKIP LOCKED`), but there is no autoscale rule.
- **The file scan is a basic check, not antivirus.** Turn on Microsoft Defender for Storage for the account.
- **Public network access** to PostgreSQL is on, restricted by firewall rules plus "allow Azure services". For real data, use private networking.
- **Prototype data rules.** Do not load real student data until the privacy points in `transcript-parsing.md` and `ai-pipeline.md` are resolved.
