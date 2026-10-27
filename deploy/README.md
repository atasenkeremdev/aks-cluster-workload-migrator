# Jenkins build and AKS hosting with Microsoft user sign-in

This setup builds/tests/pushes the image from your chosen Jenkins agent. The AKS
Deployment presents **Sign in with Microsoft** and runs AKS/Azure IAM operations
using the signed-in user's own permissions, not a shared service principal.
The pipeline does not deploy or grant Azure permissions automatically.

## 1. Jenkins configuration

Create a Pipeline from SCM using this repository's `Jenkinsfile`. Use a **Linux**
agent with Git, a running Docker daemon and BuildKit. Add a Jenkins **Username
with password** credential for the internal registry.

| Parameter | Value |
| --- | --- |
| `AGENT_LABEL` | Your agent label/expression, e.g. `linux && docker` |
| `REGISTRY_HOST` | Registry hostname[:port], without a URL scheme |
| `IMAGE_REPOSITORY` | Repository path, e.g. `platform/aks-migrator` |
| `REGISTRY_CREDENTIALS_ID` | Jenkins registry username/password credential ID |
| `IMAGE_TAG` | Optional tag; default is `<build-number>-<commit-sha>` |
| `PYTHON_IMAGE` | Python 3.12 Debian Bookworm base, optionally an internal mirror/digest |
| `KUBECTL_VERSION` | A kubectl version within one minor of BOTH source and target AKS APIs |
| `KUBELOGIN_VERSION` / `AZURE_CLI_VERSION` | Pinned tool versions |
| `BUILD_CA_CREDENTIALS_ID` | Optional Jenkins Secret file: trusted build proxy CA bundle |

The pipeline checks out the project, logs in with `--password-stdin`, builds the
Docker `test` target (Python unit tests), builds the release image and pushes it.
The release image includes Azure CLI, kubectl and kubelogin, but not tests or
workstation credentials. Registry authentication uses a private temporary
`DOCKER_CONFIG`, deleted after the build. `image-reference.txt` and
`image-digest.txt` are archived after push. No implicit `latest` tag is published.

Builds need the Python base, Debian repositories, PyPI, `dl.k8s.io` and Azure's
kubelogin GitHub release assets. SHA-256 checksums verify the downloaded binaries.
Apt, pip and TLS verification remain enabled. Standard proxy environment variables
are forwarded through Docker's predefined proxy arguments, not image ENV.
The optional `build_ca` BuildKit secret supplies a trusted CA bundle only while
installing/downloading dependencies; it is not baked into the image. A custom
runtime CA should be provided through your trusted base image. Configure the
Docker daemon's CA trust for your internal registry separately.

## 2. Microsoft sign-in model

This is **Microsoft device-code authentication**, using the same Microsoft Azure
CLI public client as an interactive `az login --use-device-code`:

1. Open the app and select **Sign in with Microsoft**.
2. Open the Microsoft link and enter the one-time code shown by the app.
3. Sign in to the configured Entra tenant and complete your organization's MFA.
4. Return to the app. Its private browser session now uses your own Azure account.

Microsoft can show **Microsoft Azure CLI** as the application being authorized.
No Azure password is entered in the app, and no service-principal client secret
or new Entra web-app registration is needed. The tenant must permit device-code
sign-in. If Conditional Access blocks this flow, do not weaken that policy: use
a separately implemented Entra web-app authorization-code/redirect flow instead.
This implementation is intended for a private administrative operator UI, not a
public multi-tenant application. There is no automatic service-principal fallback.

Each browser login has a separate, private Azure CLI cache on ephemeral storage.
Both Azure IAM commands and `kubelogin --login azurecli` receive that session's
`AZURE_CONFIG_DIR`. Background jobs keep the initiating session context. Jobs,
preview tokens and workload/IAM history records are owned by tenant + user object
ID; another user cannot read or consume them. Names and object IDs come from the
completed Microsoft sign-in, not caller-provided request identity fields.

Kubeconfig **connection metadata** is shared, never its original admin/user login.
On startup all original user tokens, admin client certificates/keys, passwords,
legacy auth-provider credentials and exec environments are discarded. Each context
uses a fresh kubelogin exec entry that obtains the signed-in user's AKS token.
The server also applies this stripping when started directly in signed-in mode.
Your existing AKS RBAC/Azure permissions still govern every operation. Signing in
does not grant access or copy your workstation's token cache.
Both AKS clusters must support Microsoft Entra user authentication, and your user
must have the required Kubernetes/Azure roles. Access that only worked locally
through an admin certificate will not carry over. This setup does not enable
Entra integration or modify cluster authorization settings automatically.

Sessions last up to eight hours and must sign in again after app restart. Signing
out deletes that session's cache and blocks new requests; it cannot undo requests
already accepted by Azure/Kubernetes. No access/refresh tokens are returned to the
browser or stored in browser localStorage. Only an opaque HttpOnly, SameSite
session cookie is used. HTTPS origins use Secure cookies. User profiles, device
challenges and directory lookup data are not written to application logs.

## 3. Set tenant and permitted users

From your existing local Azure CLI session, obtain these metadata values:

```sh
az account show --query tenantId -o tsv
az ad signed-in-user show --query id -o tsv
```

Set in `deploy/aks.yaml`:

- `MIGRATOR_TENANT_ID`: your Entra tenant GUID.
- `MIGRATOR_ALLOWED_OBJECT_IDS`: your user object GUID, or comma-separated allowed
  user GUIDs. An empty value permits other users from that tenant, each with only
  their own Azure permissions; configure an explicit list for the operator app.
- `MIGRATOR_PUBLIC_ORIGIN`: the exact browser origin. For the default port-forward
  setup use `http://localhost:8081`; for an approved HTTPS endpoint use its exact
  `https://...` origin, without a trailing slash/path.

HTTP origins are accepted only for localhost/127.0.0.1 development/port-forward.
An authenticated user's display name and **Sign out** appear in the workspace.
The current preset is for Azure public-cloud AKS. The standard AKS server audience
is preserved from kubeconfig where present; certificate-only configs use the
public-cloud AKS audience. `MIGRATOR_AKS_SERVER_ID` can supply a deliberate alternate
audience, but sovereign-cloud authentication is not validated here.

## 4. Export connection metadata and create Secrets

From the checked-out repository on your workstation, use the helper to export
only your intended contexts. It captures `kubectl config view --raw --flatten`
privately and strips credentials **before writing the export file**:

```sh
python3 -m container.export_connections \
  --tenant YOUR_ENTRA_TENANT_GUID \
  --context YOUR_SOURCE_CONTEXT --context YOUR_TARGET_CONTEXT \
  --output /secure/path/migrator-connections.json
```

On Windows use `py -3 -m container.export_connections` with the same parameters.
The output is UTF-8 JSON (accepted as kubeconfig), without user certificate/token
material. Existing output files are not overwritten. It contains private endpoint
names and trusted CA data, so use an appropriate local directory. Exported
connections must retain TLS verification; embedded URL passwords and
`insecure-skip-tls-verify` are rejected.

Run the following against the **hosting cluster**, using its explicit `--context`
if necessary:

```sh
kubectl create namespace aks-migrator
kubectl -n aks-migrator create secret generic migrator-kubeconfig \
  --from-file=config=/secure/path/migrator-connections.json
```

A regular flattened kubeconfig can also be mounted; the container/server strips
its original user credentials before use. The export helper avoids storing those
credentials in the hosting cluster at all. No Azure service-principal Secret is
required for this user sign-in model.

Create the image pull Secret from a self-contained registry Docker configuration
provided by your credential manager (`auths` with authentication, not a local
`credsStore` helper reference):

```sh
kubectl -n aks-migrator create secret generic internal-registry-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=/secure/path/registry-config.json
```

This is separate from Jenkins Credentials. You can change `imagePullSecrets` to
an existing pull Secret available in the deployment namespace.

## 5. Deploy and connect

Edit `images.newName` and `images.newTag` in `deploy/kustomization.yaml` using the
Jenkins image artifact. For immutable deployment, replace `newTag` with
`digest: sha256:...`. Adjust the PVC storage class if the cluster has no suitable
default StorageClass.

```sh
kubectl apply -k deploy
kubectl -n aks-migrator rollout status deployment/aks-migrator
kubectl -n aks-migrator port-forward service/aks-migrator 8081:8081
```

Open **http://localhost:8081**, matching `MIGRATOR_PUBLIC_ORIGIN`, and sign in. Keep
port 8081 locally: the origin/Host checks reject other browser origins unless you
explicitly configure them. `/health` verifies process readiness, not Azure access.

The pod must reach Microsoft sign-in/management/Graph and both AKS API endpoints,
including private DNS/routes. Neither kubeconfig nor Microsoft login supplies
missing private networking. Existing user permissions remain necessary: AKS
namespace/workload read and write rights for migration/scaling, Azure IAM read
rights for inventory, and `roleAssignments/write` at the target for IAM sync.
Directory names are optional when Graph read permissions are unavailable.

The default Service is ClusterIP and the NetworkPolicy denies ordinary pod
inbound access when enforced by the CNI. Authorized Kubernetes port-forward is the
initial operator access path; no public ingress is created. If your CNI blocks
port-forward, adapt the ingress policy to an approved operator path. For an HTTPS
reverse proxy, preserve the original Host header, set the exact public origin and
permit only that proxy through the NetworkPolicy. Do not trust arbitrary forwarded
headers or disable the origin check.

The pod runs non-root with a read-only root filesystem. `/tmp` is an ephemeral
volume for private user profiles, connection copies and temporary patch files.
Workload/IAM history and backups use the PVC. Azure caches must not go on that PVC.
One replica with `Recreate` is intentional: sessions/jobs/previews live in process
memory. Multi-replica routing requires a different shared session/job design.
Existing history records from unauthenticated local mode are retained on disk but
are not automatically assigned or shown to signed-in users.

Restart after connection/configuration changes. Sign in and re-analyze afterward;
old previews/running jobs are not restored. For a private offline/certificate-based
smoke test only, explicitly choose `MIGRATOR_AUTH_MODE=local` and
`MIGRATOR_AZURE_AUTH=none`. That mode has no Microsoft gate and must not be exposed
as a shared administrative service.

## Validation boundaries

```sh
docker build --target test -t aks-migrator:test .
docker build --target release -t aks-migrator:local .
```

The image test target runs Python unit tests. Optional Playwright tests are a
separate development check. Simulated sign-in tests verify challenge display,
session isolation, owner checks and logout without real credentials. A real
Microsoft/MFA login and your organization's AKS/ARM access must be validated after
supplying your tenant, allowed user IDs, network access and connection Secret.
Registry push runs in your configured Jenkins; no registry credentials are baked
into the image or required for the local test build.
