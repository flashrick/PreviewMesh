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

Create a kubeconfig outside the repository from the cluster CA data and a
short-lived ServiceAccount token:

```bash
sudo k3s kubectl -n previewmesh-system create token previewmesh-runner --duration=24h
chmod 600 /absolute/path/previewmesh-runner.yaml
export KUBECONFIG=/absolute/path/previewmesh-runner.yaml
kubectl auth can-i create namespaces
kubectl auth can-i delete namespaces
kubectl auth can-i create secrets --all-namespaces
kubectl auth can-i create clusterroles
```

The first three checks should return `yes`; the last should return `no`.
Renew the token before it expires and restart the runner if its environment
changes.

The chart does not create namespaces. The trusted local job creates and labels
one namespace per repository ID and pull-request number before invoking Helm.

