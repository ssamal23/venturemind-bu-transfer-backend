# Deploy as one container (Azure Web App for Containers)

The simplest way to run the backend on Azure: **one container image runs both the API and the background worker.** A Web App for Containers runs the image; the frontend stays on Static Web Apps and calls the API over HTTPS.

**Status:** written and partly tested. The worker loop and its start and stop inside the API are unit tested. The Docker image has **not** been built or run yet (no Docker on the development machine). The `docker` job in `.github/workflows/checks.yml` builds and smoke-tests it on GitHub's servers, so run that first. The portal steps have not been tried; menu wording may differ.

## Why one container

The worker is a long-running process that polls for jobs. Web App for Containers runs one container per app and expects it to answer web requests, so a separate worker app would need a web port it does not use. Setting `EMBEDDED_WORKER=true` starts the worker as a background thread inside the API process. It stops cleanly when the app stops, and survives temporary database errors.

| Option | Runs | Good for | Cost of the choice |
|---|---|---|---|
| **One container, embedded worker** (this guide) | API and worker together | A demo or small load; one app to manage | AI work shares CPU and memory with the API; scaling scales both |
| Two apps from the same image (Container Apps) | API and worker apart | Heavier load; independent scaling | More to set up. See [deployment.md](deployment.md) |
| A virtual machine with Docker | Anything | Full control | You patch, secure and monitor the server yourself |

Several instances are safe: jobs are claimed with `FOR UPDATE SKIP LOCKED`, so two workers never take the same job.

## 1. Check the image builds

Push the backend repository. The **Checks** workflow runs two jobs: the tests that need no database, and a Docker job that builds the image, imports the app inside it, and confirms the embedding model is baked in. Do not deploy until both are green.

## 2. Create the supporting resources (portal)

| Resource | Settings |
|---|---|
| **Storage account** | Standard, LRS, StorageV2. Turn **off** "Allow Blob anonymous access". The app creates the `transcripts` and `syllabi` containers on first upload |
| **Container Registry** | Basic. Leave the admin user **disabled** |
| **Limited database user** | Run `python scripts/create_app_user.py` once (see [deployment.md](deployment.md) section 4). The app must not use the admin account |

## 3. Create the Web App

| Field | Value |
|---|---|
| Publish | **Container** |
| Operating system | **Linux** |
| Region | The same as the database |
| Pricing plan | **Basic B2** or larger. The embedding model needs memory; the free and smallest tiers are not enough. Check the portal for the price |
| Container image | If the form needs an existing image, choose the **Quickstart** sample and replace it in the next step |

## 4. Build and deploy from GitHub

Open the Web App, then **Deployment Center**.

1. Source: **GitHub Actions**. Choose `ssamal23`, `venturemind-bu-transfer-backend`, `main`.
2. Registry: the Container Registry you created. Image name: `bu-api`.
3. Save. Azure adds a workflow file to the repository that builds the `Dockerfile`, pushes the image, and deploys it. It also grants the app permission to pull from the registry.

Run `git pull --rebase` in the backend folder before your next push, because Azure adds a commit.

## 5. Let the app use storage (no keys)

1. Web App, **Identity**, **System assigned**: **On**, Save.
2. Storage account, **Access control (IAM)**, **Add role assignment**: role **Storage Blob Data Contributor**, assign to **Managed identity**, choose the Web App.

Wait a few minutes for the role to take effect.

## 6. Settings

Web App, **Environment variables** (also called app settings). Mark secrets as such.

| Name | Value |
|---|---|
| `ENVIRONMENT` | `production` (the app then refuses an unsafe configuration) |
| `WEBSITES_PORT` | `8000` |
| `EMBEDDED_WORKER` | `true` |
| `DATABASE_URL` | `postgresql+psycopg://bu_app:<password>@<server>.postgres.database.azure.com:5432/transfer?sslmode=require` |
| `JWT_SECRET` | A random value of 32+ characters: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | Your keys |
| `STORAGE_BACKEND` | `azure` |
| `AZURE_STORAGE_ACCOUNT_URL` | `https://<storage-account>.blob.core.windows.net` |
| `CHECKPOINT_AUTO_SETUP` | `false` (the limited user cannot create tables) |
| `CORS_ORIGINS` | `["https://<your-site>.azurestaticapps.net"]` |

Do not also turn on the platform's own CORS setting; the app handles CORS itself.

Under **Configuration**, **General settings**, turn on **Always on**, **HTTPS Only**, and set the **Health check** path to `/api/health`. Save, which restarts the app.

## 7. Check it

- `https://<app>.azurewebsites.net/api/health` returns `{"status":"ok"}`.
- **Log stream** shows `Embedded worker started`.
- The first start can take a minute while the container starts.

## 8. Point the frontend at it

In the frontend repository's Static Web Apps workflow, set the build variable and push:

```yaml
        env:
          VITE_API_BASE: https://<app>.azurewebsites.net/api
```

The site's security headers already allow `*.azurewebsites.net`.

## 9. Lock it down

The demo accounts use the password `demo1234`, and this API has a public address. Before sharing the link:

- Web App, **Networking**, **Access restriction**: allow only your team's IP addresses. The browser calls the API directly, so this protects the data even though the site is public.
- If your IP changes you lose access to the API too; add the new one.

## Known limits

- **Not run yet:** the image build, the Azure deployment, and managed-identity sign-in to storage.
- **The API documentation page** (`/docs`) is public unless restricted.
- **A deploy interrupts a running job.** It is retried automatically after ten minutes.
- **Memory.** The first AI job loads the embedding model (about 90 MB, baked into the image). Watch memory on small plans.
- **No Key Vault, no private networking.** Settings are stored encrypted by the platform, and the database is reached over its public address with firewall rules. Do not load real student data in this setup.
