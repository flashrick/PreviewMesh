# Manual installation reference

[Guided installer](../../README.md#install)

[Preview access guide](access.md)

This document is the manual path. The guided `install` command may prepare a local source notification file, but it does not commit, push, create or merge source changes. For the guided onboarding flow, run `bash scripts/setup.sh onboard-source --config PATH --source OWNER/REPO`; its default mode shows the complete diff only, and `--create-pr` is the explicit reviewed-PR mode. The manual steps below remain available when you want to copy and edit the workflow yourself.

## Requirements

For local checks, install Git, Go 1.25 or newer, Python 3, Bash, and the GitHub CLI. For real previews, also install K3s with Traefik, Helm 3, and `kubectl` on a Linux or WSL2 machine. For `onboard-source`, a classic PAT used by `gh auth login` needs the `repo` and `workflow` scopes. A fine-grained PAT needs access to the target repository plus **Contents: Read and write**, **Workflows: Read and write**, and **Pull requests: Read and write** permissions; see GitHub's [permissions required for fine-grained personal access tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).

The guided installer checks for Ubuntu's `python3-yaml` package during its first stage and installs it only when missing; an existing compatible module is reused. Standalone installer tests and local checks that exercise the Traefik comparison need it too: `sudo apt-get update && sudo apt-get install -y python3-yaml`.

### Set project variables once

Set these paths, repository names, and the source default branch once in the current terminal. Replace the examples with your own values. Later steps reuse them. Setting variables does not create directories, files, or repositories.

```bash
export PUBLIC_REPOSITORY=flashrick/PreviewMesh
export CONTROL_REPOSITORY=YOUR_GITHUB_OWNER/previewmesh-control
export SOURCE_REPOSITORY=YOUR_GITHUB_OWNER/your-application
export SOURCE_DEFAULT_BRANCH=main
export TEMPLATE_DIR="$HOME/workspace/PreviewMesh"
export CONTROL_DIR="$HOME/workspace/previewmesh-control"
export SOURCE_DIR="$HOME/workspace/your-application"
export PREVIEWMESH_RUNNER_CONFIG="$CONTROL_DIR/config/previewmesh-runner.yaml"
```

In a new terminal or on another machine, repeat this block with the correct local paths. To resume an existing setup, also repeat the repository ID lookup in step 3, the `KUBECONFIG` selection in step 5, and the PR variable block in step 8; you do not need to recreate repositories or redeploy. When changing sources, update the source path, default branch, and repository ID too. When changing PRs, recalculate the namespace and hostname.

### Download the public template

Before running any `scripts/...` commands, download the public template. `TEMPLATE_DIR` holds the setup scripts; `CONTROL_DIR` is the separate private checkout created in step 1. Use different directories. Install Git first if it is missing.

```bash
# Create the parent directory for the public template checkout.
mkdir -p "$(dirname "$TEMPLATE_DIR")"
# Download the public template and its setup scripts.
git clone "https://github.com/$PUBLIC_REPOSITORY.git" "$TEMPLATE_DIR"
# Enter the template checkout before running its scripts.
cd "$TEMPLATE_DIR"
```

If you already cloned this public template, set `TEMPLATE_DIR` to that checkout and run only `cd "$TEMPLATE_DIR"`. Confirm that `scripts/create-control-repository.sh` exists there before continuing. Keep using this terminal so the project variables remain available.

### Check local tools

Run these scripts from a downloaded template or control checkout. They do not install software, change GitHub, or deploy applications; missing tools stop the check.

Run this in Bash to list missing commands and inspect installed versions. Go must be 1.25 or newer, and `gh auth status` must show an authenticated account:

```bash
scripts/check-environment.sh local
```

### Check live-preview requirements

On the K3s operator machine, this checks Helm 3, `kubectl`, cluster access, and the Traefik IngressClass. Use the operator's kubeconfig for these read checks; keep the runner on its restricted kubeconfig from setup step 5. The Helm version output must start with `v3`.

```bash
scripts/check-environment.sh cluster
```

The self-hosted runner must have the labels `self-hosted`, `Linux`, `X64`, and `previewmesh`, plus Go, Python, Helm, and `kubectl` on its `PATH`. Run the following check after creating the control checkout and configuring the runner in step 5. Check tools under the same account and environment as the runner service because its `PATH` can differ from an interactive shell:

```bash
"$CONTROL_DIR/scripts/check-environment.sh" runner
```

The normal image build runs on a GitHub-hosted runner, so Docker is only needed locally for optional manual image builds.

## First setup

Complete these steps in order. Commands that change GitHub or K3s are operator actions; inspect the target repository and cluster before running them.

### 1. Create a private control repository

Run the helper from the public PreviewMesh template checkout. It checks GitHub authentication, clones the public template into `CONTROL_DIR`, creates the destination repository as **Private**, adds the public template as the `upstream` remote, and pushes `main` to the new control repository. It stops if the GitHub repository already exists or if the local target directory is non-empty; it never removes an existing directory.

```bash
# Enter the downloaded public template checkout.
cd "$TEMPLATE_DIR"
# Create the private control repository using the downloaded setup script.
scripts/create-control-repository.sh \
  --source "$PUBLIC_REPOSITORY" \
  --repository "$CONTROL_REPOSITORY" \
  --directory "$CONTROL_DIR"
# After the script succeeds, enter the new private checkout for the next steps.
cd "$CONTROL_DIR"
```

The account used by `gh` must be allowed to create private repositories and push workflow files. Run `gh auth login --hostname github.com` first. If your login lacks permission to push workflows, refresh it with `gh auth refresh --scopes workflow`. See the [GitHub CLI login documentation](https://cli.github.com/manual/gh_auth_login).

After the script finishes, the control repository's default branch must be `main`, Actions must be enabled, and no self-hosted runner should be registered with the public template repository.

### 2. Enable the private workflow

The public copy is intentionally inert. In the private control repository, create the repository Actions variable:

```bash
# Enable the preview lifecycle workflow in the private Control repository.
gh variable set PREVIEWMESH_ENABLED --repo "$CONTROL_REPOSITORY" --body true
```

This command saves the Actions variable `PREVIEWMESH_ENABLED=true` in the repository named by `CONTROL_REPOSITORY`. The preview workflow checks this switch before running its build, local deployment/cleanup, and report jobs. The value must be exactly `true`; if missing or set to another value, those jobs are skipped. Setting it does not start a preview run; a PR notification or manual dispatch starts the workflow. Do not create this variable in the public template repository.

### 3. Register a source repository

Before editing the registry, choose the source repository and get its numeric ID from GitHub; do not infer it from the repository URL:

```bash
REPOSITORY_ID=$(gh api "repos/$SOURCE_REPOSITORY" --jq '.id')
export REPOSITORY_ID
printf '%s\n' "$REPOSITORY_ID"
```

The tracked `config/repositories.json` is empty by design. Copy the generic example and edit it in the private control checkout, using the ID and repository name from above. This uses `nano`; replace it with an editor installed on your machine if needed:

```bash
cp config/repositories.example.json config/repositories.json
nano config/repositories.json
```

Each entry contains an immutable repository ID, the exact GitHub owner/name, the application's HTTP port, and the name of one source-access Secret. Set `port` to the port the application actually listens on. PreviewMesh uses it for the container and its ClusterIP Service; it does not reserve a port on the K3s node. Each preview runs in its own namespace, so multiple previews can use port 8080 without a node-port conflict:

```json
[
  {
    "repository_id": "123456789",
    "source_repository": "YOUR_GITHUB_OWNER/your-application",
    "port": 8080,
    "source_secret": "SOURCE_APP"
  }
]
```

Give every source repository a unique `source_secret` name, such as `SOURCE_APP_ONE`. In setup step 6, the helper prompts for that repository's token and saves it as an Actions secret with this name in the private control repository. Put the secret name in this file; never put the token value here.

Compile and validate the registration:

```bash
# Create a directory for the compiled command-line tools.
mkdir -p bin
# Compile the registration, PR inspection, and GitHub reporting tool.
go build -o bin/control ./cmd/control
# Compile the image, deployment, verification, and cleanup tool.
go build -o bin/previewmesh ./cmd/previewmesh
# Check the source registration without calling GitHub.
./bin/control resolve \
  --repository-id "$REPOSITORY_ID" \
  --source-repository "$SOURCE_REPOSITORY" \
  --pr 1
# Stage the private source registration for the next commit.
git add config/repositories.json
# Record the source registration in Git.
git commit -m "Configure preview source repository"
# Push the private control configuration to its main branch.
git push origin main
```

The example `--pr 1` only checks the number format; it does not require that pull request to exist.

If `resolve` prints `repository not registered with this exact identity`, check that `config/repositories.json` contains exactly one entry with both the same numeric repository ID and the exact `OWNER/REPO` name. The public registry is empty by design, so populate the registry in this private control checkout before running the command.

### 4. Prepare the source application

The source repository must contain a root `Dockerfile` that builds a `linux/amd64` image. The application must:

- listen on `0.0.0.0` at the registered port;
- run as UID/GID `65532` without extra capabilities; and
- return HTTP 200 from `GET /health` with this shape:

```json
{"status":"ok","commit_sha":"0123456789abcdef0123456789abcdef01234567"}
```

The returned `commit_sha` must come from the `PREVIEW_COMMIT_SHA` environment variable. PreviewMesh injects the requested source SHA during deployment; do not hardcode the example value. PreviewMesh does not modify the source application: if it does not already return this health response, you must adapt the application before it can pass revision verification.

Revision verification is exact: the build rejects a checkout whose `HEAD` differs from the pull-request SHA, and the deployment records both `requested_sha` and `served_sha`. The preview is reported ready only when `/health` returns the requested SHA and the workflow's result check confirms that both values match.

#### Run the application preflight

Before configuring K3s, build the CLI outside the source checkout and inspect the application with a clean Git checkout. The command is `previewmesh preflight --source-dir PATH --port PORT [--sha SHA]`; `--sha` is optional and defaults to the checkout's `HEAD`. When supplied, it must be a full lowercase Git SHA.

```bash
# Build the CLI outside the application checkout.
cd "$CONTROL_DIR"
go build -o /tmp/previewmesh ./cmd/previewmesh
# Keep JSON reports outside SOURCE_DIR because ignored files count as dirty.
PREFLIGHT_DIR=$(mktemp -d /tmp/previewmesh-preflight.XXXXXX)
EXPECTED_SHA=$(git -C "$SOURCE_DIR" rev-parse HEAD)
# Static mode is the default and does not call Docker.
/tmp/previewmesh preflight \
  --source-dir "$SOURCE_DIR" \
  --port 8080 \
  --sha "$EXPECTED_SHA" \
  > "$PREFLIGHT_DIR/static.json"
```

Replace `8080` with the registered application port when it differs; use the same value for a direct `previewmesh build` invocation.

Static mode checks the port range, Git root and revision, and a clean checkout using `--untracked-files=all --ignored`; it therefore includes ignored build output and temporary files. It inspects the regular root `Dockerfile`, its final-stage `FROM`, `EXPOSE` and `USER` declarations, and bounded source markers for `0.0.0.0`, `/health`, `PREVIEW_COMMIT_SHA` and `commit_sha`. The scan is a source heuristic. It cannot prove the listener, actual HTTP response, environment propagation, file permissions or cluster behavior.

The JSON report contains the source SHA, a configuration fingerprint, the UTC check time, port and contract, `static`, `container`, `deployment`, and `findings`. Findings are either `blocker` or `warning`, and each includes a repair direction. A blocker exits nonzero and prevents a build; warnings leave static status as `passed_with_warnings` (the static-limit warning is always present because source scanning cannot prove runtime behavior) and should be reviewed before deployment. The report contains controlled diagnostics, not source snippets or external tool logs. Keep both the report and any generated CLI outside `SOURCE_DIR`.

The runtime check is opt-in. Add `--container-check` only when Docker is available and you want a local startup check:

```bash
# Build and run a temporary local image/container; it is not pushed or deployed.
/tmp/previewmesh preflight \
  --source-dir "$SOURCE_DIR" \
  --port 8080 \
  --sha "$EXPECTED_SHA" \
  --container-check \
  > "$PREFLIGHT_DIR/container.json"
```

This mode builds and runs `linux/amd64`, passes `PREVIEW_COMMIT_SHA`, binds an ephemeral loopback-only host port and checks `/health`. The local container uses UID/GID `65532:65532`, `cap-drop ALL`, `no-new-privileges`, a 512 MiB memory limit, one CPU and 128 processes. `--timeout` defaults to five minutes per external operation and `--http-timeout` to one minute for startup/health; both values are recorded in `container_configuration`, which is included in the configuration fingerprint. If static inspection fails first, Docker is not called and `container` is reported as `not_run_static_failed`. PreviewMesh removes the temporary image and container after the check. A successful result proves only local startup and health behavior; it does not cover cluster networking, mounts, ingress or a real deployment, and it does not publish an image or preview.

The normal `previewmesh build` path repeats the static inspection on the exact clean checkout before its Docker build and push. Pass the same `--port PORT` used by preflight (the default is `8080`) so the report matches the registered application port. It reports static warnings in the build result and stops before publishing when a static blocker is found. It does not run `--container-check` automatically. Neither static nor container preflight is evidence that a preview has been deployed; continue with the ordinary test PR and live `/health` verification in step 8.

### 5. Configure K3s and the runner

Run this step on the K3s server, using the Linux account that will run the GitHub runner. It needs `sudo` access for K3s administration, plus Python 3 and `kubectl`. This example keeps the runner on the same machine as K3s and uses the `CONTROL_DIR` and `PREVIEWMESH_RUNNER_CONFIG` set above.

Using the configured path, exclude credentials from Git and apply the restricted runner permissions:

```bash
# Enter the private control checkout.
cd "$CONTROL_DIR"
# Create the directory that will hold the ignored runner kubeconfig.
mkdir -p "$(dirname "$PREVIEWMESH_RUNNER_CONFIG")"
# Keep credentials and temporary files out of Git, including older checkouts.
grep -qxF '/config/previewmesh-runner.yaml' .gitignore || printf '\n/config/previewmesh-runner.yaml\n' >> .gitignore
grep -qxF '/config/.runner-*' .gitignore || printf '\n/config/.runner-*\n' >> .gitignore
# Confirm that the K3s node is reachable with the administrator kubeconfig.
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml get nodes
# Install or update the restricted Kubernetes permissions for the Runner.
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml apply -f ops/kubernetes/runner-rbac.yaml
```

Generate a restricted kubeconfig and check that it can create/delete namespaces and create Secrets, while ClusterRole creation is denied. The script stores only the cluster address, CA, and runner token; it does not copy administrator credentials or print the token. Run it as the runner's normal user:

```bash
# Generate or renew the restricted Runner kubeconfig and verify its permissions.
python3 scripts/configure-runner.py
# Use the generated kubeconfig for kubectl commands in this shell.
export KUBECONFIG="$PREVIEWMESH_RUNNER_CONFIG"
```

Continue only after `Created runner kubeconfig` appears. The script writes a temporary file with mode `600` and replaces the configuration only after all permission checks pass; failures preserve the existing file. The output must be an absolute, untracked, Git-ignored path inside the control checkout, and temporary `.runner-*` files must also be ignored. The default path already meets these requirements.

The requested token lifetime is 24 hours, subject to API server policy. Rerun `python3 scripts/configure-runner.py` before expiry to renew it; renewal is not automatic. Never give the runner the K3s administrator kubeconfig. For a runner on another machine, the cluster address must be reachable and covered by the API server certificate. See [K3s cluster access](https://docs.k3s.io/cluster-access) and [`kubectl create token`](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_create/kubectl_create_token/).

In GitHub, open the repository named by `$CONTROL_REPOSITORY`, then go to **Settings → Actions → Runners → New self-hosted runner**. Follow the Linux x64 instructions in a new directory on the K3s machine, and add the `previewmesh` label when prompted. The runner must be registered to this exact control repository because the `local` job is queued there; a runner registered to another private repository cannot accept its job. Ensure Go, Python, Helm, and `kubectl` are available to the runner account. After registration, install the runner as a service and explicitly give it the restricted kubeconfig. Run the following in the runner directory, keeping the project variables set in this shell. If the service is already installed, skip `svc.sh install` and use `sudo ./svc.sh stop` before starting it again.

```bash
# Install the GitHub Runner as a system service.
sudo ./svc.sh install "$(id -un)"
# Read the systemd service name created by the Runner installer.
RUNNER_SERVICE=$(cat .service)
# Create a systemd drop-in directory for the Runner configuration.
sudo mkdir -p "/etc/systemd/system/${RUNNER_SERVICE}.d"
# Tell the Runner service to use the restricted kubeconfig.
printf '[Service]\nEnvironment="KUBECONFIG=%s"\n' "$PREVIEWMESH_RUNNER_CONFIG" \
  | sudo tee "/etc/systemd/system/${RUNNER_SERVICE}.d/previewmesh.conf" >/dev/null
# Reload systemd after adding the Runner environment override.
sudo systemctl daemon-reload
# Start the GitHub Runner service.
sudo ./svc.sh start
```

Return to the terminal where you configured the project variables in the private control checkout. Run the following there to verify that the runner appears online with the required labels. If that terminal has been closed, first restore the variables using [the project variable setup](#set-project-variables-once); changing directories alone does not restore them.

```bash
# In the original control-repository terminal, confirm the target repository.
printf 'CONTROL_REPOSITORY=%s\n' "$CONTROL_REPOSITORY"
# List the Control repository's registered Runner and its labels.
gh api "repos/$CONTROL_REPOSITORY/actions/runners" \
  --jq '.runners[] | {name, status, labels: [.labels[].name]}'
```

The runner must show `status: online` and the labels `self-hosted`, `Linux`, `X64`, and `previewmesh`. The `export` above only affects this shell and its child processes: for a foreground runner, start `./run.sh` from this shell; for a service, set `KUBECONFIG` to the printed absolute file path in the runner service environment and restart the service. The service account must be able to read the file and traverse its parent directories.

For a new terminal, follow [the project variable setup](#set-project-variables-once). See [the Kubernetes runner notes](../kubernetes/README.md).

### 6. Configure GitHub Secrets

The setup script uploads tokens; it does not create them. If you registered one source repository, create the three tokens below before running it. For additional sources, create one source token per repository. Set an expiration date and renew the saved secret before it expires.

| Secret name | Repository the token can access | Permissions | Save the secret in |
| --- | --- | --- | --- |
| The `source_secret` from each registry entry, such as `SOURCE_APP` | That source repository only | Contents: Read-only; Metadata: Read-only; Pull requests: Read and write; Commit statuses: Read and write | Control repository |
| `PREVIEWMESH_DISPATCH_TOKEN` | Control repository only | Actions: Read and write; Metadata: Read-only | Every registered source repository |
| `GHCR_READ_TOKEN` | Packages containing the preview images | A **classic** PAT with `read:packages`, owned by a user who can read those packages | Control repository |

`SOURCE_APP` is just a secret name; it does not mean you need to create a GitHub App. Create a separate source token for each registry entry. The dispatch token calls `workflow_dispatch`, which requires [Actions write permission](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event). GHCR requires a [classic PAT for this login](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry); a fine-grained token cannot replace it. If your organization requires token approval or SSO authorization, complete that before continuing.

#### Create the source token

Purpose: lets the Control workflow read Source code and PR information, and post status updates and preview links to the Source PR. Save it in the Control repository under the registered secret name, such as `SOURCE_APP`.

Open the [fine-grained token creation page](https://github.com/settings/personal-access-tokens/new), or go to your personal **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.

1. Set **Token name** to a descriptive label, such as `previewmesh-source`. This label is for your own reference; it does not have to match the Actions secret name.
2. Choose an **Expiration** date.
3. Set **Resource owner** to the owner of your source repository.
4. Under **Repository access**, choose **Only select repositories**, then select the **Source repository** named by `SOURCE_REPOSITORY` (the repository containing your application code). Do not select the Control repository here.
5. Under **Repository permissions**, set **Contents: Read-only**, **Pull requests: Read and write**, and **Commit statuses: Read and write**. Keep **Metadata: Read-only**, which GitHub normally adds automatically.
6. Click **Generate token** and copy the generated value into a password manager so you can paste it when the script asks for it. Do not put it in `config/repositories.json`.

For an entry with `source_secret: SOURCE_APP`, paste this value at `Paste SOURCE_APP for OWNER/APPLICATION`. If you chose another secret name, the prompt uses that name. Repeat for each registered source. See [GitHub's token creation guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

#### Create the dispatch token

Purpose: lets the Source notification workflow trigger the Control workflow when a PR changes. Save it in each Source repository as `PREVIEWMESH_DISPATCH_TOKEN`.

Open the [fine-grained token creation page](https://github.com/settings/personal-access-tokens/new) again.

1. Set **Token name** to `previewmesh-dispatch` and choose an **Expiration** date.
2. Set **Resource owner** to the owner of your control repository.
3. Under **Repository access → Only select repositories**, select `previewmesh-control` (or your chosen control repository name).
4. Under **Repository permissions**, set **Actions: Read and write** and keep **Metadata: Read-only**.
5. Click **Generate token** and save the generated value in your password manager.

Paste this value at `Paste PREVIEWMESH_DISPATCH_TOKEN (control Actions write only)`. The script saves the same dispatch token in every registered source repository so each can request a control run.

#### Create the GHCR read token

Purpose: lets K3s download private preview images from GHCR to run the application. Save it in the Control repository as `GHCR_READ_TOKEN`.

Open the [classic token creation page](https://github.com/settings/tokens/new), or go to **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)**.

1. Set **Note** to `previewmesh-ghcr-read` and choose an **Expiration** date.
2. Under scopes, select **`read:packages`**. This token only needs to pull images; leave `write:packages` and `delete:packages` unchecked.
3. Click **Generate token** and save the generated value in your password manager.

Paste this value at `Paste GHCR_READ_TOKEN (classic PAT, read:packages only)`. The user who creates it must have read access to the preview image packages; selecting the scope does not grant access to someone else's private packages. Complete SSO authorization if your organization requires it. See [GHCR authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

#### Upload and check the secrets

Return to the control checkout and reuse the repository variables set above. The account logged into `gh` must be allowed to manage Actions secrets in both repositories. Its login is used to upload secrets; the tokens you paste are the credentials the workflows will use later.

```bash
# Enter the private control checkout.
cd "$CONTROL_DIR"
# Confirm that the GitHub CLI is authenticated.
gh auth status
# Upload the source, dispatch, and GHCR Actions Secrets.
bash scripts/configure-github-secrets.sh "$CONTROL_REPOSITORY" config/repositories.json
```

The script asks for each source token, then the dispatch token, then the GHCR token. Pasted text stays hidden; press Enter after each token. It saves repository Actions secrets and stops if an upload fails. Earlier uploads remain saved, so rerunning the script replaces those values.

Check the saved names without displaying their values:

```bash
# List the Secrets configured on the private Control repository.
gh secret list --repo "$CONTROL_REPOSITORY" --app actions
# List the dispatch Secret configured on the Source repository.
gh secret list --repo "$SOURCE_REPOSITORY" --app actions
```

The control list should contain every configured `source_secret` and `GHCR_READ_TOKEN`; each source list should contain `PREVIEWMESH_DISPATCH_TOKEN`. Repeat the second command for any other registered sources. This confirms the secrets were saved; the first preview run will check whether they have the right access. You can also manage them under **Repository Settings → Secrets and variables → Actions → Repository secrets**.

#### Image package ownership

Each source image is published as `ghcr.io/OWNER/previewmesh-cCONTROL_REPOSITORY_ID-rSOURCE_REPOSITORY_ID`. The workflow takes the control repository ID from GitHub and the source repository ID from the validated registration. Its `GITHUB_TOKEN` creates and publishes the private package for this control repository; `GHCR_READ_TOKEN` lets K3s pull it. No manual package creation is needed.

### 7. Configure ingress and source notifications

Choose the address scheme before running the preview steps. The [preview access guide](access.md) is canonical. The guided installer is the recommended path for a LAN address and `domain_suffix = auto` with `sslip.io`; this manual reference keeps the socket entry on `127.0.0.1` and is a local hosts-file alternative. The source repository ID and PR number are inserted into the hostname by the deployment flow. This manual path does not configure LAN forwarding, so it cannot prove that a second LAN machine can reach the preview.

Set the suffix in the control repository before a workflow uses it. The legacy `preview.test` value is suitable for the local hosts alternative below; replace it with another lowercase suffix only when you manage its DNS or hosts entries. This manual path has no setup.ini or installer `doctor` checkpoint:

```bash
export PREVIEWMESH_DOMAIN_SUFFIX=preview.test
# For a private DNS zone or another local suffix, replace the value above.
gh variable set PREVIEWMESH_DOMAIN_SUFFIX --repo "$CONTROL_REPOSITORY" \
  --body "$PREVIEWMESH_DOMAIN_SUFFIX"
```

#### Set up the HTTP entry point

PreviewMesh uses host port `18080` for preview URLs and health checks. The proxy still connects to Traefik on its internal port `80`; the application port in `config/repositories.json` is separate. A hosts entry maps an IP address to a complete hostname and contains no port. Follow the [preview access guide](access.md) for the probe and per-PR entries.

Before installing the proxy, run `ss -ltn 'sport = :18080'` in WSL and `Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue` in Windows PowerShell. Neither should show a listener. If the port is occupied, resolve the conflict before continuing.

This manual socket configuration listens on `127.0.0.1:18080`. Use it only when the Runner and browser can use that loopback entry, and map the generated hostname to `127.0.0.1` on each such machine. For LAN access through `sslip.io`, stop here and use the guided installer, which configures the LAN entry and its checks.

For K3s inside WSL with a browser on Windows, the included socket proxy forwards port 18080 on WSL's loopback address to Traefik. This requires systemd in WSL, `/usr/lib/systemd/systemd-socket-proxyd`, a free local port 18080, and working [Windows-to-WSL localhost access](https://learn.microsoft.com/en-us/windows/wsl/networking). If the executable check fails, install your distribution's package providing `systemd-socket-proxyd` before continuing.

Run these commands in the WSL control checkout. They use the administrator kubeconfig explicitly because the restricted runner cannot manage Traefik:

```bash
cd "$CONTROL_DIR"
test -x /usr/lib/systemd/systemd-socket-proxyd
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml apply -f ops/kubernetes/traefik-helmchartconfig.yaml
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n kube-system get service traefik -w
```

The HelmChartConfig keeps Traefik's Service internal, disables `publishedService` address copying, and tells Traefik to publish `127.0.0.1` in Ingress status. A ClusterIP Service has no external address to copy; leaving `publishedService` enabled would keep Ingress status empty and block readiness. PreviewMesh uses that status as a readiness signal; requests still reach Traefik through the socket proxy on port `18080`. Wait until `TYPE` shows `ClusterIP` and `CLUSTER-IP` contains an address, then press Ctrl+C to stop watching. Confirm Traefik has finished applying the chart configuration:

```bash
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n kube-system rollout status deployment/traefik --timeout=120s
```

During guided installation, stage 6 compares this `valuesContent` as YAML. It accepts the two shipped legacy profiles (service-only `ClusterIP`, or that profile plus `providers.kubernetesIngress.ingressEndpoint.ip: 127.0.0.1`) and upgrades either to the current manifest. Any other field or local customization stops the stage before apply. Review the existing object with:

```bash
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml \
  -n kube-system get helmchartconfig traefik --ignore-not-found -o yaml
```

After reviewing the setting, rerun the same guided `install` command.

Read the address again and create the local proxy configuration:

```bash
sudo install -d -m 0755 /etc/previewmesh
sudo k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n kube-system get service traefik \
  -o jsonpath='{.spec.clusterIP}{"\n"}'
sudoedit /etc/previewmesh/ingress.env
```

In the editor, add this line, replacing `TRAEFIK_CLUSTER_IP` with the address just printed, then save and exit:

```text
PREVIEWMESH_TRAEFIK_ENDPOINT=TRAEFIK_CLUSTER_IP:80
```

Install and start the socket, then check the route:

```bash
sudo install -m 0644 ops/wsl/previewmesh-ingress.socket ops/wsl/previewmesh-ingress.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now previewmesh-ingress.socket
curl --noproxy '*' --silent --show-error --max-time 5 -o /dev/null -w '%{http_code}\n' \
  -H 'Host: previewmesh-ingress-check.invalid' http://127.0.0.1:18080
```

Expect `404`: the request reached Traefik, but the check hostname has no preview route. In Windows PowerShell, run `curl.exe --noproxy "*" -I http://127.0.0.1:18080` and confirm it also reaches Traefik. If the Linux check fails, inspect `sudo journalctl -u previewmesh-ingress.service -n 30 --no-pager`. If only Windows fails, check WSL networking before moving on. Keep `/etc/previewmesh/ingress.env` local. If Traefik's ClusterIP changes, update that file and run `sudo systemctl restart previewmesh-ingress.service`.

#### Add the source notification workflow

Reuse the checkout paths set above. The destination below is the **source repository**, where your application lives:

```bash
cd "$SOURCE_DIR"
git remote -v
mkdir -p .github/workflows
cp -i "$CONTROL_DIR/templates/source-notify.yml" .github/workflows/previewmesh-notify.yml
```

Check that `git remote -v` shows the intended source repository. If the workflow already exists, review it before agreeing to overwrite it. Open `.github/workflows/previewmesh-notify.yml` in your editor and replace the two values under `jobs.notify.env`:

```yaml
PREVIEWMESH_CONTROL_OWNER: YOUR_GITHUB_OWNER
PREVIEWMESH_CONTROL_REPOSITORY: previewmesh-control
```

The owner is the part before `/` in `CONTROL_REPOSITORY`; the repository value is the part after it. Commit the file and merge or push it to the source repository's default branch using your usual review process. Enable Actions there and confirm step 6 saved `PREVIEWMESH_DISPATCH_TOKEN` before opening a test PR. Repeat this setup for each registered source.

The workflow sends a notification when a PR opens, reopens, receives a commit (`synchronize`), or closes. It skips fork PRs and only relays metadata. Keep its `pull_request_target` job free of steps that check out or execute PR code. The control workflow checks the current PR head around deployment and will not report an older revision as ready if a newer commit has replaced it.

### 8. Run a preview

In the source checkout, create a branch from the source repository's default branch. Use the `SOURCE_DEFAULT_BRANCH` set at the start of this guide. If you already have a pushed application branch with the change you want to preview, use it and skip this block.

```bash
cd "$SOURCE_DIR"
git status --short
git fetch origin "$SOURCE_DEFAULT_BRANCH"
git switch -c preview/demo "origin/$SOURCE_DEFAULT_BRANCH"
```

If `git status --short` lists uncommitted changes, commit or stash them before switching. Now make the application change you want PreviewMesh to build, then commit and push the changed file. Replace `path/to/changed-file` with its path relative to the source checkout:

```bash
git add -- path/to/changed-file
git commit -m "Add preview demo change"
git push -u origin preview/demo
```

On GitHub, choose **Pull requests → New pull request**. Set **base** to the source default branch (`main` in this example) and **compare** to `preview/demo`. Check that GitHub shows the same source repository on both sides, then create the PR. Use an account with write access. The PR tells PreviewMesh which application revision to build. Once the step 7 notification workflow is on the source default branch and Actions is enabled, opening it sends the preview request automatically.

In the control checkout, reuse the repository variables from setup and set the actual PR number and configured suffix. These variables are needed even when the workflow starts automatically:

```bash
cd "$CONTROL_DIR"
export PR_NUMBER=YOUR_PR_NUMBER
export REPOSITORY_ID="$(gh repo view "$SOURCE_REPOSITORY" --json databaseId --jq '.databaseId')"
export PREVIEW_NAMESPACE="pm-r${REPOSITORY_ID}-pr${PR_NUMBER}"
export PREVIEWMESH_DOMAIN_SUFFIX="$(gh variable get PREVIEWMESH_DOMAIN_SUFFIX --repo "$CONTROL_REPOSITORY" --json value --jq '.value')"
test -n "$PREVIEWMESH_DOMAIN_SUFFIX"
export PREVIEW_DOMAIN_SUFFIX="$PREVIEWMESH_DOMAIN_SUFFIX"
export PREVIEW_HOST="${PREVIEW_NAMESPACE}.${PREVIEW_DOMAIN_SUFFIX}"
go run ./cmd/control resolve \
  --repository-id "$REPOSITORY_ID" \
  --source-repository "$SOURCE_REPOSITORY" \
  --pr "$PR_NUMBER"
gh api "repos/$SOURCE_REPOSITORY/pulls/$PR_NUMBER" \
  --jq '{state: .state, base: .base.repo.full_name, head: .head.repo.full_name, sha: .head.sha}'
printf 'Preview hostname: %s\n' "$PREVIEW_HOST"
```

Confirm `resolve` accepts the registration, the PR is `open`, and both `base` and `head` name your source repository. Then follow the [preview access guide](access.md) for the local manual mapping: where the socket is reachable, add the generated full hostname with `127.0.0.1` on each hosts file. Hosts files need one entry per preview and do not support wildcard hostnames.

Look in the source repository's Actions tab for **Notify PreviewMesh**, then in the control repository for **PreviewMesh lifecycle**. A successful notification only means the request was sent. Wait for the control run to finish before checking the app.

If the PR was already open when you installed the notification, or the first attempt failed while you were setting up networking, start a run manually. First wait for any existing run for that PR to finish:

```bash
gh workflow run preview.yml --repo "$CONTROL_REPOSITORY" --ref main \
  -f repository_id="$REPOSITORY_ID" \
  -f source_repository="$SOURCE_REPOSITORY" \
  -f pr_number="$PR_NUMBER"
gh run list --repo "$CONTROL_REPOSITORY" --workflow preview.yml --limit 5
```

Copy this run's ID from the list, replacing the example below. If it is not listed yet, repeat the list command. Check the source and PR inputs on the run page. Update `RUN_ID` whenever you follow a new run:

```bash
export RUN_ID=123456789
gh run view "$RUN_ID" --repo "$CONTROL_REPOSITORY" --web
gh run watch "$RUN_ID" --repo "$CONTROL_REPOSITORY" --exit-status
```

After the control run succeeds, fetch the current PR head and compare the live response. Run this on a machine that can resolve and reach the preview hostname. Repeat the check after every new commit to refresh `EXPECTED_SHA`:

```bash
EXPECTED_SHA=$(gh api "repos/$SOURCE_REPOSITORY/pulls/$PR_NUMBER" --jq '.head.sha')
export EXPECTED_SHA
curl --noproxy '*' --fail --silent --show-error --max-time 10 "http://${PREVIEW_HOST}:18080/health" \
  | python3 -c '
import json, os, sys
result = json.load(sys.stdin)
if result.get("status") != "ok" or result.get("commit_sha") != os.environ["EXPECTED_SHA"]:
    raise SystemExit(f"Preview does not match the PR head: {result}")
print(json.dumps(result))
'
```

A passing check prints `status: ok` and the expected `commit_sha`. Open `http://${PREVIEW_HOST}:18080` if the app has a page. Close or merge the PR when finished and wait for the cleanup run; the walkthrough below shows how to confirm removal.

## End-to-end demo

After setup, reuse the PR and hosts entries from step 8 to demonstrate creation, updates, and cleanup. Choose a PR you are allowed to close. Its base and head must both belong to the registered source repository, and its author must have write access. Do not start concurrent deployments for the same PR during the demo.

### Prepare the demo

Reuse your project and PR variables. If you choose another PR, return to [step 8](#8-run-a-preview) to update its number, namespace, and hostname and check the registration and PR state. In a new terminal, restore the environment using [the project variable setup](#set-project-variables-once). Confirm that both the runner and browser can resolve the preview hostname.

### Start and follow the workflow

Open a PR or push a commit to an existing one to trigger **Notify PreviewMesh** in the source repository, followed by the control workflow. To reuse an existing commit, run step 8's manual dispatch commands; this skips the source notification and runs the same preview lifecycle. Follow it with step 8's tracking commands, updating `RUN_ID` to this run before waiting for completion.

On the Actions page, `build` validates the registration and PR and publishes an image, `local` deploys to K3s and checks the response, and `report` combines the results. The `combined-*` artifact contains the summary and CSV evidence. If the source token has the required write permissions, the PR also shows the PreviewMesh status and a result comment linking to the run.

### Verify the revision shown to the audience

Repeat the health check in [step 8](#8-run-a-preview) to fetch the current `EXPECTED_SHA` and compare the response. HTTP 200 alone is not enough: `status` must be `ok` and `commit_sha` must match. Once the check passes, open `http://${PREVIEW_HOST}:18080` to show the application page.

To demonstrate an update, wait for the current run to finish, push another commit to the same PR, wait for its `synchronize` notification and control run, and repeat the check. The URL and namespace stay the same.

### Show automatic cleanup

Close or merge the demo pull request, then follow the control run started by the `closed` notification. Wait for that cleanup run to finish before checking. Do not delete the namespace manually, since that would hide whether automatic cleanup worked. On the K3s machine, select the restricted runner kubeconfig from setup step 5 and confirm the namespace is absent:

```bash
kubectl --kubeconfig "$PREVIEWMESH_RUNNER_CONFIG" get namespace "$PREVIEW_NAMESPACE" --ignore-not-found
```

If the command exits successfully with no output, the namespace has been removed. An authentication or connection error does not confirm cleanup. Use your chosen kubeconfig path if it differs from the example. In a new terminal, also restore `PREVIEW_NAMESPACE` from step 8. To demonstrate cleanup idempotency, manually dispatch the same workflow once more for the closed pull request; the report should confirm the namespace is already absent.
