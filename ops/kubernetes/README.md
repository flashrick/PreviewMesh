# Runner Kubernetes access

PreviewMesh uses a dedicated ServiceAccount for the self-hosted runner. Apply the
manifest from the private control checkout:

```bash
sudo k3s kubectl get nodes
sudo k3s kubectl apply -f ops/kubernetes/runner-rbac.yaml
```

The runner can create, update, inspect, and delete the namespaces and workload
resources needed by previews. It cannot create ClusterRoles or
ClusterRoleBindings, read Pod logs, or execute commands in Pods.

## Security boundary

Use this role only on a dedicated development cluster. The ClusterRole is
cluster-scoped because previews create and remove namespaces dynamically.
Ownership labels and CLI checks protect against accidental cross-preview
operations, but they are not tenant isolation.

The manifest creates the namespace, ServiceAccount, ClusterRole, and binding. It
does not create a kubeconfig or mint a token. Do not give the runner a
cluster-admin kubeconfig.

## Runner kubeconfig

Follow [setup step 5](../../README.md#5-configure-k3s-and-the-runner) for the
complete commands to create the runner kubeconfig, protect it from Git, and check
its permissions. The example stores it in the private control checkout at
`config/previewmesh-runner.yaml`, with mode `600`, owned by the runner account.
The generated file contains the cluster address, CA, and a dedicated
ServiceAccount token; it contains no administrator credentials.

The requested token lifetime is 24 hours, subject to the API server's policy.
Rerun the generation block before expiry; renewal is not automatic. Configure
`KUBECONFIG` in the runner service environment, and restart the service when that
environment changes. An interactive shell's `export` does not update an existing
service.

The chart does not create namespaces. The trusted local job creates and labels
one namespace per repository ID and pull-request number before invoking Helm.

